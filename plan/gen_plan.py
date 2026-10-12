# -*- coding: utf-8 -*-
"""plan/gen_plan.py — 每交易日盘后「关键价位预案」生成器

产出 plan/plan.json：为上证指数生成「明天到哪些价位 ⇒ 会进入什么状态」的条件表，
以及「三类买卖点信号」的当前状态与前向触发条件。

★ 数据源单一：全部取自 chanlun.analyze() 的返回（与 report.py **同一函数、同一参数**），
  本侧不重算任何结构量（规则 111：同一事实多出口必须同口径）。
★ 已实跑对账：本脚本产出的 ZG/ZD 与 report.html 渲染的 "ZG 3884 / ZD 3768" 一致。

R542（深挖 bug 修复，均为**产物静默失真/崩溃**类，全部由故障注入实测触发）：
  · 合并组内**成员自身价位**落盘（`seg_prices`）—— 代表价只取首个候选，实测 MA60 真实
    3,897.68 被展示成 3,892.75（差 4.93 点），与 R541 修的缺口失真同源（规则 169 的推广）。
  · 缺口带「压力/支撑/跨现价」分类与方向文案**在生成侧判一次**（`side`/`dir_label`/`side_label`）
    —— 此前渲染层两套判据，带横跨现价时图上与右侧列表给出相反结论（规则 111）。
  · position 自校验由「单分支 + 严格不等」改为**三态双向 + 闭区间**：原写法在收盘恰等于
    ZG/ZD 时会让生成器断言崩溃（引擎口径本就是闭区间），且反向不一致静默通过。

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
MAX_PER_SIDE = 5     # 上方 / 下方各最多列几个位（证伪位不占额、强制保留）
# R540: 3 → 5。候选池新增「均线 MA20/60/120/250」与「斐波那契回调位」两个维度后，
# 名额 3 会被最近的结构位占满 ⇒ 均线/Fib 永远进不了表（实测：均线 4 档全在现价上方
# +2.1%~+4.7%，距现价远于 3884/3995 这些结构位）。放宽到 5 后，多维度的位都有机会入表；
# 因 _dedup 会合并 <0.15% 的近位，实际每侧多为 4~5 条。
SIG_TOP_N = {"笔": 6, "段": 4}   # 买卖点信号：各级别各取最近 N 条（全列会淹掉重点）
SIG_TTL = "卖点：收盘上破失效位 ⇒ 失效；买点：收盘跌破失效位 ⇒ 失效。"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "plan.json")


def _pct(a, b):
    """a 相对 b 的偏离百分比。"""
    return (a / b - 1.0) * 100.0


def _semantic(kind):
    """结构位的客观语义（状态描述，非买卖指令）。

    R541：Fib 位支持 `fib@38.2%` 形式 —— 此前三段 Fib 的含义文字**一字不差**，
    读者分不清哪档是哪档（实测 38.2% / 50% / 61.8% 三档 semantic 完全相同 ⇒ 等于没说）。
    带上百分比后至少能对上号。★ 至于「38.2% 是浅回调、61.8% 是黄金分割」这类经验判断
    **刻意不加**：无实证支撑，且 R535 已拍板本区块不预测方向、只答「到该位进入什么状态」。
    """
    if kind.startswith("fib@"):
        return "斐波那契回调位（前一笔摆动的 %s）：常见反抽/回踩目标区" % kind[4:]
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
        "ma20": "均线位（MA20）：收盘站上 ⇒ 短线转强（20 日动量修复）",
        "ma60": "均线位（MA60）：中期成本线，站稳才谈得上趋势修复",
        "ma120": "均线位（MA120 · 半年线）",
        "ma250": "均线位（MA250 · 年线）：牛熊分界，收盘站上 ⇒ 中长期转强",
        "fib": "斐波那契回调位（基于前一笔摆动）：常见反抽/回踩目标区",
    }.get(kind, "")


def _gap_sem(edge, gtype):
    """缺口带上下沿的含义 —— **必须按缺口方向区分**，不能一套通用文案套到底（R541）。

    向下缺口（跳空下跌，通常位于现价上方 ⇒ 压力）：价格自下而上回补
      · 站上**下沿** ⇒ 进入缺口区，开始回补（第一个压力触点）
      · 站上**上沿** ⇒ 缺口被完全回补，该压力解除
    向上缺口（跳空上涨，通常位于现价下方 ⇒ 支撑）：价格自上而下跌破
      · 跌破**上沿** ⇒ 进入缺口区，开始回补（第一个支撑触点）
      · 跌破**下沿** ⇒ 缺口被完全回补，该支撑失效
    """
    down = (gtype or "down") == "down"
    if edge == "top":
        return ("收盘站上 ⇒ 缺口被完全回补，该压力解除" if down
                else "收盘跌破 ⇒ 进入缺口区，开始回补（首个支撑触点）")
    return ("收盘站上 ⇒ 进入缺口区，开始回补（首个压力触点）" if down
            else "收盘跌破 ⇒ 缺口被完全回补，该支撑失效")


def _unfilled_gaps(r, top_n=2):
    """未补的跳空缺口（取最近 top_n 个，保持引擎给出的原始顺序）。

    top_n=2（R540）：跳空缺口是缠论结构之外的独立价位锚（A 股「逢缺必补」），且同一轮
    调整常留下**多个**相邻缺口（实测 2026-09-24 与 2026-09-28 中秋假期前后各一个，
    w=0.101% / 0.257%）⇒ 只取最新一个会静默丢掉更上方的那个压力位。

    R541 抽成函数：供 collect()（缺口入关键位表）与 main()（上下沿配对成「缺口带」）
    **共用同一份筛选** —— 两处各写一份必然漂移（规则 111：同一事实单一来源）。
    """
    return [g for g in (r.get("gaps") or []) if not g.get("filled")][-top_n:]


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

    # R541：**未补缺口不再作为「关键位候选」参与去重** —— 缺口是**区间**（上沿~下沿），
    # 当成两条点型刻度会踩两个坑：
    #   ① 与近价合并后**数字失真**：实测 9/28 缺口真实为 [3878.41, 3888.37]，而上沿 3888.37
    #      与末中枢 ZG = 3884.44 相差 0.10%，落在 DEDUP_PCT(0.15%) 容差内被合并 ⇒ 标签写成
    #      「3,884.44 = 缺口上沿 09-28」，而 3888.37 这个真实价位在图/表里彻底消失。
    #      对以点位判触发的场景，0.1% ≈ 3.9 点足以给出**不同结论**（收盘站上 3884.44 与站上
    #      3888.37 不是同一件事）⇒ 属静默改判据值。
    #   ② 区间被拆成两个"无关的点"：读者看不出它们围出的是同一段真空区。
    # ⇒ 改为在图上画「缺口带」（gap_bands 字段，取引擎 top/bottom 原值），右侧逐带写含义。
    # （R540 引入缺口维度这件事本身是对的，错的只是"当点处理"这个形态。）

    # R540 新增维度①：均线（MA20/60/120/250）。直接取引擎 classify.ma_alignment，
    # **本侧不重算**（规则 111/156：同一事实单一来源 ⇒ 与主报告同口径）。
    # 此前 plan 只有缠论结构位，均线这一最广泛使用的动态支撑/压力完全缺失。
    ma = (r.get("classify") or {}).get("ma_alignment") or {}
    if ma:
        _ma_tag = "均线（%s）" % (ma.get("alignment") or "?")
        for _k, _nm in (("ma20", "MA20"), ("ma60", "MA60"),
                        ("ma120", "MA120 半年线"), ("ma250", "MA250 年线")):
            if ma.get(_k):
                out.append((float(ma[_k]), _nm, _ma_tag, _k))

    # R540 新增维度②：斐波那契回调位 —— 与主报告 report.py 同公式同参数
    # （同一摆动 leg = bis[-2]，同一组 0 / 0.382 / 0.5 / 0.618）。主报告画在图上，
    # 本侧作为候选关键位参与「到哪个价会怎样」的编排。
    _bis = r.get("bis") or []
    if len(_bis) >= 2:
        _leg = _bis[-2]
        _hi, _lo = ((_leg["end_price"], _leg["start_price"]) if _leg.get("dir") == 1
                    else (_leg["start_price"], _leg["end_price"]))
        _swing = _hi - _lo
        if _swing > 0:
            _ftag = "斐波那契回调（前一笔摆动 %.2f~%.2f）" % (_lo, _hi)
            # kind 带上百分比（fib@50% 等）⇒ 三档的「含义」各不相同，读者能对上号（见 _semantic）。
            # F0（= 前一笔终点本身）不加百分比：写「回调 0%」没有意义。
            for _f, _lab, _pct in ((0.0, "Fib F0", None), (0.382, "Fib 38.2%", "38.2%"),
                                   (0.5, "Fib 50%", "50%"), (0.618, "Fib 61.8%", "61.8%")):
                _pv = _hi - _swing * _f if _leg.get("dir") == 1 else _lo + _swing * _f
                out.append((_pv, _lab, _ftag, ("fib@" + _pct) if _pct else "fib"))
    return out


def _dedup(levels):
    """重合位**合并**（标签 / 来源 / 语义并列），不丢弃任何一条语义。

    实测必要性：2026-10-09 末中枢 GG = 3995.18 与顶背驰证伪位同价 ——
    「先到先得」式去重会静默丢掉「判据证伪」这条最强语义。

    ★ R542：合并后**必须同时保留每个成员自己的价位**（`prices`，与 labels 同步 append）。
      理由＝R541 修缺口失真的同一类缺陷在「点型对象」上同样存在：`prices[0]` 被当作整组的
      展示价，其余成员的真实价位在产物里消失。实测本轮数据：
        · 组 3,892.75 = MA20(3,892.75) ＋ MA60(**3,897.68**) ＋ Fib61.8%(**3,890.45**)
          ⇒ 读者按展示价理解「站上 3,892.75 ＝ MA60 站稳」，实际 MA60 在 3,897.68，差 **4.93 点**；
        · 组 3,995.18 = GG ＋ 顶背驰证伪位 ＋ MA120(**3,993.00**) ＋ MA250(**3,990.78**)。
      对以「收盘站上 X」判触发的区块，0.1% 的偏移就足以给出不同结论 ⇒ 与规则 169 同类，
      故成员价位必须落到产物里、由渲染层逐段标出（本侧不做任何字号/阈值判断）。
      （代表价仍取**首个候选**——collect 的顺序是「中枢 → 笔 → 背驰 → 均线 → Fib」，
       结构位天然优先，这一点是有意为之，不是副作用。）
    """
    uniq = []
    for price, label, src, kind in levels:
        for u in uniq:
            if abs(price / u["price"] - 1.0) * 100.0 < DEDUP_PCT:
                if label not in u["labels"]:
                    u["labels"].append(label)
                    u["srcs"].append(src)
                    u["kinds"].append(kind)
                    u["prices"].append(price)      # R542：与 labels 同步，段与价一一对应
                break
        else:
            uniq.append({"price": price, "labels": [label], "srcs": [src], "kinds": [kind],
                         "prices": [price]})
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
    # ★ R542 两处修正（原实现有两类缺陷）：
    #   ① 断言只覆盖「pos == 中枢内部」一个分支 ⇒ **反向不一致静默通过**：若引擎说
    #      「中枢上方」而 close 实际落在 ZG 下方（口径漂移 / 将来有人换了中枢来源），
    #      这里什么都不会报。改为**三态双向**（与 chanlun.classify 的判定式逐字同源）。
    #   ② 原判据用**严格**不等（zd < close < zg），而引擎口径是 zd <= close <= zg
    #      （`if close > zg … elif close < zd … else 中枢内部`）⇒ 收盘**恰好等于** ZG/ZD 时
    #      引擎说「中枢内部」而这里断言失败，直接把 CI 打红（生成器崩溃，非产品问题）。
    #      改为闭区间后，pos_pct = 0.0 / 100.0 也成了**合法边界**而不再是漏网状态。
    #   前提已实证：classify 的 last_zs 与 analyze 返回的 zhongshu[-1] **同一对象**
    #   （chanlun.py:1018 `last_zs = zss[-1] if zss else None`，analyze 传的就是 zss）。
    pos = (r.get("classify") or {}).get("position") or ""
    if zs:
        _exp_pos = ("中枢上方" if close > zs["zg"]
                    else ("中枢下方" if close < zs["zd"] else "中枢内部"))
        assert pos == _exp_pos, \
            "position=%s 与 close=%.2f 相对末中枢 [ZD %.2f, ZG %.2f] 的实际位置（%s）不符 " \
            "⇒ 与 analyze() 口径不一致，本轮读数作废" % (
                pos, close, zs["zd"], zs["zg"], _exp_pos)
    else:
        assert pos == "无中枢", \
            "无中枢时 position 应为「无中枢」，实测 %r ⇒ 与 analyze() 口径不一致" % pos

    up, dn = pick(collect(r), close)
    assert up and dn, "上方 / 下方价位至少各需 1 个（收集中断或合并过严）"

    def pack(u):
        # R541 自证契约：渲染层要把 `label` 段与 `semantic` 段**按序配对**（图右侧逐段写含义，
        # 「这个位是什么」对「到它会怎样」）。配对成立的前提是两字段**段数相等、顺序同源** ——
        # labels 与 kinds 由 _dedup 同步 append，semantic 由同一 kinds 逐项映射。
        # 一旦将来新增 kind 却忘了补 _semantic 文案，段数就会错位 ⇒ 让断言在这里炸，
        # 而不是让前端静默把含义配到错误的段上。
        # R542：`prices`（各成员自身价位）也必须与 labels **同步 append** —— 它是「段名 → 段自己
        # 真实价位」的映射，错位一位就会把别的成员的价位挂到错误的段上（比缺价更坏）。
        assert len(u["labels"]) == len(u["kinds"]), \
            "label 段数与 kinds 不符（%d vs %d）：%s" % (len(u["labels"]), len(u["kinds"]), u["labels"])
        assert len(u["labels"]) == len(u["prices"]), \
            "label 段数与成员价位不符（%d vs %d）：%s" % (len(u["labels"]), len(u["prices"]), u["labels"])
        _no_sem = [k for k in u["kinds"] if not _semantic(k)]
        assert not _no_sem, \
            "kind 无 _semantic 文案 ⇒ 前端按段配对时含义会串位：%s" % _no_sem
        return {"price": round(u["price"], 2), "label": _label_of(u),
                "src": " ｜ ".join(u["srcs"]),
                "dist_pct": round(_pct(u["price"], close), 2),
                "state": state_of(u["price"], close, prev_close),
                "semantic": _sem_of(u),
                # R542：逐段自身价位（与 label 的 ` / ` 分段一一对应）。代表价（price）之外的
                # 成员常与之相差 0.05%~0.13%（≈2~5 点）⇒ 渲染层据此逐段标出真实价位。
                "seg_prices": [round(float(p), 2) for p in u["prices"]]}

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

    # R540: 导出均线与乖离率（与主报告同源），供前端「均线状态」行使用。
    _ma = (r.get("classify") or {}).get("ma_alignment")
    ma_out = None
    if _ma:
        ma_out = {"ma20": _ma.get("ma20"), "ma60": _ma.get("ma60"),
                  "ma120": _ma.get("ma120"), "ma250": _ma.get("ma250"),
                  "alignment": _ma.get("alignment")}

    # R541 新增①中枢内位置分位：close 在 [ZD, ZG] 中的相对位置（0% = ZD，100% = ZG）。
    # 原来只写「中枢内部」——看不出偏强还是偏弱，而这恰恰是实战上第一个该问的问题。
    # 分位是 (close−zd)/(zg−zd) 的直接算术，输入全部来自引擎的末中枢与收盘价 ⇒ 在**生成侧**
    # 算好落盘（规则 149：区块内所有数字必须可溯源到 plan.json，渲染层不得自造统计值）。
    pos_pct = None
    if zs and zs["zg"] > zs["zd"]:
        pos_pct = round((close - zs["zd"]) / (zs["zg"] - zs["zd"]) * 100.0, 1)

    up_packed, dn_packed = [pack(u) for u in up], [pack(u) for u in dn]

    # R541 新增②未补缺口带：缺口是**区间**，独立于 up/dn 表输出（理由见 collect() 里的注释：
    # 当点型候选参与去重会把真实上沿 3888.37 折进 ZG 3884.44）。这里直接取引擎 gaps 的
    # top/bottom **原值**，配 direction / 带宽 / 距现价 / 上下沿含义 ⇒ 图上画矩形带、
    # 右侧逐带写含义。不设「上下沿是否在表内」的前置条件：带与表是两种呈现，互不隶属
    # （R540 的缺口「入选关键位」做法在本轮被这条替代，原因已记录）。
    gap_bands = []
    for _g in _unfilled_gaps(r):
        _t, _b = round(float(_g["top"]), 2), round(float(_g["bottom"]), 2)
        _ty = _g.get("type") or "down"
        # ★ R542：缺口带相对现价的**分类只在这里判一次**，落盘后图上与右侧列表**共用**
        #   （规则 111：同一事实多出口必须同口径）。此前渲染层有**两套判据**且结论相反：
        #     · 图上按几何 —— bottom > close ⇒ 压力带；top < close ⇒ 支撑带；否则「跨现价缺口区」；
        #     · 右侧列表按 `dist_pct`（带**中点**相对现价）符号 ⇒ >0 记「压力」、否则「支撑」。
        #   两者在「带横跨现价」时必然打架（引擎**可达**：未补缺口的成立条件只看
        #   `high < top` / `low > bottom`，收盘完全可以落在带内）——实测 带[3800,3830] 跨
        #   close 3813.79 时图上写「跨现价缺口区」而列表写「压力」，同一条缺口两个结论。
        #   另外 `dist_pct` 缺失时列表会退化成「支撑」（`(x or 0) > 0`），而图上仍按几何判压力。
        #   边界取**闭**：bottom >= close（价格贴在带下沿 ⇒ 正要进入缺口区，仍是压力）、
        #   top <= close（价格贴在带上沿 ⇒ 仍是支撑），与图上原判据同向、只补掉开闭差异。
        if _b >= close:
            _side = "res"
        elif _t <= close:
            _side = "sup"
        else:
            _side = "cross"
        gap_bands.append({
            "date": _g.get("date"), "top": _t, "bottom": _b, "type": _ty,
            "side": _side,
            # 方向与位置的**文案**也在生成侧给出（规则 149：渲染层不编写统计/判定文案）
            "dir_label": "向上跳空" if _ty == "up" else "向下跳空",
            "side_label": {"res": "压力", "sup": "支撑", "cross": "跨现价"}[_side],
            # 带宽（%）：跳空幅度，越大越难一次回补（供右侧列表如实标出）
            "width_pct": round((_t / _b - 1.0) * 100.0, 3),
            # 带中点相对现价的距离：正值 = 在现价上方（压力），负值 = 下方（支撑）
            "dist_pct": round(_pct((_t + _b) / 2.0, close), 2),
            "sem_top": _gap_sem("top", _ty),
            "sem_bot": _gap_sem("bot", _ty),
        })

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
        "up": up_packed,
        "dn": dn_packed,
        "actions": acts,
        "invalidations": invalid,
        "signals": sigs,
        "ma": ma_out,
        "bias": r.get("bias"),
        "pos_pct": pos_pct,
        "gap_bands": gap_bands,
        "near_pct": NEAR_PCT,
        # R541：原 `notes`（6 条「怎么用」说明）字段**已删除** —— 旭总 2026-10-12 要求删掉
        # 页面底部那段冗长说明。删除前逐条核过承载方（规则 107：披露不得静默丢失）：
        #   ① 快照漂移/以 asof 为准 → 区块副标题「数据截至 X 收盘」+ 图内「数据 X 收盘」
        #   ② 所有位取自 analyze() 同口径 → 页面 .plan-ref 一行保留（同源声明无他处载体）
        #   ③ 买卖点取自 find_signals() → 同上 + 折叠块「引擎 chanlun.find_signals 产出」
        #   ④ 三类需新中枢 / 一类需 MACD 确认 → 折叠块「下一信号 · 触发条件」逐条列出
        #   ⑤ 二类需一类先出现 → 同上（每类各一行 price_label/cond）
        #   ⑥ 不构成买卖建议 → 全站「六、免责说明」覆盖
        # 全部有更细载体 ⇒ 字段一并删除，不留无消费者的死数据。
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    _sig_rows = sum(len(l["rows"]) for l in sigs["levels"])
    _sig_live = sum(1 for l in sigs["levels"] for x in l["rows"] if x["status"] == "判据成立")
    # R542：观测「有多少段的自身价位与组代表价不同」—— 这正是此前被静默吞掉的信息量，
    # 打出来便于每轮盘后一眼看出产品里到底有多少个「真实但未显示」的价位。
    _seg_off = sum(1 for _x in (up_packed + dn_packed)
                   for _p in _x["seg_prices"] if _p != _x["price"])
    print("plan.json OK  asof=%s  close=%s  上%d 下%d  动作%d  失效%d  信号%d(判据成立%d)"
          "  中枢分位=%s  缺口带%d  段内异价%d" % (
              plan["asof"], plan["close"], len(plan["up"]), len(plan["dn"]),
              len(plan["actions"]), len(plan["invalidations"]), _sig_rows, _sig_live,
              ("%.1f%%" % pos_pct) if pos_pct is not None else "—", len(gap_bands), _seg_off))


if __name__ == "__main__":
    main()
