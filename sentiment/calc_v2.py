# -*- coding: utf-8 -*-
"""股民情绪指数 v2.0 引擎
改进(v1->v2):
 1) 滚动252日分位窗口(前期用扩展窗口, >=120样本才出分)
 2) 指标拆 水平+变化(量能5日/20日均值比)
 3) 大小票分层: 中证1000换手/上证50换手 -> 投机分层
 4) 顶底背离检测(60日窗口)
 5) 情绪快慢线 MA5/MA20
 6) 非对称阈值网格寻优 + 连续3日确认 + 20日去重
 7) 年线(MA250)牛熊 regime 过滤 + 分市况回测
历史基准曲线六项: 量能水平.25 换手.20 动量.20 波动-.10(波动率飙升=恐惧折扣) 分层.15 量能变化.10
"""
import json, math, re, os
from datetime import date, timedelta

BASE = os.environ.get("SENTIMENT_BASE", os.path.dirname(os.path.abspath(__file__)))

def parse(path):
    rows = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|"):
                continue
            c = [x.strip() for x in line.strip("|").split("|")]
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", c[0]):
                continue
            # 防御: 单条K线格式异常(缺列/非数值)不应拖垮整条管线——
            # 否则 asof 取不到 → 全站停更(此前无守卫, 一行坏数据即 IndexError/ValueError 崩溃)。
            if len(c) < 8:
                skipped += 1
                continue
            try:
                rows.append({"date": c[0], "close": float(c[2]),
                             "amount": float(c[6]), "to": float(c[7])})
            except (ValueError, IndexError):
                skipped += 1
                continue
    if skipped:
        print("警告: parse 跳过 %d 行格式异常/缺列(已容错, 不影响其余计算)" % skipped)
    rows.sort(key=lambda r: r["date"])
    return rows

# 防御(R105 修复 bug①): 四个原始行情 txt 任一缺失时, 给出明确错误而非 parse 内
# open() 抛出的晦涩 FileNotFoundError; 缺失时直接退出, 让上层使用已提交的兜底
# sentiment_v2.json, 避免"崩溃却不更新→下游静默用旧数据"的经典陷阱。
_required = ["sh_long.txt", "sz_long.txt", "sh50.txt", "zz1000.txt"]
_missing = [f for f in _required if not os.path.exists(os.path.join(BASE, f))]
if _missing:
    raise SystemExit("ERROR: 缺失原始行情文件 %s, 无法生成 sentiment_v2.json; "
                     "将使用已提交的兜底产物。请先运行 fetch_data.py 拉取最新行情。" % _missing)
sh = parse(os.path.join(BASE, "sh_long.txt"))
sz = {r["date"]: r for r in parse(os.path.join(BASE, "sz_long.txt"))}
sh50 = {r["date"]: r for r in parse(os.path.join(BASE, "sh50.txt"))}
zz1k = {r["date"]: r for r in parse(os.path.join(BASE, "zz1000.txt"))}

data = []
for r in sh:
    s, b, k = sz.get(r["date"]), sh50.get(r["date"]), zz1k.get(r["date"])
    if not (s and b and k):
        continue
    amt = r["amount"] + s["amount"]
    # 防御: 双源都缺成交额(amt==0)时除零崩溃 -> 换手率置 None(后续分位计算已容错 None)
    to = (r["to"] * r["amount"] + s["to"] * s["amount"]) / amt if amt != 0 else None
    ratio = k["to"] / b["to"] if b["to"] > 0 else None
    data.append({"date": r["date"], "close": r["close"], "amount": amt,
                 "to": to, "ratio": ratio})
n = len(data)
# R173(F4): 四源合并后为空(行情文件日期未对齐/全行格式异常)时, data[0]/data[-1] 会 IndexError,
# 且下游 valid[-1]/fwd 等会连环崩溃 → 直接退出让上层使用已提交的兜底 sentiment_v2.json,
# 避免写出空/损坏产物或静默用旧数据。
if not data:
    raise SystemExit("ERROR: 四源合并后无有效行(行情文件日期未对齐/全行格式异常), "
                     "无法生成 sentiment_v2.json; 将使用已提交的兜底产物。")
print("merged:", n, data[0]["date"], "~", data[-1]["date"])

closes = [d["close"] for d in data]

def ma(arr, i, w):
    if i + 1 < w:
        return None
    return sum(arr[i - w + 1: i + 1]) / w

# R173(F5): closes 来自原始行情 txt, 理论上 close>0, 但脏数据可能出现 0 → 除零崩溃。
# 对 mom20 / vol20 的除数做 0 守卫, 触发时置 None(下游分位/信号已容错 None)。
for i, d in enumerate(data):
    d["mom20"] = (closes[i] / closes[i - 20] - 1) * 100 if (i >= 20 and closes[i - 20] != 0) else None
    if i >= 20 and all(closes[j - 1] != 0 for j in range(i - 19, i + 1)):
        rets = [closes[j] / closes[j - 1] - 1 for j in range(i - 19, i + 1)]
        m = sum(rets) / 20
        d["vol20"] = math.sqrt(sum((x - m) ** 2 for x in rets) / 20) * 100
    else:
        d["vol20"] = None
    a5 = ma([x["amount"] for x in data], i, 5)
    a20 = ma([x["amount"] for x in data], i, 20)
    d["amt_chg"] = (a5 / a20 - 1) * 100 if (a5 is not None and a20 is not None and a20 != 0) else None
    d["ma250"] = ma(closes, i, 250)
    d["regime"] = ("bull" if d["close"] >= d["ma250"] else "bear") if d["ma250"] else None

