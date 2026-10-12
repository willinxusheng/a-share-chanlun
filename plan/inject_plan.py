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

R542（深挖 bug 修复 · 全部由故障注入实测触发）：
  ① **缺口带不进量程**（R541 连带回归）：R541 把缺口从点型候选移出 up/dn 后，`_chart` 的
     `vals` 忘了纳入 `gap_bands` ⇒ 当缺口落在所有关键位之外时（暴跌后留一串跳空，完全可达）
     py() 给出绘图区外 y，整条带与标签画到图外（实测 y=-738 / y=1338）。修：量程改为
     「所有会被绘制的值」的集合，并新增 `_selfcheck_geometry()` 对**产物本体**做永久护栏。
  ② **压力/支撑分类两套判据**（规则 111）：图上按几何、右侧列表按 `dist_pct` 中点符号 ⇒
     带横跨现价时同一条缺口给出相反结论（引擎可达：未补的成立条件只看 high/low）。修：
     生成侧判一次落盘（`side`/`dir_label`/`side_label`），渲染侧唯一入口 `_gap_side()`。
  ③ **缺口带标签与现价徽章重叠**（跨现价时 48.4×8.2 px）：标签改右对齐到带区右沿内侧。
  ④ **合并组内成员价位被静默改写**：代表价只取首个候选 ⇒ 实测 MA60 真实 3,897.68 被展示成
     3,892.75（差 4.93 点）、MA120/MA250 差 2.18/4.40 点，与 R541 修的缺口失真同源。修：
     生成侧落盘 `seg_prices`，右侧逐段标出各体系自身价位。
  ⑤ **避让算法容量**：档数×间距超可用高度时贪心避让必然重新重叠（图会静默压字）⇒ 加容量
     WARN + verify 判据 18。

R541（旭总 2026-10-12 三条要求）：
  ① 删掉区块底部那段「怎么用」说明（plan-note）：原 6 条各有更细的载体，只有「同源同口径」
     一条没有他处显式声明 ⇒ 压成一行 .plan-ref 保留（规则 107 披露不得静默丢失）。
  ② 右侧新增「图上点位含义」：图上标签区仅 114px，只能显示缩写（「GG · 顶背…」），
     而这些术语对非专业读者等于黑话，且**手机没有 hover**（title 里的全文结构上读不到）
     ⇒ 逐档写全「这个位是什么 + 到它会怎样」，并与图上新增的带圈序号 ①②③… 一一对应。
  ③ 顺带补齐 R540 导出却**零渲染**的维度（ma.alignment / bias）、新增中枢内分位（pos_pct），
     并把未补缺口由「两条点型刻度」改为「区间带」—— 后者是**修 R540 的一处数字失真**：
     9/28 缺口真实为 [3878.41, 3888.37]，上沿 3888.37 与 ZG 3884.44 相差 0.10%，
     落在 DEDUP_PCT(0.15%) 内被合并 ⇒ 标签曾写成「3,884.44 = 缺口上沿 09-28」。

R538：把「关键价位表」改为**价格水位图**（纯 SVG，无 JS、无外部请求）
  → 图为准，原表折叠进 <details>（保留「结构来源」依据，规则 107 披露纪律：不静默丢信息）。
  → 「明日九成区间」从**已生成的 index.html 内联推演数据**同源提取（规则 111：同一事实单一来源，
     不重算）。提取失败则**不画带**、不阻断部署。
  → **不画 P25–P75**：单日覆盖率实测 72.5%（上证），与名义 50% 严重不符 ⇒ 标不出可靠口径。
     九成区间（P05–P95）实测 93.2%，过门 ⇒ 画。
