#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股全市场缠论雷达 —— 信号基线自更新（R531）

★ 背景（R531 实证，2026-11-20）：
  R526 的「产物健康门禁」用**绝对下限** `n_signal >= 50` 拦「信号族整体清空」。
  该阈值用 10 月 8 个历史产物标定（245~478，折算 ≈160~320），但没料到**单边
  下跌市况**：连续阴线 ⇒ MACD 单边扩张 ⇒ 新底背驰几乎不生成，而旧信号逐日
  滑出 7 交易日近端窗口 ⇒ n_signal 自然衰减（实测 478→261→55→51），最终跌破
  50 ⇒ 门禁把**市场状态**误判为**口径退化** ⇒ 全量扫描做完却拒绝入库，
  雷达停更（11-18/11-19/11-20 连续 3 天，见 run#1326~#1337）。

  修复 = 下限改为**动态**：`max(ABS_FLOOR, RATIO × ema)`。
  · ema = 通过门禁产物的 n_signal 指数滑动均值（0.8/0.2），存 radar/baseline.json；
  · 只采样**通过门禁**的产物（本脚本只在「产物自检」通过后被调用 ⇒ 坏数据
    不会拖低基线；门禁失败路径 job 直接红叉，本步骤不执行）；
  · ABS_FLOOR = 15：真灾难态（口径退化/信号族清空）必然远低于此；
  · 同一 asof 幂等（同日 force 重扫不重复累计）。

用法:
    python3 radar/update_baseline.py     # 须在 repo 根运行（读 radar/radar.json）
退出码:
    0 = 已更新(或幂等跳过);  1 = 异常
"""

import json, os

ABS_FLOOR = 8           # 绝对下限（真灾难态兜底，与 EMA 无关）
RATIO = 0.15            # 动态下限 = RATIO × ema
EMA_NEW = 0.2           # ema = 0.8*old + 0.2*new
HIST_KEEP = 10          # 基线文件保留最近 N 条样本（可审计性）

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")

d = json.load(open("radar/radar.json", encoding="utf-8"))
m = d["meta"]
asof, ns = m["asof"], int(m["n_signal"])

if os.path.exists(BASE):
    b = json.load(open(BASE, encoding="utf-8"))
else:
    # 基线文件丢失（不应发生——与 radar.json 同 commit；丢失则从当前产物重建）
    b = {"ema": float(ns), "n": 0, "updated": "", "history": []}

if b.get("updated") == asof:
    print("基线已是 %s(幂等, 同日重扫不重复累计), ema=%.1f" % (asof, b["ema"]))
    raise SystemExit(0)

old = float(b.get("ema", ns))
ema = old * (1 - EMA_NEW) + ns * EMA_NEW
hist = (b.get("history") or [])[-(HIST_KEEP - 1):]
hist.append({"asof": asof, "n_signal": ns})
out = {"ema": round(ema, 1), "n": int(b.get("n", 0)) + 1,
       "updated": asof, "history": hist}
json.dump(out, open(BASE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
floor = max(ABS_FLOOR, RATIO * ema)
print("基线更新: ema %.1f -> %.1f (样本 %d), 下一轮动态下限 = max(%d, %.1f) = %.1f"
      % (old, ema, out["n"], ABS_FLOOR, RATIO * ema, floor))