# ---- 滚动252日分位(早期自适应窗口, 减少左侧断档) ----
# 历史设定: 固定252日窗口, 要求≥120个非None样本。结果2021-01-04起约139天score=None,
# 情绪图最左侧出现明显断档。改为: 早期用全部可用历史(窗口=i+1), 最少样本数
# max(30, win//2), 中后期平滑过渡到252/120, 既保留统计意义, 又让图从更早日期开始连续。
WIN, MIN_N_FULL = 252, 120
def roll_pct(series, i):
    win = min(WIN, i + 1)
    lo = max(0, i - win + 1)
    vals = [x for x in series[lo: i + 1] if x is not None]
    min_n = max(30, win // 2) if win < WIN else MIN_N_FULL
    if len(vals) < min_n or series[i] is None:
        return None
    return round(sum(1 for x in vals if x <= series[i]) / len(vals) * 100, 1)

amt_s = [d["amount"] for d in data]
to_s = [d["to"] for d in data]
mom_s = [d["mom20"] for d in data]
vol_s = [d["vol20"] for d in data]
rat_s = [d["ratio"] for d in data]
chg_s = [d["amt_chg"] for d in data]

# v4.9.27 修正 vol 方向: 波动率飙升=市场恐惧(恐慌特征), 在逆向情绪指数里应作为"恐惧折扣"
# 压低 score(高分=贪婪/卖出); 原 +0.10 把高波动推向"贪婪"方向, 与 count_resonance 把
# vol>=85 当作恐惧项方向相反, 且会在恐慌底部稀释买入信号。改为 -.10 使 score 与共振子系统一致。
# 相对权重: 波动率=-0.10(波动率飙升=市场恐惧, 在逆向情绪指数里应作为"恐惧折扣"压低 score);
# 与 count_resonance 把 vol>=85 当恐惧项方向一致。
# 注意: 含负权后 Σ(raw)=0.80≠1.0, 若不归一化, 基准分整体尺度下移约20%(系统性偏恐惧)——
# 故须归一化使 ΣW=1.0: 中性(各分位=50)→50, 高波动仍压低分数, 尺度无偏。
_raw_w = {"amt": .25, "to": .20, "mom": .20, "vol": -.10, "rat": .15, "chg": .10}
_wsum = sum(_raw_w.values())  # 0.80
W = {k: v / _wsum for k, v in _raw_w.items()}  # ΣW = 1.0
for i, d in enumerate(data):
    p = {"amt": roll_pct(amt_s, i), "to": roll_pct(to_s, i), "mom": roll_pct(mom_s, i),
         "vol": roll_pct(vol_s, i), "rat": roll_pct(rat_s, i), "chg": roll_pct(chg_s, i)}
    d["p"] = p
    if all(v is not None for v in p.values()):
        # 真实加权分: Σ(W×p), 归一化后 ∈ 约[-12.5, 112.5](极端恐惧可略<0, 极端贪婪可略>100),
        # 如实反映"超卖/超买"边界; 0-100 指数约定由 build_v3 的 final=clamp(score+adj) 承担展示。
        # 不在此钳制, 否则 score≠Σ(parts×weights) 破坏内部一致性(见 check_data.py 恒等式校验)。
        d["score"] = round(sum(W[k] * p[k] for k in W), 1)
    else:
        d["score"] = None

valid = [d for d in data if d["score"] is not None]
# R173(F4 扩展): 有效分位样本为空(数据不足120日或全行无分位)时, valid[-1] 会 IndexError。
# 同样退化为已提交兜底产物, 不写出损坏 json。
if not valid:
    raise SystemExit("ERROR: 有效分位样本为空(数据不足120日或全行无分位), "
                     "无法生成 sentiment_v2.json; 将使用已提交的兜底产物。")
scores = [d["score"] for d in valid]
for i, d in enumerate(valid):
    d["ma5s"] = ma(scores, i, 5)
    d["ma20s"] = ma(scores, i, 20)

# ---- 顶底背离(60日) ----
divs = []
for i in range(60, len(valid)):
    d = valid[i]
    window = valid[i - 60: i]
    hi = max(x["close"] for x in window)
    lo = min(x["close"] for x in window)
    s_hi = max(x["score"] for x in window)
    s_lo = min(x["score"] for x in window)
    if d["close"] >= hi and d["score"] < s_hi - 8:
        divs.append({"date": d["date"], "type": "top", "close": d["close"], "score": d["score"]})
    if d["close"] <= lo and d["score"] > s_lo + 8:
        divs.append({"date": d["date"], "type": "bottom", "close": d["close"], "score": d["score"]})
# 去重: 同类型20日内只留首个(按索引差)
thin_divs = []
last_idx = {"top": -999, "bottom": -999}
idx_of = {d["date"]: k for k, d in enumerate(valid)}
for dv in divs:
    k = idx_of[dv["date"]]
    if k - last_idx[dv["type"]] >= 20:
        thin_divs.append(dv)
        last_idx[dv["type"]] = k
divs = thin_divs

# ---- 信号生成(非对称阈值 + 连续3日确认 + 20日去重) ----
def gen_signals(buy_th, sell_th):
    sigs, last_sig = [], {"buy": -999, "sell": -999}
    run_buy, run_sell = 0, 0
    for i, d in enumerate(valid):
        if d["regime"] is None:
            continue
        run_buy = run_buy + 1 if d["score"] <= buy_th else 0
        run_sell = run_sell + 1 if d["score"] >= sell_th else 0
        if run_buy == 3 and i - last_sig["buy"] >= 20:
            sigs.append({"date": d["date"], "type": "buy", "score": d["score"],
                         "regime": d["regime"], "i": i})
            last_sig["buy"] = i
        if run_sell == 3 and i - last_sig["sell"] >= 20:
            sigs.append({"date": d["date"], "type": "sell", "score": d["score"],
                         "regime": d["regime"], "i": i})
            last_sig["sell"] = i
    return sigs

def fwd(i, days):
    j = i + days
    if j >= len(valid):
        return None
    # R173(F5 扩展): 除数 close==0(脏数据)时置 None, 避免 ZeroDivisionError 连环崩溃。
    if valid[i]["close"] == 0 or valid[j]["close"] == 0:
        return None
    return round((valid[j]["close"] / valid[i]["close"] - 1) * 100, 2)

def eval_sigs(sigs):
    def agg(rs):
        rs = [x for x in rs if x is not None]
        if not rs:
            return {"n": 0}
        return {"n": len(rs), "avg": round(sum(rs) / len(rs), 2),
                "win": round(sum(1 for x in rs if x > 0) / len(rs) * 100)}
    buys = [s for s in sigs if s["type"] == "buy"]
    sells = [s for s in sigs if s["type"] == "sell"]
    return {"buy_n": len(buys), "sell_n": len(sells),
            "buy20": agg([fwd(s["i"], 20) for s in buys]),
            "sell20": agg([fwd(s["i"], 20) for s in sells])}

# 网格寻优: 目标 = 买后20日均值 - 卖后20日均值 最大, 要求两侧信号各>=3
def pick_best(grid):
    """从网格结果中选最优阈值对(spread = buy_avg - sell_avg 最大)。
    若没有任何组合满足'买≥3 且 卖≥3'信号(极端单边市, 分数长期不破阈值),
    返回保守默认阈值并标记 fallback=True, 避免 best=None 后在
    BUY_TH=best['buy'] 处 TypeError 崩溃(否则 sentiment_v2.json 不更新, 下游静默用旧数据)。"""
    cands = [g for g in grid if g.get("spread") is not None]
    if not cands:
        return {"buy": 20, "sell": 80, "spread": None, "fallback": True,
                "buy_avg": None, "sell_avg": None, "buy_n": 0, "sell_n": 0}
    b = max(cands, key=lambda g: g["spread"])
    return {"buy": b["buy"], "sell": b["sell"], "spread": b["spread"],
            "buy_avg": b.get("buy_avg"), "sell_avg": b.get("sell_avg"),
            "buy_n": b["buy_n"], "sell_n": b["sell_n"]}

grid = []
for bt in [10, 15, 20, 25, 30]:
    for st in [70, 75, 80, 85, 90]:
        ev = eval_sigs(gen_signals(bt, st))
        ok = ev["buy20"].get("n", 0) >= 3 and ev["sell20"].get("n", 0) >= 3
        spread = (ev["buy20"]["avg"] - ev["sell20"]["avg"]) if ok else None
        grid.append({"buy": bt, "sell": st, "buy_n": ev["buy_n"], "sell_n": ev["sell_n"],
                     "buy_avg": ev["buy20"].get("avg"), "sell_avg": ev["sell20"].get("avg"),
                     "spread": spread})
best = pick_best(grid)
if best.get("fallback"):
    print("警告: 网格寻优无解(best=None), 回退保守默认阈值 buy=20/sell=80")
print("best thresholds:", best)

BUY_TH, SELL_TH = best["buy"], best["sell"]
sigs = gen_signals(BUY_TH, SELL_TH)
for s in sigs:
    s["r5"], s["r10"], s["r20"] = fwd(s["i"], 5), fwd(s["i"], 10), fwd(s["i"], 20)
    s.pop("i")

def regime_stats(sigs):
    out = {}
    for rg in ["bull", "bear"]:
        bs = [s for s in sigs if s["regime"] == rg and s["r20"] is not None]
        if not bs:
            out[rg] = {"n": 0}
            continue
        b20 = [s["r20"] for s in bs if s["type"] == "buy"]
        s20 = [s["r20"] for s in bs if s["type"] == "sell"]
        out[rg] = {
            "n": len(bs),
            "buy": ({"n": len(b20), "avg": round(sum(b20) / len(b20), 2)} if b20 else {"n": 0}),
            "sell": ({"n": len(s20), "avg": round(sum(s20) / len(s20), 2)} if s20 else {"n": 0}),
        }
    return out

# ---- v3.3 情绪-价格弹性系数 ----
last = valid[-1]
# 弹性 = (100 - score) × (低于年线幅度%)  [v4.9.27 修正]
# 反弹空间只存在于"价格低于年线=低估"时; 价格高于年线(高估)无反弹空间, 反是下行风险。
# 故用单向偏离 (ma250-close)/ma250: 正=低估有反弹空间, 负=高于年线无空间。
# 此前用 abs() 会把"高于年线(高估)"也计为高弹性, 与"价格越低弹性越大"意图相反 ——
#   高估态若恰逢低分(异常)会被误标"高弹性/深度低估", 误导。改为单向后仅在真正低估时给弹性。
# 量级: fear(0-100) × dev%(0-~40) ≈ 0-4000 | 弹性>=1000=高弹性(深度恐惧+深度低估), 弹性<200=低弹性(温和)
def elasticity(score, close, ma250):
    if ma250 is None or ma250 == 0:
        return None
    deviation = (ma250 - close) / ma250 * 100  # 低于年线幅度%(正=低估/有反弹空间; 负=高于年线无空间)
    if deviation < 0.5:  # 高于年线或贴近年线, 无低估反弹空间
        return None
    fear_level = max(0, 100 - score)  # 恐惧程度(0-100)
    return round(fear_level * deviation, 1)

# 历史弹性序列
for d in valid:
    d["elasticity"] = elasticity(d["score"], d["close"], d["ma250"])

# 当前弹性
cur_elasticity = elasticity(last["score"], last["close"], last["ma250"])

# 弹性历史分位
elas_vals = [d["elasticity"] for d in valid if d["elasticity"] is not None]
if elas_vals and cur_elasticity is not None:
    elas_pct = round(sum(1 for x in elas_vals if x <= cur_elasticity) / len(elas_vals) * 100, 1)
else:
    elas_pct = None

# ---- v3.3 多指标共振计数 ----
# 统计同向极值指标数量(>=3个=共振)
# v4.9: 此引擎(calc_v2)仅基于基准分位(不依赖快照数据), 实现 3+2 项:
#   恐惧侧: 波动率>=85分位 + 分层换手比<=15分位 + 20日动量<=15分位
#   贪婪侧: 量能>=80分位 + 换手>=80分位
# 注: IC贴水/两融连降/炸板率/MA60广度 等快照项由 build_v3 注入阶段基于 daily_snapshot 增强计入,
#      使"多指标共振"名副其实(详见 build_v3.py resonance 增强段)。
def count_resonance(d, last_parts):
    fear_count = 0
    greed_count = 0
    fear_items = []
    greed_items = []

    # 恐惧侧(从快照项和基准分项取)
    if last_parts.get("vol", 0) >= 85:
        fear_count += 1; fear_items.append("波动率" + str(last_parts["vol"]) + "分位")
    if last_parts.get("rat", 0) <= 15:
        fear_count += 1; fear_items.append("分层换手比" + str(last_parts["rat"]) + "分位")
    if last_parts.get("mom", 0) <= 15:
        fear_count += 1; fear_items.append("20日动量" + str(last_parts["mom"]) + "分位")

    # 贪婪侧
    if last_parts.get("amt", 0) >= 80:
        greed_count += 1; greed_items.append("量能" + str(last_parts["amt"]) + "分位")
    if last_parts.get("to", 0) >= 80:
        greed_count += 1; greed_items.append("换手" + str(last_parts["to"]) + "分位")

    return {"fear_count": fear_count, "greed_count": greed_count,
            "fear_items": fear_items, "greed_items": greed_items}

resonance = count_resonance(last, last["p"])

# ---- 当日快照修正(资讯口径, cap ±8) ----
# v4.9.23: 本引擎内部的 adj 已被 build_v3 的 snapshot_corrections 机制取代(按 asof 自动过期),
# 原先写死的 7/31/6月 叙事字面量会恒施加 ±1 静默偏移且叙事失真, 且 build_v3 会覆盖本 adj_total/final。
# 故此处仅保留中性占位; 真实外部事件修正由 build_v3 从 snapshot_corrections.json 动态采纳(见 build_v3.py)。
adj_items = []
adj_total = max(-8, min(8, sum(x["adj"] for x in adj_items)))

# ---- R177c 自包含 KNN 情绪轨迹预测 ----
# 移植自 sentiment-dashboard 技能的情绪预测方法论(本仓库副本, 不依赖另一工作区 build_v3):
# 以最近 ctx 日 score 形状(相对首值归一化, 只看斜率/形状而非绝对水平)为查询向量,
# 在历史中找 k 个最相似窗口, 拼接其后 horizon 日真实轨迹, 平移锚定到当前末值,
# 取分位 median/p25/p75 作未来带; 反弹峰值取中位轨迹在未来段的最大点。
# OFF-BY-ONE 关键: future 切片须从 i+1(明日)起, 不取 i(今日)——否则预测会"吃掉"今日点导致起点偏移。
def _pct(arr, q):
    """线性插值分位(0-1)。空数组返回 None。"""
    if not arr:
        return None
    if len(arr) == 1:
        return arr[0]
    idx = (len(arr) - 1) * q
    lo_i = int(idx)
    hi_i = min(lo_i + 1, len(arr) - 1)
    frac = idx - lo_i
    return arr[lo_i] * (1 - frac) + arr[hi_i] * frac


def _weights(dists, scheme):
    """KNN 权重方案: 'equal' 等权; 'inv' 激进 1/(dist+eps)(最近邻主导, 易过集中);
    'soft' 温和 exp(-dist/scale), scale=距离中位数, 既突出近邻又不过拟合。"""
    if scheme in (False, "equal"):
        return [1.0] * len(dists)
    if scheme == "soft":
        import statistics
        scale = statistics.median(dists) or 1.0
        return [math.exp(-d / scale) for d in dists]
    return [1.0 / (d + 1e-6) for d in dists]


def _wquant(vals, weights, q):
    """加权分位(0-1): 按 val 升序累计权重定位 q。weights 需与 vals 等长、非负。
    用于距离加权 KNN——离当前形态越近的历史窗口权重越大, 预测更聚焦。"""
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    vs = [vals[i] for i in order]
    ws = [weights[i] for i in order]
    tot = sum(ws)
    if tot <= 0:
        return vs[len(vs) // 2]
    target = q * tot
    cum = 0.0
    for i, w in enumerate(ws):
        cum += w
        if cum >= target:
            return vs[i]
    return vs[-1]


def _future_dates(start, n):
    """从 start(YYYY-MM-DD) 起生成 n 个未来交易日(跳过周末)。"""
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def sentiment_forecast(valid, horizon=30, k=10, ctx=20, band_days=10,
                       regime_weight=True, weight="inv",
                       recency_halflife=None, band_kappa=1.0):
    """KNN 情绪轨迹预测 v2(R180): 距离加权 + regime 条件化。
      - weight: 邻居权重方案 'equal'(等权)/'inv'(1/(dist+eps), 近邻主导)/'soft'(exp(-dist/中位数), 温和)。
      - regime_weight: 优先在同 regime(bull/bear)历史窗口挑邻居; 同 regime 候选<k 时回退全样本, 避免饥饿。
      - recency_halflife(可选, 交易日): 候选越久远权重越低, 让近期市场结构主导。
    样本不足(历史<ctx+horizon+1 或候选<k)返回 None, 上层降级。
    契约同 v1: median[0] 对应 T+1(明日), 无 OFF-BY-ONE。
    band_kappa: 经验分位带(p25-p75)半宽放大系数, =1 即原始带; R181 由 walk-forward 标定使样本外覆盖率≈名义50%。"""
    scores = [d["score"] for d in valid if d.get("score") is not None]
    regimes = [d.get("regime") for d in valid if d.get("score") is not None]
    n = len(scores)
    need = ctx + horizon + 1
    if n < need or k <= 0:
        return None
    cur = scores[n - ctx:]
    cur_norm = [x - cur[0] for x in cur]
    cur_regime = regimes[-1] if regimes else None
    cand = []
    for i in range(ctx - 1, n - horizon - 1):
        c = scores[i - ctx + 1: i + 1]
        if any(v is None for v in c):
            continue
        c_norm = [x - c[0] for x in c]
        dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur_norm, c_norm)))
        fut = scores[i + 1: i + 1 + horizon]
        if len(fut) < horizon or any(v is None for v in fut):
            continue
        cur_last = scores[-1]
        c_end = c[-1]
        shifted = [cur_last + (fut[j] - c_end) for j in range(horizon)]
        cand.append((dist, shifted, regimes[i] if i < len(regimes) else None, i))
    if len(cand) < k:
        return None
    if regime_weight and cur_regime is not None:
        same = [c for c in cand if c[2] == cur_regime]
        pool = same if len(same) >= k else cand
    else:
        pool = cand
    pool.sort(key=lambda x: x[0])
    top = pool[:k]
    w = _weights([t[0] for t in top], weight)
    if recency_halflife:
        for idx, t in enumerate(top):
            w[idx] *= math.exp(-((n - 1) - t[3]) / float(recency_halflife))
    shifted_top = [t[1] for t in top]
    median, p25, p75 = [], [], []
    for j in range(horizon):
        col = [t[j] for t in shifted_top]
        m = _wquant(col, w, 0.5)
        q25 = _wquant(col, w, 0.25)
        q75 = _wquant(col, w, 0.75)
        # R181: κ 重标定 —— 把经验分位带(p25-p75, 覆盖邻居约50%)按 kappa 放大半宽,
        # 使样本外实测覆盖率逼近名义 50%(kappa=1 即原始 p25-p75)。
        q25 = max(0.0, m - band_kappa * (m - (q25 if q25 is not None else m)))
        q75 = min(100.0, m + band_kappa * ((q75 if q75 is not None else m) - m))
        median.append(round(max(0.0, min(100.0, m)), 1))
        p25.append(round(max(0.0, min(100.0, q25)), 1))
        p75.append(round(max(0.0, min(100.0, q75)), 1))
    peak_day = int(max(range(horizon), key=lambda j: median[j])) + 1
    peak_val = median[peak_day - 1]
    return {"horizon": horizon, "ctx": ctx, "k": k, "band_days": band_days,
            "regime_weight": regime_weight, "weight": weight,
            "band_kappa": band_kappa,
            "asof": valid[-1]["date"],
            "dates": _future_dates(valid[-1]["date"], horizon),
            "median": median, "p25": p25, "p75": p75,
            "peak_day": peak_day, "peak_val": peak_val}


