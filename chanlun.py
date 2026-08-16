# -*- coding: utf-8 -*-
"""缠论分析流水线：包含处理 → 分型 → 笔 → 中枢 → 背驰 → 买卖点 → 分类推演"""
import json

MIN_BI_PCT = 0.018       # 日线单笔最小幅度过滤
MIN_BI_PCT_WEEK = 0.04   # 周线单笔最小幅度过滤


# ---------- MACD ----------
def ema(values, period):
    k = 2.0 / (period + 1)
    out = []
    e = values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def macd(closes):
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    hist = [(d - s) * 2 for d, s in zip(dif, dea)]
    return dif, dea, hist


# ---------- 1. K线包含处理 ----------
def merge_inclusion(klines):
    """返回合并后的K线列表 [{date,high,low,idx_start,idx_end}]"""
    merged = []
    for i, k in enumerate(klines):
        bar = {"date": k["date"], "high": k["high"], "low": k["low"],
               "idx_start": i, "idx_end": i}
        if len(merged) >= 2:
            prev, cur = merged[-1], bar
            contained = (prev["high"] >= cur["high"] and prev["low"] <= cur["low"]) or \
                        (prev["high"] <= cur["high"] and prev["low"] >= cur["low"])
            if contained:
                # 方向由再前一根决定
                direction = 1 if merged[-1]["high"] > merged[-2]["high"] else -1
                if direction == 1:  # 向上取高高
                    prev["high"] = max(prev["high"], cur["high"])
                    prev["low"] = max(prev["low"], cur["low"])
                else:  # 向下取低低
                    prev["high"] = min(prev["high"], cur["high"])
                    prev["low"] = min(prev["low"], cur["low"])
                prev["idx_end"] = i
                prev["date"] = k["date"]
                continue
        merged.append(bar)
    return merged


# ---------- 2. 顶底分型 ----------
def find_fractals(merged):
    tops, bottoms = [], []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b["high"] > a["high"] and b["high"] > c["high"] and \
           b["low"] > a["low"] and b["low"] > c["low"]:
            tops.append(i)
        if b["low"] < a["low"] and b["low"] < c["low"] and \
           b["high"] < a["high"] and b["high"] < c["high"]:
            bottoms.append(i)
    return tops, bottoms


# ---------- 3. 笔 ----------
def build_bi(merged, min_pct=MIN_BI_PCT):
    tops, bottoms = find_fractals(merged)
    top_set, bot_set = set(tops), set(bottoms)
    candidates = []  # (merged_idx, type) type: 1=顶 -1=底
    for i in sorted(tops + bottoms):
        candidates.append((i, 1 if i in top_set else -1))

    bis = []  # {start_idx, end_idx, dir, high, low}
    if not candidates:
        return bis
    # 从第一个分型开始，顶底交替
    seq = []
    for idx, t in candidates:
        if not seq:
            seq.append((idx, t))
            continue
        li, lt = seq[-1]
        if t == lt:
            # 同类分型：保留更极值者
            if t == 1 and merged[idx]["high"] >= merged[li]["high"]:
                seq[-1] = (idx, t)
            elif t == -1 and merged[idx]["low"] <= merged[li]["low"]:
                seq[-1] = (idx, t)
        else:
            # 交替：要求间隔至少1根独立K线 + 幅度过滤
            gap = idx - li
            if lt == 1:
                amp = (merged[li]["high"] - merged[idx]["low"]) / merged[li]["high"]
            else:
                amp = (merged[idx]["high"] - merged[li]["low"]) / merged[li]["low"]
            if gap >= 2 and amp >= min_pct:
                seq.append((idx, t))
            else:
                # 不满足条件：若新分型比上一个更极值，仍替换（防止漏大波段）
                pass
    for j in range(1, len(seq)):
        (i0, t0), (i1, t1) = seq[j - 1], seq[j]
        d = 1 if t0 == -1 else -1  # 底->顶 为向上笔
        hi = max(merged[i0]["high"], merged[i1]["high"])
        lo = min(merged[i0]["low"], merged[i1]["low"])
        bis.append({
            "start": i0, "end": i1, "dir": d,
            "start_price": merged[i0]["low"] if d == 1 else merged[i0]["high"],
            "end_price": merged[i1]["high"] if d == 1 else merged[i1]["low"],
            "high": hi, "low": lo,
            "date_start": merged[i0]["date"], "date_end": merged[i1]["date"],
        })
    return bis


