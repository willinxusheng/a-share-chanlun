#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R543 背驰分支「目标达成度」文案一致性自检（双向判据 · 入仓但不挂 CI）

背景（为什么需要这个检查）
----------------------------------------------------------------------
`chanlun.classify()` 的背驰分支原为**无条件模板**：顶背驰固定输出
「…回落目标先看最近中枢ZG(N)」、底背驰固定输出「…第一压力看最近中枢ZD(N)」，
**不读 pos**。后果：目标早已达成时仍以**未来时**输出。
2026-10-12 实测（5 指数 × 日/周/月三级）**3 个指数 × 4 处**命中：
  · 上证指数  日线 顶背驰 · 中枢内部  ⇒「回落目标先看 ZG 3884.4」早已达成
  · 沪深300  周线 顶背驰 · 中枢下方  ⇒ 价已到 4287.98，ZG 4761.8 远在上方
  · 深证成指  周线 顶背驰 · 中枢下方  ⇒ 同上（ZG 16207.8）
而这些指数的 detail **整段完全不含当前位置信息**，与同一份报告里的非背驰分支
（「当前向下笔运行于最后中枢ZD(4492.1)下方，走势偏弱…」）信息量严重不对称。

修复（R543）：`chanlun.py` 新增 `_pos_note`，**只在目标已达成/越界时追加**（追加而非替换，
保留判据来源原文 ⇒ 历史可比、下游消费方与探针期望值不受影响）。

本检查（**双向**，规则 141 同族 —— 只判单侧会被同词双现骗过）
----------------------------------------------------------------------
  A. 背驰分支 + pos 在「已达成 / 越界」侧 ⇒ detail **必须**含位置补充句（正向）
  B. 背驰分支 + pos 在「未达成」侧     ⇒ detail **不得**含位置补充句（反向）
  C. 非背驰分支                        ⇒ detail **不得**含位置补充句（反向）
  D. pos 不属已知 3 枚举（如「无中枢」）⇒ 显式报 FAIL 并说明
     （防 `pos` 枚举将来扩展后 `_pos_note` 静默退化 —— 规则 172）

用法
----------------------------------------------------------------------
    python3 audit_beichi_pos_note.py [data.json]

不传参数时读本脚本同目录的 data.json。退出码 0 = 全部一致；1 = 有不一致（打印明细）。
★ 不挂进 CI：本检查是**呈现层一致性**护栏，不是部署阻断项（规则 175 同族：不可把
  非致命项做成抛异常阻断，否则会令当日整站停发）。
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

from chanlun import analyze, MIN_BI_PCT_WEEK, MIN_BI_PCT_MONTH  # noqa: E402

# `pos` 的已知枚举（与 chanlun.classify 的 L1025-1032 三分支对应）
POS_STATES = ("中枢上方", "中枢内部", "中枢下方")

# 位置补充句的**唯一识别串** —— 与 chanlun._pos_note 的四种文案一一对应（规则 111：同事实同口径）
POS_NOTE_MARKS = ("已回落至中枢内部", "已跌破中枢下沿", "已反弹回中枢内部", "已站上中枢上沿")

# (背驰方向, pos) -> 期望的识别串；None = 该组合**不应**出现补充句（目标未达成）
EXPECT = {
    ("top", "中枢内部"): "已回落至中枢内部",
    ("top", "中枢下方"): "已跌破中枢下沿",
    ("top", "中枢上方"): None,
    ("bottom", "中枢内部"): "已反弹回中枢内部",
    ("bottom", "中枢上方"): "已站上中枢上沿",
    ("bottom", "中枢下方"): None,
}


def expect_mark(detail):
    """由 detail 文本反推背驰方向，并给出期望的补充句识别串。"""
    is_top = "构成顶背驰" in detail
    is_bot = "构成底背驰" in detail
    if is_top == is_bot:                      # 两者皆无（非背驰分支）或同时出现（异常）
        return ("none" if not is_top else "both"), None
    return ("top" if is_top else "bottom"), None


def check_one(level, sym, name, cls, rows):
    detail = cls.get("detail") or ""
    pos = cls.get("position") or "?"
    scen = cls.get("scenario") or "?"
    kind, _ = expect_mark(detail)
    found = [m for m in POS_NOTE_MARKS if m in detail]

    if kind == "both":
        rows.append((level, sym, name, scen, pos, "FAIL",
                     "detail 同时含「构成顶背驰」与「构成底背驰」⇒ 分支判定歧义"))
        return
    if kind == "none":
        if found:
            rows.append((level, sym, name, scen, pos, "FAIL",
                         "非背驰分支却含位置句：%s" % ",".join(found)))
        else:
            rows.append((level, sym, name, scen, pos, "ok", "非背驰分支，无位置句（正确）"))
        return
    # 背驰分支
    if pos not in POS_STATES:
        rows.append((level, sym, name, scen, pos, "FAIL",
                     "背驰分支但 pos=%s 不在已知枚举 %s ⇒ 补充句落空（规则 172 静默退化）"
                     % (pos, list(POS_STATES))))
        return
    want = EXPECT[(kind, pos)]
    if want is None:
        if found:
            rows.append((level, sym, name, scen, pos, "FAIL",
                         "目标**未达成**（%s 对 %s）却含位置句：%s" % (kind, pos, ",".join(found))))
        else:
            rows.append((level, sym, name, scen, pos, "ok", "目标未达成，按设计不追加（正确）"))
    else:
        if found == [want]:
            rows.append((level, sym, name, scen, pos, "ok", "含「%s」（正确）" % want))
        elif not found:
            rows.append((level, sym, name, scen, pos, "FAIL",
                         "目标**已达成**（%s 对 %s）却**缺**位置句（缺：%s）" % (kind, pos, want)))
        else:
            rows.append((level, sym, name, scen, pos, "FAIL",
                         "位置句与 pos 不符：期望「%s」，实得 %s" % (want, found)))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "data.json")
    if not os.path.exists(path):
        print("ERROR data 不存在: %s" % path, file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    for sym, d in data.items():
        name = d.get("name") or sym
        kl = d.get("klines") or []
        wk = d.get("week_klines") or []
        mk = d.get("month_klines") or []
        if kl:
            check_one("日线", sym, name, analyze(kl)["classify"], rows)
        if wk:
            check_one("周线", sym, name, analyze(wk, MIN_BI_PCT_WEEK)["classify"], rows)
        if mk:
            check_one("月线", sym, name, analyze(mk, MIN_BI_PCT_MONTH)["classify"], rows)

    print("=" * 96)
    print("R543 背驰分支「目标达成度」文案一致性（双向判据）  data=%s" % path)
    print("=" * 96)
    print("%-8s %-10s %-8s %-14s %-8s %-5s %s" % ("层级", "代码", "名称", "情景", "pos", "判定", "说明"))
    print("-" * 96)
    for r in rows:
        print("%-8s %-10s %-8s %-14s %-8s %-5s %s" % r)

    bad = [r for r in rows if r[5] == "FAIL"]
    hit = [r for r in rows if "含「" in r[6]]
    print("-" * 96)
    print("命中位置句 %d 条；共检查 %d 条（日+周+月 × %d 标的）" % (len(hit), len(rows), len(data)))
    if bad:
        print("结论: ❌ FAIL —— %d 条不一致（详见上表）" % len(bad))
        return 1
    print("结论: ✅ PASS —— 背驰分支的位置句与 pos 完全一致（正向 4 组合 + 反向 5 组合 + 非背驰）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
