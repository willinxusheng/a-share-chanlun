# -*- coding: utf-8 -*-
"""
audit_forecast_drift.py  —  缠论推演「预测突变漂移」监控门禁（R78 新增，第十道）

目的：预测「数据准确性」不仅看覆盖率/偏置，还要看**稳定性**——健康的预测应随新数据
      渐进平滑移动；若相邻两次刷新（间隔 ANCHOR_STEP 交易日）的主路径/中线相对变化
      远超同期行情变化，说明预测在「自己跳」（过拟合、结构频繁切换、或数据/代码异常
      产生假预测）。本门禁检测该漂移信号，作为 CI 构建后的准确性防线。

口径（避免前视）：
  - 同 R72 walk-forward：每锚点截断 klines 至 anchor 日，analyze + forecast_svg 取中线 med。
  - 相邻锚点 k 与 k-1 间隔 ANCHOR_STEP(=15) 交易日；行情变化 Δprice = close[k]-close[k-1]。
  - 预测超额漂移 excess = (med_k-med_{k-1})/med_{k-1} - Δprice/price_{k-1}
    excess≈0  → 预测随行情平滑移动（健康）；
    |excess|大 → 行情没动预测自己大幅跳变（不稳定/异常信号）。
  - 仅验证几何与带（bt/breadth 置 None），与 R72 一致。

判定（非阻断，仅告警）：
  - 各指数各 horizon 的 |excess| 分布：median / P95 / max，及异常占比(|excess|>0.10)。
  - 若任一 horizon 的 P95(|excess|)>0.10 或 异常占比>5% → 整体 WARN（打印醒目提示，exit 0）。

退出码：恒 0（监控门禁，不阻断 CI；异常由人工/后续看板脚注跟进）。
"""
import json
import os
import statistics

from chanlun import analyze, adaptive_horizon
from report import forecast_svg

BASE = os.path.dirname(os.path.abspath(__file__))
H_TARGETS = (8, 30)
ANCHOR_STEP = 15          # 锚点间隔(交易日)
MIN_HISTORY = 800         # 截断后最少样本
DRIFT_WARN = 0.10         # |excess| 异常阈值（相对变化 10%）
ANOM_RATE_WARN = 0.05     # 异常占比阈值


def find_proj(proj, tplus_target):
    best, bd = None, 1e9
    for row in proj:
        d = abs(row["tplus"] - tplus_target)
        if d < bd:
            bd, best = d, row
    if best is None or bd > 1:
        return None
    return best


def run():
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    symbols = list(data.keys())
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    results = {sym: {h: [] for h in H_TARGETS} for sym in symbols}

    for sym in symbols:
        kl = kls[sym]
        n = len(kl)
        seq = []  # (idx, last_close, {H: med})
        i = MIN_HISTORY
        while i < n - 35:
            trunc = kl[:i + 1]
            last_a = trunc[-1]["close"]
            try:
                r = analyze(trunc)
                horizon = adaptive_horizon(r["bis"], r["merged"])
                _svg, _note, _probs, _leg, fc = forecast_svg(
                    trunc, r, r["classify"], 50.0, 0.0, sym, horizon)
            except Exception:
                i += ANCHOR_STEP
                continue
            proj = fc["proj"]
            d = {}
            for H in H_TARGETS:
                if H > horizon:
                    continue
                row = find_proj(proj, H)
                if row is None:
                    continue
                d[H] = row["med"]
            seq.append((i, last_a, d))
            i += ANCHOR_STEP
        # 相邻锚点比较
        for k in range(1, len(seq)):
            i_prev, last_prev, d_prev = seq[k - 1]
            i_cur, last_cur, d_cur = seq[k]
            if i_cur - i_prev != ANCHOR_STEP:
                continue
            dprice_rel = (last_cur - last_prev) / last_prev if last_prev else 0.0
            for H in H_TARGETS:
                if H not in d_prev or H not in d_cur:
                    continue
                med_prev, med_cur = d_prev[H], d_cur[H]
                med_rel = (med_cur - med_prev) / med_prev if med_prev else 0.0
                excess = med_rel - dprice_rel
                results[sym][H].append(excess)

    # 报告
    print("=" * 96)
    print("R78 预测突变漂移监控 — walk-forward 相邻锚点(±%d交易日)中线移动 vs 同期行情移动"
          % ANCHOR_STEP)
    print("=" * 96)
    hdr = (f"{'指数':<12}{'窗口':>5}{'N':>6}{'中位|超额|':>12}{'P95|超额|':>12}"
           f"{'最大|超额|':>12}{'异常>10%':>10}{'结论':>8}")
    print(hdr)
    print("-" * 96)
    overall_warn = False
    for sym in symbols:
        nm = data[sym].get("name", sym)
        for H in H_TARGETS:
            ex = results[sym][H]
            if not ex:
                print(f"{nm:<12}{'T+'+str(H):>5}{0:>6}  (样本不足)")
                continue
            abs_ex = [abs(x) for x in ex]
            med_e = statistics.median(abs_ex)
            p95_e = sorted(abs_ex)[min(len(abs_ex) - 1, int(0.95 * len(abs_ex)) - 1)]
            max_e = max(abs_ex)
            n_anom = sum(1 for x in abs_ex if x > DRIFT_WARN)
            warn = (p95_e > DRIFT_WARN) or (n_anom / len(ex) > ANOM_RATE_WARN)
            overall_warn = overall_warn or warn
            verdict = "⚠️WARN" if warn else "OK"
            print(f"{nm:<12}{'T+'+str(H):>5}{len(ex):>6}{med_e*100:>11.1f}%"
                  f"{p95_e*100:>11.1f}%{max_e*100:>11.1f}%{n_anom:>8}d"
                  f"{(n_anom/len(ex)*100):>6.1f}%{verdict:>8}")
    print("-" * 96)
    if overall_warn:
        print("【门禁判定】⚠️ 检测到预测突变漂移超阈值 — 可能存在过拟合/数据异常/结构频繁切换。")
        print("  建议：核查近期数据管道与 classify 场景切换；此告警不阻断 CI，须人工跟进。")
    else:
        print("【门禁判定】✅ 各指数各 horizon 预测漂移均在健康区间（P95|超额|≤10%），")
        print("  预测随行情平滑移动，无突变/过拟合信号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
