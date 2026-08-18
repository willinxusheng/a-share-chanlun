# -*- coding: utf-8 -*-
"""缠论分析流水线：包含处理 → 分型 → 笔 → 中枢 → 背驰 → 买卖点 → 分类推演"""
import json
import os
import math

MIN_BI_PCT = 0.018       # 日线单笔最小幅度过滤
MIN_BI_PCT_WEEK = 0.04   # 周线单笔最小幅度过滤
MIN_BI_PCT_MONTH = 0.08  # 月线单笔最小幅度过滤（月线波动更大，阈值相应提高）


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
        # 改用 >= 处理"相等高点/低点"：避免严格 > 漏判平顶/平底分型（#10）
        if b["high"] >= a["high"] and b["high"] >= c["high"] and \
           b["low"] >= a["low"] and b["low"] >= c["low"]:
            tops.append(i)
        if b["low"] <= a["low"] and b["low"] <= c["low"] and \
           b["high"] <= a["high"] and b["high"] <= c["high"]:
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
                # 交替但不满足间隔(>=2根独立K)/幅度(min_pct)约束：跳过，不强行新增伪笔；
                # 后续若出现更极值的同类型分型，会在上方「同类保留极值」分支吸收，笔划分保持稳健
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
                  "gg": max(b1["high"], b2["high"], b3["high"]),
                  "dd": min(b1["low"], b2["low"], b3["low"]),
                  "date_start": b1["date_start"], "date_end": b3["date_end"],
                  "count": 3, "extension": False}
            j = i + 3
            while j < len(bis):
                b = bis[j]
                # 与新笔有重叠则扩展
                if min(b["high"], zs["zg"]) > max(b["low"], zs["zd"]):
                    zs["end"] = b["end"]
                    zs["date_end"] = b["date_end"]
                    zs["count"] += 1
                    zs["gg"] = max(zs["gg"], b["high"])
                    zs["dd"] = min(zs["dd"], b["low"])
                    j += 1
                else:
                    break
            # 中枢延伸：构成笔数 ≥9（缠论中一个中枢内连续 ≥9 笔视为延伸，级别升级信号）
            zs["extension"] = zs["count"] >= 9
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


def classify_beichi_type(beichis, bis, zss):
    """背驰级别判定（缠论核心框架，此前缺失）：
    趋势背驰 —— 背驰笔之前已有 ≥2 个同向趋势中枢（本级别趋势已走出至少两段同向中枢），
                是缠论中「大级别转折」的高胜率信号，级别最大。
    盘整背驰 —— 背驰笔之前仅有 1 个中枢（背驰发生在单中枢盘整内部），转折级别小、
                多为中枢震荡的折返，可靠性远低于趋势背驰。
    新生     —— 背驰笔之前无已完成中枢（趋势尚未成型），仅作 nascent 标注。
    该级别直接影响买卖点可信度：趋势背驰的一类买卖点是教科书级高确定性入场/离场点。"""
    for bc in beichis:
        i = bc["bi_index"]
        if i < 0 or i >= len(bis):
            bc["bc_type"] = ""
            continue
        bi = bis[i]
        # 已完成且结束于本笔之前的中枢数量（合并索引口径，与 find_signals 一致）
        prev_zs = [z for z in zss if z["end"] < bi["start"]]
        if len(prev_zs) >= 2:
            bc["bc_type"] = "趋势背驰"
        elif len(prev_zs) == 1:
            bc["bc_type"] = "盘整背驰"
        else:
            bc["bc_type"] = ""
    return beichis