"""
import json
import os
import re
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
/* R541：原 `.plan-note`（「怎么用」+ 6 条说明）**已删除** —— 旭总 2026-10-12 反馈冗长。
   删除前逐条核过承载方（规则 107：披露不得静默丢失，见 gen_plan.py 同处注释）：
   asof 快照 → 副标题 + 图内日期；同源同口径 → 下面这行 .plan-ref；前置条件（三类需新中枢 /
   二类需一类先出）→ 折叠块「下一信号 · 触发条件」逐条列出；不构成买卖建议 → 全站「六、免责说明」。
   ⇒ 无信息丢失，只是不再用一段说明文字占版面。 */
.plan-ref { margin-top: 6px; font-size: 12px; color: #94a3b8; line-height: 1.7; }
/* 图上点位含义（R541）：与图上带圈序号 ①②③… 一一对应 —— 图上只能放缩写（标签区仅 114px），
   含义放在模块右侧逐段写全，解决「看图看不懂 GG / ZG / MA20 是什么」。 */
.plan-lvl { margin: 4px 0 0; padding: 0; list-style: none; }
.plan-lvl > li { padding: 7px 0; border-bottom: 1px dashed #f1f5f9; }
.plan-lvl-h { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; }
.plan-lvl-i { font-size: 13px; color: #94a3b8; }
.plan-lvl-n { font-weight: 700; font-size: 14px; font-variant-numeric: tabular-nums; }
.plan-lvl--up .plan-lvl-n { color: #e54545; }
.plan-lvl--dn .plan-lvl-n { color: #18a058; }
.plan-lvl-d { font-size: 12px; color: #64748b; font-variant-numeric: tabular-nums; }
.plan-lvl-t { font-size: 11px; padding: 1px 7px; border-radius: 999px;
  background: #f3e8ff; border: 1px solid #d8b4fe; color: #7c3aed; font-weight: 600; }
.plan-lvl-dl { margin: 5px 0 0; font-size: 12.5px; line-height: 1.65; }
.plan-lvl-dl dt { color: #475569; font-weight: 600; margin-top: 4px; }
.plan-lvl-dl dt:first-child { margin-top: 0; }
.plan-lvl-dl dd { margin: 0; color: #64748b; padding-left: 12px; }
/* R542：段（成员）**自身**价位。同一档常由多个体系共同确认，展示价只取首选来源 ⇒
   其余成员的价位与它相差 0.05%~0.13%（≈2~5 点），必须逐段标出，否则「站上哪个数」会判错。 */
.plan-lvl-mp { margin-left: 5px; font-variant-numeric: tabular-nums; color: #334155;
  font-weight: 700; font-size: 12px; }
/* 未补缺口带（R541）：区间型对象，不与点位序号共用 —— 用 ▨ 与「压力/支撑」标签区分 */
.plan-gap-i { font-size: 12px; color: #94a3b8; }
.plan-gap--res .plan-lvl-t { background: #fdecec; border-color: #eab8b8; color: #c22f2f; }
.plan-gap--sup .plan-lvl-t { background: #eefaf1; border-color: #a9dcbb; color: #12874b; }
/* R542：横跨现价的缺口带 —— 既非纯压力亦非纯支撑，用中性色，避免读者按「压力」去用 */
.plan-gap--cross .plan-lvl-t { background: #f1f5f9; border-color: #cbd5e1; color: #475569; }
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
/* 宽屏：右侧「点位含义 8 档 + 缺口带 2 条」竖排会到 ~1740px，而左图只有 ~730px
   ⇒ 左图下方一大片留白。≥1200px 时这两个列表改**双列**，侧栏高度近乎减半。
   ★ 列数由探针判据 17 自证：grid 计算列数必须 == min(2, 档数) —— 防「基础规则改了、
     覆盖规则没跟」把子元素挤进隐式列/行（跨项目通用坑）。 */
@media (min-width: 1200px) {
  .plan-side > .plan-lvl { display: grid; grid-template-columns: 1fr 1fr;
    gap: 0 22px; align-content: start; }
}
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
_CH_W, _CH_H = 300, 514         # R540: 486→500（图例加「买卖点触发位」）· R541: →514（加「缺口带」）
_CH_PT, _CH_PB = 30, 396        # 绘图区上下沿
_CH_AX = 54                     # 价格轴竖线 x
_CH_BX1 = 176                   # 带区右沿（左沿 = 价格轴）
_CH_MK = 182                    # 关键位刻度线终点
_CH_LB = 186                    # 关键位标签起始 x
_CH_TICK = 46                   # 价格刻度文字右对齐 x
_CH_LEG = (418, 434, 450, 466, 482, 500)  # 图例各行基线 y（R541 加「缺口带」一行）
_C_UP, _C_DN, _C_CLOSE = "#e54545", "#18a058", "#334155"
_C_GRID, _C_AXIS = "#eef2f7", "#cbd5e1"
_C_B95, _C_B95S = "#fbead0", "#dfb571"
_C_ZS, _C_ZSS = "#e9eef7", "#c3cfe0"
# 买卖点触发标记：**刻意用红绿之外的第二色系**（红=上方/绿=下方 的价位语义不能被买卖点语义污染）
_C_TRG, _C_TRGB, _C_TRGS = "#7c3aed", "#f3e8ff", "#d8b4fe"
# 未补缺口带（R541）：缺口是**区间**不是点。带色沿用「红=上方 / 绿=下方」的既有语义，
# 但取极淡填充 + 虚线描边，与「九成区间（琥珀实底）」「中枢（灰蓝实底）」区分开。
_C_GAP_F, _C_GAP_S = "#fdecec", "#eab8b8"      # 上方 ⇒ 压力带
_C_SUP_F, _C_SUP_S = "#eefaf1", "#a9dcbb"      # 下方 ⇒ 支撑带

# R542：关键位**标签块**的垂直占位与避让可用区间 —— 抽成常量 + 单一函数，
# 供 `_chart`（实际下夹/上夹）与 `verify_inject` 判据 18（容量）**共用**。
# ★ 为什么必须收紧：R540/R541 的下夹边界取 `_CH_PB + 4 = 400`，只考虑了「绘图区底」，
#   没算**图例块**（首行基线 418、字号 9.5 ⇒ 文字顶 410.2）。标签块名称行基线 = ym+12、
#   下缘 ≈ ym+14.2 ⇒ ym=400 时底到 414.2，压进图例首行 3.9px（故障注入实测抓出）。
#   上夹边界同样只用了 14，未算**图内日期行**（基线 13、下缘 15.1、且它与标签同处 x≥186
#   横向重叠）⇒ 标签被推到顶部时会与「数据 YYYY-MM-DD 收盘」叠字。
_LBL_TOP = 12.4                                 # 标签块上缘相对 ym 的偏移（价格行基线 ym−3 − 上缘）
_LBL_BOT = 14.2                                 # 标签块下缘相对 ym 的偏移（名称行基线 ym+12 + 下缘）
_MIN_MK_GAP = _LBL_TOP + _LBL_BOT + 1.0         # 两档标签块**最小**可辨间距（硬容量用，27.6px）
_DATE_Y = 13.0                                  # 图内日期行基线
# R543 副产物（必修）：★ 上/下缘系数原为 0.82 / 0.22（合计 1.04em），但浏览器**实渲染**
#   单行文字占高实测（无头 Chrome + Range.getBoundingClientRect，fs=9.5，3 档视口）为：
#     1440 / 1024 档：ascent 1.058em + descent 0.226em = **1.284em**
#     390       档：ascent 1.075em + descent 0.294em = **1.369em**（小屏 px 取整放大）
#   ⇒ 每行**少算 2.32~3.13 属性单位**。这是「模型自洽、实渲染不符」的典型：
#     R542 的随机 fuzz 用**同一套占高模型**自校验 ⇒ 300/500 例全过，浏览器里却叠字。
#   后果（2026-10-12 线上产物，探针判据 20 抓出 3 对，各重叠 2px）：
#     ①「九成区间」×「中枢 ZD~ZG」—— 两者基线差 10.9 > 模型占高 9.88 ⇒ 模型判「净间隙 1.02」，
#       而实渲染占 12.35 ⇒ 真重叠 1.45 单位；
#     ②「DD」（关键位标签块名称行）、③「△一类买」（触发标）× 图例首行「九成区间 P05-P95…」——
#       标签块被下夹夹到 bot 界（ym=395.01）时，名称行字形底 409.16 > 图例字形顶 407.95 ⇒ 叠 1.21 单位。
#   修法：把系数标定为**实渲染值 + 余量**（1.12 / 0.34 ⇒ 合计 1.46em ≥ 最坏档 1.369）。
#   单一来源 ⇒ `_label_limits()` / `_anno_limits()` / `_place_col()` 三处自动同口径，
#   无需各自打补丁；且**只在「几乎挨着」时生效** ⇒ 原本满足净间隙的候选不动（默认形态零位移）。
_FS_DESC = 0.34                                 # 下缘系数（实渲染 ≈0.226~0.294em，取 0.34 含余量）
_FS_ASC = 1.12                                  # 上缘系数（实渲染 ≈1.058~1.075em，取 1.12 含余量）


def _label_limits():
    """标签避让的 [上界, 下界]（ym 允许范围，px）。单一来源：图上夹/下夹与 verify 容量同用。"""
    top = _DATE_Y + 9.5 * _FS_DESC + 1.0 + _LBL_TOP          # 日期行下缘 + 净间隙 1px + 块高
    bot = (_CH_LEG[0] - 9.5 * _FS_ASC) - 1.0 - _LBL_BOT      # 图例首行文字顶 − 净间隙 1px − 块高
    return top, bot


def _anno_limits(fs=9.5):
    """**单行标注文字**的基线可用范围（px）：上方避开图内日期行，下方避开图例块。
    与 `_label_limits()`（多行「关键位块」口径）刻意分开 —— 两者占高不同（块 ≈ 26.6px、
    单行 ≈ fs），混用一个界会把单行标签压得过紧（跨块净间隙以各自真实占高算，规则 166）。"""
    return (_DATE_Y + 9.5 * _FS_DESC + 1.0 + fs * _FS_ASC,
            (_CH_LEG[0] - 9.5 * _FS_ASC) - 1.0 - fs * _FS_DESC)


def _place_col(items, obstacles, lo, hi):
    """把**同一列**的单行文字按候选基线的优先级逐个落位，保证互不重叠（含与固定障碍）。

    items: [{"cands": [候选基线（按偏好排序）], "fs": 字号}, ...]（顺序 = 优先级）
    obstacles: [(top, bottom)] 固定障碍（现价徽章 —— 它的位置有语义，不能挪）
    返回与 items 等长的基线列表。纯字典序遍历 ⇒ 同一输入必得同一张图（可复现）。

    ★ 为什么需要它（R542 第 6 个 bug）：图上三处标注（九成区间 / 中枢 ZD~ZG / 缺口带）
      各自只按「自己矩形的顶边 + 13/19px」定位，而它们与现价徽章在 **x 方向重叠**
      （徽章 x 59~111、左列标注 x 60~108）⇒ 只要两个矩形靠得近，文字就叠在一起。
      实测可达性很高：收盘落在「ZG 下方 1.8~31.8 点」区间时，中枢标注与现价徽章必然重叠
      —— 而「中枢内部偏强」正是最常见的形态之一（按均匀分布算约每 4 个交易日出现一次）。
      随机 fuzz 300 例中 298 例能复现本族重叠（含「两个缺口带日期标签互压」）。
    """
    placed = list(obstacles)
    out = []
    for it in items:
        fs = it["fs"]
        up, dn = fs * _FS_ASC, fs * _FS_DESC
        best, best_ov = None, None
        for y in it["cands"]:
            if y - up < lo - 0.4 or y + dn > hi + 0.4:
                continue                                  # 越出该列可用范围 ⇒ 跳过
            ov = sum(max(0.0, min(y + dn, b) - max(y - up, t)) for t, b in placed)
            if ov <= 0.0:
                best, best_ov = y, 0.0
                break                                     # 首个不重叠的候选即采纳（保偏好序）
            if best_ov is None or ov < best_ov:
                best, best_ov = y, ov                     # 全部有冲突 ⇒ 取冲突最小者
        if best is None:                                  # 所有候选都越界 ⇒ 夹进范围
            best = min(max(it["cands"][0], lo + up), hi - dn)
        out.append(best)
        placed.append((best - up, best + dn))
    return out


def _cands(t, b, base_off, fs, n=3):
    """标注文字的候选基线序列：默认位 → 同侧逐步下移/上移 → 矩形底边内侧。
    偏好序 = 「离自己的矩形顶边最近」优先，使默认形态与历史一致（不无谓挪动）。"""
    step = fs + 3.0
    ys = [t + base_off]
    for k in range(1, n + 1):
        ys += [t + base_off + k * step, t + base_off - k * step]
    ys += [b - fs * _FS_DESC - 2.0, b - fs * _FS_DESC - 2.0 - step]
    return ys


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


# R541: 带圈序号 ①..⑩ —— 图上名称行前缀，与模块右侧「图上点位含义」逐条对应。
# ★ 为什么需要序号：图上标签区只有 x=186~300（114px），只能放缩写（「GG · 顶背…」），
#   而 8 档里 3,884.44 与 3,878.41 只差 6 元 —— 肉眼极易看串行。显式序号把「图上这个标」
#   与「右侧这条含义」钉死，而不是要求读者自行比对价位（可读性必须自证，规则 166 同族）。
_NUM = "\u2460\u2461\u2462\u2463\u2464\u2465\u2466\u2467\u2468\u2469"   # ①..⑩


def _lv_name(x, limit=11.6):
    """关键位名称：图上显示**前两段**（` / ` 分隔，段名经 _abbr 缩写），余下加省略号；
    全文在 SVG 悬停 title、折叠表、以及右侧「图上点位含义」里，不丢信息。
    limit 可调：该档若还要挂「买卖点触发」小标，需让出宽度。

    ★ R541 自纠：带圈序号**不放这一行**。首版把序号加在名称行前缀（limit 11.6→10.4），
      实测 3,995.18 那档因为还挂着触发标（limit 再降到 5.4），可用宽度只剩 4.4 个单位，
      名称退化成「GG · …」——比加序号**之前**（「GG · 顶背…」）信息更少。改为把序号放到
      价格行前（`① 3,995.18 +4.76%`，那行本来就有 ~15px 余量）⇒ 名称行预算不受影响。

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