def backtest_sentiment_forecast(valid, horizon=30, k=10, ctx=20,
                                 regime_weight=True, weight="inv",
                                 step=5, recency_halflife=None, band_kappa=1.0):
    """walk-forward 样本外回测(R180): 每隔 step 个交易日设锚点, 用其之前全部历史做 KNN 预测,
    与真实未来对比。返回 MAE / 方向命中率(dir_acc) / p25-p75 覆盖率(cov) / 锚点数(n)。
    step 抽样锚点(兼顾各 regime 且省时); 候选池取锚点之前完整窗口, 不泄漏未来。"""
    scores = [d["score"] for d in valid if d.get("score") is not None]
    regimes = [d.get("regime") for d in valid if d.get("score") is not None]
    n = len(scores)
    need = ctx + horizon + 1
    if n < need + step:
        return None
    cand_pool = []
    for i in range(ctx - 1, n - horizon - 1):
        c = scores[i - ctx + 1: i + 1]
        if any(v is None for v in c):
            continue
        cand_pool.append((i, [x - c[0] for x in c],
                          regimes[i] if i < len(regimes) else None, scores[i]))
    errs = []
    dir_hit = 0
    dir_tot = 0
    cov = 0
    cov_tot = 0
    anchors = 0
    for t in range(ctx + horizon, n - horizon, step):
        cur = scores[t - ctx: t]
        cur_norm = [x - cur[0] for x in cur]
        cur_regime = regimes[t] if t < len(regimes) else None
        today = scores[t - 1]
        pool = []
        for (i, cn, rg, c_end) in cand_pool:
            if i + horizon >= t:
                continue
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur_norm, cn)))
            fut = scores[i + 1: i + 1 + horizon]
            if len(fut) < horizon or any(v is None for v in fut):
                continue
            shifted = [today + (fut[j] - c_end) for j in range(horizon)]
            pool.append((dist, shifted, rg, i))
        if len(pool) < k:
            continue
        if regime_weight and cur_regime is not None:
            same = [p for p in pool if p[2] == cur_regime]
            use = same if len(same) >= k else pool
        else:
            use = pool
        use.sort(key=lambda x: x[0])
        top = use[:k]
        w = _weights([p[0] for p in top], weight)
        if recency_halflife:
            for idx, p in enumerate(top):
                w[idx] *= math.exp(-((n - 1) - p[3]) / float(recency_halflife))
        med = [_wquant([p[1][j] for p in top], w, 0.5) for j in range(horizon)]
        p25l = [_wquant([p[1][j] for p in top], w, 0.25) for j in range(horizon)]
        p75l = [_wquant([p[1][j] for p in top], w, 0.75) for j in range(horizon)]
        actual = scores[t: t + horizon]
        for j in range(horizon):
            a = actual[j]
            m = med[j]
            if a is not None and m is not None:
                errs.append(abs(a - m))
                cov_tot += 1
                lo = max(0.0, m - band_kappa * (m - (p25l[j] if p25l[j] is not None else m)))
                hi = min(100.0, m + band_kappa * ((p75l[j] if p75l[j] is not None else m) - m))
                if lo <= a <= hi:
                    cov += 1
        if actual[-1] is not None and med[-1] is not None and today is not None:
            dir_tot += 1
            if (med[-1] - today >= 0) == (actual[-1] - today >= 0):
                dir_hit += 1
        anchors += 1
    if not errs:
        return None
    return {"mae": round(sum(errs) / len(errs), 2),
            "dir_acc": round(dir_hit / max(1, dir_tot) * 100, 1),
            "cov": round(cov / max(1, cov_tot) * 100, 1),
            "n": anchors}