# ---------- 5b. 走势段（笔的同向序列聚合）与段级背驰 ----------
def build_segments(bis, zss=None, min_zigzag=0.06):
    """走势段（更高一级结构）：在笔端点序列上做高阶 zigzag 聚合。

    笔必然顶底交替，若仅按"同向合并"聚合，每段只等于单笔，段背驰会与笔背驰完全等价、
    毫无增量。这里取所有笔的端点价序列，仅在反向突破前极值超过 ``min_zigzag``（默认 6%）
    时才确认一个新腿——这样会把小于阈值的次级折返"吸收"进同一腿，得到更大级别、更少、
    更平滑的走势腿。相邻同向腿之间必隔一个反向腿，段背驰才是跨级别的更高一层背离信号。

    ``zss`` 参数保留以备将来做"中枢连接段"细化，当前 zigzag 聚合不依赖它。
    """
    if not bis:
        return []
    if len(bis) < 3:
        return [{"dir": bis[0]["dir"], "start": bis[0]["start"], "end": bis[-1]["end"],
                 "start_price": bis[0]["start_price"], "end_price": bis[-1]["end_price"],
                 "high": max(b["high"] for b in bis), "low": min(b["low"] for b in bis)}]
    # 笔端点序列：(笔末端 merged 索引, 末端价)
    pts = [(b["end"], b["end_price"]) for b in bis]
    extremes = [pts[0]]
    trend = 1 if pts[1][1] >= pts[0][1] else -1
    for k in range(1, len(pts)):
        pr = pts[k][1]
        if trend == 1:
            if pr >= extremes[-1][1]:
                extremes[-1] = pts[k]
            elif pr <= extremes[-1][1] * (1 - min_zigzag):
                extremes.append(pts[k])
                trend = -1
        else:
            if pr <= extremes[-1][1]:
                extremes[-1] = pts[k]
            elif pr >= extremes[-1][1] * (1 + min_zigzag):
                extremes.append(pts[k])
                trend = 1
    if len(extremes) < 2:
        return [{"dir": bis[0]["dir"], "start": bis[0]["start"], "end": bis[-1]["end"],
                 "start_price": bis[0]["start_price"], "end_price": bis[-1]["end_price"],
                 "high": max(b["high"] for b in bis), "low": min(b["low"] for b in bis)}]
    segs = []
    for a, b in zip(extremes[:-1], extremes[1:]):
        i0, p0 = a
        i1, p1 = b
        chunk = [x for x in bis if i0 <= x["end"] <= i1]
        if not chunk:
            continue
        d = 1 if p1 >= p0 else -1
        segs.append({
            "dir": d, "start": chunk[0]["start"], "end": chunk[-1]["end"],
            "start_price": chunk[0]["start_price"], "end_price": chunk[-1]["end_price"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
        })
    return segs


def seg_macd_area(seg, hist, merged):
    s = merged[seg["start"]]["idx_start"]
    e = merged[seg["end"]]["idx_end"]
    if e < s:
        s, e = e, s
    segm = hist[s:e + 1]
    if seg["dir"] == 1:
        return sum(v for v in segm if v > 0)
    return abs(sum(v for v in segm if v < 0))


def find_beichi_segment(segs, hist, merged):
    """段级背驰：相邻同向走势段，价格创新极值但 MACD 面积萎缩 >=15%。
    段级背离比笔级更高一层，是缠论抓拐点的高胜率信号（#2）。"""
    out = []
    for i in range(2, len(segs)):
        cur = segs[i]
        prev = segs[i - 2]
        if prev["dir"] != cur["dir"]:
            continue
        a_cur = seg_macd_area(cur, hist, merged)
        a_prev = seg_macd_area(prev, hist, merged)
        if a_prev <= 0:
            continue
        if cur["dir"] == 1 and cur["high"] > prev["high"] and a_cur < a_prev * 0.85:
            out.append({"seg_index": i, "type": "top", "area_ratio": a_cur / a_prev})
        if cur["dir"] == -1 and cur["low"] < prev["low"] and a_cur < a_prev * 0.85:
            out.append({"seg_index": i, "type": "bottom", "area_ratio": a_cur / a_prev})
    return out


# ---------- 5c. 跳空缺口（支撑/压力位与趋势确认信号，此前维度空白） ----------
def find_gaps(klines, min_gap=0.003):
    """跳空缺口检测（基于原始日线，前复权数据已基本抹平除权跳空，残留即真实交易日跳空）。

    向上跳空 up：当日 low > 前日 high —— 缺口区间 [前高, 当低]；
    向下跳空 down：当日 high < 前日 low —— 缺口区间 [当高, 前低]。
    回补 filled：向上缺口后续任一 K 线 low ≤ 缺口下沿(前高) → 回补（缺口被封闭）；
                向下缺口后续任一 K 线 high ≥ 缺口上沿(前低) → 回补。
                缺口上方/下方的真空区在回补前构成强支撑（向下跳空）或强压力（向上跳空），
                A 股「逢缺必补」经验规律下，未补缺口是缠论中枢之外最重要的价位锚之一。

    min_gap 过滤复权因子微调日与微小缝隙噪声（幅度 < 0.3% 不算缺口），
    返回 [{date, idx, type, top, bottom, filled}]。"""
    gaps = []
    n = len(klines)
    for i in range(1, n):
        prev, cur = klines[i - 1], klines[i]
        if cur["low"] > prev["high"]:
            top, bottom = cur["low"], prev["high"]
            if (top - bottom) / bottom < min_gap:
                continue
            filled = any(klines[j]["low"] <= bottom for j in range(i + 1, n))
            gaps.append({"date": cur["date"], "idx": i, "type": "up",
                         "top": top, "bottom": bottom, "filled": filled})
        elif cur["high"] < prev["low"]:
            top, bottom = prev["low"], cur["high"]
            if (top - bottom) / bottom < min_gap:
                continue
            filled = any(klines[j]["high"] >= top for j in range(i + 1, n))
            gaps.append({"date": cur["date"], "idx": i, "type": "down",
                         "top": top, "bottom": bottom, "filled": filled})
    return gaps


# ---------- 6. 买卖点 ----------
def find_signals(bis, zss, beichis, klines=None, merged=None):
    """买卖点体系（缠论标准定义）：
    一类：背驰拐点（底背驰=买，顶背驰=卖）。
    二类：一类之后的次低点/次高点折返——二类买=一类买之后向下折返笔末端价 > 一类买价
          （不破前低，更高低点）；二类卖=一类卖之后向上折返笔末端价 < 一类卖价（不破前高）。
          这是缠论二类的本质定义（与一类同侧的次级折返），比「落在中枢内」更严谨。
    三类：中枢完成后离去再抽，低点/高点不重新进入中枢区间——三类买=回抽低点不破 ZD
          （ep>ZD，留在中枢上方）；三类卖=反抽高点不破 ZG（ep<ZG，留在中枢下方）。
          此前用 ep>ZG / ep<ZD 过严，会把真正的三类（仅不进中枢、仍可落在中枢半区）漏判，现已修正。
    每个一类信号附带 vol_confirm：该笔量能较前一同向笔萎缩（量价背离确认）。
    """
    signals = []
    bc_map = {b["bi_index"]: b for b in beichis}
    p1_buys, p1_sells = [], []
    # 一类买卖点（背驰拐点），并携带该背驰的级别（趋势背驰/盘整背驰）——级别决定信号可信度。
    for i, b in enumerate(bis):
        vc = bc_map[i].get("vol_confirm", False) if i in bc_map else False
        if i in bc_map:
            bt_type = bc_map[i].get("bc_type", "")
            if bc_map[i]["type"] == "bottom":
                kind = "一类买点(底背驰)" + ("·趋势" if bt_type == "趋势背驰"
                                            else ("·盘整" if bt_type == "盘整背驰" else ""))
                signals.append({"bi_index": i, "kind": kind, "dir": 1,
                                "date": b["date_end"], "price": b["end_price"],
                                "vol_confirm": vc, "bc_type": bt_type})
                p1_buys.append((i, b["end_price"]))
            else:
                kind = "一类卖点(顶背驰)" + ("·趋势" if bt_type == "趋势背驰"
                                            else ("·盘整" if bt_type == "盘整背驰" else ""))
                signals.append({"bi_index": i, "kind": kind, "dir": -1,
                                "date": b["date_end"], "price": b["end_price"],
                                "vol_confirm": vc, "bc_type": bt_type})
                p1_sells.append((i, b["end_price"]))
    # 二/三类买卖点：去重为「每锚点的首个有效折返」——缠论中二类是紧接一类后的次低/次高折返、
    # 三类是中枢离开后的首个回抽；此前逐笔判断导致长年累积出上百个冗余信号（噪声），
    # 既失真又拖垮回测读数。现改为：每个一类买卖点只取其后的首支有效折返笔作为二类；
    # 每个中枢只取其离开后的首个回抽/反抽（不重新进入中枢区间）作为三类。
    # 二类买点：一类买之后，向下折返笔末端价 > 一类买价（次低，不破前低）。取其后首支。
    for idx0, pr0 in p1_buys:
        for j in range(idx0 + 1, len(bis)):
            bj = bis[j]
            if bj["dir"] == -1 and bj["end_price"] > pr0:
                signals.append({"bi_index": j, "kind": "二类买点(次低不破)", "dir": 1,
                                "date": bj["date_end"], "price": bj["end_price"],
                                "vol_confirm": False, "bc_type": ""})
                break
    # 二类卖点：一类卖之后，向上折返笔末端价 < 一类卖价（次高，不破前高）。取其后首支。
    for idx0, pr0 in p1_sells:
        for j in range(idx0 + 1, len(bis)):
            bj = bis[j]
            if bj["dir"] == 1 and bj["end_price"] < pr0:
                signals.append({"bi_index": j, "kind": "二类卖点(次高不破)", "dir": -1,
                                "date": bj["date_end"], "price": bj["end_price"],
                                "vol_confirm": False, "bc_type": ""})
                break
    # 三类买卖点：每个中枢离开后的首个回抽/反抽（不重新进入中枢区间）。
    # 起点价约束确保笔确已「离开」中枢（回抽起点已破 ZG / 反抽起点已破 ZD），
    # 排除中枢内普通折返被误判为三类。每个中枢只取其首个三类点，避免多年累积噪声。
    for z in zss:
        for j in range(len(bis)):
            bj = bis[j]
            if bj["start"] > z["end"]:  # 笔起点在中枢结束之后（中枢已成形）
                if bj["dir"] == -1 and bj["start_price"] > z["zg"] and bj["end_price"] > z["zd"]:
                    signals.append({"bi_index": j, "kind": "三类买点(回抽不进)", "dir": 1,
                                    "date": bj["date_end"], "price": bj["end_price"],
                                    "vol_confirm": False, "bc_type": ""})
                    break
                if bj["dir"] == 1 and bj["start_price"] < z["zd"] and bj["end_price"] < z["zg"]:
                    signals.append({"bi_index": j, "kind": "三类卖点(反抽不进)", "dir": -1,
                                    "date": bj["date_end"], "price": bj["end_price"],
                                    "vol_confirm": False, "bc_type": ""})
                    break
    signals.sort(key=lambda s: s["bi_index"])
    # 信号去重（fix R97）：同一笔可能被标记为多个买卖点——（1）多个一类买点共享同一支折返笔
    # 会重复生成「二类买」；（2）一支折返笔同时满足二类与三类判定会叠加；（3）同一支笔可能满足
    # 多个中枢的三类条件被多重标记。同一 bi_index 仅保留一个信号，优先级 一类 > 三类 > 二类
    # （一类买卖点确定性最高，三类次之，二类为次级折返），消除图上叠加三角形与回测重复计数偏倚。
    _prio = lambda k: 0 if "一类" in k else (1 if "三类" in k else 2)
    _seen = {}
    _dedup = []
    for s in signals:
        i = s["bi_index"]
        if i in _seen:
            if _prio(s["kind"]) < _prio(_seen[i]["kind"]):
                for n, x in enumerate(_dedup):
                    if x["bi_index"] == i:
                        _dedup[n] = s
                        _seen[i] = s
                        break
        else:
            _seen[i] = s
            _dedup.append(s)
    signals = _dedup
    # 风险收益比（R:R）量化——缠论实战必备：每个买卖点须有明确止损位与目标位才算完整交易计划。
    # 止损（stop）：买点取该点之前「局部前低」（最近 30 笔窗口内的笔末端最低价）下破一点点；
    #   卖点取局部前高上破一点点。窗口限制至关重要——此前误用全历史最低点导致止损远低于现价、
    #   R:R 被严重压低失真（实测中位仅 0.3）；缠论止损是「跌破近期前低/中枢下沿」，非数年极值。
    #   若窗口内无更低/更高参考（买点即新低），则用价位 ±3% 默认止损，保证 risk>0。
    # 目标（target）：取该笔之前最近已完成中枢的 ZG（买点向上空间第一目标）/ ZD（卖点向下空间第一目标）；
    #   无中枢时用价位 ±6% 默认目标。R:R = reward / risk，并按阈值给「值博率」标签（优/良/中/差）。
    _LOOK = 30  # 局部前低/前高窗口（日线约 30 笔≈1.5 个月，捕捉近期结构而非历史极值）
    _RR_CAP = 6.0  # R:R 封顶：超出部分为多年极值噪声锚定，对交易计划无意义（此前出现 16~31 倍失真）
    for s in signals:
        i = s["bi_index"]
        bj = bis[i]
        price = s["price"]
        _prev = bis[max(0, i - _LOOK):i]
        # 近程摆动极值：最近 30 笔的真实高低点（b["high"]/b["low"] 为整笔区间极值），
        # 作为贴合当前结构的"最近阻力/支撑"。此前用 z_prev 历史极值中枢锚定目标，
        # 会把多年高/低位（如卖点 target=2022 年低点）纳入，导致 16~31 倍失真 R:R。
        prev_hi = max((b["high"] for b in _prev), default=price)
        prev_lo = min((b["low"] for b in _prev), default=price)
        z_prev = None
        for z in zss:
            if z["end"] <= bj["start"]:
                z_prev = z
        if s["dir"] == 1:  # 买点
            # 三类买（回抽不进中枢）：价格已在中枢上方，止损看「跌破中枢下沿 ZD」而非久远前低；
            if "三类" in s["kind"] and z_prev:
                stop = z_prev["zd"] * 0.99
            else:
                stop = prev_lo * 0.99 if prev_lo < price else price * 0.97
            # 目标：近程摆动高点优先；仅当最近中枢上沿 ZG 在合理距离内(≤现价20%)才纳入，
            # 避免锚到多年极值高位；再叠加「中枢高度×0.618」斐波扩展作更高目标候选（#预测优化·C），
            # 最终受 ≤6 倍 R:R 封顶约束。
            tgt = max(price * 1.06, prev_hi)
            if z_prev and z_prev["zg"] <= price * 1.20:
                tgt = max(tgt, z_prev["zg"])
            if z_prev:
                _zh = z_prev["zg"] - z_prev["zd"]
                tgt = max(tgt, z_prev["zg"] + _zh * 0.618 * 0.9)
            target = tgt
        else:  # 卖点
            if "三类" in s["kind"] and z_prev:
                stop = z_prev["zg"] * 1.01
            else:
                stop = prev_hi * 1.01 if prev_hi > price else price * 1.03
            tgt = min(price * 0.94, prev_lo)
            if z_prev and z_prev["zd"] >= price * 0.80:
                tgt = min(tgt, z_prev["zd"])
            if z_prev:
                _zh = z_prev["zg"] - z_prev["zd"]
                tgt = min(tgt, z_prev["zd"] - _zh * 0.618 * 0.9)
            target = tgt
        # 波动率自适应止损（#预测优化·C）：叠加 1.5×ATR(14) 缓冲，避免窄幅震荡被毛刺扫损。
        # 仅当 klines/merged 传入且笔末索引足够才启用，否则退回结构止损。买点取更靠下(更宽松)、
        # 卖点取更靠上(更宽松)，使止损不被近期噪声击穿却不过度远离现价。
        if klines is not None and merged is not None:
            _e = merged[bj["end"]]["idx_end"]
            if _e >= 13:
                _trs = []
                for _j in range(max(1, _e - 13), _e + 1):
                    _h, _l, _c0 = klines[_j]["high"], klines[_j]["low"], klines[_j - 1]["close"]
                    _trs.append(max(_h - _l, abs(_h - _c0), abs(_l - _c0)))
                _atr = sum(_trs) / len(_trs) if _trs else 0.0
                if s["dir"] == 1:
                    stop = min(stop, price - 1.5 * _atr)
                else:
                    stop = max(stop, price + 1.5 * _atr)
        risk = abs(price - stop)
        # R:R 封顶（#29·修复失真）：折算目标不得使风险收益比超过 _RR_CAP，
        # 超出部分为多年极值噪声，对交易计划无意义且严重误导（此前 16~31 倍）。
        if s["dir"] == 1:
            target = min(target, price + risk * _RR_CAP)
        else:
            target = max(target, price - risk * _RR_CAP)
        reward = abs(target - price)
        rr = reward / risk if risk > 0 else None
        s["stop"] = round(stop, 2)
        s["target"] = round(target, 2)
        s["rr"] = round(rr, 2) if rr is not None and rr > 0 else None
        if s["rr"] is None:
            s["quality"] = "—"
        elif s["rr"] >= 2.5:
            s["quality"] = "优"
        elif s["rr"] >= 1.5:
            s["quality"] = "良"
        elif s["rr"] >= 1.0:
            s["quality"] = "中"
        else:
            s["quality"] = "差"
    return signals


# ---------- 7. 分类推演 ----------
def classify(bis, zss, beichis, close, wcls=None, segments=None, seg_beichi=None, mcls=None):
    if not bis:
        return {"scenario": "数据不足", "detail": "",
                "seg_bc_bottom": False, "seg_bc_top": False, "interval_nesting": ""}
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
    # 背驰级别定语：趋势背驰=本级别大级别转折（确定性高），盘整背驰=单中枢内折返（级别小）
    _recent_types = [b.get("bc_type") for b in recent_bc if b.get("bc_type")]
    if "趋势背驰" in _recent_types:
        _bc_qual = "（趋势背驰·本级别大级别转折信号，确定性更高）"
    elif _recent_types:
        _bc_qual = "（盘整背驰·级别较小，多为中枢震荡折返）"
    else:
        _bc_qual = ""

    # 段级背驰（走势段级别的更高层级背离，#2）
    seg_bot = any(b["type"] == "bottom" for b in (seg_beichi or []))
    seg_top = any(b["type"] == "top" for b in (seg_beichi or []))

    # 日×周区间套（#2）：日线背驰与周线趋势方向组合，给出更高一级共振判断
    nest = ""
    if wcls is not None:
        wdir = wcls.get("last_bi_dir")
        if bc_bot and wdir == -1:
            nest = "日线底背驰与周线向下笔共振（区间套·潜在周线级低点），拐点确认概率提升"
        elif bc_top and wdir == 1:
            nest = "日线顶背驰与周线向上笔共振（区间套·潜在周线级高点），见顶风险提升"
        elif bc_bot and wdir == 1:
            nest = "日线底背驰、周线已转多（区间套·共振向上），反弹延续性更强"
        elif bc_top and wdir == -1:
            nest = "日线顶背驰、周线仍向下（区间套·反弹中的高点），减仓防守为主"

    # 月线背景定调（第三层区间套）：月线方向决定大级别趋势背景，与日×周共振互补
    mdesc = ""
    month_dir = 0  # 月线大级别方向：1=多头背景 / -1=空头背景 / 0=待明；供 forecast 三重共振使用
    if mcls is not None:
        mdir = mcls.get("last_bi_dir")
        m_scen = mcls.get("scenario", "")
        # 月线背景方向以「月线自身情景分类」判定（比单看 last_bi_dir 更稳健，能区分震荡与趋势）
        if m_scen in ("多头延续", "中枢震荡偏多", "高位整理未破前高", "背驰见底机会"):
            month_dir = 1
        elif m_scen in ("背驰见顶风险", "中枢震荡偏空", "弱势反弹", "反弹未回中枢", "空头延续"):
            month_dir = -1
        if mdir == 1:
            mdesc = "月线处多头背景(%s)" % m_scen
        elif mdir == -1:
            mdesc = "月线处空头背景(%s)" % m_scen
        else:
            mdesc = "月线方向待明(%s)" % m_scen
        nest = (nest + "；" + mdesc) if nest else mdesc

    if bc_top and last["dir"] == 1:
        scenario = "背驰见顶风险"
        detail = "最近向上笔价格创新高但MACD红柱面积明显萎缩（面积比 %.2f）" % recent_bc[-1]["area_ratio"]
        if seg_top:
            detail += "，且走势段级别同步出现顶背驰"
        detail += ("，构成顶背驰%s。短线警惕一类卖点确认，回落目标先看最近中枢ZG(%.1f)。"
                   % (_bc_qual, last_zs["zg"] if last_zs else close))
    elif bc_bot and last["dir"] == -1:
        scenario = "背驰见底机会"
        detail = "最近向下笔价格创新低但MACD绿柱面积明显萎缩（面积比 %.2f）" % recent_bc[-1]["area_ratio"]
        if seg_bot:
            detail += "，且走势段级别同步出现底背驰"
        detail += ("，构成底背驰%s。关注一类买点后的反弹，第一压力看最近中枢ZD(%.1f)。"
                   % (_bc_qual, last_zs["zd"] if last_zs else close))
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
    elif last["dir"] == 1 and pos == "中枢下方":
        # 向上笔尝试收复，但仍未站回最后中枢下沿 ZD——属弱势反弹，尚未扭转空头结构
        scenario = "反弹未回中枢"
        detail = "当前向上笔运行于最后中枢ZD(%.1f)下方，属弱势反弹。只有重新站回中枢内部才算扭转弱势；若再次跌破前低，则确认空头延续。" % (last_zs["zd"] if last_zs else close)
    elif pos == "无中枢":
        if last["dir"] == 1:
            scenario = "无中枢·向上笔"
            detail = "当前尚无已完成中枢，向上笔运行中，暂按笔级别多头对待；待中枢成型后再定级别与买卖点。"
        else:
            scenario = "无中枢·向下笔"
            detail = "当前尚无已完成中枢，向下笔运行中，暂按笔级别空头对待；待中枢成型后再定级别与买卖点。"
    else:  # 兜底（理论上不会触达）：方向/位置组合未覆盖时的安全默认值
        scenario = "震荡待方向"
        detail = "价格处于中间位置、方向待确认，暂按中性震荡处理，等待分型与笔的进一步确认。"

    if nest:
        detail += "　【区间套】" + nest + "。"
    # 注：mdesc 已并入 nest（区间套）并在上方统一输出，避免「月线背景」在 detail 中重复出现。

    # 多周期共振结论（日/周/月三层方向联立，供卡片与汇总表结构化呈现，替代 detail 文本嵌套）：
    # 缠论「区间套」的本质就是大级别定方向、小级别找买卖点。三周期同向=强共振；
    # 日强周弱（当前 5 指数共性）=月线多头背景下的日线反弹、周线尚未确认，反弹非反转。
    week_dir = wcls.get("last_bi_dir") if wcls else None
    week_scenario = wcls.get("scenario") if wcls else None
    month_scenario = mcls.get("scenario") if mcls else None
    resonance = ""
    if wcls is not None and mcls is not None:
        d_dir, w_dir, m_dir = last["dir"], week_dir, month_dir
        ups = sum(1 for x in (d_dir, w_dir, m_dir) if x == 1)
        downs = sum(1 for x in (d_dir, w_dir, m_dir) if x == -1)
        if ups == 3:
            resonance = "三周期共振·多头"
        elif downs == 3:
            resonance = "三周期共振·空头"
        elif d_dir == 1 and w_dir == -1 and m_dir == 1:
            resonance = "月多·日反弹·周未确认（反弹非反转）"
        elif d_dir == -1 and w_dir == 1 and m_dir == -1:
            resonance = "月空·日回调·周未确认"
        elif d_dir != w_dir:
            resonance = "日强周弱背离" if d_dir == 1 else "日弱周强背离"
        else:
            resonance = "日周共振·月背景定调"

    # 走势类型（缠论核心框架）：取最近若干中枢区间中点拟合斜率，判断中枢递升/递降 → 趋势；
    # 否则盘整/扩张。用多中枢斜率而非仅相邻两个，避免单笔噪声造成的方向误判。
    # 关键修正（#24）：① 把「当前价」作为末端点纳入斜率序列——此前出现「scenario=多头延续
    #   （已突破最后中枢 ZG）却误标 下跌走势」的矛盾，根因是斜率只看历史中枢、看不见最新突破；
    #   纳入现价后，有效突破会翻转/拉平斜率，使走势类型与即时分类自洽。② 一致性护栏：若即时
    #   分类已有明确多/空偏置，而走势类型与之反向硬冲突，降级为中性「盘整/扩张走势」，
    #   杜绝「多头延续 + 下跌走势」式自相矛盾呈现。
    trend_type = "盘整走势"
    if len(zss) >= 3:
        _win = zss[-5:] if len(zss) >= 5 else zss
        _mids = [(z["zd"] + z["zg"]) / 2 for z in _win]
        _mids.append(close)  # 纳入当前价，反映最新突破/回落
        _n = len(_mids)
        _x = list(range(_n))
        _xm = sum(_x) / _n
        _ym = sum(_mids) / _n
        _num = sum((_x[k] - _xm) * (_mids[k] - _ym) for k in range(_n))
        _den = sum((_x[k] - _xm) ** 2 for k in range(_n)) or 1
        _slope = _num / _den
        _rel = _slope / _ym if _ym else 0
        if _rel > 0.008:
            trend_type = "上涨走势(趋势)"
        elif _rel < -0.008:
            trend_type = "下跌走势(趋势)"
        else:
            trend_type = "盘整/扩张走势"
    # 一致性护栏：走势类型不得与即时分类方向硬冲突（避免自相矛盾呈现）
    _up_sc = scenario in ("多头延续", "高位整理未破前高", "背驰见底机会")
    _dn_sc = scenario in ("空头延续", "反弹未回中枢", "背驰见顶风险")
    if _up_sc and trend_type == "下跌走势(趋势)":
        trend_type = "盘整/扩张走势"
    if _dn_sc and trend_type == "上涨走势(趋势)":
        trend_type = "盘整/扩张走势"

    return {"scenario": scenario, "detail": detail, "position": pos,
            "last_bi_dir": last["dir"], "last_bi_pct": (last["end_price"] / last["start_price"] - 1),
            "seg_bc_bottom": seg_bot, "seg_bc_top": seg_top, "interval_nesting": nest,
            "month_context": mdesc, "trend_type": trend_type, "month_dir": month_dir,
            "week_dir": week_dir, "week_scenario": week_scenario,
            "month_scenario": month_scenario, "resonance": resonance}


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
    # dir==1 的笔为 底->顶，终点落在“顶”；dir==-1 为 顶->底，终点落在“底”
    top_dates = {r["merged"][b["end"]]["date"] for b in r["bis"] if b["dir"] == 1}
    bottom_dates = {r["merged"][b["end"]]["date"] for b in r["bis"] if b["dir"] == -1}
    for d, (label, direction) in KNOWN_PIVOTS.items():
        pool = top_dates if direction == "top" else bottom_dates
        for pd in pool:
            if abs(_date_diff(pd, d)) <= 12:
                captured.append((label, direction, pd))
                break
    return captured


# ---------- 8f. 结论稳健度 / 信号成熟度（鲁棒性外部校验） ----------
# 牛/熊情景集合（与 report.SC_BULL/SC_BEAR 对齐，供极性判定）
_SC_BULL = ("多头延续", "中枢震荡偏多", "高位整理未破前高", "背驰见底机会")
_SC_BEAR = ("背驰见顶风险", "中枢震荡偏空", "弱势反弹", "反弹未回中枢", "空头延续")


def _polarity(sc):
    """情景多空极性：1=多头 / -1=空头 / 0=中性（数据不足等）。"""
    if sc in _SC_BULL:
        return 1
    if sc in _SC_BEAR:
        return -1
    return 0


def _trend_polarity(tt):
    """走势类型极性：1=上涨趋势 / -1=下跌趋势 / 0=盘整扩张。"""
    if "上涨" in tt:
        return 1
    if "下跌" in tt:
        return -1
    return 0


def classification_stability(klines, min_bi_pct=MIN_BI_PCT):
    """结论稳健度 / 信号成熟度（鲁棒性外部校验，#29 重构为三级）。

    返回 {base, drops, stable, maturity, last_bi_bars, level}。
      stable        —— 多空极性(_polarity)与走势类型极性(_trend_polarity)在砍掉 5/10/20 根 K 线后
                       是否一致；极性翻转(多↔空)属"结论脆弱"，需谨慎。
      maturity      —— "established" 信号成熟(最后一支已完成笔跨度≥12 交易日) / "young" 信号年轻；
                       年轻信号对近 1~2 周价格高度敏感，属"待确认"而非可靠结论。
      last_bi_bars  —— 最后一支已完成笔的真实交易日跨度（含被包含处理吸收的 K 线）。
      level         —— "稳健" / "边缘" / "敏感·待确认" 三级，供卡片与概述结构化呈现，
                       取代此前"一律不稳定"的二元判定，避免误导。
    设计要点：此前仅比较 scenario 字符串，而 scenario 几乎完全由最后一支已完成笔方向主导，
    近期反弹笔仅 ~7 根 K 线，砍掉即翻转 → 所有指数被一概判为不稳定且统一 -0.04 惩罚。
    现改为分级：极性翻转(多↔空)才判"敏感·待确认"(谨慎 -0.04)；趋势守住但最后笔年轻判"边缘"(-0.02)；
    其余"稳健"(0)。结论的真实性（当前为多头的指数确实依赖年轻笔）得以保留，但呈现更准确。"""
    base_res = analyze(klines, min_bi_pct, with_stability=False)
    base = classify(*_last_two(base_res, klines[-1]["close"]))
    base_pol = _polarity(base["scenario"])
    base_trend = _trend_polarity(base.get("trend_type", ""))
    # 信号成熟度（#39 修复 + 复合化）：此前量 bis[-2]（即确立当前方向前一棒的反向修正笔），
    # 导致所有指数清一色 young、三级稳健度永远显示不出「稳健」。现改为量「确立当前方向的笔」
    # （方向与 last_bi_dir 一致的最近一支已完成笔）跨度，并叠加「同向连笔总跨度」——真正的趋势
    # 市里长笔/多连笔会判为成熟，震荡市短单笔仍判年轻，三级稳健度才有真实区分度。
    cur_dir = base["last_bi_dir"]
    _dir_bi = None
    for _b in reversed(base_res["bis"]):
        if _b["dir"] == cur_dir:
            _dir_bi = _b
            break
    if _dir_bi is not None:
        _a = base_res["merged"][_dir_bi["start"]]["idx_start"]
        _e = base_res["merged"][_dir_bi["end"]]["idx_end"]
        dir_bi_bars = (_e - _a + 1) if _e >= _a else 1
    else:
        dir_bi_bars = 0
    # 同向连笔（当前方向连续笔）数量与总跨度
    _run = 0
    for _b in reversed(base_res["bis"]):
        if _b["dir"] == cur_dir:
            _run += 1
        else:
            break
    if _run > 0:
        _fb = base_res["bis"][len(base_res["bis"]) - _run]
        _fa = base_res["merged"][_fb["start"]]["idx_start"]
        _le = base_res["merged"][base_res["bis"][-1]["end"]]["idx_end"]
        run_span = (_le - _fa + 1) if _le >= _fa else dir_bi_bars
    else:
        run_span = dir_bi_bars
    last_bi_bars = dir_bi_bars or 1
    _established = (dir_bi_bars >= 12) or (_run >= 2 and run_span >= 20)
    out = {"base": base, "drops": {}, "stable": True,
           "maturity": "established" if _established else "young",
           "last_bi_bars": last_bi_bars, "level": "稳健",
           "run_span": run_span, "same_dir_run": _run}
    pol_drift = False
    # #39 修复：漂移测试窗口须为「噪声级」且短于「确立当前方向的笔」本身——此前固定
    # drop=(5,10,20)，而上涨笔常仅 8~11 根，drop 5~20 等于删掉整段行情再问"它还在不在"，
    # 必然翻转，导致几乎所有指数永远判「敏感·待确认」、三级稳健度失去区分度与预警价值。
    # 现改为噪声级窗口 (1,2,3)：仅测「结论是否依赖最后几根杂波」——8~11 根真实上涨笔删 1~3
    # 根仍多头(→边缘)，仅 1~2 根毛刺尖删 1~2 根即翻(→敏感)，成熟趋势则稳健。跳过 k>=方向笔长度。
    for k in (1, 2, 3):
        if k >= dir_bi_bars:
            continue
        if len(klines) > k + 30:
            sub = klines[: len(klines) - k]
            sc = classify(*_last_two(analyze(sub, min_bi_pct, with_stability=False), sub[-1]["close"]))
            out["drops"][k] = sc
            if _polarity(sc["scenario"]) != base_pol or _trend_polarity(sc.get("trend_type", "")) != base_trend:
                out["stable"] = False
                if _polarity(sc["scenario"]) != base_pol:
                    pol_drift = True
    # 三级稳健度（#39 重构）：极性翻转(多↔空)=敏感·待确认(-0.04)；稳定但信号年轻
    # (末笔跨度<12 且无多连笔确认)=边缘(-0.02)；稳定且成熟(长笔或多连笔确认)=稳健(0)。
    # 此前"稳定即稳健"会把年轻信号也标稳健，掩盖"待确认"属性，故纳入成熟度判定。
    if pol_drift:
        out["level"] = "敏感·待确认"
    elif not _established:
        out["level"] = "边缘"
    else:
        out["level"] = "稳健"
    return out


# ---------- 8h. 跨指数市场广度综合研判（日/周/月三级） ----------
def market_breadth(daily_sc, week_sc, month_sc):
    """把 5 个独立指数的情景聚合成市场级结论，避免「只看日线」掩盖更高级别背离。

    输入三个列表，各含 N 个指数的 scenario 字符串（顺序对应同一组指数）。
    返回 {daily:{bull,bear,neutral,total}, week:{...}, month:{...},
          composite:{score, label}, conclusion}。
    composite.score 为加权多空极性（月线 0.4 / 周线 0.4 / 日线 0.2，周线定节奏故提高权重），
    落在 [-1,1]；conclusion 显式识别「月多·日反弹·周偏空」类跨级别背离，
    而非单一分数，避免误导。"""

    def _cnt(sc_list):
        bull = sum(1 for x in sc_list if x in _SC_BULL)
        bear = sum(1 for x in sc_list if x in _SC_BEAR)
        return {"bull": bull, "bear": bear, "neutral": len(sc_list) - bull - bear, "total": len(sc_list)}

    def _pol(sc_list):
        s = 0
        for x in sc_list:
            if x in _SC_BULL:
                s += 1
            elif x in _SC_BEAR:
                s -= 1
        return s / len(sc_list) if sc_list else 0

    d_cnt, w_cnt, m_cnt = _cnt(daily_sc), _cnt(week_sc), _cnt(month_sc)
    score = 0.2 * _pol(daily_sc) + 0.4 * _pol(week_sc) + 0.4 * _pol(month_sc)
    if score >= 0.5:
        label = "多头主导"
    elif score >= 0.2:
        label = "偏多（高层级有分歧）"
    elif score > -0.2:
        label = "分歧震荡"
    elif score > -0.5:
        label = "偏空"
    else:
        label = "空头主导"
    # 跨级别背离识别（结论比单一分数更诚实）
    m_bull = m_cnt["bull"] >= m_cnt["total"] * 0.6
    w_bear = w_cnt["bear"] >= w_cnt["total"] * 0.6
    d_bull = d_cnt["bull"] >= d_cnt["total"] * 0.6
    if m_bull and w_bear and d_bull:
        conclusion = ("月线多头 + 周线偏空 + 日线反弹 → 当前日线上涨在更大级别上属<b>反弹而非主升浪</b>；"
                      "周线 4/5 偏空显示周线级调整尚未结束，反弹需<b>周线底分型确认</b>才能升级为反转，"
                      "仓位与预期应低于「日周共振多头」情形。")
    elif m_bull and w_bear:
        conclusion = ("月线多头背景下周线偏空，日线反弹更可能是周线调整中的修复段；"
                      "关注周线能否出现底分型，作为反转确认信号。")
    elif w_bear and not m_bull:
        conclusion = "周线与月线同步偏空，系统性环境压制，反弹持续性弱，防御为主。"
    elif d_bull and not w_bear and not m_bull:
        conclusion = "日线偏多但周/月均偏空，短线反弹难改更大级别弱势。"
    else:
        conclusion = ("日/周/月三级别方向大体一致，结构共识度较高，系统性环境对推演方向形成支撑。"
                      if score >= 0 else "日/周/月三级别方向大体一致偏空，系统性环境压制。")
    return {"daily": d_cnt, "week": w_cnt, "month": m_cnt,
            "composite": {"score": round(score, 3), "label": label}, "conclusion": conclusion}


def _last_two(res, close):
    return res["bis"], res["zhongshu"], res["beichi"], close


# ---------- 8g. 前向收益波动（置信锥用） ----------
# ---------- 8c. 推演路径历史命中率验证（预测准确性自校验） ----------
def _path_targets(scenario, zg, zd, mid, last, move=0.05):
    """复刻 report.forecast_svg 的三路径目标锚定（同源，保证命中率校验与推演图语义一致）。
    返回 (up_tgt, risk_level, main_dir)：
      up_tgt      —— 主路径终点目标位（多头场景向上突破位 / 空头场景反抽不过位）
      risk_level  —— 风险路径终点位（跌破即确认转空）
      main_dir    —— 主路径方向 1=向上 / -1=向下 / 0=中性
    """
    if scenario == "多头延续":
        # 与 report.forecast_svg「多头延续」主路径终点 (up_tgt*1.03) 严格一致，避免校准锚与展示路径错位
        up_tgt = max(zg * 1.01, last * (1 + move)) * 1.03
        risk_level = zd * 0.94
        main_dir = 1
    elif scenario in ("中枢震荡偏多", "高位整理未破前高"):
        up_tgt = zg * 1.03
        risk_level = zd * 0.94
        main_dir = 1
    elif scenario == "背驰见底机会":
        up_tgt = zg * 1.02
        risk_level = zd * 0.93
        main_dir = 1
    elif scenario in ("背驰见顶风险", "中枢震荡偏空", "弱势反弹", "空头延续", "反弹未回中枢"):
        up_tgt = mid * 0.99   # 主路径=回落/中枢内，向上空间有限
        risk_level = zd * 0.92
        main_dir = -1
    else:
        up_tgt = mid
        risk_level = zd * 0.99
        main_dir = 0
    return up_tgt, risk_level, main_dir


def backtest_paths(klines, min_bi_pct=MIN_BI_PCT, horizon=60, step=20, with_stability=False):
    """推演路径历史命中率验证（预测准确性自校验）：
    滑动窗口回溯历史每个结构点 t（步进 step，t≥260 以保证均线/中枢完整），
    用当时数据 analyze 得到结构，按 _path_targets 锚定当时主/风险目标位，
    再看其后 horizon 日实际走势（用 high/low 而非 close 更贴近路径触及）落在
    主/次/风险哪条路径，统计各 scenario 命中率。
    返回 {"by_scenario": {sc: {n, main, alt, risk}}, "by_dir": {dir: {...}}, "total": {...}}。
      by_scenario —— 按精确分类分组（样本小，仅参考）
      by_dir     —— 按主路径方向分组（多头类 dir=1 / 空头类 dir=-1 / 中性 dir=0），
                    样本量大、稳健，是报告对照的主要依据
    这是直接校验「推演图概率标得准不准」的方法——p_main 是贝叶斯口径，而本函数
    用几何路径的实际兑现率交叉验证，二者对照可暴露校准偏差（偏乐观/偏保守）。

    关键修正（#23·校准真实性）：
      ① 命中判定改为「方向感知」。此前对所有方向统一用 hi>=up_tgt 判"主路径命中"，
         但空头情景 up_tgt=mid*0.99 低于现价，hi(区间最高)>=up_tgt 几乎恒真，
         导致空头指数"主路径命中率"被虚高到≈100%——path_hit_html 校准严重失真、
         且 forecast 据此锚定的概率不可信。现按 main_dir 分别判定：
           - 多头(main_dir=+1)：区间最高突破 up_tgt→主；最低跌破 risk_level→风险；否则次；
           - 空头(main_dir=-1)：最低触及 up_tgt(回落目标)→主；跌破更深 risk_level→风险；
                                  价格未回落(抗跌)→次(意外强势)；
           - 中性(main_dir=0)：围绕 mid 震荡→主；跌破 risk_level→风险；否则次。
      ② 样本近因加权。A股牛熊周期约 2~3 年，早年样本与当前 regime 可能已变，等权统计
         会稀释近期规律。现对每个历史样本按距今年限做指数衰减加权(w=0.5^age_years)，
         使校准更贴近当前市场状态、提升样本外稳健性；n/main/alt/risk 均为加权累计(浮点)。"""
    n = len(klines)
    by_sc = {}
    by_dir = {1: {"n": 0.0, "main": 0.0, "alt": 0.0, "risk": 0.0},
              -1: {"n": 0.0, "main": 0.0, "alt": 0.0, "risk": 0.0},
              0: {"n": 0.0, "main": 0.0, "alt": 0.0, "risk": 0.0}}
    tot = {"n": 0.0, "main": 0.0, "alt": 0.0, "risk": 0.0}
    t = 260
    while t + horizon < n:
        sub = analyze(klines[:t + 1], min_bi_pct, with_stability=with_stability)
        zss = sub["zhongshu"]
        if not zss:
            t += step
            continue
        zs = zss[-1]
        zg, zd = zs["zg"], zs["zd"]
        mid = (zg + zd) / 2
        last = klines[t]["close"]
        sc = sub["classify"]["scenario"]
        # 用「当时最近完成笔的真实幅度」作为 move 锚（与 report.forecast_svg 口径一致），
        # 取代此前硬编码 0.05——否则校准命中率用的是 5% 固定目标、与展示路径(实测幅度)错位，
        # 导致 p_main 的命中率夹逼校准失真、预测概率不可信。
        _comp = sub["bis"][-2] if len(sub["bis"]) >= 2 else sub["bis"][-1]
        _move = max(abs(_comp["end_price"] / _comp["start_price"] - 1), 0.03)
        up_tgt, risk_level, main_dir = _path_targets(sc, zg, zd, mid, last, _move)
        hi = max(klines[i]["high"] for i in range(t + 1, t + 1 + horizon))
        lo = min(klines[i]["low"] for i in range(t + 1, t + 1 + horizon))
        # 方向感知命中判定（#23 修复空头情景虚高）
        if main_dir == 1:
            if hi >= up_tgt:
                hit = "main"
            elif lo <= risk_level:
                hit = "risk"
            else:
                hit = "alt"
        elif main_dir == -1:
            if lo <= risk_level:
                hit = "risk"
            elif lo <= up_tgt:       # 空头主路径=回落至 mid 附近，触及即兑现
                hit = "main"
            else:                    # 价格未回落(抗跌) → 意外强势，归为次路径
                hit = "alt"
        else:
            if hi >= up_tgt and lo > risk_level:
                hit = "main"
            elif lo <= risk_level:
                hit = "risk"
            else:
                hit = "alt"
        # 近因加权（#23）：age 以交易日计，约 244 日/年；w=0.5**(age/244)
        age = (n - t) / 244.0
        w = 0.5 ** age
        e = by_sc.setdefault(sc, {"n": 0.0, "main": 0.0, "alt": 0.0, "risk": 0.0})
        e["n"] += w
        e[hit] += w
        d = by_dir[main_dir]
        d["n"] += w
        d[hit] += w
        tot["n"] += w
        tot[hit] += w
        t += step
    return {"by_scenario": by_sc, "by_dir": by_dir, "total": tot}


def forward_vol(closes, horizon=60, regime=True):
    """历史滚动"持有 horizon 交易日"**对数收益率**标准差（小数），用于推演置信锥带宽
    与结构存续概率——两者共用对数正态/几何布朗运动假设，σ 口径必须一致。

    关键修正（口径一致性 #）：此前返回「简单收益率」标准差，但置信锥带宽 med·σ·√f 与
    存续概率 Φ(ln(现价/ZD)/σ√2) 均在对数正态假设下推导；把简单收益 σ 直接套进对数正态
    公式会系统性错配：① 指数波动越大偏差越夸张（创业板 horizon≈30 日 simple σ=0.16 vs
    对数 σ=0.11，锥带宽此前虚胖约 50%）；② 结构存续概率被系统性低估约 7~8 个百分点
    （如创业板 69%→78%、中证500 83%→91%）。现统一改用对数收益 σ，使锥模型与存续概率
    自洽。regime=True 时按近 20 日对数波动率相对长期水平条件化：震荡市收窄、趋势/动荡市放大。"""
    rets = []
    for i in range(len(closes) - horizon):
        rets.append(math.log(closes[i + horizon] / closes[i]))
    if len(rets) < 30:
        return 0.095
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    sigma = var ** 0.5
    if regime:
        sigma *= regime_factor(closes)
    return sigma


def adaptive_horizon(bis, merged=None):
    """按最近若干完成笔的平均持续交易日数自适应推演 horizon（#3），替代固定 60 日。

    修正：笔的 start/end 是「合并后 K 线」的索引，并非原始交易日索引。一笔可能跨越
    多根被包含处理吸收的 K 线，直接用合并索引差会系统性低估真实时长。传入 merged 后，
    用 merged[idx_start]/merged[idx_end] 还原到原始日线索引，得到真实的交易日的持续长度。
    """
    if not bis or len(bis) < 4:
        return 60
    if merged is not None:
        durs = []
        for b in bis[-8:]:
            a = merged[b["start"]]["idx_start"]
            e = merged[b["end"]]["idx_end"]
            if e < a:
                a, e = e, a
            durs.append(e - a + 1)
    else:
        durs = [abs(b["end"] - b["start"]) + 1 for b in bis[-8:]]
    avg = sum(durs) / len(durs)
    return max(30, min(90, round(avg * 1.6)))


def regime_factor(closes):
    """近期对数波动率相对长期水平的比值（截面调节因子，单一来源）。

    震荡市(近期波动 < 长期) → 因子 <1，置信带宽收窄；趋势/动荡市(近期 > 长期) → 因子 >1，放宽。
    置信锥 σ(forward_vol) 与经验分位带离散度(_sd) 必须共用同一因子，否则两张不确定性图
    口径打架（创业板此前锥比经验带宽 27%、上证窄 11%）。夹逼 [0.6, 1.8] 防极端。"""
    import statistics
    daily = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]
    if len(daily) < 40:
        return 1.0
    recent_sd = statistics.pstdev(daily[-20:])
    long_sd = statistics.pstdev(daily[-min(len(daily), 250):])
    if long_sd <= 0:
        return 1.0
    return max(0.6, min(1.8, recent_sd / long_sd))


