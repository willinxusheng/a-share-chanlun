# -*- coding: utf-8 -*-
"""缠论分析流水线：包含处理 → 分型 → 笔 → 中枢 → 背驰 → 买卖点 → 分类推演"""
import json
import os

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


# ---------- 6. 买卖点 ----------
def find_signals(bis, zss, beichis):
    """买卖点体系：
    一类：背驰拐点（底背驰=买，顶背驰=卖）。
    二类：中枢完成后，回踩/反抽停留在中枢内部（未破 ZD/ZG）的折返笔——
          向下笔末端落在 (ZD, ZG] = 二类买；向上笔末端落在 [ZD, ZG) = 二类卖。
          （买/卖统一以笔末端价 end_price 为判定基准，口径一致，避免漏判。）
    三类：中枢完成后，回踩不破 ZG = 三类买；反抽不过 ZD = 三类卖。
    每个信号附带 vol_confirm：该笔量能较前一同向笔萎缩（量价背离确认）。
    """
    signals = []
    last_zs = zss[-1] if zss else None
    bc_map = {b["bi_index"]: b for b in beichis}
    for i, b in enumerate(bis):
        vc = bc_map[i].get("vol_confirm", False) if i in bc_map else False
        if i in bc_map:
            if bc_map[i]["type"] == "bottom":
                signals.append({"bi_index": i, "kind": "一类买点(底背驰)", "dir": 1,
                                "date": b["date_end"], "price": b["end_price"],
                                "vol_confirm": vc})
            else:
                signals.append({"bi_index": i, "kind": "一类卖点(顶背驰)", "dir": -1,
                                "date": b["date_end"], "price": b["end_price"],
                                "vol_confirm": vc})
    if last_zs:
        zd, zg = last_zs["zd"], last_zs["zg"]
        for i, b in enumerate(bis):
            if b["end"] <= last_zs["end"]:
                continue
            ep = b["end_price"]
            if b["dir"] == -1 and ep > zg:
                signals.append({"bi_index": i, "kind": "三类买点(回踩不破ZG)", "dir": 1,
                                "date": b["date_end"], "price": ep, "vol_confirm": False})
            if b["dir"] == 1 and ep < zd:
                signals.append({"bi_index": i, "kind": "三类卖点(反抽不过ZD)", "dir": -1,
                                "date": b["date_end"], "price": ep, "vol_confirm": False})
        # 二类买卖点：中枢内折返笔（端点价落在中枢内部，未破 ZD/ZG）
        for i, b in enumerate(bis):
            if b["end"] <= last_zs["end"]:
                continue
            ep = b["end_price"]
            if b["dir"] == -1 and zd < ep <= zg:
                signals.append({"bi_index": i, "kind": "二类买点(回踩不破)", "dir": 1,
                                "date": b["date_end"], "price": ep, "vol_confirm": False})
            if b["dir"] == 1 and zd <= ep < zg:
                signals.append({"bi_index": i, "kind": "二类卖点(反抽不过)", "dir": -1,
                                "date": b["date_end"], "price": ep, "vol_confirm": False})
    signals.sort(key=lambda s: s["bi_index"])
    return signals


# ---------- 7. 分类推演 ----------
def classify(bis, zss, beichis, close, wcls=None, segments=None, seg_beichi=None):
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

    if bc_top and last["dir"] == 1:
        scenario = "背驰见顶风险"
        detail = "最近向上笔价格创新高但MACD红柱面积明显萎缩（面积比 %.2f）" % recent_bc[-1]["area_ratio"]
        if seg_top:
            detail += "，且走势段级别同步出现顶背驰"
        detail += ("，构成顶背驰。短线警惕一类卖点确认，回落目标先看最近中枢ZG(%.1f)。"
                   % (last_zs["zg"] if last_zs else close))
    elif bc_bot and last["dir"] == -1:
        scenario = "背驰见底机会"
        detail = "最近向下笔价格创新低但MACD绿柱面积明显萎缩（面积比 %.2f）" % recent_bc[-1]["area_ratio"]
        if seg_bot:
            detail += "，且走势段级别同步出现底背驰"
        detail += ("，构成底背驰。关注一类买点后的反弹，第一压力看最近中枢ZD(%.1f)。"
                   % (last_zs["zd"] if last_zs else close))
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
    elif pos == "无中枢":
        if last["dir"] == 1:
            scenario = "无中枢·向上笔"
            detail = "当前尚无已完成中枢，向上笔运行中，暂按笔级别多头对待；待中枢成型后再定级别与买卖点。"
        else:
            scenario = "无中枢·向下笔"
            detail = "当前尚无已完成中枢，向下笔运行中，暂按笔级别空头对待；待中枢成型后再定级别与买卖点。"
    else:  # dir==1, pos==中枢下方
        scenario = "弱势反弹"
        detail = "向下趋势中的反弹笔，仍在中枢ZD(%.1f)下方。反弹无法回到中枢内部则仍是空头格局，警惕三类卖点。" % (last_zs["zd"] if last_zs else close)

    if nest:
        detail += "　【区间套】" + nest + "。"

    return {"scenario": scenario, "detail": detail, "position": pos,
            "last_bi_dir": last["dir"], "last_bi_pct": (last["end_price"] / last["start_price"] - 1),
            "seg_bc_bottom": seg_bot, "seg_bc_top": seg_top, "interval_nesting": nest}


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
def forward_vol(closes, horizon=60, regime=True):
    """历史滚动"持有 horizon 交易日"收益率标准差（小数），用于推演置信锥宽度。
    regime=True 时按近 20 日波动率相对长期水平条件化：震荡市收窄、趋势/动荡市放大（#3）。"""
    rets = []
    for i in range(len(closes) - horizon):
        rets.append(closes[i + horizon] / closes[i] - 1)
    if len(rets) < 30:
        return 0.10
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    sigma = var ** 0.5
    if regime:
        import statistics
        daily = [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1)]
        if len(daily) >= 40:
            recent_sd = statistics.pstdev(daily[-20:])
            long_sd = statistics.pstdev(daily[-min(len(daily), 250):])
            if long_sd > 0:
                sigma *= max(0.6, min(1.8, recent_sd / long_sd))
    return sigma


