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

# ---- 滚动252日分位 ----
WIN, MIN_N = 252, 120
def roll_pct(series, i):
    lo = max(0, i - WIN + 1)
    vals = [x for x in series[lo: i + 1] if x is not None]
    if len(vals) < MIN_N or series[i] is None:
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

result = {
    "asof": last["date"],
    "close": last["close"],
    "ma250": round(last["ma250"], 1),
    "regime": last["regime"],
    "score": last["score"],
    "ma5s": last["ma5s"], "ma20s": last["ma20s"],
    "adj_items": adj_items, "adj_total": adj_total,
    "final": round(max(0, min(100, last["score"] + adj_total)), 1),
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
    "hist": [[d["date"], d["close"], d["score"], d["ma5s"], d["ma20s"],
              (1 if d["regime"] == "bull" else 0),
              d.get("elasticity")] for d in valid],
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