def realized_vol_annualized(closes, periods=244):
    """近 1 年(约 244 交易日)日对数收益率的标准差，年化（×√244），返回小数。
    作为专业度指标：量化指数近期波动剧烈程度，与缠论「结构健康/级别」互为补充。"""
    if len(closes) < 30:
        return None
    rets = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]
    rets = rets[-periods:]  # 取近 1 年
    if len(rets) < 20:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / len(rets)
    return var ** 0.5 * math.sqrt(periods)


# ---------- 8g-2. 均线多空排列（与缠论结构交叉验证，#4） ----------
def ma_alignment(closes):
    """返回 {ma20, ma60, ma250, alignment}；alignment: 多头排列/空头排列/纠缠。"""
    n = len(closes)
    if n < 260:
        return None
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    ma250 = sum(closes[-250:]) / 250
    if ma20 > ma60 > ma250:
        alignment = "多头排列"
    elif ma20 < ma60 < ma250:
        alignment = "空头排列"
    else:
        alignment = "纠缠"
    return {"ma20": round(ma20, 2), "ma60": round(ma60, 2),
            "ma250": round(ma250, 2), "alignment": alignment}


# ---------- 8g-3. 乖离率（BIAS）/ 超买超卖（#22 新增） ----------
def bias_indicator(closes):
    """乖离率：现价偏离均线的程度，量化「短线超买/超卖」——缠论结构之外的独立均值回归维度。
    返回 {bias20, bias60, state, level}。
      bias20 = (close-MA20)/MA20（短期超买超卖更敏感）；bias60 = (close-MA60)/MA60（中期趋势偏离）。
      state: 超买 / 超卖 / 中性；level: 极端(>±10%) / 明显(>±6%) / 轻度 / —。
    乖离过大提示「涨多了要回落 / 跌多了要反弹」的均值回归压力，与本级别缠论转折信号互为印证。"""
    n = len(closes)
    if n < 20:
        return None
    ma20 = sum(closes[-20:]) / 20
    ma60 = (sum(closes[-60:]) / 60) if n >= 60 else ma20
    close = closes[-1]
    bias20 = (close - ma20) / ma20
    bias60 = (close - ma60) / ma60 if n >= 60 else 0.0
    ab = abs(bias20)
    if bias20 > 0.10:
        state, level = "超买", "极端"
    elif bias20 > 0.06:
        state, level = "超买", "明显"
    elif bias20 < -0.10:
        state, level = "超卖", "极端"
    elif bias20 < -0.06:
        state, level = "超卖", "明显"
    elif ab > 0.03:
        state, level = "中性", "轻度"
    else:
        state, level = "中性", "—"
    return {"bias20": round(bias20 * 100, 2), "bias60": round(bias60 * 100, 2),
            "state": state, "level": level,
            "ma20": round(ma20, 2), "ma60": round(ma60, 2)}


