# -*- coding: utf-8 -*-
"""plan/gen_plan.py — 每交易日盘后「关键价位预案」生成器

产出 plan/plan.json：为上证指数生成「明天到哪些价位 ⇒ 会进入什么状态」的条件表。

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
        "near_pct": NEAR_PCT,
        "notes": [
            "本表是当日快照：中枢随新笔生成会漂移，请以 asof 为准。",
            "所有位取自 chanlun.analyze() 同一函数，与主报告 ZG/ZD 同口径。",
            "仅描述结构状态，不含方向预测；动作幅度由使用者自定。",
            "同类信号经回测无可检测超额（R528/R533），故本表不构成买卖建议。",
        ],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    print("plan.json OK  asof=%s  close=%s  上%d 下%d  动作%d  失效%d" % (
        plan["asof"], plan["close"], len(plan["up"]), len(plan["dn"]),
        len(plan["actions"]), len(plan["invalidations"])))


if __name__ == "__main__":
    main()