# ---------- 4. 笔中枢 ----------
def build_zhongshu(bis):
    zss = []
    i = 0
    while i + 2 < len(bis):
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        zg = min(b1["high"], b2["high"], b3["high"])
        zd = max(b1["low"], b2["low"], b3["low"])
        if zg > zd:
            zs = {"start": b1["start"], "end": b3["end"], "zg": zg, "zd": zd,
                  "date_start": b1["date_start"], "date_end": b3["date_end"], "count": 3}
            j = i + 3
            while j < len(bis):
                b = bis[j]
                # 与新笔有重叠则扩展
                if min(b["high"], zs["zg"]) > max(b["low"], zs["zd"]):
                    zs["end"] = b["end"]
                    zs["date_end"] = b["date_end"]
                    zs["count"] += 1
                    j += 1
                else:
                    break
            zss.append(zs)
            i = j
        else:
            i += 1
    return zss


# ---------- 5. 背驰 ----------
def bi_macd_area(bi, hist, merged):
    s, e = merged[bi["start"]]["idx_start"], merged[bi["end"]]["idx_end"]
    if e < s:
        s, e = e, s
    seg = hist[s:e + 1]
    if bi["dir"] == 1:
        return sum(v for v in seg if v > 0)
    return abs(sum(v for v in seg if v < 0))


def find_beichi(bis, hist, merged):
    """返回 [{bi_index, type}] type: top/bottom"""
    out = []
    for i in range(2, len(bis)):
        cur = bis[i]
        # 找前一同向笔
        prev = None
        for j in range(i - 2, -1, -2):
            prev = bis[j]
            break
        if prev is None or prev["dir"] != cur["dir"]:
            continue
        a_cur = bi_macd_area(cur, hist, merged)
        a_prev = bi_macd_area(prev, hist, merged)
        if a_prev <= 0:
            continue
        if cur["dir"] == 1 and cur["end_price"] > prev["end_price"] and a_cur < a_prev * 0.85:
            out.append({"bi_index": i, "type": "top", "area_ratio": a_cur / a_prev})
        if cur["dir"] == -1 and cur["end_price"] < prev["end_price"] and a_cur < a_prev * 0.85:
            out.append({"bi_index": i, "type": "bottom", "area_ratio": a_cur / a_prev})
    return out


# ---------- 6. 买卖点 ----------
def find_signals(bis, zss, beichis):
    signals = []
    last_zs = zss[-1] if zss else None
    bc_map = {b["bi_index"]: b for b in beichis}
    for i, b in enumerate(bis):
        if i in bc_map:
            if bc_map[i]["type"] == "bottom":
                signals.append({"bi_index": i, "kind": "一类买点(底背驰)", "dir": 1,
                                "date": b["date_end"], "price": b["end_price"]})
            else:
                signals.append({"bi_index": i, "kind": "一类卖点(顶背驰)", "dir": -1,
                                "date": b["date_end"], "price": b["end_price"]})
    if last_zs:
        # 中枢完成后：回踩不破ZG=三买；反抽不过ZD=三卖
        for i, b in enumerate(bis):
            if b["end"] <= last_zs["end"]:
                continue
            if b["dir"] == -1 and b["end_price"] > last_zs["zg"]:
                signals.append({"bi_index": i, "kind": "三类买点(回踩不破ZG)", "dir": 1,
                                "date": b["date_end"], "price": b["end_price"]})
            if b["dir"] == 1 and b["end_price"] < last_zs["zd"]:
                signals.append({"bi_index": i, "kind": "三类卖点(反抽不过ZD)", "dir": -1,
                                "date": b["date_end"], "price": b["end_price"]})
    return signals