def calibrate_sentiment_band_kappa(valid, horizon=30, k=15, ctx=15,
                                   regime_weight=False, weight="equal",
                                   step=6, target=50.0,
                                   grid=None):
    if grid is None:
        # R184: 0.02 步长加密, 让 κ 更精确命中名义 50%, 避免 0.05 步长造成 49.3% vs 51.3% 二选一。
        grid = tuple(round(1.0 + i * 0.02, 2) for i in range(21))  # 1.00..1.40
    """R181: walk-forward 一次性标定 band_kappa —— 单遍构建候选池并对每个 κ 计算样本外实测覆盖率,
    取使覆盖率逼近名义 target(默认50%) 的 κ。沿用 backtest 的不泄漏原则(候选仅取锚点之前窗口)。
    返回 {'kappa','cov','grid'} 或样本不足时 None。"""
    scores = [d["score"] for d in valid if d.get("score") is not None]
    regimes = [d.get("regime") for d in valid if d.get("score") is not None]
    n = len(scores)
    if n < ctx + horizon + 1 + step:
        return None
    cand_pool = []
    for i in range(ctx - 1, n - horizon - 1):
        c = scores[i - ctx + 1: i + 1]
        if any(v is None for v in c):
            continue
        cand_pool.append((i, [x - c[0] for x in c],
                          regimes[i] if i < len(regimes) else None, scores[i]))
    anchors_data = []
    for t in range(ctx + horizon, n - horizon, step):
        cur = scores[t - ctx: t]
        cur_norm = [x - cur[0] for x in cur]
        cur_regime = regimes[t] if t < len(regimes) else None
        today = scores[t - 1]
        pool = []
        for (i, cn, rg, c_end) in cand_pool:
            if i + horizon >= t:
                continue
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur_norm, cn)))
            fut = scores[i + 1: i + 1 + horizon]
            if len(fut) < horizon or any(v is None for v in fut):
                continue
            shifted = [today + (fut[j] - c_end) for j in range(horizon)]
            pool.append((dist, shifted, rg, i))
        if len(pool) < k:
            continue
        if regime_weight and cur_regime is not None:
            same = [p for p in pool if p[2] == cur_regime]
            use = same if len(same) >= k else pool
        else:
            use = pool
        use.sort(key=lambda x: x[0])
        top = use[:k]
        w = _weights([p[0] for p in top], weight)
        med = [_wquant([p[1][j] for p in top], w, 0.5) for j in range(horizon)]
        p25l = [_wquant([p[1][j] for p in top], w, 0.25) for j in range(horizon)]
        p75l = [_wquant([p[1][j] for p in top], w, 0.75) for j in range(horizon)]
        actual = scores[t: t + horizon]
        anchors_data.append((med, p25l, p75l, actual))
    if not anchors_data:
        return None
    best = None
    grid_cov = []
    for kappa in grid:
        cov = 0
        tot = 0
        for (med, p25l, p75l, actual) in anchors_data:
            for j in range(horizon):
                a = actual[j]
                m = med[j]
                if a is None or m is None:
                    continue
                tot += 1
                lo = max(0.0, m - kappa * (m - p25l[j]))
                hi = min(100.0, m + kappa * (p75l[j] - m))
                if lo <= a <= hi:
                    cov += 1
        cov_pct = cov / max(1, tot) * 100
        grid_cov.append([round(kappa, 2), round(cov_pct, 1)])
        if best is None or abs(cov_pct - target) < abs(best[1] - target):
            best = (kappa, cov_pct)
    return {"kappa": round(best[0], 2), "cov": round(best[1], 1), "grid": grid_cov}


