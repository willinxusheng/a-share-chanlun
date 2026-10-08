#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股全市场缠论雷达 —— 产物健康门禁（**唯一来源**）

★ R526 抽出说明（规则 20「唯一来源」/ 规则 43「空集恒真=假绿」同族）:
  本文件此前是 `.github/workflows/radar-scan.yml` 步骤「产物自检」里的**内联 heredoc**。
  抽成独立文件的触发原因是一次**实测抓到的门禁盲区**：

    该 workflow 的「当日已扫描守卫」有 4 个 skip 出口，其中两个是**产物性**的 ——
    ①「本地产物已是今日」②「远端 main 已有今日产物」。命中即 `skip=true` 并 `exit 0`，
    而后续**全部**步骤（含「产物自检」）都带 `if: steps.guard.outputs.skip != 'true'`
    ⇒ 整条门禁链被跳过、job 仍报 **success**。

  实证（2026-10-08 节后首日）: `radar.json` 是旧口径代码产出的退化产物
  （`n_signal=17`，按新门禁 `>=50` 应当失败；且 `meta.fresh_max` 缺失），
  但当天所有**不带 force** 的定时调度全部走「本地产物已是今日」出口 ⇒ skip ⇒ **全绿**。
  对比同 head 的两次派发: `37774991596`（force=true，真跑）→ failure;
  `37776091490`（无 force）→ 步骤 4~8 **全部 skipped** → success。
  ⇒ 「坏产物」在当天**再也无人发现**，直到 R526 人工审计才暴露。

  故本脚本必须被**两处**调用（这就是"唯一来源"的意义）:
    ① 步骤「产物自检」—— 真扫之后校验新产物（原有行为，逐字不变）；
    ② 守卫的**两个产物性 skip 出口** —— 跳过前先校验**将要被沿用的那份产物**；
       不健康则**放弃跳过、放行全量重扫以自愈**（规则 98: 兜底/自愈必须落云端）。

用法:
    python3 radar/check_artifact.py              # 校验 radar/radar.json（须在 repo 根运行）
退出码:
    0 = 健康;  1 = 不健康（失败原因由 assert 消息给出）

注意: 断言体与 workflow 内联版本保持**逐字节一致**（R526 已用 diff 自证），
      任何口径调整只改本文件一处（规则 20）。