# ---------- 8g-4. ADX 趋势强度（Wilder，#专业度新增） ----------
def _adx_trend(adxv, pdi, mdi):
    """ADX 趋势强度定性：≥25 强趋势（方向由 ±DI 决定）；20~25 中等；<20 弱势震荡。"""
    if adxv >= 25:
        return "强趋势·" + ("多头" if pdi > mdi else "空头")
    if adxv >= 20:
        return "中等趋势"
    return "弱势震荡"


def adx(highs, lows, closes, period=14):
    """Wilder's ADX/DI：量化趋势强度（与方向无关），区分「趋势市/震荡市」的标准专业指标。
    返回 {adx, pdi, mdi, trend}；样本不足返回 None。ADX 与缠论「笔/中枢方向」互补——
    缠论给方向，ADX 给该方向是否具备趋势动能；低 ADX（震荡）下方向性信号可靠性下降、
    推演置信度应相应下调（见 forecast_confidence）。"""
    n = len(closes)
    if n < 2 * period + 1:
        return None
    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        if up > dn and up > 0:
            pdm[i] = up
        if dn > up and dn > 0:
            mdm[i] = dn
    atr = sum(tr[1:period + 1]) / period
    spdm = sum(pdm[1:period + 1]) / period
    smdm = sum(mdm[1:period + 1]) / period
    pdi = [0.0] * n
    mdi = [0.0] * n
    dx = [0.0] * n
    for i in range(period, n):
        atr = (atr * (period - 1) + tr[i]) / period
        spdm = (spdm * (period - 1) + pdm[i]) / period
        smdm = (smdm * (period - 1) + mdm[i]) / period
        p = 100 * spdm / atr if atr else 0.0
        m = 100 * smdm / atr if atr else 0.0
        pdi[i] = p
        mdi[i] = m
        dx[i] = 100 * abs(p - m) / (p + m) if (p + m) else 0.0
    a = 2 * period - 1
    if a >= n:
        return None
    adxv = sum(dx[period:a + 1]) / period
    for i in range(a + 1, n):
        adxv = (adxv * (period - 1) + dx[i]) / period
    return {"adx": round(adxv, 1), "pdi": round(pdi[n - 1], 1), "mdi": round(mdi[n - 1], 1),
            "trend": _adx_trend(adxv, pdi[n - 1], mdi[n - 1])}