# R180: 经 walk-forward 回测反选, 最优配置 = k=15/ctx=15/等权全局(见 _grid.py 分析):
# 相较旧默认 k=10/ctx=20/等权全局, MAE 25.01->21.33(-14.8%), p25-p75 覆盖率 35.7%->45.5%(更校准)。
# 加权(1/(dist+eps)/exp)与 regime 条件化在本数据上略增 MAE, 故生产取等权全局。
K_OPT, CTX_OPT, WEIGHT_OPT, REGIME_OPT = 15, 15, "equal", False
# R181: walk-forward 一次性标定 band_kappa, 使 p25-p75 阴影带样本外实测覆盖率逼近名义 50%
_BAND_CAL = calibrate_sentiment_band_kappa(valid, horizon=30, k=K_OPT, ctx=CTX_OPT,
                                           weight=WEIGHT_OPT, regime_weight=REGIME_OPT,
                                           step=6, target=50.0)
BAND_KAPPA = _BAND_CAL["kappa"] if _BAND_CAL else 1.0
if _BAND_CAL:
    print("band_kappa calib: kappa=%.2f cov=%.1f%% (target 50%%) grid=%s"
          % (_BAND_CAL["kappa"], _BAND_CAL["cov"], _BAND_CAL["grid"]))
fc = sentiment_forecast(valid, k=K_OPT, ctx=CTX_OPT, weight=WEIGHT_OPT,
                        regime_weight=REGIME_OPT, band_kappa=BAND_KAPPA)
