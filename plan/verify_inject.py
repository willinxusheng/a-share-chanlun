# -*- coding: utf-8 -*-
"""plan/verify_inject.py — 注入的**零回归**验证（规则 8 / 119）

判据（必须全部成立，任一不成立即 FAIL）：
  1. 从注入后的 html 中**精确移除**新增的两段（CSS 与区块）后，**逐字节等于**注入前基线
     ⇒ 证明是「纯插入、零改动」，而不是「顺手改了既有字符」。
  2. 新增标记恰好出现 1 次（幂等）；既有章节锚点 s1..s6 各仍恰好 1 次。
  3. 插入位置正确：CSS 在 </head> 之前、区块在 s1 标题之前。
  4. **正控**：人为改动既有字符后，判据 1 必须失败（证明判据真的在工作，规则 119）。

用法：
  python3 plan/verify_inject.py <基线html> <注入后html>

退出码：0 = PASS，1 = FAIL。
"""
import json
import os
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
    block = inject_plan.build(p)
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
    absent = [t for t in must if t not in new]
    if not absent:
        print("  [PASS] 判据5 区块内 %d 个数字/日期全部可溯源到 plan.json" % len(must))
    else:
        fails.append("判据5 未在产物中找到：%s" % absent)

    # --- 判据 6：信号表行数与 plan.json 对账（防注入漏行/多行） ---
    # 计数锚的选择有讲究：信号行的 plan-k 带方向类（plan-dir--up/dn）⇒ 精确串只匹配「下一信号」行。
    n_sig = sum(len(l["rows"]) for l in sg.get("levels") or [])
    n_next = len(sg.get("next") or [])
    got_sig = new.count('<td class="plan-lv">')
    got_next = new.count('<td class="plan-k">')
    if got_sig == n_sig and got_next == n_next:
        print("  [PASS] 判据6 信号表行数与 plan.json 一致（信号 %d 行 + 下一信号 %d 行）" % (n_sig, n_next))
    else:
        fails.append("判据6 行数不符：信号 %d/%d、下一信号 %d/%d" % (got_sig, n_sig, got_next, n_next))

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
