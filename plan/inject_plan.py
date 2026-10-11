# -*- coding: utf-8 -*-
"""plan/inject_plan.py — 把「明日关键价位」区块注入主报告 index.html

用法（挂在 deploy.yml：national/inject_banner.py 之后）：
  python3 plan/inject_plan.py index.html

设计（沿用 radar/national 两个注入器的既有约定）：
  · 读 plan/plan.json（由 plan/gen_plan.py 产出）；缺失时**不阻断部署**（WARN + 跳过）。
  · 幂等：已含 id="plan-board" 则跳过（防双份 CSS / 区块，R353 同款教训）。
  · 锚点：注入到 `<h2 class="sec" id="s1">` 之前 —— 即紧跟「今日结构信号」条之后。
  · 区块内**所有数字来自 plan.json**，本脚本不写任何统计值（规则 149）。
  · 不新增章节编号（保持 s1..s6 既有编号体系不被扰动）。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
PLAN = os.path.join(HERE, "plan.json")
MARK = 'id="plan-board"'
ANCHOR = '<h2 class="sec" id="s1">'

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


# ===== 买卖点信号两小节 =====
# 设计原则：① 全部数字来自 plan.json（规则 149）；② 复用既有 .plan-* 类，只加 4 个小类；
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


def build(p):
    zs = p.get("zhongshu") or {}
    ztxt = ("%.2f ~ %.2f（%s 笔 · %s）" % (zs.get("zd", 0), zs.get("zg", 0),
                                           zs.get("count", "?"), zs.get("date_end", "?"))
            if zs else "—")
    body = (_row(x, "\u25b2", "plan-dir--up") for x in p.get("up") or [])
    rows = "".join(body) + "".join(_row(x, "\u25bc", "plan-dir--dn") for x in p.get("dn") or [])

    acts = p.get("actions") or []
    if acts:
        act_txt = "；".join("%s %s 后进入：%s" % (a["dir"], _money(a["price"]), a["semantic"])
                           for a in acts)
    else:
        act_txt = '<b>无</b>（收盘价未穿越任何触发位 —— 今日不需要动作）'

    inv = p.get("invalidations") or []
    inv_txt = "；".join("<b>%s</b>：%s" % (_money(i["price"]), i["text"]) for i in inv) or "无"

    notes = "".join("<li>%s</li>" % esc(n) for n in p.get("notes") or [])

    return """
<section class="panel" %s>
  <h3 class="plan-h">明日关键价位 · 条件预案 <span class="plan-sub">%s · 数据截至 %s 收盘 · 只讲「到哪个价会怎样」，不预测涨跌</span></h3>
  <div class="plan-meta">
    <span>收盘 <b>%s</b>（<b>%s</b>）</span>
    <span>情景 <b>%s</b></span>
    <span>位置 <b>%s</b></span>
    <span>中枢 <b>%s</b></span>
  </div>
  <div class="tablescroll">
  <table class="plan-tbl">
    <thead><tr><th>方向</th><th>价位</th><th>距现价</th><th>结构来源</th><th>收盘到该位的含义</th></tr></thead>
    <tbody>%s</tbody>
  </table>
  </div>
  <div class="plan-foot">
    <div>今日动作：%s</div>
    <div>判据失效条件：%s</div>
  </div>%s
  <div class="plan-note"><b>怎么用</b>：本表只是把<b>已有的结构位</b>摆成一排，标出每个位对应什么状态变化。
    幅度请自行决定（不提供仓位比例）。<ul>%s</ul></div>
</section>
""" % (MARK, esc(p.get("name") or ""), esc(p.get("asof") or ""),
       _money(p.get("close") or 0), _sign(p.get("chg_pct") or 0),
       esc(p.get("scenario") or "—"), esc(p.get("position") or "—"), ztxt,
       rows, act_txt, inv_txt, build_signals(p.get("signals")), notes)


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

    block = build(p)

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
    print("index.html 就绪(关键价位区块=有, asof=%s), %d KB" % (p.get("asof"),
                                                          os.path.getsize(src) // 1024))


if __name__ == "__main__":
    main()