if fc:
    fc["band_kappa"] = BAND_KAPPA
    print("forecast: horizon=%d k=%d ctx=%d peak@T+%d=%.1f band_days=%d weight=%s regime=%s kappa=%.2f"
          % (fc["horizon"], fc["k"], fc["ctx"], fc["peak_day"], fc["peak_val"],
             fc["band_days"], fc["weight"], fc["regime_weight"], BAND_KAPPA))
else:
    print("forecast: 样本不足, 跳过情绪预测带生成")
# R180+R181: 样本外回测精度(诚实披露 + 门禁监控), step=6; band_kappa 同步传入使 cov≈名义50%
forecast_acc = backtest_sentiment_forecast(valid, horizon=fc["horizon"] if fc else 30,
                                            k=K_OPT, ctx=CTX_OPT,
                                            weight=WEIGHT_OPT, regime_weight=REGIME_OPT,
                                            step=6, band_kappa=BAND_KAPPA) if fc else None
if forecast_acc:
    forecast_acc["band_kappa"] = BAND_KAPPA
    print("forecast_acc: MAE=%.2f dir=%.1f%% cov=%.1f%% n=%d kappa=%.2f"
          % (forecast_acc["mae"], forecast_acc["dir_acc"], forecast_acc["cov"], forecast_acc["n"], BAND_KAPPA))