def _lv_items(p):
    """图上点位的**绘制顺序**：上方（价格降序）在前、下方在后 = `_chart` 里 items 的顺序。

    序号（图上名称行的 ①②③… 与右侧含义列表）依赖这个顺序 ⇒ 两处必须同源，
    故抽成单一函数（规则 111：同一事实单一来源；顺序若各写一份，加一个维度就会错位）。
    """
    return list(p.get("up") or []) + list(p.get("dn") or [])


# R542：缺口带相对现价分类的**唯一入口**（图上矩形、图上文字、右侧列表条目类名、右侧标签
# 全部经此函数取值）。此前两套判据并存且结论可相反 —— 图上按几何、列表按 dist_pct 中点符号；
# 带横跨现价时必然打架（见 gen_plan.gap_bands 处注释：该状态引擎**可达**）。
# 优先读生成侧写入的 `side`（单一来源），缺失时按**同一几何规则**兜底（边界闭区间，
# 与生成侧逐字同源）；两处都不看 `dist_pct`，因为它是带**中点**的口径，与「带相对现价的位置」
# 不是同一个量 —— 用中点代表整条带正是最初那处 bug 的根因。
_GAP_SIDE_LABEL = {"res": "压力", "sup": "支撑", "cross": "跨现价"}


def _gap_side(g, close):
    s = g.get("side")
    if s in _GAP_SIDE_LABEL:
        return s
    try:
        t, b = float(g["top"]), float(g["bottom"])
    except (KeyError, TypeError, ValueError):
        return "cross"
    if b >= close:
        return "res"
    return "sup" if t <= close else "cross"