# ---------- 7. 分类推演 ----------
def classify(bis, zss, beichis, close):
    if not bis:
        return {"scenario": "数据不足", "detail": ""}
    last = bis[-1]
    last_zs = zss[-1] if zss else None
    recent_bc = [b for b in beichis if b["bi_index"] >= len(bis) - 3]

    pos = "无中枢"
    if last_zs:
        if close > last_zs["zg"]:
            pos = "中枢上方"
        elif close < last_zs["zd"]:
            pos = "中枢下方"
        else:
            pos = "中枢内部"

    bc_top = any(b["type"] == "top" for b in recent_bc)
    bc_bot = any(b["type"] == "bottom" for b in recent_bc)

    if bc_top and last["dir"] == 1:
        scenario = "背驰见顶风险"
        detail = "最近向上笔价格创新高但MACD红柱面积明显萎缩（面积比 %.2f），构成顶背驰。短线警惕一类卖点确认，回落目标先看最近中枢ZG(%.1f)。" % (recent_bc[-1]["area_ratio"], last_zs["zg"] if last_zs else close)
    elif bc_bot and last["dir"] == -1:
        scenario = "背驰见底机会"
        detail = "最近向下笔价格创新低但MACD绿柱面积明显萎缩（面积比 %.2f），构成底背驰。关注一类买点后的反弹，第一压力看最近中枢ZD(%.1f)。" % (recent_bc[-1]["area_ratio"], last_zs["zd"] if last_zs else close)
    elif last["dir"] == 1 and pos == "中枢上方":
        scenario = "多头延续"
        detail = "当前向上笔运行于最后中枢上方，走势处于强势区。只要不跌破中枢上沿ZG(%.1f)，按多头延续对待；若回踩ZG不破，构成三类买点。" % (last_zs["zg"] if last_zs else close)
    elif last["dir"] == 1 and pos == "中枢内部":
        scenario = "中枢震荡偏多"
        detail = "价格在中枢内部向上运行，区间ZG %.1f / ZD %.1f。向上突破ZG并站稳才能打开空间，否则按区间震荡处理。" % ((last_zs["zg"], last_zs["zd"]) if last_zs else (close, close))
    elif last["dir"] == -1 and pos == "中枢上方":
        scenario = "高位整理未破前高"
        detail = "向上趋势中出现回调笔，但仍在中枢ZG(%.1f)上方，属强势整理。回踩不破ZG即三类买点；若跌回中枢内部则转为震荡。" % (last_zs["zg"] if last_zs else close)
    elif last["dir"] == -1 and pos == "中枢内部":
        scenario = "中枢震荡偏空"
        detail = "价格在中枢内部向下运行，区间ZG %.1f / ZD %.1f。向下跌破ZD需警惕中枢级别扩大或三类卖点。" % ((last_zs["zg"], last_zs["zd"]) if last_zs else (close, close))
    elif last["dir"] == -1 and pos == "中枢下方":
        scenario = "空头延续"
        detail = "当前向下笔运行于最后中枢ZD(%.1f)下方，走势偏弱。反抽不过ZD将构成三类卖点；只有重回中枢内部才能扭转弱势。" % (last_zs["zd"] if last_zs else close)
    else:  # dir==1, pos==中枢下方
        scenario = "弱势反弹"
        detail = "向下趋势中的反弹笔，仍在中枢ZD(%.1f)下方。反弹无法回到中枢内部则仍是空头格局，警惕三类卖点。" % (last_zs["zd"] if last_zs else close)

    return {"scenario": scenario, "detail": detail, "position": pos,
            "last_bi_dir": last["dir"], "last_bi_pct": (last["end_price"] / last["start_price"] - 1)}


# ---------- 8b. 标准严格笔（第二套，无幅度过滤，交叉验证用） ----------
def build_bi_strict(merged, min_sep=3):
    """标准缠论笔：顶底交替，相邻顶底极值点间至少 min_sep 根独立K线，
    新分型必须创极值才接受。无幅度过滤，与第一套(振幅过滤)互为校验。"""
    tops, bottoms = find_fractals(merged)
    top_set, bot_set = set(tops), set(bottoms)
    cand = sorted(((i, 1 if i in top_set else -1) for i in tops + bottoms))
    seq = []
    for idx, t in cand:
        if not seq:
            seq.append((idx, t))
            continue
        li, lt = seq[-1]
        if t == lt:
            if t == 1 and merged[idx]["high"] >= merged[li]["high"]:
                seq[-1] = (idx, t)
            elif t == -1 and merged[idx]["low"] <= merged[li]["low"]:
                seq[-1] = (idx, t)
        else:
            if idx - li >= min_sep:
                seq.append((idx, t))
    bis = []
    for j in range(1, len(seq)):
        (i0, t0), (i1, t1) = seq[j - 1], seq[j]
        d = 1 if t0 == -1 else -1
        bis.append({
            "start": i0, "end": i1, "dir": d,
            "start_price": merged[i0]["low"] if d == 1 else merged[i0]["high"],
            "end_price": merged[i1]["high"] if d == 1 else merged[i1]["low"],
            "high": max(merged[i0]["high"], merged[i1]["high"]),
            "low": min(merged[i0]["low"], merged[i1]["low"]),
            "date_start": merged[i0]["date"], "date_end": merged[i1]["date"],
        })
    return bis