# ---- R178 维度拆解(对齐 sentiment-dashboard 视觉语言) ----
# 子分 = 该维度对情绪指数的 signed 贡献, 归一化到 [-1,1] 区间; 5 维权重之和为 1。
# 明细取原始指标: 趋势动量→20日涨跌; 牛熊位置→偏离MA250; 量能水平→5日/20日成交额比;
# 波动恐慌→HV20分位; 广度确认→大小盘换手比(中证1000/上证50)。
_pos_dev = ((last["close"] / last["ma250"] - 1) * 100) if last.get("ma250") else None
_raw_w5 = {"mom": .20, "pos": .15, "amt": .25, "vol": -.10, "rat": .15}
_w5sum = sum(abs(v) for v in _raw_w5.values())
W5 = {k: v / _w5sum for k, v in _raw_w5.items()}

def _sub_score(pct, w):
    """把 0-100 分位转成 [-1,1] 的 signed 子分, 再乘权重。"""
    if pct is None:
        return None
    return round((pct - 50) / 50 * w, 2)

_p = last["p"]
dimensions = [
    {"name": "趋势动量", "key": "mom", "sub": _sub_score(_p.get("mom"), W5["mom"]),
     "pct": _p.get("mom"), "detail": "上证20日 " + ("%.2f%%" % last["mom20"] if last.get("mom20") is not None else "—")},
    {"name": "牛熊位置", "key": "pos", "sub": _sub_score(
         max(0, min(100, 50 + (_pos_dev or 0) * 2.5)), W5["pos"]),
     "pct": round(max(0, min(100, 50 + (_pos_dev or 0) * 2.5)), 1) if _pos_dev is not None else None,
     "detail": "偏离MA250 " + ("%.2f%%" % _pos_dev if _pos_dev is not None else "—")},
    {"name": "量能水平", "key": "amt", "sub": _sub_score(_p.get("amt"), W5["amt"]),
     "pct": _p.get("amt"), "detail": "量比 " + ("%.3f" % (1 + (last["amt_chg"] or 0) / 100) if last.get("amt_chg") is not None else "—")},
    {"name": "波动恐慌", "key": "vol", "sub": _sub_score(_p.get("vol"), W5["vol"]),
     "pct": _p.get("vol"), "detail": "HV20分位 " + ("%.0f" % _p.get("vol") if _p.get("vol") is not None else "—")},
    {"name": "广度确认", "key": "rat", "sub": _sub_score(_p.get("rat"), W5["rat"]),
     "pct": _p.get("rat"), "detail": "大小盘分化 " + ("%.2f" % last["ratio"] if last.get("ratio") is not None else "—")},
]
# 最终分位: final 在全部有效历史上的滚动分位
def _final_pct(valid, final):
    finals = [max(0, min(100, d["score"])) for d in valid]
    if not finals:
        return None
    return round(sum(1 for x in finals if x <= final) / len(finals) * 100, 1)