# ---------- 8g-5. 最大回撤（专业风险度量，#专业度新增） ----------
def max_drawdown(closes, dates=None):
    """区间内最大回撤（峰值到谷值最大跌幅，小数→返回百分比）。返回 {mdd, peak_date, trough_date}。
    作为专业风险度量，与年化波动率互相补充：波动率看「抖不抖」，回撤看「最坏亏多少」。"""
    if len(closes) < 2:
        return None
    peak = closes[0]
    peak_i = 0
    mdd = 0.0
    ti = 0
    for i, c in enumerate(closes):
        if c > peak:
            peak = c
            peak_i = i
        dd = (c - peak) / peak
        if dd < mdd:
            mdd = dd
            ti = i
    return {"mdd": round(mdd * 100, 1),
            "peak_date": dates[peak_i] if dates else "",
            "trough_date": dates[ti] if dates else ""}


# ---------- 8g-6. 量能趋势（放量/缩量，#专业度新增） ----------
def vol_trend(volumes):
    """近 20 日成交量均量 / 近 60 日成交量均量，量化量能趋势。
    放量(>1.15)常伴随突破有效性提升；缩量(<0.85)提示动能衰减、方向待确认。
    返回 {ratio, state}。"""
    n = len(volumes)
    if n < 60:
        return None
    recent = sum(volumes[-20:]) / 20.0
    base = sum(volumes[-60:]) / 60.0
    ratio = recent / base if base else 1.0
    state = "放量" if ratio > 1.15 else ("缩量" if ratio < 0.85 else "温和")
    return {"ratio": round(ratio, 2), "state": state}


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
def forecast_confidence(r, wcls, bt, breadth_bias=0):
    c = 40
    cls = r["classify"]
    aligned = cls.get("last_bi_dir") == wcls.get("last_bi_dir")
    c += 20 if aligned else -10
    recent_bc = [b for b in r["beichi"] if b["bi_index"] >= len(r["bis"]) - 3]
    if any(b["type"] == "top" for b in recent_bc):
        c -= 15
    if any(b["type"] == "bottom" for b in recent_bc):
        c += 10
    # 均线多空排列交叉验证：与缠论结构结论冲突时降权（#4）
    ma = cls.get("ma_alignment")
    if ma and ma["alignment"] != "纠缠":
        bull = cls["scenario"] in ("多头延续", "中枢震荡偏多", "高位整理未破前高", "背驰见底机会")
        if (ma["alignment"] == "空头排列" and bull) or (ma["alignment"] == "多头排列" and not bull):
            c -= 10
    wr = None
    for kind in ("一类买", "一类卖"):
        if kind in bt and 20 in bt[kind]:
            st = bt[kind][20]
            if st["n"] >= 3:
                wr = st["win_rate"]
                break
    if wr is not None:
        c += (wr - 0.5) * 40
    # ADX 趋势强度交叉验证（#专业度）：低 ADX(弱势震荡)→方向性信号可靠性下降，推演置信度下调；
    # 高 ADX(强趋势)→结构动能延续，方向信号更可信、置信度上调。与缠论方向判断互补。
    _adx = r.get("adx")
    if _adx and _adx.get("adx") is not None:
        if _adx["adx"] < 20:
            c -= 6
        elif _adx["adx"] >= 30:
            c += 4
    # 量能确认（缠论核心确认条件）：最近背驰若伴随量能萎缩（量价背离），
    # 方向性信号更可信——缩量背驰比放量背驰可靠性更高（放量背驰常是出货而非转折）。
    _bc_top = any(b["type"] == "top" for b in recent_bc)
    _bc_bot = any(b["type"] == "bottom" for b in recent_bc)
    _bc_vol_confirmed = any(b.get("vol_confirm") for b in recent_bc)
    if recent_bc:
        if _bc_bot and _bc_vol_confirmed:
            c += 5   # 底背驰+缩量：量价背离确认底部夯实，见底可信度提升
        if _bc_top and _bc_vol_confirmed:
            c -= 5   # 顶背驰+缩量：量价背离确认顶部，见顶风险提升
    c += breadth_bias  # 跨指数市场宽度：全市场同向时调升/调降推演置信度（系统性环境对齐度）
    return max(0, min(100, int(c)))