def _seg_pairs(x):
    """把 label / semantic / **各段自身价位** 按段配对 => [(段名, 含义, 段自身价), ...]。

    gen_plan 里 `labels` 与 `kinds`、`prices` 由 _dedup **同步 append**，`semantic` 由同一
    `kinds` 逐项映射 ⇒ 正常情况下三列段数相等、顺序一致（gen_plan.pack 内有断言锁住该契约）。
    仍做防御：段数不等时多出的段**单列**，既不丢文字也不硬凑。
    """
    labs = [s.strip() for s in str(x.get("label") or "").split(" / ") if s.strip()]
    sems = [s.strip() for s in str(x.get("semantic") or "").split(" ｜ ") if s.strip()]
    prs = list(x.get("seg_prices") or [])
    def _p(i):
        try:
            return float(prs[i])
        except (IndexError, TypeError, ValueError):
            return None
    out = [(lb, sems[i] if i < len(sems) else "", _p(i)) for i, lb in enumerate(labs)]
    out += [("", s, None) for s in sems[len(labs):]]
    return out


_MA_PREFIX = "均线位（"


def _sem_clean(seg):
    """含义段的**机械去重**：段名已经写了「MA120 半年线」，含义段再写一遍「均线位（MA120 ·
    半年线）」纯属重复。只做「剥掉『均线位（…）』前缀」这一种可解释操作 —— 不改写、不编造，
    剥完为空就不显示这一行（信息在段名里已经完整）。
    """
    s = str(seg or "").strip()
    if s.startswith(_MA_PREFIX):
        k = s.find("\uff09")                       # 全角右括号
        if k >= 0:
            s = s[k + 1:].strip()
            s = s[1:].strip() if s[:1] in ("\uff1a", "\uff0c") else s   # 去掉孤零零的「：」
    return s


def _lv_meaning(p):
    """右侧「图上点位含义」（R541 · 旭总：「图片上每一个点位的含义在这个模块右侧写上含义，
    不然我看不懂」）。

    为什么必须有：图上标签区仅 114px 宽，只能显示缩写（「GG · 顶背…」「Fib 50%」）；
    而这些术语（GG/ZG/MA/Fib）对非专业读者等于黑话；悬停 title 里虽有全文，但**手机上
    没有 hover** ⇒ 结构上不可读。故把「这个位是什么（段名）+ 到它会怎样（含义）」常显在
    模块右侧，逐档一条、序号与图上带圈数字对应。

    ★ 全部文字取自 plan.json 的 label / semantic / dist_pct（规则 149：区块内不写统计值、
      不编文案）；本函数只做**排版拆分**与一处机械去重（_sem_clean）。
    """
    items = _lv_items(p)
    if not items:
        return ""
    trg = _triggers(p)
    n_up = len(p.get("up") or [])
    lis = []
    for i, x in enumerate(items):
        t = trg.get(round(float(x["price"]), 2))
        tag = ('<span class="plan-lvl-t">%s</span>' % esc(_trig_short(t))) if t else ""
        body = ""
        for lb, sm, pv in _seg_pairs(x):
            sm = _sem_clean(sm)
            if lb:
                # R542：段的**自身价位**与组代表价不同时逐段标出（差 0.05%~0.13% ≈ 2~5 点，
                # 对「收盘站上 X」判触发足以给出不同结论 —— 见 gen_plan._dedup 注释）。
                # 判据用「四舍五入到分是否相等」而非拍一个百分比阈值：无魔数、且与展示精度一致。
                ext = ""
                if pv is not None and round(pv, 2) != round(float(x["price"]), 2):
                    ext = '<span class="plan-lvl-mp">%s</span>' % _money(pv)
                body += "<dt>%s%s</dt>" % (esc(lb), ext)
            if sm:
                body += "<dd>%s</dd>" % esc(sm)
        lis.append(
            '<li class="plan-lvl--%s">'
            '<div class="plan-lvl-h"><span class="plan-lvl-i">%s</span>'
            '<b class="plan-lvl-n">%s</b><span class="plan-lvl-d">%s</span>%s</div>'
            '<dl class="plan-lvl-dl">%s</dl></li>'
            % ("up" if i < n_up else "dn", _NUM[i] if i < len(_NUM) else "",
               _money(x["price"]), esc(_sign(x.get("dist_pct") or 0)), tag, body))
    # R542：把「有多少段与组代表价不同、差多少点」**从数据算出来**再写文案 ——
    # 不手写「2~5 点」这类描述（规则 149：渲染层不编写统计值；也避免数据变了文案不变）。
    _n_seg = sum(len(_seg_pairs(x)) for x in items)
    _offs = [abs(_pv - float(x["price"])) for x in items
             for _lb, _sm, _pv in _seg_pairs(x)
             if _lb and _pv is not None and round(_pv, 2) != round(float(x["price"]), 2)]
    tip = ""
    if _offs:
        tip = ('<div class="plan-tip">同一价位由多个体系共同确认时已并列（图上只放得下前两段缩写，'
               '此处逐段写全）。<b>段名后标出的数字 = 该体系自己的价位</b>：%d 段中有 %d 段与组代表价'
               '不同，各差 <b>%.2f ~ %.2f 点</b>（代表价取该档首选来源）⇒ 判「站上哪个数」时请用段'
               '自己的价位。</div>'
               % (_n_seg, len(_offs), min(_offs), max(_offs)))
    return ("""
  <div class="plan-sec">图上点位含义 <span>（序号与图上标注一一对应 · %d 个：上方 %d / 下方 %d）</span></div>
  <ul class="plan-lvl">%s</ul>%s"""
            % (len(items), n_up, len(items) - n_up, "".join(lis), tip))


