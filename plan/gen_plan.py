# -*- coding: utf-8 -*-
"""plan/gen_plan.py — 每交易日盘后「关键价位预案」生成器

产出 plan/plan.json：为上证指数生成「明天到哪些价位 ⇒ 会进入什么状态」的条件表，
以及「三类买卖点信号」的当前状态与前向触发条件。

★ 数据源单一：全部取自 chanlun.analyze() 的返回（与 report.py **同一函数、同一参数**），
  本侧不重算任何结构量（规则 111：同一事实多出口必须同口径）。
★ 已实跑对账：本脚本产出的 ZG/ZD 与 report.html 渲染的 "ZG 3884 / ZD 3768" 一致。

纪律（旭总已拍板口径 + 项目铁律）：
  · 不预测方向 —— 只给「到 X 会进入什么状态」，不给涨跌概率
    （R533/R534/R529 已证：日频方向无可用信息）。
  · 每个价位标注结构来源 —— 中枢随新笔漂移，本表是**当日快照**。
  · 底侧证伪位**不得**当止损 —— R489/R513：端点破位后胜率 53%、p=0.598。
  · 本表非经实证优化的买卖点 —— R528/R533：同类信号无可检测超额；
    动作幅度一律由用户自定，本脚本不给仓位比例。
  · 买卖点信号**不重算判据** —— 三类买卖点全部由 chanlun.find_signals 产出，
    本侧只做「取最近 N 条 + 标失效位 + 写前向触发条件」（规则 156：复用同一函数）。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
from chanlun import analyze, bc_invalidation   # noqa: E402

SYM = "sh000001"
NEAR_PCT = 0.30      # 「接近」阈值(%)：现价距该位 < 此值 ⇒ 预警态
DEDUP_PCT = 0.15     # 合并阈值(%)：两位距离 < 此值 ⇒ 视为同一个位（合并语义，不丢弃）
MAX_PER_SIDE = 3     # 上方 / 下方各最多列几个位（证伪位不占额、强制保留）
SIG_TOP_N = {"笔": 6, "段": 4}   # 买卖点信号：各级别各取最近 N 条（全列会淹掉重点）
SIG_TTL = "卖点：收盘上破失效位 ⇒ 失效；买点：收盘跌破失效位 ⇒ 失效。"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "plan.json")


def _pct(a, b):
    """a 相对 b 的偏离百分比。"""
    return (a / b - 1.0) * 100.0


def _semantic(kind):
    """结构位的客观语义（状态描述，非买卖指令）。"""
    return {
        "zg": "收盘站上 ⇒ 回到中枢上半区（中枢内偏强）",
        "zd": "收盘跌破 ⇒ 中枢下沿失守，进入中枢下方（结构转弱）",
        "gg": "收盘站上 ⇒ 向上突破该中枢成立，创中枢新高",
        "dd": "收盘跌破 ⇒ 跌破中枢最低，形成新低结构",
        "bi_dn": "收盘跌破 ⇒ 当前向下笔低点失守，笔将向下延伸",
        "bi_up": "收盘站上 ⇒ 向上笔高点被突破，笔将向上延伸",
        "bi_prev_dn": "收盘跌破 ⇒ 前一笔低点失守（前低被破）",
        "bi_prev_up": "收盘站上 ⇒ 前一笔高点被收复（前高被破）",
        "inv_top": "★ 收盘上破 ⇒ 顶背驰判据被证伪（见顶判断作废，需重判）",
        "inv_bot": "★ 收盘跌破 ⇒ 底背驰判据被证伪（⚠ 仅作判据参考，不可当止损 —— R489 已证）",
        "gap_top": "收盘站上 ⇒ 向下缺口开始被回补",
        "gap_bot": "回落至缺口中，缺口被逐渐回补",
    }.get(kind, "")


def collect(r):
    """从 analyze() 返回收集候选结构位 => [(price, label, src, kind)]。"""
    out = []
    zss = r.get("zhongshu") or []
    if zss:
        zs = zss[-1]
        tag = "末中枢 %s~%s（%s 笔）" % (zs.get("date_start", "?"), zs.get("date_end", "?"),
                                       zs.get("count", "?"))
        out.append((zs["zg"], "中枢上沿 ZG", tag, "zg"))
        out.append((zs["zd"], "中枢下沿 ZD", tag, "zd"))
        out.append((zs["gg"], "中枢最高 GG", tag, "gg"))
        out.append((zs["dd"], "中枢最低 DD", tag, "dd"))

    bis = r.get("bis") or []
    for off, nm in ((1, "当前笔"), (2, "前一笔")):
        if len(bis) >= off:
            b = bis[-off]
            dn = b.get("dir", 0) < 0
            out.append((b["end_price"], nm + "终点",
                        "%s %s~%s" % ("向下笔" if dn else "向上笔",
                                      b.get("date_start", "?"), b.get("date_end", "?")),
                        ("bi_dn" if dn else "bi_up") if off == 1
                        else ("bi_prev_dn" if dn else "bi_prev_up")))

    inv = bc_invalidation(bis, r.get("beichi") or [])
    for key, label in (("top", "顶背驰证伪位"), ("bot", "底背驰证伪位")):
        e = inv.get(key)
        if e:
            out.append((e["price"], label,
                        "背驰端点 %s（%s · MACD 面积比 %.3f）" % (
                            e.get("date", "?"), e.get("bc_type") or "?", e.get("area_ratio") or 0.0),
                        "inv_" + key))

    unfilled = [g for g in (r.get("gaps") or []) if not g.get("filled")]
    if unfilled:
        g = unfilled[-1]
        gtag = "未补缺口 %s（%s）" % (g.get("date", "?"),
                                    "向下跳空" if g.get("type") == "down" else "向上跳空")
        out.append((g["top"], "缺口上沿", gtag, "gap_top"))
        out.append((g["bottom"], "缺口下沿", gtag, "gap_bot"))
    return out


def _dedup(levels):
    """重合位**合并**（标签 / 来源 / 语义并列），不丢弃任何一条语义。

    实测必要性：2026-10-09 末中枢 GG = 3995.18 与顶背驰证伪位同价 ——
    「先到先得」式去重会静默丢掉「判据证伪」这条最强语义。
    """
    uniq = []
    for price, label, src, kind in levels:
        for u in uniq:
            if abs(price / u["price"] - 1.0) * 100.0 < DEDUP_PCT:
                if label not in u["labels"]:
                    u["labels"].append(label)
                    u["srcs"].append(src)
                    u["kinds"].append(kind)
                break
        else:
            uniq.append({"price": price, "labels": [label], "srcs": [src], "kinds": [kind]})
    return uniq


def pick(levels, close):
    """分上 / 下各取前 N；证伪位强制入表（不占额）。两侧均按价格降序。"""
    uniq = _dedup(levels)

    def take(pool):
        pool = sorted(pool, key=lambda u: abs(u["price"] - close))   # 先近后远
        out = [u for u in pool if any(k.startswith("inv_") for k in u["kinds"])]
        seen = {id(u) for u in out}
        for u in pool:
            if len(out) >= MAX_PER_SIDE:
                break
            if id(u) not in seen:
                out.append(u)
                seen.add(id(u))
        return sorted(out, key=lambda u: -u["price"])                # 价格降序

    return take([u for u in uniq if u["price"] > close]), take([u for u in uniq if u["price"] < close])


def state_of(price, close, prev_close):
    """up_cross / dn_cross（当日收盘穿越）· near（接近，预警态）· far。"""
    if prev_close <= price < close:
        return "up_cross"
    if prev_close >= price > close:
        return "dn_cross"
    if abs(price / close - 1.0) * 100.0 < NEAR_PCT:
        return "near"
    return "far"


# ---------- 买卖点信号（前向化）----------
# 引擎 chanlun.find_signals 已产出三类买卖点（笔级 + 段级），本侧**不重算任何判据**，
# 只做两件事：① 取最近 N 条并给出「失效位」；② 给出「下一信号需要先看到什么」。
# ★ 单点化程度天然不同，必须在页面上如实呈现，不得笼统写成「突破 X 即形成 N 类买点」：
#     一类 = 背驰（价格创新高/新低 **且** MACD 面积收缩）⇒ 只能给候选位 + 确认条件；
#     二类 = 一类之后的次低/次高折返            ⇒ 必须等一类先出现；
#     三类 = 中枢离开后回抽/反抽不进中枢区间      ⇒ 必须等**新中枢**形成（且一个中枢只出一个三类点）。
def _sig_family(kind):
    """信号族：一 / 二 / 三。笔级（一/二/三类…）与段级（段一/段二/段三/段类…）共用。"""
    for fam, keys in (("一", ("一类", "段一")), ("三", ("三类", "段三")),
                      ("二", ("二类", "类二", "段二", "段类"))):
        if any(k in kind for k in keys):
            return fam
    return "?"


def _collect_level(sigs, close, top_n):
    """按 bi_index 升序回扫，为二类点找**同方向最近的一类点**作锚；返回最近 top_n 条。

    失效位口径（页面同步披露，不留歧义）：
      · 一类 / 三类 —— 信号**自身价**（被反向突破即证伪）；
      · 二类 —— 其**锚定的一类点价**（「不破前极值」的前提被否定 ⇒ 一族同时失效）。
    """
    last_p1 = {1: None, -1: None}
    rows = []
    for s in sorted(sigs, key=lambda x: x["bi_index"]):
        fam = _sig_family(s["kind"])
        if fam == "一":
            last_p1[s["dir"]] = s["price"]
        anchor = last_p1.get(s["dir"]) if fam == "二" else None
        inval = anchor if anchor is not None else s["price"]
        if fam == "二":
            inval_src = "锚定一类点" if anchor is not None else "信号自身价"
        else:
            inval_src = "信号自身价"
        off = (close < inval) if s["dir"] > 0 else (close > inval)
        rows.append({
            "kind": s["kind"], "dir": s["dir"], "date": s["date"],
            "price": round(s["price"], 2), "dist_pct": round(_pct(s["price"], close), 2),
            "stop": (round(s["stop"], 2) if s.get("stop") is not None else None),
            "target": (round(s["target"], 2) if s.get("target") is not None else None),
            "rr": (round(s["rr"], 2) if s.get("rr") is not None else None),
            "inval": round(inval, 2), "inval_src": inval_src,
            "status": ("判据已破" if off else "判据成立"),
        })
    return rows[-top_n:]


def _next_rows(r, inv):
    """下一信号的触发条件 —— 价格位从数据取（规则 149），条件文案按引擎实际判据写。

    `inv` 由调用方传入（与上方「判据失效条件」**同一次调用**的结果）
    ⇒ 两处不可能出现口径漂移（规则 111）。
    """
    zss = r.get("zhongshu") or []
    zs = zss[-1] if zss else None
    hi = (inv.get("top") or {}).get("price")     # 一类卖候选位：现有顶背驰高点（= 创新高参考）
    lo = zs["dd"] if zs else None                # 一类买候选位：中枢最低（= 创新低参考）
    return [
        {"kind": "一类买", "price": lo, "price_label": "中枢最低 DD",
         "cond": "跌破中枢最低（创新低）且 MACD 面积比收缩 ⇒ 构成底背驰"},
        {"kind": "二类买", "price": None, "price_label": "需一类买先出现",
         "cond": "先出现一类买点，其后首个向下折返不跌破该低点"},
        {"kind": "三类买", "price": None, "price_label": "需新中枢形成",
         "cond": "新中枢形成后向上离开，回抽低点不跌回中枢下沿"},
        {"kind": "一类卖", "price": hi, "price_label": "顶背驰高点",
         "cond": "上破现有顶背驰高点（创新高）且 MACD 面积比收缩 ⇒ 构成新顶背驰"},
        {"kind": "二类卖", "price": None, "price_label": "需一类卖先出现",
         "cond": "先出现一类卖点，其后首个向上反抽不破该高点"},
        {"kind": "三类卖", "price": None, "price_label": "需新中枢形成",
         "cond": "新中枢形成后向下离开，反抽高点不重回中枢上沿"},
    ]


def _label_of(u):
    return " / ".join(u["labels"])


def _sem_of(u):
    return " ｜ ".join([s for s in (_semantic(k) for k in u["kinds"]) if s])


def main():
    with open(os.path.join(BASE, "data.json"), encoding="utf-8") as f:
        D = json.load(f)
    d = D[SYM]
    k = d["klines"]
    r = analyze(k)
    close, prev_close = k[-1]["close"], k[-2]["close"]
    zss = r.get("zhongshu") or []
    zs = zss[-1] if zss else None

    # 自校验：position 语义必须与 close 的实际位置自洽（不硬编码任何数字）
    pos = (r.get("classify") or {}).get("position") or ""
    if zs and pos == "中枢内部":
        assert zs["zd"] < close < zs["zg"], \
            "position=中枢内部 但 close 不在 [ZD, ZG] 内 ⇒ 与 analyze() 口径不一致，本轮读数作废"

    up, dn = pick(collect(r), close)
    assert up and dn, "上方 / 下方价位至少各需 1 个（收集中断或合并过严）"

    def pack(u):
        return {"price": round(u["price"], 2), "label": _label_of(u),
                "src": " ｜ ".join(u["srcs"]),
                "dist_pct": round(_pct(u["price"], close), 2),
                "state": state_of(u["price"], close, prev_close),
                "semantic": _sem_of(u)}

    acts = []
    for u in list(up) + list(dn):
        st = state_of(u["price"], close, prev_close)
        if st in ("up_cross", "dn_cross"):
            acts.append({"price": round(u["price"], 2), "label": _label_of(u),
                         "dir": "上破" if st == "up_cross" else "跌破", "semantic": _sem_of(u)})

    inv = bc_invalidation(r.get("bis") or [], r.get("beichi") or [])
    invalid = []
    if inv.get("top"):
        invalid.append({"kind": "top", "price": round(inv["top"]["price"], 2),
                        "text": "收盘上破 ⇒ 顶背驰判据作废，见顶判断需重判"})
    if inv.get("bot"):
        invalid.append({"kind": "bot", "price": round(inv["bot"]["price"], 2),
                        "text": "收盘跌破 ⇒ 底背驰判据作废（⚠ 不可当止损，仅作判据参考）"})

    # 买卖点信号：取最近 N 条（笔级 + 段级并列，不混计 —— 引擎本身即分表）+ 前向触发条件
    # ★ 状态列只答「判据还成立吗」，**不等于**「该执行」—— 措辞必须区分，否则会被读成买卖指令。
    _lv = []
    for _name, _key in (("笔", "signals"), ("段", "seg_signals")):
        _rows = _collect_level(r.get(_key) or [], close, SIG_TOP_N[_name])
        _lv.append({"level": _name, "rows": _rows,
                    "counts": {"买": sum(1 for x in _rows if x["dir"] > 0),
                               "卖": sum(1 for x in _rows if x["dir"] < 0)}})
    sigs = {
        "levels": _lv,
        "next": _next_rows(r, inv),
        "rule": SIG_TTL,
        "total": {"笔": len(r.get("signals") or []), "段": len(r.get("seg_signals") or [])},
    }

    plan = {
        "asof": k[-1]["date"],
        "sym": SYM,
        "name": d.get("name"),
        "close": round(close, 2),
        "chg_pct": round(_pct(close, prev_close), 2),
        "scenario": (r.get("classify") or {}).get("scenario"),
        "position": pos,
        "zhongshu": ({"zg": round(zs["zg"], 2), "zd": round(zs["zd"], 2),
                      "gg": round(zs["gg"], 2), "dd": round(zs["dd"], 2),
                      "date_start": zs.get("date_start"), "date_end": zs.get("date_end"),
                      "count": zs.get("count")} if zs else None),
        "up": [pack(u) for u in up],
        "dn": [pack(u) for u in dn],
        "actions": acts,
        "invalidations": invalid,
        "signals": sigs,
        "near_pct": NEAR_PCT,
        "notes": [
            "本表是当日快照：中枢随新笔生成会漂移，请以 asof 为准。",
            "所有位取自 chanlun.analyze() 同一函数，与主报告 ZG/ZD 同口径。",
            "买卖点信号取自 chanlun.find_signals()，与主报告图上的三角标记同源同判据，本侧不重算。",
            "三类买/卖点需等**新中枢**形成（且一个中枢只出一个三类点）；一类买/卖点需 MACD 面积收缩确认 —— 两者都不是单点位可预判，故「下一信号」一栏如实标出「需先看到什么」。",
            "二类买/卖点必须等对应的一类点先出现，无法凭空产生。",
            "引擎给出的历史胜率是被 beta 污染过的指标（R528/R533 已证同类信号无可检测超额）；本表不构成买卖建议，动作幅度由使用者自定。",
        ],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    _sig_rows = sum(len(l["rows"]) for l in sigs["levels"])
    _sig_live = sum(1 for l in sigs["levels"] for x in l["rows"] if x["status"] == "判据成立")
    print("plan.json OK  asof=%s  close=%s  上%d 下%d  动作%d  失效%d  信号%d(判据成立%d)" % (
        plan["asof"], plan["close"], len(plan["up"]), len(plan["dn"]),
        len(plan["actions"]), len(plan["invalidations"]), _sig_rows, _sig_live))


if __name__ == "__main__":
    main()