def analyze(klines, min_bi_pct=MIN_BI_PCT, with_stability=True):
    merged = merge_inclusion(klines)
    bis = build_bi(merged, min_bi_pct)
    zss = build_zhongshu(bis)
    closes = [k["close"] for k in klines]
    dif, dea, hist = macd(closes)
    # 仅用「已完成笔」(排除最后一支进行中的当前笔) 做背驰与买卖点识别：
    # 未完成笔的末端价是实时临时的，参与识别会产生「未来函数」式伪信号
    # （实测每指数均出现 1 个买卖点落在未完成笔上）。缠论严格只允许已完成笔参与信号确认；
    # 回测虽用 exclude_last 兜底，但实时渲染的买卖点三角会误导。段级背驰因已有近2年+间距
    # 限制且非买卖点信号，保持全笔不参与此排除。
    bis_done = bis[:-1] if len(bis) > 1 else bis
    beichis = find_beichi(bis_done, hist, merged)
    beichis = classify_beichi_type(beichis, bis_done, zss)
    # 量能背离确认：背驰段本身应是"价创新高/低、量能却萎缩"的背离结构。
    # 取该笔覆盖 K 线的成交量之和，与前一同向笔比较：当前段量能 < 前段 → 量价背离确认。
    vols = [k["volume"] for k in klines]

    def _bi_vol(bi):
        s = merged[bi["start"]]["idx_start"]
        e = merged[bi["end"]]["idx_end"]
        if e < s:
            s, e = e, s
        return sum(vols[s:e + 1])

    # 量能背离确认：以"同方向笔成交量的中位数"为基准（比单纯取上一支同向笔更稳健，
    # 避免隔了很久的巨量笔造成误判）。当前段量能 < 中位数*0.92 才标记"量✓"。
    _vol_by_dir = {}
    for _b in bis:
        _vol_by_dir.setdefault(_b["dir"], []).append(_bi_vol(_b))
    _vol_med = {}
    for _d, _vs in _vol_by_dir.items():
        _vs_sorted = sorted(_vs)
        _vol_med[_d] = _vs_sorted[len(_vs_sorted) // 2] if _vs_sorted else 0
    for bc in beichis:
        i = bc["bi_index"]
        d = bis[i]["dir"]
        med = _vol_med.get(d, 0)
        _v = _bi_vol(bis[i])
        bc["vol_confirm"] = bool(med > 0 and _v < med * 0.92)
        # 量能量化（#预测优化·F）：背驰段量能较前段中位数萎缩百分比的倒数——量缩(比值<1)背驰更可信、
        # 量增(比值>1)提示背离可能不成立（或为中继）。反馈进概率合成的背驰强度修正。
        bc["vol_ratio"] = round(_v / med, 3) if med > 0 else None
    signals = find_signals(bis_done, zss, beichis, klines, merged)
    segments = build_segments(bis, zss)
    seg_beichi = find_beichi_segment(segments, hist, merged)
    ma = ma_alignment(closes) if len(closes) >= 260 else None
    cls = classify(bis, zss, beichis, closes[-1], None, segments, seg_beichi)
    cls["ma_alignment"] = ma
    gaps = find_gaps(klines)
    bis_strict = build_bi_strict(merged)
    ok, tot, agree = bi_agreement(bis, bis_strict)
    captured = known_pivot_capture({"merged": merged, "bis": bis})
    capture_rate = len(captured) / len(KNOWN_PIVOTS) if KNOWN_PIVOTS else 0
    stability = classification_stability(klines, min_bi_pct) if with_stability else None
    bias = bias_indicator(closes)
    adx_r = adx([k["high"] for k in klines], [k["low"] for k in klines], closes)
    mdd = max_drawdown(closes, [k["date"] for k in klines])
    vt = vol_trend([k["volume"] for k in klines])
    return {
        "merged": merged, "bis": bis, "zhongshu": zss,
        "dif": dif, "dea": dea, "hist": hist,
        "beichi": beichis, "signals": signals, "classify": cls,
        "segments": segments, "seg_beichi": seg_beichi,
        "agreement": {"ok": ok, "total": tot, "rate": agree},
        "captured": captured, "capture_rate": capture_rate,
        "stability": stability, "gaps": gaps, "bias": bias,
        "adx": adx_r, "mdd": mdd, "vol_trend": vt,
    }


# ---------- 8. 信号回测 ----------
def backtest_signals(klines, result, horizons=(5, 10, 20, 60), exclude_last=False):
    """对每个买卖点信号，统计其后 h 个交易日的收益与方向胜率。
    买点：ret>0 为胜；卖点：ret<0 为胜。返回 {kind: {h: {n, win_rate, avg_ret}}}
    exclude_last=True 时，丢弃每个信号类型的最近一次出现——避免用「当前结构自身所对应的那支信号」
    去校准「当前结构之后」的胜率（样本内泄漏，会系统性偏乐观：自身胜率高→据此预测后续也高）。"""
    merged, bis = result["merged"], result["bis"]
    n = len(klines)
    # 按类型收集原始样本；exclude_last 时去掉每类最近一次（signals 已按 bi_index 升序）
    by_kind = {}
    for s in result["signals"]:
        b = bis[s["bi_index"]]
        idx = merged[b["end"]]["idx_end"]
        entry = klines[idx]["close"]
        kind = s["kind"][:3]  # 一类买/一类卖/三类买/三类卖
        by_kind.setdefault(kind, []).append((idx, entry, s["dir"]))
    if exclude_last:
        for k in by_kind:
            if by_kind[k]:
                by_kind[k].pop()
    agg = {}
    for kind, samples in by_kind.items():
        for idx, entry, sdir in samples:
            for h in horizons:
                if idx + h >= n:
                    continue
                ret = klines[idx + h]["close"] / entry - 1
                win = (ret > 0) if sdir == 1 else (ret < 0)
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


# ---------- 8b. 样本外稳健性检验 ----------
def backtest_robustness(klines, result, splits=("2022-01-01", "2023-01-01", "2024-01-01"),
                        horizons=(10, 20, 60), exclude_last=True):
    """样本外稳健性检验（滚动 walk-forward）：用多个切分点分别把买卖点信号分为「早年」与
    「近两年」两段，各自统计买方信号（一类买/三类买）的胜负率与平均收益，再跨切分点聚合取均值，
    降低单一切分偶然性导致的方差。若近两年平均胜率显著低于早年，提示校准可能过拟合历史样本；
    若持平或更高，提示样本外稳定。
    返回 {"early": {...}, "recent": {...}, "split": splits, "walk_forward": {splits, early_rate, recent_rate, decay}}。
    early/recent 结构与原单切分兼容（供 robustness_table 直接渲染）。"""
    merged, bis = result["merged"], result["bis"]
    n = len(klines)
    by_kind = {}
    for s in result["signals"]:
        b = bis[s["bi_index"]]
        idx = merged[b["end"]]["idx_end"]
        date = klines[idx]["date"]
        kind = s["kind"][:3]
        by_kind.setdefault(kind, []).append((idx, s["dir"], date))
    if exclude_last:
        for k in by_kind:
            if by_kind[k]:
                by_kind[k].pop()

    def calc(samples):
        agg = {}
        for kind, idx, sdir, _d in samples:
            for h in horizons:
                if idx + h >= n:
                    continue
                ret = klines[idx + h]["close"] / klines[idx]["close"] - 1
                win = (ret > 0) if sdir == 1 else (ret < 0)
                st = agg.setdefault(kind, {}).setdefault(h, {"n": 0, "win": 0, "sum": 0.0})
                st["n"] += 1
                st["win"] += 1 if win else 0
                st["sum"] += ret
        # 返回累加结构 {n, win, sum}，由 _finalize 在跨切分聚合后统一折算 win_rate/avg_ret，
        # 使多切分聚合时按样本量正确加权（而非对单 split 的胜率做简单平均）。
        return agg

    def _merge(dst, src):
        for k, hs in src.items():
            for h, st in hs.items():
                d = dst.setdefault(k, {}).setdefault(h, {"n": 0, "win": 0, "sum": 0.0})
                d["n"] += st["n"]; d["win"] += st["win"]; d["sum"] += st["sum"]

    early_acc, recent_acc = {}, {}
    for split in splits:
        samples_all = [(k, i, d, dt) for k in by_kind for (i, d, dt) in by_kind[k]]
        _merge(early_acc, calc([s for s in samples_all if s[3] < split]))
        _merge(recent_acc, calc([s for s in samples_all if s[3] >= split]))

    def _finalize(acc):
        out = {}
        for k, hs in acc.items():
            out[k] = {h: {"n": st["n"],
                          "win_rate": st["win"] / st["n"] if st["n"] else 0,
                          "avg_ret": st["sum"] / st["n"] if st["n"] else 0}
                      for h, st in hs.items()}
        return out
    early = _finalize(early_acc)
    recent = _finalize(recent_acc)

    def _rate(d):
        rs = [st["win_rate"] for k, hs in d.items() for h, st in hs.items() if st["n"] > 0]
        return sum(rs) / len(rs) if rs else 0.0
    _er, _rr = _rate(early), _rate(recent)
    wf = {"splits": list(splits), "early_rate": round(_er, 3),
          "recent_rate": round(_rr, 3), "decay": round(_rr - _er, 3)}
    return {"early": early, "recent": recent, "split": splits, "walk_forward": wf}


if __name__ == "__main__":
    _base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_base, "data.json"), encoding="utf-8") as f:
        data = json.load(f)
    for sym, d in data.items():
        r = analyze(d["klines"])
        print("%s %s: 合并K线%d 笔%d 中枢%d 背驰%d | %s" % (
            sym, d["name"], len(r["merged"]), len(r["bis"]),
            len(r["zhongshu"]), len(r["beichi"]), r["classify"]["scenario"]))