def _gap_list(p):
    """右侧「未补缺口带」（R541）。

    缺口是**区间**不是点（见 gen_plan.collect 注释：当点处理会把真实上沿 3888.37 折进
    ZG 3884.44）。所以它不占关键位序号，单独成节：区间 + 方向 + 跳空幅度 + 上下沿含义。
    全部字段取自 plan.json 的 gap_bands（含语义文案，不在渲染层编写）。

    R542：压力/支撑/跨现价的分类与方向文案**不再在本层现算** —— 一律经 `_gap_side()` 取
    （图上矩形、图上文字、这里条目类名与标签共用同一判据，规则 111）。此前本层按 `dist_pct`
    中点符号判，与图上的几何判据**在带横跨现价时结论相反**（实测可复现）。
    """
    gb = p.get("gap_bands") or []
    if not gb:
        return ""
    close = float(p.get("close") or 0.0)
    lis = []
    for g in gb:
        _side = _gap_side(g, close)
        _is_res = (_side == "res")
        _head = ('<div class="plan-lvl-h">'
                 '<span class="plan-gap-i">%s</span>'
                 '<b class="plan-lvl-n">%s ~ %s</b>'
                 '<span class="plan-lvl-d" title="＝缺口带中点距现价">%s</span>'
                 '<span class="plan-lvl-t">%s</span></div>')
        body = (
            "<dt>缺口上沿 %s</dt><dd>%s</dd>"
            "<dt>缺口下沿 %s</dt><dd>%s</dd>"
            "<dt>跳空幅度</dt><dd>%s%%（当日上沿~下沿之间没有成交）</dd>"
            % (_money(g["top"]), esc(g.get("sem_top") or ""),
               _money(g["bottom"]), esc(g.get("sem_bot") or ""),
               ("%.3f" % g["width_pct"]) if g.get("width_pct") is not None else "—"))
        # 方向与位置来自**生成侧**（`dir_label` / `side_label`）；仅为老产物兜底才在本层回退，
        # 且回退规则与生成侧逐字同源（type 缺省 = down、side 走 _gap_side）。
        _dl = g.get("dir_label") or ("向上跳空" if g.get("type") == "up" else "向下跳空")
        _sl = g.get("side_label") or _GAP_SIDE_LABEL[_side]
        tag = "%s · %s" % (esc(_dl), esc(_sl))
        lis.append('<li class="plan-gap--%s">%s<dl class="plan-lvl-dl">%s</dl></li>'
                   % (_side,
                      _head % ("\u25a8", _money(g["bottom"]), _money(g["top"]),
                               _sign(g.get("dist_pct") or 0), tag),
                      body))
    return ("""
  <div class="plan-sec">未补缺口带 <span>（图上淡色矩形带 · 区间不是点位 · %d 处）</span></div>
  <ul class="plan-lvl">%s</ul>""" % (len(gb), "".join(lis)))


