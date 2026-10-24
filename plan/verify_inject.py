# -*- coding: utf-8 -*-
"""plan/verify_inject.py — 注入的**零回归**验证（规则 8 / 119）

判据（必须全部成立，任一不成立即 FAIL）：
  1. 从注入后的 html 中**精确移除**新增的两段（CSS 与区块）后，**逐字节等于**注入前基线
     ⇒ 证明是「纯插入、零改动」，而不是「顺手改了既有字符」。
  2. 新增标记恰好出现 1 次（幂等）；既有章节锚点 s1..s6 各仍恰好 1 次。
  3. 插入位置正确：CSS 在 </head> 之前、区块在 s1 标题之前。
  4. **正控**：人为改动既有字符后，判据 1 必须失败（证明判据真的在工作，规则 119）。
  5. 数据日与全部关键数字可溯源到 plan.json（非手写，规则 149）。
     ★ R544：`pos_pct` 的**显示条件**改由渲染层单一给出（`inject_plan._pos_pct_text()`）——
     此前这里自己写了一份「只要 pos_pct 不为 None 就必须出现」的错口径，在中枢下方/上方时
     必然 FAIL（2026-10-15 首次暴露）。规则 168 / 172。
  6. 信号表行数与 plan.json 对账（防注入漏行/多行）。
  7. **同源自证**：明日九成区间在「基线」与「注入后」两处**提取结果相同**，且区块内可检出该区间两端
     ⇒ 证明带子来自 index.html 内联推演数据本身，未重算（规则 111）。
  8. 价格水位图的关键位档位数 == plan.json 的 up + dn 行数。
  9. （R539）图上买卖点触发标数 == signals.next 中**给了价位**的条数
     ⇒ 防「漏标」或「把引擎没给价位的类别硬编上去」。
 10. （R539）两张表确实折进 <details>，且表体 + 失效位口径 + R:R 口径文字一字未丢（规则 107）。
 11. （R539）标题层级与全站 panel 对齐（h2），旧 h3.plan-h 已清除。
 12. （R539）meta 徽章数 == 渲染层实际输出的维度数（收盘 / 情景 / 位置 / 中枢 ＋ 均线? ＋ BIAS20?）。
     ★ R544：原实现把 6 写死，但「均线排列」「BIAS20」是**条件渲染**，任缺一维即 FAIL ⇒
     同族停发风险。现向渲染层取同一份条件（规则 168 / 172）。
 14. （R541）右侧「图上点位含义」档数 == 关键位档数，且**每个 label 段**都被写出
     （图上只显缩写 ⇒ 逐段核，避免「列了档但漏了语义段」）。
 15. （R541）图上带圈序号数与右侧列表一一对应（各 == 档数）。
 16. （R541）未补缺口带：图上矩形带数 == 右侧条目数 == plan.json gap_bands 条数
     ＋ 判据 5 的 must 中加入缺口上下沿原值（证明真实上沿如 3,888.37 确实落在产物里 ——
     R540 它曾被 ZG 的去重吞掉，这正是本轮修的失真）。
 17. （R542）合并组内**每个成员自身价位**都必须在产物里逐段标出
     （代表价只取首选来源 ⇒ 实测 MA60 真实 3,897.68 曾被展示成 3,892.75，差 4.93 点，
     与 R541 修的缺口失真同源 ⇒ 逐段核，不能只核组价）。
 18. （R542）标签避让容量：档数 × MK_GAP ≤ 可用高度 —— 不足时贪心避让必然重新重叠，
     而「图上有两档压成一行」是静默错误（规则 163 的必要条件）。
 19. （R542）缺口带压力/支撑/跨现价分类**单一来源**：右侧条目类名 == 图上 title 用词 ==
     plan.json 的 `side`（规则 111：此前图上按几何、右侧按 dist_pct 中点，带横跨现价时相反）。
 20. （R544）结构真空区说明**双向**与 plan.json 的 `side_note` 一致：声明 ⇔ 产物实读；
     每一侧文案须**含本侧、不含另侧**关键词；边界价须落在说明里（规则 160 / 149 / 111）。

用法：
  python3 plan/verify_inject.py <基线html> <注入后html>

退出码：0 = PASS，1 = FAIL。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import inject_plan   # noqa: E402


def strip_and_compare(base, new, css, block):
    """移除两段新增后与基线比较。返回 (ok, 说明)。"""
    tail = block + "\n"
    n = new
    for piece, name in ((css, "CSS"), (tail, "区块")):
        # 新增段必须唯一出现（否则幂等被破坏 / 误匹配）
        first = n.find(piece)
        if first < 0:
            return False, "未找到新增段：%s" % name
        if n.find(piece, first + 1) >= 0:
            return False, "新增段 %s 出现多次（幂等被破坏）" % name
        n = n[:first] + n[first + len(piece):]
    if n == base:
        return True, "纯插入、零改动（移除新增后逐字节等于基线）"
    # 定位第一处不同，便于排查
    m = min(len(n), len(base))
    for i in range(m):
        if n[i] != base[i]:
            return False, "既有字符被改动：offset %d, base=%r new=%r" % (
                i, base[max(0, i - 30):i + 30], n[max(0, i - 30):i + 30])
    return False, "长度不等：base=%d new=%d（疑有残留或截断）" % (len(base), len(n))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    bpath, npath = sys.argv[1], sys.argv[2]
    with open(bpath, encoding="utf-8") as f:
        base = f.read()
    with open(npath, encoding="utf-8") as f:
        new = f.read()
    with open(os.path.join(HERE, "plan.json"), encoding="utf-8") as f:
        p = json.load(f)

    css = inject_plan.PLAN_CSS
    # 区块内容依赖「明日九成区间」——它从 index.html 内联推演数据提取（不重算）。
    # 基线里同样含该内联块（注入是纯插入），故两边提取结果必须一致（判据 7 单独核验）。
    band_b = inject_plan.extract_band(base)
    band_n = inject_plan.extract_band(new)
    block = inject_plan.build(p, band_b)
    fails = []

    # --- 判据 1+4：移除新增后等于基线；正控必须让同一判据失败 ---
    ok, why = strip_and_compare(base, new, css, block)
    if ok:
        print("  [PASS] 判据1 零回归：%s" % why)
    else:
        fails.append("判据1 零回归：%s" % why)
    bad = new.replace("tabular-nums", "tabular-numz", 1)      # 正控：改既有字符
    okc, _ = strip_and_compare(base, bad, css, block)
    if not okc:
        print("  [PASS] 判据4 正控：既有字符被改动时判据确实 FAIL（判据有效）")
    else:
        fails.append("判据4 正控失效：改动既有字符却仍判 PASS ⇒ 判据形同虚设")

    # --- 判据 2：新增标记唯一、既有锚点未丢失 ---
    n_mark = new.count('id="plan-board"')
    if n_mark == 1:
        print("  [PASS] 判据2a 新区块标记唯一（1 次）")
    else:
        fails.append("判据2a 新区块标记出现 %d 次（应 1）" % n_mark)
    miss = [s for s in range(1, 7) if ('id="s%d"' % s) not in new]
    if not miss and all(new.count('id="s%d"' % s) == 1 for s in range(1, 7)):
        print("  [PASS] 判据2b 既有章节锚点 s1..s6 各仍恰好 1 次")
    else:
        fails.append("判据2b 既有章节锚点异常（缺失 %s 或重复）" % miss)

    # --- 判据 3：插入位置 ---
    ip_css, ip_blk = new.find(css), new.find(block)
    ih, is1 = new.find("</head>"), new.find('<h2 class="sec" id="s1">')
    if 0 < ip_css < ih and 0 < ip_blk < is1:
        print("  [PASS] 判据3 插入位置正确（CSS 在 </head> 前、区块在 s1 前）")
    else:
        fails.append("判据3 插入位置异常 css=%d head=%d blk=%d s1=%d" % (ip_css, ih, ip_blk, is1))

    # --- 判据 5：数据日与关键数字确实来自 plan.json（非手写） ---
    must = [str(p["close"]), str(p["asof"])] + ["{:,.2f}".format(x["price"])
                                                for x in (p.get("up") or []) + (p.get("dn") or [])]
    sg = p.get("signals") or {}
    for _l in sg.get("levels") or []:
        for _x in _l["rows"]:
            must.append("{:,.2f}".format(_x["price"]))
            must.append("{:,.2f}".format(_x["inval"]))
            if _x.get("target") is not None:
                must.append("{:,.2f}".format(_x["target"]))
    must += ["{:,.2f}".format(_x["price"]) for _x in (sg.get("next") or [])
             if _x.get("price") is not None]
    if band_b:
        must += ["{:,.2f}".format(band_b["low"]), "{:,.2f}".format(band_b["high"])]
    # R541：缺口上下沿取引擎**原值**（gap_bands）—— 这条最要紧：R540 时 9/28 缺口上沿
    # 3,888.37 与 ZG 3,884.44 相差 0.10%，落在去重容差内被合并 ⇒ 真实价位在产物里消失。
    # 把它列进 must，等于给「区间不再被点型去重吞掉」上了一道锁。
    for _g in (p.get("gap_bands") or []):
        must += ["{:,.2f}".format(_g["top"]), "{:,.2f}".format(_g["bottom"])]
    # ★ R544：中枢内分位的**显示条件**必须与渲染层同一份（渲染层只在 position == 「中枢内部」
    #   时输出分位）。此前这里自己写了一遍 `if pos_pct is not None ⇒ 必须出现`，口径不一致，
    #   且**只在某一种位置态下才暴露**：2026-10-15 上证收在**中枢下方**（pos_pct = −51.3%）
    #   时判据 5 第一次 FAIL —— 而本脚本是**阻断式**门禁（非零退出 ⇒ 当日停发），
    #   与本次主 bug 同一后果。规则 168（阈值口径与调用方一致）/ 172（判据单一来源）。
    _ppt = inject_plan._pos_pct_text(p)
    if _ppt:
        must.append(_ppt)
    _b20 = (p.get("bias") or {}).get("bias20")
    if _b20 is not None:                                  # R541 BIAS 徽章
        must.append(inject_plan._sign(_b20))
    absent = [t for t in must if t not in new]
    if not absent:
        print("  [PASS] 判据5 区块内 %d 个数字/日期全部可溯源到 plan.json" % len(must))
    else:
        fails.append("判据5 未在产物中找到：%s" % absent)

    # --- 判据 6：信号表行数与 plan.json 对账（防注入漏行/多行） ---
    # 计数锚的选择有讲究：信号行的 plan-k 带方向类（plan-dir--up/dn）⇒ 精确串只匹配「下一信号」行。
    n_sig = sum(len(l["rows"]) for l in sg.get("levels") or [])
    n_next = len(sg.get("next") or [])
    n_lv = len(p.get("up") or []) + len(p.get("dn") or [])
    got_sig = new.count('<td class="plan-lv">')
    got_next = new.count('<td class="plan-k">')
    if got_sig == n_sig and got_next == n_next:
        print("  [PASS] 判据6 信号表行数与 plan.json 一致（信号 %d 行 + 下一信号 %d 行）" % (n_sig, n_next))
    else:
        fails.append("判据6 行数不符：信号 %d/%d、下一信号 %d/%d" % (got_sig, n_sig, got_next, n_next))

    # --- 判据 7：明日九成区间「同源自证」（规则 111：同一事实单一来源，不重算） ---
    if band_b is None and band_n is None:
        print("  [PASS] 判据7 九成区间：两边均未取到 ⇒ 图上不画该带（一致，无重算）")
    elif band_b != band_n:
        fails.append("判据7 九成区间两侧提取不一致：base=%r new=%r ⇒ 疑被重算或有第二来源"
                     % (band_b, band_n))
    else:
        lo_s, hi_s = "{:,.2f}".format(band_b["low"]), "{:,.2f}".format(band_b["high"])
        if lo_s in new and hi_s in new:
            print("  [PASS] 判据7 九成区间同源自证：%s ~ %s（取自页面内联推演数据，两侧一致）"
                  % (lo_s, hi_s))
        else:
            fails.append("判据7 九成区间数值未在产物中检出：%s / %s" % (lo_s, hi_s))

    # --- 判据 8：水位图关键位档位数 == plan.json（防图少画/多画） ---
    got_mk = new.count('<g class="plan-mk">')
    if got_mk == n_lv:
        print("  [PASS] 判据8 水位图关键位档位数与 plan.json 一致（%d 档）" % n_lv)
    else:
        fails.append("判据8 水位图档位数不符：%d/%d" % (got_mk, n_lv))

    # --- 判据 9：图上买卖点触发标数 == signals.next 中**给了价位**的条数（R539） ---
    # 语义：只标引擎给了明确价位的类别；二/三类买卖点无价 ⇒ 不得凭空多标（规则 111/114）。
    n_trg = len([x for x in (sg.get("next") or []) if x.get("price") is not None])
    got_trg = new.count('<rect class="plan-trg"')
    if got_trg == n_trg:
        print("  [PASS] 判据9 图上买卖点触发标数与 plan.json 一致（%d 个）" % n_trg)
    else:
        fails.append("判据9 触发标数不符：%d/%d ⇒ 图上漏标，或把引擎没给价位的类别硬编上去了"
                     % (got_trg, n_trg))

    # --- 判据 10：两表已折叠，但表体与披露一字未丢（R539 · 规则 107） ---
    n_det = new.count('<details class="plan-det">')
    segs = new.split('<details class="plan-det">')
    sig_seg = segs[2].split("</details>")[0] if len(segs) > 2 else ""
    has_all = all(k in sig_seg for k in ("买卖点信号", "下一信号", "失效位口径", "attach_rr()"))
    if n_det == 2 and has_all:
        print("  [PASS] 判据10 两表折进 details（共 %d 个折叠块），表体 + 失效位口径 + R:R 口径均在"
              % n_det)
    else:
        fails.append("判据10 折叠结构异常：details=%d, 表体/口径齐全=%s" % (n_det, has_all))

    # --- 判据 11：标题层级与全站 panel 对齐（h2，旧 h3.plan-h 必须清除） ---
    if "<h2>明日关键价位" in new and 'class="plan-h"' not in new:
        print("  [PASS] 判据11 标题沿用全站 .panel h2 层级（旧 h3/plan-h 已清除）")
    else:
        fails.append("判据11 标题层级未与全站对齐（h2 缺失或旧 plan-h 残留）")

    # --- 判据 12：meta 徽章数 == 渲染层实际会输出的维度数（收盘 / 情景 / 位置 / 中枢 ＋ 均线? ＋ BIAS20?） ---
    # 锚点必须带**词边界**：单纯 class="plan-chip 会前缀匹配到 plan-chip--x 之类（正控实测抓到两次）。
    # 故：先框定 meta 容器，再要求 plan-chip 后紧跟空格或引号。
    # ★ R544：原实现把 6 写死 —— 但「均线排列」「BIAS20」两枚是**条件渲染**（空值不输出），
    #   任缺一维就变 5 ⇒ 门禁 FAIL ⇒ 当日停发。与判据 5 同族（把「条件渲染」当「必然渲染」）。
    #   现改为向渲染层取同一份条件（`_ma_chip` / `_bias_chip` 为空串则不计数），规则 168 / 172。
    _n_chip_exp = (4 + (1 if inject_plan._ma_chip(p) else 0)
                     + (1 if inject_plan._bias_chip(p) else 0))
    m_meta = re.search(r'<div class="plan-meta">(.*?)</div>', new, re.S)
    got_chip = len(re.findall(r'<span class="plan-chip(?=[ "])', m_meta.group(1))) if m_meta else -1
    if got_chip == _n_chip_exp:
        print("  [PASS] 判据12 meta 徽章数与渲染层维度一致（%d 个：收盘/情景/位置/中枢%s%s）"
              % (got_chip,
                 "＋均线" if inject_plan._ma_chip(p) else "（均线缺值不显示）",
                 "＋BIAS20" if inject_plan._bias_chip(p) else "（BIAS20 缺值不显示）"))
    else:
        fails.append("判据12 meta 徽章数不符：%d/%d" % (got_chip, _n_chip_exp))

    # --- 判据 13：图旁「买卖点触发位」速查条目数 == 有价位的条数（R539） ---
    got_tl = new.count('<li><b class="plan-tk">')
    if got_tl == n_trg:
        print("  [PASS] 判据13 触发位速查条目数与引擎给价条数一致（%d 条）" % n_trg)
    else:
        fails.append("判据13 触发位速查条目数不符：%d/%d" % (got_tl, n_trg))

    # --- 判据 14：右侧「图上点位含义」档数 + label **逐段**写出（R541）---
    # 为什么逐段而不是只核档数：图上是缩写（「① GG · 顶背…」），右侧的价值就在于把
    # **每一段**语义写全；只核档数会漏掉「列了 8 档但每档只写第一段」这种半成品。
    # 严谨性：先在 inject_plan 重算该片段并确认它**确实被注入**（防止判据自己核自己）。
    lv_seg = inject_plan._lv_meaning(p)
    segs_all = []
    for _x in (p.get("up") or []) + (p.get("dn") or []):
        segs_all += [s.strip() for s in str(_x.get("label") or "").split(" / ") if s.strip()]
    miss_seg = [s for s in segs_all if inject_plan.esc(s) not in lv_seg]
    got_lvl = new.count('<li class="plan-lvl--')
    if lv_seg and lv_seg in new and got_lvl == n_lv and not miss_seg:
        print("  [PASS] 判据14 右侧点位含义：%d 档齐全，%d 个 label 段（含共振段）全部写出"
              % (got_lvl, len(segs_all)))
    else:
        fails.append("判据14 点位含义不全：片段已注入=%s 档数 %d/%d 缺段 %s"
                     % (bool(lv_seg) and lv_seg in new, got_lvl, n_lv, miss_seg))

    # --- 判据 15：图上带圈序号 ⇄ 右侧列表 一一对应（R541）---
    # 两侧各出现 n_lv 次才算「钉死」：图上少一个 ⇒ 有档没编号；右侧少一个 ⇒ 序号断档。
    NUM = inject_plan._NUM
    n_num = sum(new.count('<span class="plan-lvl-i">%s</span>' % NUM[i]) for i in range(n_lv))
    # 图上锚：序号是**独立 text 节点**（R541 自纠：它从名称行前缀挪到了价格行前，
    # 故锚由 `>① ` 改为 `>①</text>` —— 改锚必须跟着产品改，否则判据会恒真/恒假）。
    ch_num = sum(new.count('>%s</text>' % NUM[i]) for i in range(n_lv))
    if n_num == n_lv and ch_num == n_lv:
        print("  [PASS] 判据15 图上带圈序号与右侧列表一一对应（各 %d 个）" % n_lv)
    else:
        fails.append("判据15 序号数不符：右侧 %d/%d、图上 %d/%d" % (n_num, n_lv, ch_num, n_lv))

    # --- 判据 16：未补缺口带「图上矩形 = 右侧条目 = plan.json」（R541）---
    n_gap = len(p.get("gap_bands") or [])
    got_gap_r = new.count('stroke-dasharray="3 2"><title>未补缺口')
    got_gap_l = new.count('<li class="plan-gap--')
    if got_gap_r == n_gap and got_gap_l == n_gap:
        print("  [PASS] 判据16 未补缺口带：图上 %d 处 = 右侧 %d 条 = plan.json %d 条（区间原值）"
              % (got_gap_r, got_gap_l, n_gap))
    else:
        fails.append("判据16 缺口带数不符：图上 %d / 右侧 %d / plan.json %d"
                     % (got_gap_r, got_gap_l, n_gap))

    # --- 判据 17：合并组内**每个成员自身的价位**必须逐段出现在产物里（R542）---
    # 为什么独立成判据：档数与「组代表价」都在（判据 8/14 都会 PASS），缺失的只是**成员自己的
    # 价位** —— 而它恰恰决定「收盘站上哪个数」。实测 MA60 真实 3,897.68 曾被展示为 3,892.75
    # （差 4.93 点）、MA120/MA250 差 2.18/4.40 点 ⇒ 属静默失真，必须逐段核。
    exp_mp = []
    for _x in (p.get("up") or []) + (p.get("dn") or []):
        _labs = [s.strip() for s in str(_x.get("label") or "").split(" / ") if s.strip()]
        _prs = list(_x.get("seg_prices") or [])
        for _i, _lb in enumerate(_labs):
            try:
                _pv = round(float(_prs[_i]), 2)
            except (IndexError, TypeError, ValueError):
                continue
            if _pv != round(float(_x["price"]), 2):
                exp_mp.append((_lb, "{:,.2f}".format(_pv)))
    lv_seg2 = inject_plan._lv_meaning(p)
    got_mp = new.count('<span class="plan-lvl-mp">')
    miss_mp = [("%s %s" % kv) for kv in exp_mp
               if ('<span class="plan-lvl-mp">%s</span>' % kv[1]) not in lv_seg2
               or ('<span class="plan-lvl-mp">%s</span>' % kv[1]) not in new]
    # ★ R547: 空集（exp_mp 为空）是**合法状态**，不是缺陷 —— 当合并组各成员的价位与
    #   主价位**相同**时（如「中枢最高 GG / 顶背驰证伪位」两个名字指向同一个 3995.18、
    #   「中枢最低 DD / 当前笔终点」同指 3674.82），标注一次即可，无需逐成员标价
    #   ⇒ exp_mp 天然为空。旧判据 `if exp_mp and ...` 把这种情况判成 FAIL。
    #   ★ 真实后果（已在线上发生，非理论顾虑）：2026-10-21 / 10-22 / 10-23 连续三个交易日，
    #     CI 走到 `GATE_MODE=warn` 的 else 分支 ⇒ **plan 区块整块被静默回退**，
    #     线上 index.html 的「明日关键价位 · 条件预案」消失（plan-board / plan-lvl 均 0），
    #     而 step 仍为 success —— 正是规则 118「『跳过』下游 ⇒ job 仍绿」的形态。
    #     而 10-20 那轮 exp_mp 有 1 处（MA60 3,880.68 ≠ 主价位）故 PASS，区块正常。
    #   修法：空集时给**双向**判据 —— 页面也确实不该出现 plan-lvl-mp（出现即索引错位/多标），
    #   使空集分支同样具备分辨力（非恒真，规则 186）。
    if not exp_mp:
        if got_mp == 0:
            print("  [PASS] 判据17 本日无「成员价位 ≠ 主价位」的合并组 ⇒ 无需逐成员标价（span 0）")
        else:
            fails.append("判据17 期望无成员标价但页面出现 %d 处 plan-lvl-mp（索引错位或多标）"
                         % got_mp)
    elif got_mp == len(exp_mp) and not miss_mp:
        print("  [PASS] 判据17 合并组成员自身价位逐段标出（%d 处：%s）"
              % (len(exp_mp), "、".join("%s %s" % kv for kv in exp_mp)))
    else:
        fails.append("判据17 成员自身价位缺失：span %d/%d、缺 %s"
                     % (got_mp, len(exp_mp), miss_mp or "无"))

    # --- 判据 18：标签避让容量（R542）---
    # 边界由 inject_plan._label_limits() 单一给出（图上夹/下夹与这里同源 ⇒ 不可能各算一套）；
    # 容量口径也必须同源：判据用 inject_plan._MIN_MK_GAP（硬容量 27.6px = 两档标签块最小可辨
    # 间距），与图上那句 `if _hard <= _avail:` 的断言**同一个常量** —— 早先这里写死 34.0
    # （软容量 MK_GAP）会让「断言已 WARN 但门禁仍绿」漏过去（同一阈值两处各写一份 = 规则 111）。
    _top_lim, _bot_lim = inject_plan._label_limits()
    _need = max(0, n_lv - 1) * inject_plan._MIN_MK_GAP
    _avail = _bot_lim - _top_lim
    if _need <= _avail:
        print("  [PASS] 判据18 标签避让容量足够（%d 档需 %.0fpx ≤ 可用 %.0fpx，"
              "区间 %.1f~%.1f：上避日期行 / 下避图例）" % (n_lv, _need, _avail, _top_lim, _bot_lim))
    else:
        fails.append("判据18 避让容量不足：%d 档需 %.0fpx > 可用 %.0fpx ⇒ 图上标签会静默重叠"
                     % (n_lv, _need, _avail))

    # --- 判据 19：缺口带分类「右侧条目类名 == 图上 title 用词 == plan.json side」（R542）---
    # 规则 111：同一事实多出口必须同口径。此前图上按几何判、右侧按 dist_pct 中点判，
    # 带横跨现价（引擎可达）时两者相反；本判据给这条上锁。
    _close = float(p.get("close") or 0.0)
    exp_side = [inject_plan._gap_side(g, _close) for g in (p.get("gap_bands") or [])]
    exp_li = sum(new.count('<li class="plan-gap--%s">' % s) for s in sorted(set(exp_side)))
    _titles = re.findall(r'title>未补缺口[^<]*<', new)
    _kw = {"res": "压力带", "sup": "支撑带", "cross": "跨现价"}
    # ★ 双侧断言（规则 160 同族）：必须**含**本侧关键词**且不含**另一侧关键词。
    #   只做「含本侧」会被自证不足的锚骗过 —— 正控实测：缺口 title 里「压力带」出现两次
    #   （`（压力带 · 向下跳空）` 与 `⇒ 现价上方压力带`），只改后一处时该判据仍 PASS。
    _bad_t = []
    for _i in range(min(len(_titles), len(exp_side))):
        _have = _kw[exp_side[_i]]
        _other = [_v for _k, _v in _kw.items() if _k != exp_side[_i]]
        if _have not in _titles[_i] or any(_v in _titles[_i] for _v in _other):
            _bad_t.append((_i, exp_side[_i], _titles[_i][:46]))
    if exp_side and exp_li == n_gap and len(_titles) == n_gap and not _bad_t:
        print("  [PASS] 判据19 缺口带分类单一来源（右侧/图上/plan.json 一致：%s）"
              % "、".join(exp_side))
    else:
        fails.append("判据19 缺口带分类不一致：右侧 %d/%d、图上 title %d/%d、不符 %s"
                     % (exp_li, n_gap, len(_titles), n_gap, _bad_t))

    # --- 判据 20：某侧无参考位时**必须有**说明；未声明时**不得**有说明（R544）---
    # 背景：`up` / `dn` 允许为空（价格跌穿 / 站上**全部**已识别关键位 ⇒ 结构真空区）。
    # 渲染层其余各处都带 `or []` 兜底，页面不会崩，但读者会把「该侧 0 档」误读成数据缺失。
    # ★ 双向断言（规则 160 同族）：
    #   · 只做「声明 ⇒ 有说明」→ 会被「渲染层无条件输出一句说明」骗过；
    #   · 只做「有说明 ⇒ 声明」→ 会被「某侧真的空了但页面不说」骗过。
    #   故：plan.json 声明集合必须**恰好等于**产物实读集合，且每一侧的文案关键词**含本侧、
    #   不含另侧**（防渲染层把「上方」写成「下方」—— 段内互换位置这种错只核存在性抓不到）。
    #   另核边界价确实落在产物里（可溯源，规则 149）。
    _sn = p.get("side_note") or {}
    _decl = sorted(k for k in ("up", "dn") if _sn.get(k))
    _got_v = sorted(k for k in ("up", "dn")
                    if new.count('class="plan-vac plan-vac--%s"' % k))
    _bad_v = []
    for _k in _decl:
        _m = re.search(r'class="plan-vac plan-vac--%s"[^>]*>(.*?)</div>' % _k, new, re.S)
        _seg = _m.group(1) if _m else ""
        _have = ("下方" if _k == "dn" else "上方") + "无任何参考位"
        _other = ("上方" if _k == "dn" else "下方") + "无任何参考位"
        _bp = (_sn[_k] or {}).get("bound_price")
        if (not _seg) or _have not in _seg or _other in _seg:
            _bad_v.append("%s 文案缺/串位（含「%s」=%s）" % (_k, _have, _have in _seg))
        elif _bp is not None and ("{:,.2f}".format(_bp) not in _seg):
            _bad_v.append("%s 边界价 %s 未出现在说明里" % (_k, "{:,.2f}".format(_bp)))
    if _decl == _got_v and not _bad_v:
        print("  [PASS] 判据20 结构真空区说明与 plan.json 双向一致（声明 %s / 实读 %s，文案与边界价均对账）"
              % ("、".join(_decl) or "无", "、".join(_got_v) or "无"))
    else:
        fails.append("判据20 真空区说明不符：声明 %s、实读 %s、问题 %s"
                     % (_decl or "无", _got_v or "无", _bad_v))

    print("")
    if fails:
        print("VERIFY FAIL  %d 项未过：" % len(fails))
        for f in fails:
            print("   ✗ %s" % f)
        return 1
    print("VERIFY PASS  全部判据通过（含正控）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