def adaptive_horizon(bis):
    """按最近若干完成笔的平均持续交易日数自适应推演 horizon（#3），替代固定 60 日。"""
    if not bis or len(bis) < 4:
        return 60
    durs = [abs(b["end"] - b["start"]) + 1 for b in bis[-8:]]
    avg = sum(durs) / len(durs)
    return max(30, min(90, round(avg * 1.6)))


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
    return max(0, min(100, int(c)))


def analyze(klines, min_bi_pct=MIN_BI_PCT, with_stability=True):
    merged = merge_inclusion(klines)
    bis = build_bi(merged, min_bi_pct)
    zss = build_zhongshu(bis)
    closes = [k["close"] for k in klines]
    dif, dea, hist = macd(closes)
    beichis = find_beichi(bis, hist, merged)
    # 量能背离确认：背驰段本身应是"价创新高/低、量能却萎缩"的背离结构。
    # 取该笔覆盖 K 线的成交量之和，与前一同向笔比较：当前段量能 < 前段 → 量价背离确认。
    vols = [k["volume"] for k in klines]

    def _bi_vol(bi):
        s = merged[bi["start"]]["idx_start"]
        e = merged[bi["end"]]["idx_end"]
        if e < s:
            s, e = e, s
        return sum(vols[s:e + 1])

    for bc in beichis:
        i = bc["bi_index"]
        prev = None
        for j in range(i - 2, -1, -2):
            prev = bis[j]
            break
        bc["vol_confirm"] = bool(prev and _bi_vol(bis[i]) < _bi_vol(prev))
    signals = find_signals(bis, zss, beichis)
    segments = build_segments(bis, zss)
    seg_beichi = find_beichi_segment(segments, hist, merged)
    ma = ma_alignment(closes) if len(closes) >= 260 else None
    cls = classify(bis, zss, beichis, closes[-1], None, segments, seg_beichi)
    cls["ma_alignment"] = ma
    bis_strict = build_bi_strict(merged)
    ok, tot, agree = bi_agreement(bis, bis_strict)
    captured = known_pivot_capture({"merged": merged, "bis": bis})
    capture_rate = len(captured) / len(KNOWN_PIVOTS) if KNOWN_PIVOTS else 0
    stability = classification_stability(klines, min_bi_pct) if with_stability else None
    return {
        "merged": merged, "bis": bis, "zhongshu": zss,
        "dif": dif, "dea": dea, "hist": hist,
        "beichi": beichis, "signals": signals, "classify": cls,
        "segments": segments, "seg_beichi": seg_beichi,
        "agreement": {"ok": ok, "total": tot, "rate": agree},
        "captured": captured, "capture_rate": capture_rate,
        "stability": stability,
    }


# ---------- 8. 信号回测 ----------
def backtest_signals(klines, result, horizons=(5, 10, 20, 60)):
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
    _base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_base, "data.json"), encoding="utf-8") as f:
        data = json.load(f)
    for sym, d in data.items():
        r = analyze(d["klines"])
        print("%s %s: 合并K线%d 笔%d 中枢%d 背驰%d | %s" % (
            sym, d["name"], len(r["merged"]), len(r["bis"]),
            len(r["zhongshu"]), len(r["beichi"]), r["classify"]["scenario"]))