def _chart(p, band):
    """价格水位图。竖轴 = 价格；所有数字来自 plan.json / index.html 内联数据。

    R542：量程（`vals`）必须**覆盖每一个会被绘制的值**。此前漏了 `gap_bands` 的上下沿 ——
    R541 把缺口从「点型候选」移出 up/dn 之后，缺口就不再进入 vals，于是**当缺口落在所有
    关键位之外时**（未补缺口完全可以远高于最高的关键位：暴跌后留下的一串跳空），
    py() 会给出绘图区外的 y，把整条带与它的标签画到图外（故障注入实测：y=-738 / y=1338）。
    这是 R541 引入的连带回归 —— 修法不是「再补一行」，而是把量程改为
    「**所有会被绘制的值**的集合」并由下面的断言自证，使同类漏项无法再悄悄发生。
    """
    zs = p.get("zhongshu") or {}
    close = float(p.get("close") or 0.0)
    ups = list(p.get("up") or [])
    dns = list(p.get("dn") or [])
    trg = _triggers(p)

    # 缺口带先解析一次，供量程 / 绘制 / 图例三处共用（避免各解析一遍而口径漂移）
    gbs = []
    for _g in (p.get("gap_bands") or []):
        try:
            gbs.append((float(_g["top"]), float(_g["bottom"]), _g))
        except (KeyError, TypeError, ValueError):
            continue

    vals = [close] + [float(x["price"]) for x in ups + dns]
    for k in ("zg", "zd", "gg", "dd"):
        try:
            vals.append(float(zs[k]))
        except (KeyError, TypeError, ValueError):
            pass
    if band:
        vals += [float(band["low"]), float(band["high"])]
    for _t, _b, _ in gbs:                     # R542：缺口带上下沿必须进量程
        vals += [_t, _b]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    sp = max(hi - lo, 1.0)
    lo -= sp * 0.06
    hi += sp * 0.05
    rg = max(hi - lo, 1e-6)

    def py(v):
        return _CH_PT + (hi - float(v)) / rg * (_CH_PB - _CH_PT)

    # 自证：量程内的值映射后必然落在绘图区内（由构造保证）。把它写成断言，是为了让
    # 「以后再加一种图元却忘了进 vals」当场炸，而不是又画到图外。
    for _v in (lo, hi):
        assert _CH_PT - 0.6 <= py(_v) <= _CH_PB + 0.6, \
            "量程自证失败：v=%.2f ⇒ y=%.1f 越出绘图区 [%d, %d]" % (_v, py(_v), _CH_PT, _CH_PB)

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
    # R542：步长随量程**自适应**。固定 50 在窄量程下正常（实测 8 档 + 缺口带 ⇒ 区间约 380 点
    # ⇒ 7~8 行），但量程纳入缺口带后区间可能显著变宽（远方的未补缺口，见上方注释）——
    # 366px 的绘图区塞几十行刻度会立刻文字重叠。取「行数 ≤ 9」的最小白名单步长：
    # 9 行 ⇒ 相邻基线间距 ≥ 366/9 ≈ 40px ≫ 9.5px 字号所需的 ~13px ⇒ 结构性不重叠
    # （不是「看起来还行」，而是由行数上限保证的）。
    span = hi - lo
    step = 5000.0
    for _s in (10.0, 20.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0, 1000.0, 2000.0, 5000.0):
        if span / _s <= 9:
            step = _s
            break
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

    # R542：三处固定位置标注（九成区间 / 中枢 / 缺口带）**先收集候选、最后统一落位**
    # （见 ④c 与 `_place_col`）—— 它们各自只看自己矩形顶边定位，而彼此/与现价徽章在
    # x 方向重叠 ⇒ 矩形靠近时必然叠字。分两列：左列 x≈60（与徽章 x 59~111 重叠）、
    # 右列右对齐 x≈172（与徽章水平分离，只彼此避让）。
    anno_l, anno_r = [], []

    # ② 明日九成区间（先画，位于中枢之下层）
    if band:
        y1, y2 = py(band["high"]), py(band["low"])
        o.append('<rect x="%g" y="%.1f" width="%g" height="%.1f" rx="3" fill="%s" '
                 'stroke="%s"><title>%s</title></rect>'
                 % (_CH_AX, y1, _CH_BX1 - _CH_AX, max(y2 - y1, 1.0), _C_B95, _C_B95S,
                    esc("明日九成区间 P05-P95（T+1 %s）：%s ~ %s｜单日 point-in-time 实测覆盖 93.2%%"
                        "（名义 90%%）" % (band.get("date") or "?", _money(band["low"]),
                                          _money(band["high"])))))
        anno_l.append({"cands": _cands(y1, y2, 13, 9.5), "fs": 9.5,
                       "txt": "九成区间", "fill": "#a16207", "halo": False})

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
        # R541 自纠（保留）：标签默认位由 y1+13 下移到 y1+19 —— 缺口带上沿高于 ZG 时，
        # 缺口带的下沿正好压在「中枢 ZD~ZG」顶部（实测重叠 2px）。
        # R542：该默认位只是**首选候选**，实际落位由 `_place_col` 与现价徽章统一避让。
        anno_l.append({"cands": _cands(y1, y2, 19, 9.5), "fs": 9.5,
                       "txt": "中枢 ZD~ZG", "fill": "#64748b", "halo": False})

    # ④ 现价（深色虚线 + 轴侧标签）；虚线只画到带区右沿，避免横穿右侧关键位标签
    yc = py(close)
    o.append('<line x1="%g" y1="%.1f" x2="%g" y2="%.1f" stroke="%s" stroke-width="1.4" '
             'stroke-dasharray="5 3"/>' % (_CH_AX, yc, _CH_BX1, yc, _C_CLOSE))
    o.append('<rect x="%g" y="%.1f" width="52" height="19" rx="4" fill="%s" opacity="0.92"/>'
             % (_CH_AX + 5, yc - 9.5, _C_CLOSE))
    o.append('<text x="%.1f" y="%.1f" font-size="11" font-weight="700" fill="#ffffff" '
             'text-anchor="middle">%s</text>'
             % (_CH_AX + 31, yc + 4, _money(close)))

    # ④b 未补缺口**带**（R541）：缺口是一个**区间**（上沿~下沿），不是一个点。此前图上只画
    #     两条独立刻度 ⇒ 会被读成「两个互不相关的位」，而它其实是同一根 K 线跳空留下的
    #     一段真空区。带色按**相对现价的位置**取（生成侧 `side`，见 `_gap_side` 注释）：
    #       整条带在现价上方 ⇒ 压力带（淡红）；在下方 ⇒ 支撑带（淡绿）；横跨现价 ⇒ 中性灰。
    #     上下沿的刻度线与标签照旧画 —— 它们是独立价位锚，带只补上「这是一个区域」这层含义。
    #     R542 自纠：标签由 x=_CH_AX+6（左对齐）改为**右对齐到带区右沿内侧** —— 跨现价的带
    #     其标签 y 恰好落在现价徽章那一行，左对齐时与徽章文字重叠 48.4×8.2 px（故障注入实测）；
    #     右对齐后与徽章（x 59~111）在水平方向彻底分离，且白描边仍压在色带上清晰可读。
    for _t, _b, _g in gbs:
        _y1, _y2 = py(_t), py(_b)
        _side = _gap_side(_g, close)
        _fill, _strk = {"res": (_C_GAP_F, _C_GAP_S), "sup": (_C_SUP_F, _C_SUP_S),
                        "cross": ("#f1f5f9", "#cbd5e1")}[_side]
        _ro = "压力带" if _side == "res" else ("支撑带" if _side == "sup" else "缺口带")
        _gd = (_g.get("date") or "?")[5:]
        o.append('<rect x="%g" y="%.1f" width="%g" height="%.1f" fill="%s" stroke="%s" '
                 'stroke-dasharray="3 2"><title>%s</title></rect>'
                 % (_CH_AX, _y1, _CH_BX1 - _CH_AX, max(_y2 - _y1, 1.5), _fill, _strk,
                    esc("未补缺口 %s（%s · %s跳空）：%s ~ %s ⇒ %s"
                        % (_g.get("date"), _ro,
                           "向上" if _g.get("type") == "up" else "向下",
                           _money(_b), _money(_t),
                           {"res": "现价上方压力带", "sup": "现价下方支撑带",
                            "cross": "横跨现价，上下沿含义见右侧逐条"}[_side]))))
        # 带高实测仅 ~8px（缺口本身就是窄区间）⇒ 字号取 8 并给文字描白边，压在色带上仍清晰。
        # R542：标签位置改由 ④c 统一落位（带高太薄、装不下文字 ⇒ 候选以带中心为中心上下散开）。
        anno_r.append({"cands": [(_y1 + _y2) / 2 + 3 + _k * 10.0 for _k in (0, 1, -1, 2, -2, 3, -3)],
                       "fs": 8, "txt": "%s %s" % (_ro, _gd), "fill": "#64748b", "halo": True})

    # ④c 标注统一落位并绘制（R542）—— 左列以**现价徽章为固定障碍**（徽章位置有语义，
    #     不能挪；它标记的就是现价那条线）。纯确定性 ⇒ 同一份 plan.json 必得同一张图。
    _al = _anno_limits()
    for _it, _y in zip(anno_l, _place_col(anno_l, [(yc - 9.5, yc + 9.5)], _al[0], _al[1])):
        o.append('<text x="%g" y="%.1f" font-size="%g" fill="%s">%s</text>'
                 % (_CH_AX + 6, _y, _it["fs"], _it["fill"], esc(_it["txt"])))
    for _it, _y in zip(anno_r, _place_col(anno_r, [], _al[0], _al[1])):
        o.append('<text x="%g" y="%.1f" font-size="%g" fill="%s" text-anchor="end" '
                 'paint-order="stroke" stroke="#ffffff" stroke-width="2.2">%s</text>'
                 % (_CH_BX1 - 4, _y, _it["fs"], _it["fill"], esc(_it["txt"])))

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
    # 顺序必须与右侧「图上点位含义」的序号一致 ⇒ 统一走 _lv_items（规则 111）。
    items = [(py(x["price"]), _C_UP if i < len(ups) else _C_DN, x)
             for i, x in enumerate(_lv_items(p))]
    # R542：避让改为「前向展开 + 后向回压 + 间距自适应收缩」，并把可行性判据写成硬容量。
    #  ★ 原实现（R540/R541）只有一个「贪心下推」+ 一次「整组回夹」，有两个真缺陷：
    #    ① **下推只会加不会减** ⇒ 最终跨度 = Σ max(理想间距, MK_GAP) ≥ max(理想跨度, 档数×间距)，
    #       两者都可能超出可用高度。实测（随机 fuzz）理想跨度已 369px 时，8 档下推后到 420px，
    #       回夹把整组上移 39px，紧接着的「上夹」（避日期行）又把整组下移 21px —— 下夹被上夹
    #       **抵消**，末档落回 y=402，其名称行压进图例行（重叠 5.9px）。
    #    ② 无「间距收缩」：档数一多就只能重叠，而不是把间距压到仍可辨的下限。
    #  现方案：先夹进可用区，再前向保证间距、溢出后后向回压；间距下界取
    #  `min(MK_GAP, 可用高度/(档数−1))`，其**可行性**由构造保证（后向回压后顶端必 ≥ 上界）。
    #  硬容量 = (档数−1) × 标签块高（26.6px）—— 低于它任何算法都排不开，由 WARN + 判据 18 兜底。
    _top_lim, _bot_lim = _label_limits()
    _avail = _bot_lim - _top_lim
    _hard = max(0, len(items) - 1) * _MIN_MK_GAP
    if _hard > _avail:
        sys.stderr.write("WARN 标签避让容量不足：%d 档至少需 %.0fpx > 可用 %.0fpx ⇒ 图上必然重叠 "
                         "（请收窄档数 / 加高图）\n" % (len(items), _hard, _avail))
    ymv = [min(max(it[0], _top_lim), _bot_lim) for it in items]
    if len(items) > 1:
        _gap = min(MK_GAP, _avail / (len(items) - 1))
        _ord = sorted(range(len(items)), key=lambda i: ymv[i])
        for _k in range(1, len(_ord)):                     # 前向：保证相邻间距 ≥ _gap
            _i, _ip = _ord[_k], _ord[_k - 1]
            if ymv[_i] - ymv[_ip] < _gap:
                ymv[_i] = ymv[_ip] + _gap
        if ymv[_ord[-1]] > _bot_lim:                       # 底部溢出 ⇒ 后向回压（顶部因此必不越上界）
            ymv[_ord[-1]] = _bot_lim
            for _k in range(len(_ord) - 2, -1, -1):
                _i, _ip = _ord[_k], _ord[_k + 1]
                if ymv[_ip] - ymv[_i] < _gap:
                    ymv[_i] = ymv[_ip] - _gap
    # 自证：落位后必须①在可用区内②相邻档（按 y 序）净间隙 ≥ 一个标签块高 —— 这是「零重叠」
    # 的充分条件。★ 这两条断言只在**可行**时成立（`_hard <= _avail`），不可行时算法只能
    # best-effort（已 WARN + verify 判据 18 硬门禁）：既不谎称成立，也不把「图不好看」升级成
    # 抛异常 —— 那会让整个站点当日停止发布（report/index 的日常刷新也一起挂掉），代价不对等。
    if _hard <= _avail:
        _sy = sorted(ymv)
        for _v in _sy:
            assert _top_lim - 0.6 <= _v <= _bot_lim + 0.6, \
                "标签落位越出可用区 [%.1f, %.1f]：y=%.1f" % (_top_lim, _bot_lim, _v)
        for _k in range(1, len(_sy)):
            assert _sy[_k] - _sy[_k - 1] >= _LBL_TOP + _LBL_BOT - 0.6, \
                "相邻标签块净间隙不足：%.2f < %.2f（会叠字）" % (
                    _sy[_k] - _sy[_k - 1], _LBL_TOP + _LBL_BOT)

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
        # 带圈序号（R541）：放在**价格行**前 —— 这行原本 3,995.18 + 距现价之后还有 ~15px 余量，
        # 而名称行是稀缺资源（见 _lv_name 的自纠说明）。序号贴着价位，对照最直观。
        if i < len(_NUM):
            o.append('<text x="%g" y="%.1f" font-size="10" fill="#94a3b8">%s</text>'
                     % (_CH_LB, ym - 3, _NUM[i]))
        o.append('<text x="%g" y="%.1f" font-size="11.5" font-weight="700" fill="%s">%s</text>'
                 % (_CH_LB + 11, ym - 3, col, _money(x["price"])))
        o.append('<text x="%g" y="%.1f" font-size="9.5" fill="#94a3b8">%s</text>'
                 % (_CH_LB + 64, ym - 3, esc(_sign(x.get("dist_pct") or 0))))
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
        ("gap", _C_GAP_F, _C_GAP_S, "缺口带 %d 处 = 未补跳空区（压力 / 支撑）" % len(gbs)),
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
        elif kind == "gap":
            o.append('<rect x="%g" y="%.1f" width="9" height="9" rx="2" fill="%s" stroke="%s" '
                     'stroke-dasharray="3 2"/>' % (_CH_AX, yy - 8, fill, stroke))
        o.append('<text x="%.1f" y="%.1f" font-size="9.5" fill="#64748b">%s</text>'
                 % (_CH_AX + 14, yy, esc(_clip(text, 23.5))))

    o.append('</svg>')
    svg = "".join(o)
    _selfcheck_geometry(svg)
    return svg