def _date_diff(d1, d2):
    from datetime import datetime
    a = datetime.strptime(d1, "%Y-%m-%d")
    b = datetime.strptime(d2, "%Y-%m-%d")
    return (a - b).days


def bi_agreement(bis_a, bis_b):
    """两套笔识别的一致性：按结束日期对齐，误差<=2交易日记为一致。
    返回 (一致笔数, 总笔数, 一致率)。一致率越高，划分越稳健。"""
    dates_b = [b["date_end"] for b in bis_b]
    n = len(bis_a)
    ok = 0
    for b in bis_a:
        for d in dates_b:
            if abs(_date_diff(b["date_end"], d)) <= 2:
                ok += 1
                break
    return ok, n, (ok / n if n else 0)


# ---------- 8c. 已知拐点捕捉（算法准确度外部校验，方向性匹配） ----------
KNOWN_PIVOTS = {
    "2021-02-18": ("2021 年初顶部", "top"),
    "2021-12-13": ("2021 年末高点", "top"),
    "2022-04-27": ("2022-04 政策底", "bottom"),
    "2022-10-31": ("2022-10 市场底", "bottom"),
    "2023-01-30": ("2023 年初高点", "top"),
    "2024-02-05": ("2024-02 股灾底", "bottom"),
    "2024-09-24": ("2024 政策底(924)", "bottom"),
    "2024-10-08": ("2024 国庆后高点", "top"),
}


def known_pivot_capture(r):
    captured = []
    bottom_dates = {r["merged"][b["end"]]["date"] for b in r["bis"] if b["dir"] == 1}
    top_dates = {r["merged"][b["end"]]["date"] for b in r["bis"] if b["dir"] == -1}
    for d, (label, direction) in KNOWN_PIVOTS.items():
        pool = top_dates if direction == "top" else bottom_dates
        for pd in pool:
            if abs(_date_diff(pd, d)) <= 12:
                captured.append((label, direction, pd))
                break
    return captured


# ---------- 8f. 分类稳定性测试（鲁棒性外部校验） ----------
def classification_stability(klines, min_bi_pct=MIN_BI_PCT):
    """砍掉末尾 K 根 K 线后重算分类，检验结论是否漂移。
    返回 {drops: {5/10/20: scenario}, stable: bool, base: scenario}"""
    base = classify(*_last_two(analyze(klines, min_bi_pct, with_stability=False), klines[-1]["close"]))
    out = {"base": base, "drops": {}, "stable": True}
    for k in (5, 10, 20):
        if len(klines) > k + 30:
            sub = klines[: len(klines) - k]
            sc = classify(*_last_two(analyze(sub, min_bi_pct, with_stability=False), sub[-1]["close"]))
            out["drops"][k] = sc
            if sc != base:
                out["stable"] = False
    return out


def _last_two(res, close):
    return res["bis"], res["zhongshu"], res["beichi"], close


# ---------- 8g. 前向收益波动（置信锥用） ----------
def forward_vol(closes, horizon=60):
    """历史滚动"持有 horizon 交易日"收益率标准差（小数），用于推演置信锥宽度"""
    rets = []
    for i in range(len(closes) - horizon):
        rets.append(closes[i + horizon] / closes[i] - 1)
    if len(rets) < 30:
        return 0.10
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    return var ** 0.5


# ---------- 8d. 结构健康度（0-100，多空力量量化） ----------
def health_score(klines, r, wcls):
    s = 50
    cls = r["classify"]
    zs = r["zhongshu"][-1] if r["zhongshu"] else None
    close = klines[-1]["close"]
    if zs:
        if close > zs["zg"]:
            s += 15
        elif close < zs["zd"]:
            s -= 15
    if cls["last_bi_dir"] == 1:
        s += 10
    else:
        s -= 10
    if cls.get("last_bi_dir") == wcls.get("last_bi_dir"):
        s += 15
    else:
        s -= 10
    recent_bc = [b for b in r["beichi"] if b["bi_index"] >= len(r["bis"]) - 3]
    if any(b["type"] == "top" for b in recent_bc):
        s -= 10
    if any(b["type"] == "bottom" for b in recent_bc):
        s += 10
    return max(0, min(100, s))