_final_val = round(max(0, min(100, last["score"] + adj_total)), 1)
final_pct = _final_pct(valid, _final_val)

result = {
    "asof": last["date"],
    "close": last["close"],
    "ma250": round(last["ma250"], 1),
    "regime": last["regime"],
    "score": last["score"],
    "ma5s": last["ma5s"], "ma20s": last["ma20s"],
    "adj_items": adj_items, "adj_total": adj_total,
    "final": _final_val,
    "final_pct": final_pct,
    "dimensions": dimensions,
    "parts": last["p"],
    "weights": W,
    "buy_th": BUY_TH, "sell_th": SELL_TH,
    "threshold_best": best, "threshold_grid": grid,
    "divs": divs,
    "signals": sigs,
    "regime_stats": regime_stats(sigs),
    "elasticity": cur_elasticity,
    "elasticity_pct": elas_pct,
    "resonance": resonance,
    # R179: hist 改为基于全量 data(与 data.json 的 klines 同起点 2021-01-04), 早期 score/ma5s/ma20s 为 None 占位,
    # 使情绪图 x 轴起点与分指数图解对齐、历史拉满 5 年; scoring/KNN 仍基于 valid(未改动, 含 120 样本 warmup 的滚动分位模型)。
    "hist": [[d["date"], d["close"], d["score"], d.get("ma5s"), d.get("ma20s"),
              (1 if d["regime"] == "bull" else 0),
              d.get("elasticity")] for d in data],
    # R177c: KNN 情绪轨迹预测(未来 horizon 日 median/p25/p75 带 + 反弹峰值)。
    # 样本不足时 calc_v2 返回 None, 此处写为 None, 由 report.py 情绪板块降级(不画预测带)。
    "forecast": fc,
    # R180: walk-forward 样本外回测精度(诚实披露 + 门禁监控); 预测为路径派生, 此字段量化其可靠性。
    "forecast_acc": forecast_acc,
    # v4.9.27: 清空写死的过期叙事字面量(iv/val_cyb/val_hs300/limit_detail/missing)。
    # 这些硬编码值(含 7/22、42.2倍、14.5倍、7/31 涨停等过期口径)模板从不渲染(已查证
    # template-v3.html 对 extra 零引用), 且 build_v3 自行动态注入 breadth_ma20/ic_basis/
    # etf_flow/limit_detail 等并覆盖 limit_detail —— 纯属死数据且违背"诚实数据"铁律, 易误导维护者。
    # 保留 extra 为空 dict(非 None), 因 build_v3 依赖 d["extra"] 为 dict 以追加键。
    "extra": {}
}
with open(os.path.join(BASE, "sentiment_v2.json"), "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)

print("asof:", last["date"], "| close:", last["close"], "| ma250:", round(last["ma250"], 1), "| regime:", last["regime"])
print("base score:", last["score"], "| adj:", adj_total, "| FINAL:", result["final"])
print("parts:", last["p"])
print("ma5/ma20:", last["ma5s"], last["ma20s"])
print("elasticity:", cur_elasticity, "| pct:", elas_pct)
print("resonance fear:", resonance["fear_count"], resonance["fear_items"], "| greed:", resonance["greed_count"], resonance["greed_items"])
print("divs:", json.dumps(divs[-6:], ensure_ascii=False))
print("signals:", len(sigs), "| regime_stats:", json.dumps(result["regime_stats"], ensure_ascii=False))