# R542：图元几何**自证**（永久护栏）。上面那个 bug（缺口带不在量程 ⇒ 画到图外 y=-738）
# 是「只靠肉眼看图看不出来、只靠截图也不一定截到」的类型；把它做成对**产物本体**的检查：
# 解析出 SVG 里每个色带矩形，断言 ① 在 viewBox 内 ② 绘图区内的带都落在 [_CH_PT, _CH_PB]。
# 这样「以后再加一种带却忘了进 vals」会当场炸（CI 门禁 + 探针都会先跑到），而不是静默画飞。
_RE_RECT = re.compile(r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([-\d.]+)" height="([-\d.]+)"')


def _selfcheck_geometry(svg):
    _W = _CH_BX1 - _CH_AX                 # 带区宽度（中枢 / 九成区间 / 缺口带三者同宽）
    for x, y, w, h in _RE_RECT.findall(svg):
        x, y, w, h = float(x), float(y), float(w), float(h)
        if abs(w - _W) > 0.5:
            continue                        # 只核「带区矩形」；现价徽章(52px) / 图例小方块(9px)
            #                                 宽度不同，天然被排除
        assert -0.6 <= y and y + h <= _CH_H + 0.6, \
            "图元越出 viewBox：y=%.1f h=%.1f（viewBox 高 %d）" % (y, h, _CH_H)
        assert _CH_PT - 0.6 <= y and y + h <= _CH_PB + 0.6, \
            "带区矩形越出绘图区 [%d, %d]：y=%.1f h=%.1f ⇒ 该带的值未进量程（vals）" % (
                _CH_PT, _CH_PB, y, h)


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

    # R541：把 R540 已导出、却一直**零渲染**的两个维度真正显示出来（半成品补齐）：
    #   · `ma.alignment`（均线排列）—— 上面 8 档里已有 4 条均线位，但「多头/空头/纠缠」这个
    #     一眼可判的状态没露脸；
    #   · `bias`（乖离率）—— 距均线多远是判断「超买/超卖」的最常用尺子，且它的一手数据
    #     早已落在 plan.json 里（bias20 / bias60 / state）。
    # 两者都是**透传**（规则 149：区块内不写统计值，渲染层不自造数字）。
    _ma = p.get("ma") or {}
    ma_txt = ('<span class="plan-chip plan-chip--info" title="均线排列（引擎 classify.ma_alignment）：'
              '多头 / 空头 / 纠缠">均线 <b>%s</b></span>' % esc(_ma.get("alignment") or "—")
              ) if _ma.get("alignment") else ""
    _bias = p.get("bias") or {}
    bias_txt = ""
    if _bias.get("bias20") is not None:
        bias_txt = ('<span class="plan-chip" title="BIAS20 = 收盘价相对 MA20 的乖离率；'
                    '引擎判定：%s">BIAS20 <b>%s</b>%s</span>'
                    % (esc(_bias.get("state") or "—"), _sign(_bias["bias20"]),
                       ("（%s）" % esc(_bias["state"])) if _bias.get("state") else ""))

    # 位置徽章：R541 加「中枢内分位」——「中枢内部」看不出偏强还是偏弱，分位一眼可判。
    # 只在 position 确为「中枢内部」时显示（其它位置态下该分位无意义）。
    _pos = p.get("position") or "—"
    _pp = p.get("pos_pct")
    pos_txt = esc(_pos)
    if _pos == "中枢内部" and _pp is not None:
        pos_txt += "（%.1f%%）" % _pp

    # R541：原「怎么用」说明块（plan-note · 6 条）已删除（旭总 2026-10-12）。其中 5 条都有
    # 更细的载体（见 gen_plan.py notes 处逐条对照），唯独「所有位与买卖点取自引擎同一函数、
    # 与主报告同口径」这条**没有他处显式声明** ⇒ 压成下面这一行 12px 灰字保留（规则 107：
    # 披露不得静默丢失）。它不再是「使用说明」，只是依据声明。
    ref = ('<div class="plan-ref">依据：关键位与买卖点信号均取自 chanlun.analyze() / '
           'find_signals()，与主报告同口径、本侧不重算；本区块为当日快照（asof），'
           '免责见文末「六、免责说明」。</div>')

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
    <span class="plan-chip plan-chip--info" title="中枢内位置分位：0%% = ZD，100%% = ZG">位置 <b>%s</b></span>
    <span class="plan-chip">中枢 <b>%s</b></span>%s%s
  </div>
  <div class="plan-viz">
  <div class="plan-box">%s</div>
  <div class="plan-side">
  <div class="plan-tip">读法（<b>价格水位图</b>）：竖轴是价格。灰蓝色块 = 当前中枢 ZD~ZG；琥珀色块 = 明日九成区间；深色虚线 = 现价；右侧红 / 绿刻度 = 上方 / 下方关键位（价位 + 距现价 + 名称）；<b>紫色小标 = 买卖点触发位</b>；淡色带 = 未补缺口区（压力 / 支撑）。鼠标悬停任一元素看来源与含义 —— <b>但手机看不到悬停，请以下方「图上点位含义」为准</b>，那里把图上每个标（序号对应）逐个写全。<br>
    明日九成区间 %s。<br>
    <b>本图不画 P25–P75</b>：它在单日尺度实测只覆盖约 72.5%%（名义 50%%），口径标不住 ⇒ 不画。</div>
  %s
  %s
  %s
  %s
  %s
  <div class="plan-foot">
    <div>今日动作：%s</div>
    <div>判据失效条件：%s</div>
  </div>
  %s
  </div>
  </div>
</section>
""" % (MARK, esc(p.get("name") or ""), esc(p.get("asof") or ""),
       chg_cls, _money(p.get("close") or 0), _sign(chg),
       esc(p.get("scenario") or "—"), pos_txt, ztxt, ma_txt, bias_txt,
       _chart(p, band), band_txt, _lv_meaning(p), _gap_list(p), _trg_list(p),
       build_details(p), build_signals(p.get("signals")), act_txt, inv_txt, ref)


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