# ---------- 8e. 推演置信度（0-100） ----------
def forecast_confidence(r, wcls, bt):
    c = 40
    cls = r["classify"]
    aligned = cls.get("last_bi_dir") == wcls.get("last_bi_dir")
    c += 20 if aligned else -10
    recent_bc = [b for b in r["beichi"] if b["bi_index"] >= len(r["bis"]) - 3]
    if any(b["type"] == "top" for b in recent_bc):
        c -= 15
    if any(b["type"] == "bottom" for b in recent_bc):
        c += 10
    wr = None
    for kind in ("一类买", "一类卖"):
        if kind in bt and 20 in bt[kind]:
            st = bt[kind][20]
            if st["n"] >= 3:
                wr = st["win_rate"]
                break
    if wr is not None:
        c += (wr - 0.5) * 40
    return max(0, min(100, int(c)))


def analyze(klines, min_bi_pct=MIN_BI_PCT, with_stability=True):
    merged = merge_inclusion(klines)
    bis = build_bi(merged, min_bi_pct)
    zss = build_zhongshu(bis)
    closes = [k["close"] for k in klines]
    dif, dea, hist = macd(closes)
    beichis = find_beichi(bis, hist, merged)
    signals = find_signals(bis, zss, beichis)
    cls = classify(bis, zss, beichis, closes[-1])
    bis_strict = build_bi_strict(merged)
    ok, tot, agree = bi_agreement(bis, bis_strict)
    captured = known_pivot_capture({"merged": merged, "bis": bis})
    capture_rate = len(captured) / len(KNOWN_PIVOTS) if KNOWN_PIVOTS else 0
    stability = classification_stability(klines, min_bi_pct) if with_stability else None
    return {
        "merged": merged, "bis": bis, "zhongshu": zss,
        "dif": dif, "dea": dea, "hist": hist,
        "beichi": beichis, "signals": signals, "classify": cls,
        "agreement": {"ok": ok, "total": tot, "rate": agree},
        "captured": captured, "capture_rate": capture_rate,
        "stability": stability,
    }


# ---------- 8. 信号回测 ----------
def backtest_signals(klines, result, horizons=(5, 10, 20)):
    """对每个买卖点信号，统计其后 h 个交易日的收益与方向胜率。
    买点：ret>0 为胜；卖点：ret<0 为胜。返回 {kind: {h: {n, win_rate, avg_ret}}}"""
    merged, bis = result["merged"], result["bis"]
    n = len(klines)
    agg = {}
    for s in result["signals"]:
        b = bis[s["bi_index"]]
        idx = merged[b["end"]]["idx_end"]
        entry = klines[idx]["close"]
        kind = s["kind"][:3]  # 一类买/一类卖/三类买/三类卖
        for h in horizons:
            if idx + h >= n:
                continue
            ret = klines[idx + h]["close"] / entry - 1
            win = (ret > 0) if s["dir"] == 1 else (ret < 0)
            st = agg.setdefault(kind, {}).setdefault(h, {"n": 0, "win": 0, "sum": 0.0})
            st["n"] += 1
            st["win"] += 1 if win else 0
            st["sum"] += ret
    out = {}
    for kind, hs in agg.items():
        out[kind] = {}
        for h, st in hs.items():
            out[kind][h] = {
                "n": st["n"],
                "win_rate": st["win"] / st["n"] if st["n"] else 0,
                "avg_ret": st["sum"] / st["n"] if st["n"] else 0,
            }
    return out


if __name__ == "__main__":
    with open("chanlun/data.json", encoding="utf-8") as f:
        data = json.load(f)
    for sym, d in data.items():
        r = analyze(d["klines"])
        print("%s %s: 合并K线%d 笔%d 中枢%d 背驰%d | %s" % (
            sym, d["name"], len(r["merged"]), len(r["bis"]),
            len(r["zhongshu"]), len(r["beichi"]), r["classify"]["scenario"]))
