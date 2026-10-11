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
.plan-h { font-size: 16px; margin-bottom: 2px; }
.plan-sub { font-size: 12px; color: #64748b; font-weight: 400; }
.plan-meta { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 13px; color: #475569; margin: 10px 0 6px; }
.plan-meta b { color: #1f2937; font-variant-numeric: tabular-nums; }
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
/* ===== plan: 价格水位图（纯 SVG · 无 JS · 无外部请求）===== */
.plan-chart { display: block; width: 100%; max-width: 460px; height: auto; margin: 2px 0 0; }
/* 宽屏：图在左、读法与依据在右（纵向价格尺不宜拉宽，右侧留给文字，避免大片留白） */
.plan-viz { display: flex; flex-wrap: wrap; gap: 2px 26px; align-items: flex-start; }
.plan-viz > .plan-chart { flex: 0 0 420px; width: 420px; max-width: 100%; }
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
_CH_W, _CH_H = 300, 486
_CH_PT, _CH_PB = 30, 396        # 绘图区上下沿
_CH_AX = 54                     # 价格轴竖线 x
_CH_BX1 = 176                   # 带区右沿（左沿 = 价格轴）
_CH_MK = 182                    # 关键位刻度线终点
_CH_LB = 186                    # 关键位标签起始 x
_CH_TICK = 46                   # 价格刻度文字右对齐 x
_CH_LEG = (418, 434, 450, 466)  # 图例各行基线 y
_C_UP, _C_DN, _C_CLOSE = "#e54545", "#18a058", "#334155"
_C_GRID, _C_AXIS = "#eef2f7", "#cbd5e1"
_C_B95, _C_B95S = "#fbead0", "#dfb571"
_C_ZS, _C_ZSS = "#e9eef7", "#c3cfe0"


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


def _lv_name(x):
    """关键位名称：只取 label 的第一段（` / ` 分隔），余下加省略号；
    全文在 SVG 悬停 title 与折叠表里，不丢信息。"""
    parts = [p.strip() for p in str(x.get("label") or "").split(" / ") if p.strip()]
    txt = parts[0] if parts else ""
    if len(parts) > 1:
        txt += "\u2026"
    st = x.get("state")
    if st in ("near", "up_cross", "dn_cross"):
        txt += "（%s）" % _STATE[st]
    return _clip(txt, 11.6)


def _chart(p, band):
    """价格水位图。竖轴 = 价格；所有数字来自 plan.json / index.html 内联数据。"""
    zs = p.get("zhongshu") or {}
    close = float(p.get("close") or 0.0)
    ups = list(p.get("up") or [])
    dns = list(p.get("dn") or [])

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
    for i, x in enumerate(ups + dns):
        col = _C_UP if i < len(ups) else _C_DN
        y = py(x["price"])
        o.append('<g class="plan-mk">')
        o.append('<title>%s</title>' % esc(
            "%.2f  %s\n距现价 %s\n来源：%s\n收盘到该位：%s"
            % (float(x["price"]), x.get("label") or "", _sign(x.get("dist_pct") or 0),
               x.get("src") or "", x.get("semantic") or "")))
        o.append('<line x1="%g" y1="%.1f" x2="%g" y2="%.1f" stroke="%s" stroke-width="2"/>'
                 % (_CH_BX1, y, _CH_MK, y, col))
        o.append('<circle cx="%g" cy="%.1f" r="3" fill="%s"/>' % (_CH_MK, y, col))
        o.append('<text x="%g" y="%.1f" font-size="11.5" font-weight="700" fill="%s">%s</text>'
                 % (_CH_LB, y - 3, col, _money(x["price"])))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#94a3b8">%s</text>'
                 % (_CH_LB + 53, y - 3, esc(_sign(x.get("dist_pct") or 0))))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#94a3b8">%s</text>'
                 % (_CH_LB, y + 12, esc(_lv_name(x))))
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
    return """
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
""" % (tot.get("笔", 0), tot.get("段", 0), esc(lv_txt),
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

    return """
<section class="panel" %s>
  <h3 class="plan-h">明日关键价位 · 条件预案 <span class="plan-sub">%s · 数据截至 %s 收盘 · 只讲「到哪个价会怎样」，不预测涨跌</span></h3>
  <div class="plan-meta">
    <span>收盘 <b>%s</b>（<b>%s</b>）</span>
    <span>情景 <b>%s</b></span>
    <span>位置 <b>%s</b></span>
    <span>中枢 <b>%s</b></span>
  </div>
  <div class="plan-viz">
  %s
  <div class="plan-side">
  <div class="plan-tip">读法（<b>价格水位图</b>）：竖轴是价格。灰蓝色块 = 当前中枢 ZD~ZG；琥珀色块 = 明日九成区间；深色虚线 = 现价；右侧红 / 绿刻度 = 上方 / 下方关键位（价位 + 距现价 + 名称，鼠标悬停看来源与含义）。<br>
    明日九成区间 %s。<br>
    <b>本图不画 P25–P75</b>：它在单日尺度实测只覆盖约 72.5%%（名义 50%%），口径标不住 ⇒ 不画。</div>
  %s
  <div class="plan-foot">
    <div>今日动作：%s</div>
    <div>判据失效条件：%s</div>
  </div>
  </div>
  </div>%s
  <div class="plan-note"><b>怎么用</b>：本区块只把<b>已有的结构位</b>摆成一排，标出每个位对应什么状态变化。
    幅度请自行决定（不提供仓位比例）。<ul>%s</ul></div>
</section>
""" % (MARK, esc(p.get("name") or ""), esc(p.get("asof") or ""),
       _money(p.get("close") or 0), _sign(p.get("chg_pct") or 0),
       esc(p.get("scenario") or "—"), esc(p.get("position") or "—"), ztxt,
       _chart(p, band), band_txt, build_details(p), act_txt, inv_txt,
       build_signals(p.get("signals")), notes)


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
