# -*- coding: utf-8 -*-
"""plan/inject_plan.py — 把「明日关键价位 · 条件预案」区块注入主报告 index.html

用法（挂在 deploy.yml：report.py 之后）：
  python3 plan/inject_plan.py index.html

设计（沿用 radar/national 两个注入器的既有约定）：
  · 读 plan/plan.json（由 plan/gen_plan.py 产出）；缺失时**不阻断部署**（WARN + 跳过）。
  · 幂等：已含 id="plan-board" 则跳过（防双份 CSS / 区块，R353 同款教训）。
  · 锚点：注入到 `<h2 class="sec" id="s1">` 之前 —— 即紧跟「今日结构信号」条之后。
  · 区块内**所有数字来自 plan.json 或 index.html 的内联推演数据**，本脚本不写任何统计值（规则 149）。
  · 不新增章节编号（保持 s1..s6 既有编号体系不被扰动）。

R538：把「关键价位表」改为**价格水位图**（纯 SVG，无 JS、无外部请求）
  → 图为准，原表折叠进 <details>（保留「结构来源」依据，规则 107 披露纪律：不静默丢信息）。
  → 「明日九成区间」从**已生成的 index.html 内联推演数据**同源提取（规则 111：同一事实单一来源，
     不重算）。提取失败则**不画带**、不阻断部署。
  → **不画 P25–P75**：单日覆盖率实测 72.5%（上证），与名义 50% 严重不符 ⇒ 标不出可靠口径。
     九成区间（P05–P95）实测 93.2%，过门 ⇒ 画。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
PLAN = os.path.join(HERE, "plan.json")
MARK = 'id="plan-board"'
ANCHOR = '<h2 class="sec" id="s1">'

# 内联推演数据块的定位串（report.py 生成的 forecast 图容器 id）
_BAND_ANCHOR = "echart-forecast-sh000001"

PLAN_CSS = """<style>
/* ===== plan: 明日关键价位（条件预案 · 不预测方向）===== */
#plan-board { border-left: 4px solid #d4a017; }
/* 标题：直接沿用全站 .panel h2（18px + 底部细分隔线），副标题降为次要文字
   ⇒ R539 之前用 h3，与其余 panel 相比少了分隔线、字号小一号（风格割裂）。 */