"""

import json, datetime
d = json.load(open("radar/radar.json", encoding="utf-8"))
m = d["meta"]
assert m["n_ok"] > 4000, "有效分析过少: %s" % m["n_ok"]
# ★ R526: 原为 `assert m["n_signal"] >= 0` —— **恒真断言**（规则 43「空集恒真=假绿」）：
#   信号族**整体清空**时它照样通过，而这正是线上真实发生过的退化态 ——
#   旧「近端窗口」用**自然日**（阈值 10），休市日也算老化 ⇒ 中秋休市后官方 signals
#   由 270（09-24）骤降为 **22**（09-28），CI 全绿、无人发现（探针 `_dbg/r526/r526_radar_fresh.py`）。
#   今口径改「交易日根数」后实测预期区间：8 个历史产物重算的信号数 245~478
#   （扣掉 ST/次新等门禁剔除后按同比例折算 ≈ 160~320）⇒ 取 **50** 作灾难态下界，
#   与 R495/R496「只拦灾难态、上线后按线上实测收紧」同风格。
assert m["n_signal"] >= 50, "近端信号族疑似整体清空(口径退化/门槛失配): %s" % m["n_signal"]
# 前端 revpool 的「超窗」阈值来自 meta.fresh_max（规则 20 禁第二份副本）；
# 缺失时前端会退回硬编码，二者一旦漂移就不再严格互补 ⇒ 这里做**契约存在性**校验
# （不钉死数值，留出后续标定空间）。
assert m.get("fresh_max", 0) > 0, "meta.fresh_max 未下发(前端 revpool 将退回硬编码): %r" % m.get("fresh_max")
# R495: 段级标注 / 背驰状态产出率 —— 防「整族静默为空」。
#   为什么必须硬断言: mark 里的新键是**整块写入**、不经白名单，若 chanlun 的
#   seg_signals/segments 接口变动或返回空，前端只会"图上少了段级点"——不报错，
#   也不进任何既有门禁。阈值取得宽松（样本实测个股 6/6、行业 5/5 均有），
#   只拦灾难态；首次上线后按线上实测值收紧。
# R496: 断言口径改读 `_eng` 后缀（**引擎侧全量**）—— 因为 mark 六类已从"按条数
#   截断"改为"只受 draw_n（前端画得出的 K 线根数）约束"；**mark 里只含前端画得出的**，
#   若断言仍读 mark 计数，就成了**用守卫去迁就前端契约**（前端根数一变断言跟着变，
#   跨轮不可比）。引擎侧计数回答的正是本断言的原本意图："引擎有没有产出这一族"。
#   另加一组**落盘覆盖度**断言（引擎有产出 ≠ mark 有落盘，两者互补）。
sm = m.get("seg_mark") or {}
assert sm.get("n_uni_segsig_eng", 0) > 100, "引擎段级买卖点为空: %s" % sm
assert sm.get("n_uni_segzs_eng", 0) > 100, "引擎段级中枢为空: %s" % sm
assert sm.get("n_ind_segsig_eng", 0) > 0, "行业引擎段级标注为空: %s" % sm
_nu = sm.get("n_uni", 0) or 1
assert sm.get("n_uni_mark_sig", 0) > 0.5 * _nu, "笔级买卖点落盘覆盖过少: %s" % sm
assert sm.get("n_uni_mark_line", 0) > 0.5 * _nu, "笔折线落盘覆盖过少: %s" % sm
# R496b: 落盘**总条数**下界 —— 用实测均值×0.5 兜（样本实测 sig 均值 24.75/只、
#   line 端点均值 179/只）× 6900 只 ⇒ sig ≈ 17 万、line 端点 ≈ 124 万。
#   这里只拦"退回按条数截断"这类灾难态（旧代码 sig 恒 ≤14/只 ⇒ 总数 ≤ 9.7 万）。
assert sm.get("n_uni_sig_total", 0) > 80000, "笔级买卖点总条数异常(疑似退回按条数截断): %s" % sm
assert sm.get("n_uni_line_pts_total", 0) > 500000, "笔折线总点数异常(疑似退回按条数截断): %s" % sm
# R496b: 「后端产出 ⊆ 前端可绘制」不变量 —— 这是**静默损耗**的唯一防线。
#   个股/ETF 后端缠论跑全历史(~1380 根) 而前端 getStkData 只取 660 根；
#   行业后端跑合成全历史(~1386 根) 而前端行业K线就是入库的 320 根。
#   ⇒ off 超过该端上界的标注会被前端 xi() 返回 null **静默剔除**（图上只是"少几条"，
#     不报任何错）。R496b 前实测：行业 sig 23 条丢 5 条；上证最近 3 笔中枢里有
#     off=1083 的（前端 320 根画不出）；笔折线首端点越界到 322~339。
#   故此处按**各端 K 线根数**硬断言偏移上界（与前端 radar.html 的取数根数联动）。
assert sm.get("fe_kline_n") == 660, "前端个股K线根数契约与 CI 预期不符(改 radar.html getStkData 须同步): %s" % sm.get("fe_kline_n")
assert sm.get("ind_kline_n") == 320, "行业K线根数契约与 CI 预期不符: %s" % sm.get("ind_kline_n")
_uo = sm.get("uni_off_max", -1)
assert 0 <= _uo <= sm.get("fe_kline_n", 0) - 1, \
  "个股标注偏移越界(前端会静默剔除): uni_off_max=%s, 前端K线=%s" % (_uo, sm.get("fe_kline_n"))
_io = sm.get("ind_off_max", -1)
assert 0 <= _io <= sm.get("ind_kline_n", 0) - 1, \
  "行业标注偏移越界(前端会静默剔除): ind_off_max=%s, 行业K线=%s" % (_io, sm.get("ind_kline_n"))
print("段级标注(引擎侧): 个股 segsig %d/%d · 段级中枢 %d · 行业 segsig %d/%d · 活跃段级背驰 %d 只"
      % (sm.get("n_uni_segsig_eng", 0), sm.get("n_uni", 0), sm.get("n_uni_segzs_eng", 0),
         sm.get("n_ind_segsig_eng", 0), sm.get("n_ind", 0),
         sm.get("n_bc_state_seg", 0)))
print("落盘覆盖(只受 draw_n 约束): 笔级点 %d/%d 只(共 %d 条) · 笔折线 %d 只(共 %d 点) · 背驰 %d 只"
      % (sm.get("n_uni_mark_sig", 0), sm.get("n_uni", 0),
         sm.get("n_uni_sig_total", 0), sm.get("n_uni_mark_line", 0),
         sm.get("n_uni_line_pts_total", 0), sm.get("n_uni_mark_bc", 0)))
print("窗口一致性: 个股 off_max=%s/%s · 行业 off_max=%s/%s(不变量: 后端产出 ⊆ 前端可绘制)"
      % (sm.get("uni_off_max"), sm.get("fe_kline_n", 0) - 1,
         sm.get("ind_off_max"), sm.get("ind_kline_n", 0) - 1))
print("自检通过: 有效%d 信号%d asof=%s" % (m["n_ok"], m["n_signal"], m["asof"]))