.plan-sub { font-size: 12px; color: #64748b; font-weight: 400; }
/* meta → 徽章 chip（与全站 panel 标题旁的状态徽章同风格，替代原纯文本行） */
.plan-meta { display: flex; flex-wrap: wrap; gap: 6px 8px; font-size: 12px; margin: 0 0 12px; }
.plan-chip { display: inline-flex; align-items: baseline; gap: 5px; padding: 2px 10px;
  border-radius: 999px; border: 1px solid #e5e9f0; background: #f8fafc; color: #475569;
  white-space: nowrap; font-variant-numeric: tabular-nums; }
.plan-chip b { color: #1f2937; font-weight: 700; }
.plan-chip--up { border-color: #f4c6c6; background: #fdf3f3; }
.plan-chip--up b { color: #c22f2f; }
.plan-chip--dn { border-color: #bfe6cd; background: #f1faf4; }
.plan-chip--dn b { color: #12874b; }
.plan-chip--warn { border-color: #f0cf98; background: #fdf8ee; }
.plan-chip--warn b { color: #a16207; }
.plan-chip--info { border-color: #c7d7ee; background: #f2f6fc; }
.plan-chip--info b { color: #1d4ed8; }
/* 图容器：与全站 .chartbox 同款浅底 + 描边 + 圆角（原来 SVG 裸露在面板底色上） */
.plan-box { background: #fbfdff; border: 1px solid #eef2f7; border-radius: 10px; padding: 6px; }
.plan-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.plan-tbl th { text-align: left; font-weight: 600; color: #64748b; font-size: 12px;
  padding: 6px 8px; border-bottom: 1px solid #e5e9f0; white-space: nowrap; }
.plan-tbl td { padding: 7px 8px; border-bottom: 1px solid #f1f5f9; vertical-align: top; line-height: 1.6; }
.plan-dir { white-space: nowrap; font-weight: 600; }
.plan-dir--up { color: #e54545; }
.plan-dir--dn { color: #18a058; }
.plan-p { font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.plan-d { color: #64748b; font-variant-numeric: tabular-nums; white-space: nowrap; }
.plan-src { color: #94a3b8; font-size: 12px; margin-top: 3px; }
.plan-sem { color: #475569; }
.plan-tr--near { background: #fff8e6; }
.plan-tr--hit { background: #fdecec; }
.plan-flag { display: inline-block; margin-left: 6px; font-size: 11px; padding: 1px 6px;
  border-radius: 999px; background: #d4a017; color: #fff; font-weight: 600;
  white-space: nowrap; vertical-align: middle; }
.plan-flag--hit { background: #e54545; }
.plan-foot { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #e5e9f0;
  font-size: 13px; color: #475569; line-height: 1.85; }
.plan-foot b { color: #1f2937; font-variant-numeric: tabular-nums; }
.plan-note { margin-top: 8px; font-size: 12px; color: #64748b; line-height: 1.7;
  background: #f8fafc; border-radius: 6px; padding: 8px 12px; }
/* ===== plan: 买卖点信号（与上方「条件预案」同区块）===== */
.plan-sec { font-size: 13px; font-weight: 600; color: #1f2937; margin: 16px 0 6px; }
.plan-sec span { font-weight: 400; color: #64748b; font-size: 12px; }
.plan-na { color: #94a3b8; font-size: 12px; }
.plan-st--on { color: #64748b; }
.plan-st--off { color: #b0b7c3; }
.plan-tip { margin-top: 6px; font-size: 12px; color: #64748b; line-height: 1.7; }
/* 买卖点触发位速查（R539）：图旁文字版，与图上紫标一一对应 */
.plan-tl { margin: 4px 0 0; padding: 0; list-style: none; font-size: 12.5px; }
.plan-tl li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px;
  padding: 4px 0; border-bottom: 1px dashed #f1f5f9; }
.plan-tl li > span { color: #64748b; flex: 1 1 170px; line-height: 1.6; }
.plan-tk { color: #7c3aed; font-weight: 700; white-space: nowrap; }
.plan-tv { font-variant-numeric: tabular-nums; color: #1f2937; white-space: nowrap; }
/* ===== plan: 价格水位图（纯 SVG · 无 JS · 无外部请求）===== */
.plan-chart { display: block; width: 100%; max-width: 460px; height: auto; margin: 0; }
/* 宽屏：图在左、读法与依据在右（纵向价格尺不宜拉宽，右侧留给文字，避免大片留白） */
.plan-viz { display: flex; flex-wrap: wrap; gap: 8px 26px; align-items: flex-start; }
.plan-viz > .plan-box { flex: 0 0 432px; max-width: 100%; }
.plan-side { flex: 1 1 320px; min-width: 250px; }
.plan-side > .plan-tip { margin-top: 8px; }
.plan-side > .plan-det { margin-top: 10px; }
.plan-side > .plan-foot { margin-top: 12px; }
.plan-det { margin-top: 10px; }
.plan-det > summary { cursor: pointer; font-size: 12px; color: #64748b; padding: 6px 0;
  list-style-position: outside; }
.plan-det > summary:hover { color: #1f2937; }
.plan-det[open] > summary { margin-bottom: 4px; }
@media (max-width: 720px) {
  /* 窄屏改为**纵向卡片**：表格横滚会把「收盘到该位的含义」推到视口外，
     而那正是本区块最该被读到的列 ⇒ 放弃横滚，改为每行一卡、全部信息纵向可见。 */
  .plan-tbl { min-width: 0; }
  .plan-tbl thead { display: none; }
  .plan-tbl tbody, .plan-tbl tr, .plan-tbl td { display: block; width: auto; }
  .plan-tbl tr { padding: 9px 2px; border-bottom: 1px solid #eef2f7; }
  .plan-tbl td { padding: 0; border-bottom: 0; }
  .plan-tbl td.plan-dir { display: inline-block; margin-right: 6px; }
  .plan-tbl td.plan-p { display: inline-block; }
  .plan-tbl td.plan-d { display: inline-block; margin-left: 8px; }
  .plan-tbl td.plan-sem { margin-top: 4px; }
  .plan-tbl td.plan-lv { display: inline-block; margin-right: 6px; font-weight: 600; }
  .plan-tbl td.plan-k { display: inline-block; }
  .plan-tbl td.plan-date { display: block; font-size: 12px; color: #64748b; }
  .plan-tbl td.plan-rr, .plan-tbl td.plan-inv { display: block; font-size: 12px; margin-top: 2px; }
  .plan-meta { font-size: 12px; }
  .plan-sec { margin-top: 14px; }
  .plan-chart { max-width: 100%; }
}
</style>
"""

_STATE = {"near": "接近", "up_cross": "今日上破", "dn_cross": "今日跌破", "far": ""}


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _money(v):
    return "{:,.2f}".format(v)


def _sign(v):
    return ("+" if v > 0 else ("\u2212" if v < 0 else "")) + "{:.2f}%".format(abs(v))


# ===== 明日区间：从 index.html 内联推演数据同源提取（规则 111：不重算）=====

def extract_band(html):
    """从**已生成的 index.html** 内联的推演数据块取 T+1 的 P05–P95 区间。

    数据来源与页面「未来走势推演」图**同一处**（report.py 写出的 `var D`），
    本脚本只透传、不重算 ⇒ 图上带子与推演图口径必然一致。
    返回 {"low","high","date","med"} 或 None（取不到则图上不画带，不阻断部署）。
    """
    i = html.find(_BAND_ANCHOR)
    if i < 0:
        return None
    j = html.find("var D = ", i)
    if j < 0:
        return None
    k = html.find("\n", j)
    if k < 0:
        return None
    raw = html[j + len("var D = "):k].strip().rstrip(";")
    try:
        D = json.loads(raw)
    except Exception:
        return None
    proj = D.get("proj") or []
    if not proj:
        return None
    p0 = proj[0]
    lo, w = p0.get("f95l"), p0.get("f95h")     # f95h 是**宽度**（report.py:2182 u95-l95）
    if lo is None or w is None:
        return None
    try:
        return {"low": round(float(lo), 2), "high": round(float(lo) + float(w), 2),
                "date": p0.get("date") or "", "med": p0.get("med")}
    except (TypeError, ValueError):
        return None


# ===== 价格水位图（纯 SVG · 字号按用户单位给足，窄屏靠 viewBox 等比缩放即够读）=====
_CH_W, _CH_H = 300, 500         # R539: 486→500（图例多一行「买卖点触发位」说明）
_CH_PT, _CH_PB = 30, 396        # 绘图区上下沿
_CH_AX = 54                     # 价格轴竖线 x
_CH_BX1 = 176                   # 带区右沿（左沿 = 价格轴）
_CH_MK = 182                    # 关键位刻度线终点
_CH_LB = 186                    # 关键位标签起始 x
_CH_TICK = 46                   # 价格刻度文字右对齐 x
_CH_LEG = (418, 434, 450, 466, 482)  # 图例各行基线 y
_C_UP, _C_DN, _C_CLOSE = "#e54545", "#18a058", "#334155"
_C_GRID, _C_AXIS = "#eef2f7", "#cbd5e1"
_C_B95, _C_B95S = "#fbead0", "#dfb571"
_C_ZS, _C_ZSS = "#e9eef7", "#c3cfe0"
# 买卖点触发标记：**刻意用红绿之外的第二色系**（红=上方/绿=下方 的价位语义不能被买卖点语义污染）
_C_TRG, _C_TRGB, _C_TRGS = "#7c3aed", "#f3e8ff", "#d8b4fe"


def _dw(s):
    """显示宽度（CJK 记 1、其余记 0.55），用于判标签是否放得下。"""
    return sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in str(s))


def _clip(s, limit):
    s = str(s)
    if _dw(s) <= limit:
        return s
    out = ""
    for c in s:
        if _dw(out + c) > limit - 1.0:
            break
        out += c
    return out + "\u2026"


# R540: 图内名称的**前缀缩写表**。图宽 300、标签区仅 x=186~300（≈114px），而维度扩展后
# 一个价位常并列两个语义（如「中枢上沿 ZG / 缺口上沿 09-28」）。缩写「中枢X 」前缀每次
# 省下约 3 个 CJK 宽，使**第二段（往往正是共振来源）也能挤进图内**，而不是被省略号吞掉。
# ★ 只作用于图内显示：悬停 title 与折叠表一律使用完整 label，信息不因排版而丢失。
_SEG_ABBR = (("中枢上沿 ", ""), ("中枢下沿 ", ""), ("中枢最高 ", ""), ("中枢最低 ", ""),
             ("缺口上沿 ", "缺口上"), ("缺口下沿 ", "缺口下"), ("顶背驰证伪位", "顶背驰证伪"))


def _abbr(seg):
    for _a, _b in _SEG_ABBR:
        if seg.startswith(_a):
            return _b + seg[len(_a):]
    return seg


def _lv_name(x, limit=11.6):
    """关键位名称：图上显示**前两段**（` / ` 分隔，段名经 _abbr 缩写），余下加省略号；
    全文在 SVG 悬停 title 与折叠表里，不丢信息。
    limit 可调：该档若还要挂「买卖点触发」小标，需让出宽度。

    R540：由「只取第一段」改为**并列前两段**。维度扩展后同一价位常承载多个语义，
    典型如 3,884.44 =「中枢上沿 ZG / 缺口上沿 09-28」—— 只取第一段会让**共振信息**
    （同一价位被两个独立体系同时确认）被省略号吞掉，而共振恰恰是实战上最该看见的。
    """
    parts = [_abbr(p.strip()) for p in str(x.get("label") or "").split(" / ") if p.strip()]
    txt = parts[0] if parts else ""
    if len(parts) > 1:
        txt = txt + " \u00b7 " + parts[1]
        if len(parts) > 2:
            txt += "\u2026"
    st = x.get("state")
    if st in ("near", "up_cross", "dn_cross"):
        txt += "（%s）" % _STATE[st]
    return _clip(txt, limit)


# ===== 买卖点触发位（R539）：图上直接标「到什么点位形成哪一类买卖点」=====

def _triggers(p):
    """返回 {价位: next 条目}，**只收引擎给了明确价位的**。

    plan.json 的 signals.next 共 6 类，其中只有「一类买 / 一类卖」带 price；
    「二类买/卖」判据是「需对应一类点先出现」、「三类买/卖」是「需新中枢形成」
    ⇒ 引擎**没有**给出固定价位，本函数不硬编、不用 ZG/ZD 顶替（规则 111/114：
    同一事实单一来源，测量总体须与语义同口径）。
    """
    out = {}
    for x in ((p.get("signals") or {}).get("next") or []):
        if x.get("price") is None:
            continue
        out[round(float(x["price"]), 2)] = x
    return out


def _trig_short(x):
    """触发标短名：△ 买 / ▽ 卖（形状承载买卖语义，避免与「红=上方/绿=下方」抢色）。"""
    k = str(x.get("kind") or "")
    mark = "\u25b3" if "\u4e70" in k else "\u25bd"     # △ 买 / ▽ 卖
    return mark + _clip(k, 4.4)


def _trg_list(p):
    """图旁「买卖点触发位」速查（R539）。

    有价位的逐条列出（含判定条件）；**无价位的如实标出为什么没有**——
    原因文字直接取 plan.json 的 price_label（「需一类买先出现」/「需新中枢形成」），
    不由本脚本编写（规则 149：区块内不写任何统计值）。
    """
    nx = (p.get("signals") or {}).get("next") or []
    if not nx:
        return ""
    with_p = [x for x in nx if x.get("price") is not None]
    no_p = [x for x in nx if x.get("price") is None]
    li = "".join(
        '<li><b class="plan-tk">%s</b><b class="plan-tv">%s</b><span>%s</span></li>'
        % (esc(_trig_short(x)), _money(x["price"]), esc(x.get("cond") or ""))
        for x in with_p)
    tail = ""
    if no_p:
        tail = ('<div class="plan-tip">其余 %d 类无固定价位：%s。'
                '前置条件决定其无法用单点位预判（完整条件见下方折叠块）。</div>'
                % (len(no_p), "；".join("%s（%s）" % (esc(x["kind"]), esc(x.get("price_label") or ""))
                                       for x in no_p)))
    return ("""
  <div class="plan-sec">买卖点触发位 <span>（图上紫标 · 引擎本次给出 %d 个价位）</span></div>
  <ul class="plan-tl">%s</ul>%s""" % (len(with_p), li, tail))


def _chart(p, band):
    """价格水位图。竖轴 = 价格；所有数字来自 plan.json / index.html 内联数据。"""
    zs = p.get("zhongshu") or {}
    close = float(p.get("close") or 0.0)
    ups = list(p.get("up") or [])
    dns = list(p.get("dn") or [])
    trg = _triggers(p)

    vals = [close] + [float(x["price"]) for x in ups + dns]
    for k in ("zg", "zd", "gg", "dd"):
        try:
            vals.append(float(zs[k]))
        except (KeyError, TypeError, ValueError):
            pass
    if band:
        vals += [float(band["low"]), float(band["high"])]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    sp = max(hi - lo, 1.0)
    lo -= sp * 0.06
    hi += sp * 0.05
    rg = max(hi - lo, 1e-6)

    def py(v):
        return _CH_PT + (hi - float(v)) / rg * (_CH_PB - _CH_PT)

    o = ['<svg class="plan-chart" viewBox="0 0 %d %d" role="img" '
         'aria-label="价格水位图：当前中枢区间、明日九成区间与上下方关键位">'
         % (_CH_W, _CH_H)]

    # R540: 图内标注**数据日**。旭总需求：「给图标一个日期，这样我才能判断当日是否更新」——
    # 此前日期只出现在区块副标题里，本图被单独截图 / 转发出去后无从判断新鲜度。
    # 放右上角（顶部 y<30 区在价格轴刻度之上，不与任何图元争位）。
    o.append('<text class="plan-date" x="%g" y="13" font-size="9.5" fill="#94a3b8" '
             'text-anchor="end">数据 %s 收盘</text>'
             % (_CH_W - 4, esc(str(p.get("asof") or "?"))))

    # ① 价格网格 + 轴
    step = 50.0
    t = (int(lo // step) + 1) * step
    while t < hi:
        y = py(t)
        o.append('<line x1="%g" y1="%.1f" x2="%g" y2="%.1f" stroke="%s"/>'
                 % (_CH_AX, y, _CH_W - 4, y, _C_GRID))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#94a3b8" '
                 'text-anchor="end">%s</text>' % (_CH_TICK, y + 3.2, "{:,.0f}".format(t)))
        t += step
    o.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="%s"/>'
             % (_CH_AX, _CH_PT, _CH_AX, _CH_PB, _C_AXIS))

    # ② 明日九成区间（先画，位于中枢之下层）
    if band:
        y1, y2 = py(band["high"]), py(band["low"])
        o.append('<rect x="%g" y="%.1f" width="%g" height="%.1f" rx="3" fill="%s" '
                 'stroke="%s"><title>%s</title></rect>'
                 % (_CH_AX, y1, _CH_BX1 - _CH_AX, max(y2 - y1, 1.0), _C_B95, _C_B95S,
                    esc("明日九成区间 P05-P95（T+1 %s）：%s ~ %s｜单日 point-in-time 实测覆盖 93.2%%"
                        "（名义 90%%）" % (band.get("date") or "?", _money(band["low"]),
                                          _money(band["high"])))))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#a16207">九成区间</text>'
                 % (_CH_AX + 6, y1 + 13))

    # ③ 当前中枢（ZD~ZG）
    try:
        zg, zd = float(zs["zg"]), float(zs["zd"])
    except (KeyError, TypeError, ValueError):
        zg = zd = None
    if zg is not None:
        y1, y2 = py(zg), py(zd)
        o.append('<rect x="%g" y="%.1f" width="%g" height="%.1f" rx="3" fill="%s" '
                 'stroke="%s"><title>%s</title></rect>'
                 % (_CH_AX, y1, _CH_BX1 - _CH_AX, max(y2 - y1, 1.0), _C_ZS, _C_ZSS,
                    esc("当前中枢 ZD~ZG：%s ~ %s（%s~%s · %s 笔）"
                        % (_money(zd), _money(zg), zs.get("date_start", "?"),
                           zs.get("date_end", "?"), zs.get("count", "?")))))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#64748b">中枢 ZD~ZG</text>'
                 % (_CH_AX + 6, y1 + 13))

    # ④ 现价（深色虚线 + 轴侧标签）；虚线只画到带区右沿，避免横穿右侧关键位标签
    yc = py(close)
    o.append('<line x1="%g" y1="%.1f" x2="%g" y2="%.1f" stroke="%s" stroke-width="1.4" '
             'stroke-dasharray="5 3"/>' % (_CH_AX, yc, _CH_BX1, yc, _C_CLOSE))
    o.append('<rect x="%g" y="%.1f" width="52" height="19" rx="4" fill="%s" opacity="0.92"/>'
             % (_CH_AX + 5, yc - 9.5, _C_CLOSE))
    o.append('<text x="%.1f" y="%.1f" font-size="11" font-weight="700" fill="#ffffff" '
             'text-anchor="middle">%s</text>'
             % (_CH_AX + 31, yc + 4, _money(close)))

    # ⑤ 关键位（红 = 上方 / 绿 = 下方，沿用全站惯例）
    #    R539：凡「引擎给了明确价位」的买卖点，在同一档名称行右侧挂紫色触发标
    #    （形状 △买/▽卖 承载买卖语义；用紫而非红绿，避免与「红=上方/绿=下方」抢色）。
    #    R540：**标签避让**。维度扩展（均线 / Fib / 缺口）后，同一价格带会挤进多个位 ——
    #    实测 3,878~3,905 一段内就有 4 个（缺口下沿 / ZG+缺口上沿 / MA20+MA60+F61.8 / F50），
    #    按真实价位投影后的理想间距只有 16.1 / 10.8 / 7.8 px，而标签块高约 15px ⇒ 必然互压。
    #    做法：短线 + 圆点仍画在**真实价位** y 上；标签块用避让后的 ym（贪心下推 + 越界回夹）；
    #    偏移 > 3px 时在标签左缘补一条同色竖直虚线指回真实价位 —— 价位不因排版而失真。
    #    ★ 间距取值：每档实际占「价格行（ym−14 … ym+3）+ 名称行（ym+4 … ym+16）」≈ 30px，
    #      故 MK_GAP 取 34（净空隙 4px）；R540 首版误取 22 ⇒ 3,884.44 的名称行被
    #      3,878.41 的价格行压住（已由截图核出）。
    MK_GAP = 34.0
    items = [(py(x["price"]), _C_UP if i < len(ups) else _C_DN, x)
             for i, x in enumerate(ups + dns)]
    ymv = [it[0] for it in items]
    if items:
        _ord = sorted(range(len(items)), key=lambda i: ymv[i])
        for _k in range(1, len(_ord)):
            _i, _ip = _ord[_k], _ord[_k - 1]
            if ymv[_i] - ymv[_ip] < MK_GAP:
                ymv[_i] = ymv[_ip] + MK_GAP
        _ov = ymv[_ord[-1]] - (_CH_PB + 4)          # 底部越界 ⇒ 整组上移
        if _ov > 0:
            ymv = [v - _ov for v in ymv]
        _dd = 14.0 - ymv[_ord[0]]                   # 上移过头 ⇒ 整组下夹
        if _dd > 0:
            ymv = [v + _dd for v in ymv]

    for i, (y, col, x) in enumerate(items):
        ym = ymv[i]
        t = trg.get(round(float(x["price"]), 2))
        nm = _lv_name(x, 6.6 if t else 11.6)
        o.append('<g class="plan-mk">')
        o.append('<title>%s</title>' % esc(
            "%.2f  %s\n距现价 %s\n来源：%s\n收盘到该位：%s"
            % (float(x["price"]), x.get("label") or "", _sign(x.get("dist_pct") or 0),
               x.get("src") or "", x.get("semantic") or "")))
        o.append('<line x1="%g" y1="%.1f" x2="%g" y2="%.1f" stroke="%s" stroke-width="2"/>'
                 % (_CH_BX1, y, _CH_MK, y, col))
        o.append('<circle cx="%g" cy="%.1f" r="3" fill="%s"/>' % (_CH_MK, y, col))
        if abs(ym - y) > 3:                 # 标签被避让开 ⇒ 补虚线指回真实价位
            o.append('<line x1="%g" y1="%.1f" x2="%g" y2="%.1f" stroke="%s" '
                     'stroke-width="0.8" stroke-dasharray="2 2" opacity="0.65"/>'
                     % (_CH_LB - 2, y, _CH_LB - 2, ym + 8, col))
        o.append('<text x="%g" y="%.1f" font-size="11.5" font-weight="700" fill="%s">%s</text>'
                 % (_CH_LB, ym - 3, col, _money(x["price"])))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#94a3b8">%s</text>'
                 % (_CH_LB + 53, ym - 3, esc(_sign(x.get("dist_pct") or 0))))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#94a3b8">%s</text>'
                 % (_CH_LB, ym + 12, esc(nm)))
        if t:
            lab = _trig_short(t)
            tx = _CH_LB + _dw(nm) * 9.5 + 6
            w = _dw(lab) * 8.5 + 10
            if tx + w <= _CH_W - 2:          # 放不下则不画（宁可少标，也不裁字/出框）
                o.append('<rect class="plan-trg" x="%.1f" y="%.1f" width="%.1f" height="12" rx="6" '
                         'fill="%s" stroke="%s"><title>%s</title></rect>'
                         % (tx, ym + 3, w, _C_TRGB, _C_TRGS,
                            esc("%s · 触发位 %s\n%s" % (t["kind"], _money(x["price"]),
                                                        t.get("cond") or ""))))
                o.append('<text x="%.1f" y="%.1f" font-size="8.5" fill="%s">%s</text>'
                         % (tx + 5, ym + 12, _C_TRG, esc(lab)))
        o.append('</g>')

    # ⑥ 图例
    ly = _CH_LEG
    legs = [
        ("rect", _C_B95, _C_B95S, "九成区间 P05-P95（单日实测覆盖 93.2%）"),
        ("none", None, None, "%s ~ %s  ·  T+1 %s"
         % (_money(band["low"]), _money(band["high"]), band.get("date") or "?")
         if band else "（未能从页面数据取到，故本图不画该带）"),
        ("rect", _C_ZS, _C_ZSS, "中枢 ZD~ZG  %s ~ %s" % (_money(zd), _money(zg))
         if zg is not None else "中枢区间（本次未取到）"),
        ("line", None, None, "现价 %s  ·  红=上方 / 绿=下方" % _money(close)),
        ("trg", None, None, "紫 = 买卖点触发位（%d 个 · 悬停看条件）" % len(trg)),
    ]
    for i, (kind, fill, stroke, text) in enumerate(legs):
        yy = ly[i]
        if kind == "rect":
            o.append('<rect x="%g" y="%.1f" width="9" height="9" rx="2" fill="%s" stroke="%s"/>'
                     % (_CH_AX, yy - 8, fill, stroke))
        elif kind == "line":
            o.append('<line x1="%g" y1="%.1f" x2="%g" y2="%.1f" stroke="%s" '
                     'stroke-width="1.4" stroke-dasharray="5 3"/>'
                     % (_CH_AX, yy - 3.5, _CH_AX + 9, yy - 3.5, _C_CLOSE))
        elif kind == "trg":
            o.append('<rect x="%g" y="%.1f" width="9" height="9" rx="4.5" fill="%s" stroke="%s"/>'
                     % (_CH_AX, yy - 8, _C_TRGB, _C_TRGS))
        o.append('<text x="%.1f" y="%.1f" font-size="9.5" fill="#64748b">%s</text>'
                 % (_CH_AX + 14, yy, esc(_clip(text, 23.5))))

    o.append('</svg>')
    return "".join(o)


# ===== 关键价位表（折叠保留：结构来源依据不得静默丢失）=====

def _row(x, arrow, dir_cls):
    st = x.get("state", "far")
    cls = ""
    if st in ("up_cross", "dn_cross"):
        cls = " plan-tr--hit"
        flag = '<span class="plan-flag plan-flag--hit">%s</span>' % _STATE[st]
    elif st == "near":
        cls = " plan-tr--near"
        flag = '<span class="plan-flag">%s</span>' % _STATE["near"]
    else:
        flag = ""
    return (
        '<tr class="plan-tr%s">'
        '<td class="plan-dir %s">%s</td>'
        '<td class="plan-p">%s%s</td>'
        '<td class="plan-d">%s</td>'
        '<td>%s<div class="plan-src">%s</div></td>'
        '<td class="plan-sem">%s</td>'
        '</tr>' % (cls, dir_cls, arrow, _money(x["price"]), flag, _sign(x["dist_pct"]),
                   esc(x["label"]), esc(x["src"]), esc(x["semantic"]))
    )


def build_details(p):
    rows = "".join(_row(x, "\u25b2", "plan-dir--up") for x in p.get("up") or [])
    rows += "".join(_row(x, "\u25bc", "plan-dir--dn") for x in p.get("dn") or [])
    n = len(p.get("up") or []) + len(p.get("dn") or [])
    return """
  <details class="plan-det">
    <summary>结构来源与含义（图上 %d 个价位 · 点击展开）</summary>
    <div class="tablescroll">
    <table class="plan-tbl">
      <thead><tr><th>方向</th><th>价位</th><th>距现价</th><th>结构来源</th><th>收盘到该位的含义</th></tr></thead>
      <tbody>%s</tbody>
    </table>
    </div>
  </details>""" % (n, rows)


# ===== 买卖点信号两小节 =====
# 设计原则：① 全部数字来自 plan.json（规则 149）；② 复用既有 .plan-* 类，只加几个小类；
# ③ 「判据成立/已破」只答「判据还成立吗」，**不得**被读成买卖指令（措辞与脚注双重约束）。
_SIG_COLS = ("级别", "类型", "日期", "价位", "距现价", "止损 → 目标（R:R）", "失效位 / 判据")
_NEXT_COLS = ("目标类型", "需要先看到什么", "关键位")


def _sig_row(x, level):
    """一条买卖点信号。方向色沿用全站惯例：买=红、卖=绿（与图上三角/倒三角一致）。"""
    d_cls = "plan-dir--up" if x["dir"] > 0 else "plan-dir--dn"
    rr = "—"
    if x.get("target") is not None:
        rr = "%s → %s" % (_money(x.get("stop") or 0), _money(x["target"]))
        if x.get("rr") is not None:
            rr += "（R:R %.2f）" % x["rr"]
    broken = x["status"] == "判据已破"
    return (
        '<tr>'
        '<td class="plan-lv">%s</td>'
        '<td class="plan-k %s">%s</td>'
        '<td class="plan-date">%s</td>'
        '<td class="plan-p">%s</td>'
        '<td class="plan-d">%s</td>'
        '<td class="plan-rr">%s</td>'
        '<td class="plan-inv">%s <span class="%s">%s</span>'
        '<div class="plan-src">%s</div></td>'
        '</tr>' % (esc(level), d_cls, esc(x["kind"]), esc(x["date"]),
                   _money(x["price"]), _sign(x["dist_pct"]), rr,
                   _money(x["inval"]),
                   "plan-st--off" if broken else "plan-st--on", esc(x["status"]),
                   esc(x["inval_src"]))
    )


def _next_row(x):
    """一行「下一信号触发条件」。给不出价位的（需一类先出 / 需新中枢）如实标出原因，不留空。"""
    if x.get("price") is not None:
        cell = '%s<div class="plan-src">%s</div>' % (_money(x["price"]), esc(x["price_label"]))
    else:
        cell = '<span class="plan-na">%s</span>' % esc(x["price_label"])
    return ('<tr><td class="plan-k">%s</td><td>%s</td><td class="plan-inv">%s</td></tr>'
            % (esc(x["kind"]), esc(x["cond"]), cell))


def build_signals(sg):
    """R539：两张表**移入默认收起的 <details>**（原来是常显的两个大表）。

    理由：水位图已把主要价位画成图，其中 6 档关键位有 5 档与信号表价位重合
    （3,995.18 / 3,967.68 / 3,884.44 / 3,767.50 / 3,741.11）⇒ 常显即与图重复。
    但「失效位口径」与「R:R 口径说明」属**披露**（规则 107）⇒ 随表一起折叠保留，
    一字不删、只是默认不占版面。
    """
    if not sg or not (sg.get("levels") or sg.get("next")):
        return ""
    lv_txt = " ｜ ".join(
        "%s级近 %d 条：买 %d · 卖 %d" % (l["level"], len(l["rows"]),
                                       (l.get("counts") or {}).get("买", 0),
                                       (l.get("counts") or {}).get("卖", 0))
        for l in sg.get("levels") or [])
    tot = sg.get("total") or {}
    sig_rows = "".join(_sig_row(x, l["level"])
                       for l in sg.get("levels") or [] for x in l["rows"])
    next_rows = "".join(_next_row(x) for x in sg.get("next") or [])
    thead_sig = "".join("<th>%s</th>" % c for c in _SIG_COLS)
    thead_next = "".join("<th>%s</th>" % c for c in _NEXT_COLS)
    n_sig = sum(len(l["rows"]) for l in sg.get("levels") or [])
    n_next = len(sg.get("next") or [])
    return """
  <details class="plan-det">
    <summary>历史买卖点信号 %d 条 · 下一信号触发条件 %d 类 · 失效位口径（点击展开）</summary>
    <div class="plan-sec">买卖点信号 <span>（引擎 chanlun.find_signals 产出 · 引擎共 %s 笔 / %s 段 · %s）</span></div>
    <div class="tablescroll">
    <table class="plan-tbl">
      <thead><tr>%s</tr></thead>
      <tbody>%s</tbody>
    </table>
    </div>
    <div class="plan-tip">失效位口径：<b>%s</b><br>
      止损 / 目标 / R:R 直接沿用引擎 <b>attach_rr()</b> 的既有口径（目标 = 该笔之前最近完成中枢的边界），
      可能距现价很远（如某行目标 3,040.69 低于现价约 20%%）⇒ <b>R:R 数值大不等于可交易</b>；
      本表的看点是「失效位」与「判据是否成立」。</div>
    <div class="plan-sec">下一信号 · 触发条件 <span>（「需先看到什么」是引擎判据的前置条件，不是价位预测）</span></div>
    <div class="tablescroll">
    <table class="plan-tbl">
      <thead><tr>%s</tr></thead>
      <tbody>%s</tbody>
    </table>
    </div>
  </details>""" % (n_sig, n_next, tot.get("笔", 0), tot.get("段", 0), esc(lv_txt),
                    thead_sig, sig_rows, esc(sg.get("rule") or ""),
                    thead_next, next_rows)


def build(p, band=None):
    zs = p.get("zhongshu") or {}
    ztxt = ("%.2f ~ %.2f（%s 笔 · %s）" % (zs.get("zd", 0), zs.get("zg", 0),
                                           zs.get("count", "?"), zs.get("date_end", "?"))
            if zs else "—")

    acts = p.get("actions") or []
    if acts:
        act_txt = "；".join("%s %s 后进入：%s" % (a["dir"], _money(a["price"]), a["semantic"])
                           for a in acts)
    else:
        act_txt = '<b>无</b>（收盘价未穿越任何触发位 —— 今日不需要动作）'

    inv = p.get("invalidations") or []
    inv_txt = "；".join("<b>%s</b>：%s" % (_money(i["price"]), i["text"]) for i in inv) or "无"

    notes = "".join("<li>%s</li>" % esc(n) for n in p.get("notes") or [])

    band_txt = ("<b>%s ~ %s</b>（T+1 %s · 单日 point-in-time 实测覆盖 <b>93.2%%</b>，名义 90%%）"
                % (_money(band["low"]), _money(band["high"]), band.get("date") or "?")
                if band else
                "<b>未取到</b>（页面内联推演数据缺失 ⇒ 本图不画该带）")

    # R539：meta 由纯文本行改为**状态徽章**（与全站 panel 标题旁的 chip 同风格）
    chg = float(p.get("chg_pct") or 0.0)
    chg_cls = "plan-chip--up" if chg > 0 else ("plan-chip--dn" if chg < 0 else "")

    return """
<section class="panel" %s>
  <h2>明日关键价位 · 条件预案 <span class="plan-sub">%s · 数据截至 %s 收盘 · 只讲「到哪个价会怎样」，不预测涨跌</span></h2>
  <div class="plan-meta">
    <span class="plan-chip %s">收盘 <b>%s</b>（%s）</span>
    <span class="plan-chip plan-chip--warn">情景 <b>%s</b></span>
    <span class="plan-chip plan-chip--info">位置 <b>%s</b></span>
    <span class="plan-chip">中枢 <b>%s</b></span>
  </div>
  <div class="plan-viz">
  <div class="plan-box">%s</div>
  <div class="plan-side">
  <div class="plan-tip">读法（<b>价格水位图</b>）：竖轴是价格。灰蓝色块 = 当前中枢 ZD~ZG；琥珀色块 = 明日九成区间；深色虚线 = 现价；右侧红 / 绿刻度 = 上方 / 下方关键位（价位 + 距现价 + 名称）；<b>紫色小标 = 买卖点触发位</b>。鼠标悬停任一元素看来源与含义。<br>
    明日九成区间 %s。<br>
    <b>本图不画 P25–P75</b>：它在单日尺度实测只覆盖约 72.5%%（名义 50%%），口径标不住 ⇒ 不画。</div>
  %s
  %s
  %s
  <div class="plan-foot">
    <div>今日动作：%s</div>
    <div>判据失效条件：%s</div>
  </div>
  </div>
  </div>
  <div class="plan-note"><b>怎么用</b>：本区块只把<b>已有的结构位</b>摆成一排，标出每个位对应什么状态变化。
    幅度请自行决定（不提供仓位比例）。<ul>%s</ul></div>
</section>
""" % (MARK, esc(p.get("name") or ""), esc(p.get("asof") or ""),
       chg_cls, _money(p.get("close") or 0), _sign(chg),
       esc(p.get("scenario") or "—"), esc(p.get("position") or "—"), ztxt,
       _chart(p, band), band_txt, _trg_list(p), build_details(p), build_signals(p.get("signals")),
       act_txt, inv_txt, notes)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "index.html")
    if not os.path.exists(src):
        print("ERROR 源 index.html 不存在: %s" % src, file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(PLAN):
        print("WARN 未找到 %s（gen_plan.py 未跑或数据不足）⇒ 跳过注入, 不阻断部署" % PLAN,
              file=sys.stderr)
        return 0

    with open(PLAN, encoding="utf-8") as f:
        p = json.load(f)
    with open(src, encoding="utf-8") as f:
        html = f.read()

    if MARK in html:
        print("WARN 源 %s 已含关键价位区块, 跳过注入(防双份 CSS/区块)" % src, file=sys.stderr)
        return 0

    band = extract_band(html)
    block = build(p, band)

    # 1) CSS 注入到 </head> 前
    i = html.find("</head>")
    if i > 0:
        html = html[:i] + PLAN_CSS + html[i:]
    else:
        print("WARN 未找到 </head>, 样式追加到文件头", file=sys.stderr)
        html = PLAN_CSS + html

    # 2) 区块注入到 s1 标题前（紧跟「今日结构信号」条）
    j = html.find(ANCHOR)
    if j > 0:
        html = html[:j] + block + "\n" + html[j:]
    else:
        print("WARN 未找到锚点 %s, 区块追加到 <body> 后" % ANCHOR, file=sys.stderr)
        b = html.find("<body")
        k = html.find(">", b) + 1 if b >= 0 else 0
        html = html[:k] + block + html[k:]

    # 3) 自证契约：注入后必须能读出区块与数据日
    assert MARK in html, "注入后未检出区块标记"
    assert (p.get("asof") or "") in html, "注入后未检出数据日"
    tmp = src + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, src)
    print("index.html 就绪(关键价位区块=有, asof=%s, 图=水位图, 九成区间=%s), %d KB"
          % (p.get("asof"), ("%s~%s" % (_money(band["low"]), _money(band["high"])))
             if band else "未取到", os.path.getsize(src) // 1024))


if __name__ == "__main__":
    main()
