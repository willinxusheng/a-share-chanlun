# -*- coding: utf-8 -*-
"""
audit_forecast_drift.py  —  缠论推演「预测突变漂移 + 系统性乘性偏置」监控门禁（R78 新增，第十道；R170 增强）

目的：预测「数据准确性」不仅看覆盖率/偏置，还要看**稳定性**与**系统性偏置**：
  1) 稳定性(突变漂移)：健康预测应随新数据渐进平滑移动；若相邻两次刷新主路径/中线相对变化
     远超同期行情变化，说明预测在「自己跳」(过拟合/结构频繁切换/数据异常)。见 excess 指标。
  2) 系统性偏置(乘性)：模型若恒定系统性高估/低估 X%，相邻移动差(excess)≈0 无法察觉
     (R169 指出的假阴性)——必须直接比对「预测中线 med」与「后来真实收盘 real」，按 regime 切片
     取中位偏置，与质量证书 bias_warn=5.0% 同口径告警。R170 新增此维度，堵住假阴性。

口径（避免前视）：
  - 同 R72 walk-forward：每锚点截断 klines 至 anchor 日，analyze + forecast_svg 取中线 med。
  - 相邻锚点 k 与 k-1 间隔 ANCHOR_STEP(=15) 交易日；行情变化 Δprice = close[k]-close[k-1]。
  - 突变漂移 excess = (med_k-med_{k-1})/med_{k-1} - Δprice/price_{k-1}。
  - 系统性偏置 bias = (real_{k+H} - med_k)/med_k（乘性，与 R72/R79 校准 baseline 同口径），
    按 classify_regime(trunc) 切片，仅 N>=20 桶参与判定(与证书 regime 偏置同阈值, 避免小样本噪声误报)。
  - 仅验证几何与带（bt/breadth 置 None），与 R72 一致。

判定（非阻断，仅告警）：
  - 突变漂移：P95(|excess|)>0.10 或 异常占比>5% → WARN。
  - 系统性偏置：任一 (指数, horizon, regime) 桶(N>=20) 的 |中位 bias| > 5.0% → WARN。

退出码：恒 0（监控门禁，不阻断 CI；异常由人工/后续看板脚注跟进）。
"""
import json
import os
import statistics
import sys

from chanlun import analyze, adaptive_horizon, classify_regime
from report import forecast_svg

BASE = os.path.dirname(os.path.abspath(__file__))
H_TARGETS = (8, 30)
ANCHOR_STEP = 15          # 锚点间隔(交易日)
MIN_HISTORY = 800         # 截断后最少样本
DRIFT_WARN = 0.10         # |excess| 异常阈值（相对变化 10%）
ANOM_RATE_WARN = 0.05     # 异常占比阈值
BIAS_WARN = 0.05          # 系统性乘性偏置阈值(与 quality_cert.json bias_warn 一致)
MIN_REGIME_N = 20         # regime 偏置判定最小样本(与 R167/R169 证书同口径)


def find_proj(proj, tplus_target):
    best, bd = None, 1e9
    for row in proj:
        d = abs(row["tplus"] - tplus_target)
        if d < bd:
            bd, best = d, row
    if best is None or bd > 1:
        return None
    return best


def run(quick=None):
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    symbols = list(data.keys())
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    results = {sym: {h: [] for h in H_TARGETS} for sym in symbols}
    # R170: 系统性偏置按 (指数, horizon, regime) 桶累计 (rg, bias)
    bias_results = {sym: {h: [] for h in H_TARGETS} for sym in symbols}

    for sym in symbols:
        kl = kls[sym]
        n = len(kl)
        seq = []  # (idx, last_close, {H: med})
        _ac = 0   # R166: --quick 锚点上限计数
        i = MIN_HISTORY
        while i < n - 35:
            if quick is not None and _ac >= quick:
                break
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
                # R170: 系统性偏置——直接比对预测中线 med 与后来真实收盘 real(H 日后)
                if i + H < len(kl):
                    real = kl[i + H]["close"]
                    med = row["med"]
                    if med:
                        bias = (real - med) / med
                        rg = classify_regime(trunc)
                        bias_results[sym][H].append((rg, bias))
            seq.append((i, last_a, d))
            _ac += 1
            i += ANCHOR_STEP
        # 相邻锚点比较(突变漂移)
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

    # ---- 突变漂移报告 ----
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
                  f"{p95_e*100:>11.1f}%{max_e*100:>11.1f}%{n_anom:>8}"
                  f"{(n_anom/len(ex)*100):>6.1f}%{verdict:>8}")
    print("-" * 96)
    if overall_warn:
        print("【突变漂移】⚠️ 检测到预测突变漂移超阈值 — 可能存在过拟合/数据异常/结构频繁切换。")
        print("  建议：核查近期数据管道与 classify 场景切换；此告警不阻断 CI，须人工跟进。")
    else:
        print("【突变漂移】✅ 各指数各 horizon 预测漂移均在健康区间（P95|超额|≤10%），")
        print("  预测随行情平滑移动，无突变/过拟合信号。")

    # ---- R170: 系统性乘性偏置报告(按 regime 切片, 与证书同口径) ----
    print("=" * 96)
    print("R170 系统性乘性偏置监控 — 预测中线 med vs 后来真实收盘 real, 按牛/熊/震荡切片(中位口径)")
    print("=" * 96)
    bh = (f"{'指数':<12}{'窗口':>5}{'regime':>9}{'N':>6}{'中位偏置':>12}{'结论':>8}")
    print(bh)
    print("-" * 96)
    bias_warn = False
    worst_bias = 0.0
    for sym in symbols:
        nm = data[sym].get("name", sym)
        for H in H_TARGETS:
            pairs = bias_results[sym][H]
            if not pairs:
                continue
            by_rg = {}
            for rg, b in pairs:
                by_rg.setdefault(rg, []).append(b)
            for rg, bs in by_rg.items():
                nb = len(bs)
                mb = statistics.median(bs) * 100
                if nb >= MIN_REGIME_N:
                    warn = abs(mb) > BIAS_WARN * 100
                    bias_warn = bias_warn or warn
                    worst_bias = max(worst_bias, abs(mb))
                    verdict = "⚠️WARN" if warn else "OK"
                    print(f"{nm:<12}{'T+'+str(H):>5}{rg:>9}{nb:>6}{mb:>11.1f}%{verdict:>8}")
                else:
                    print(f"{nm:<12}{'T+'+str(H):>5}{rg:>9}{nb:>6}{mb:>11.1f}%{'样本不足':>8}")
    print("-" * 96)
    if bias_warn:
        print("【系统性偏置】⚠️ 检测到 |中位乘性偏置| > %.0f%%(N>=%d 桶) — 模型存在系统性高估/低估,"
              % (BIAS_WARN * 100, MIN_REGIME_N))
        print("  与质量证书 bias_warn 同口径; 建议核查置信带中心口径或该 regime 样本。")
    else:
        print("【系统性偏置】✅ 各 (指数,horizon,regime) 桶(N>=%d) 中位乘性偏置均在 ±%.0f%% 内, 无系统性漂移。"
              % (MIN_REGIME_N, BIAS_WARN * 100))

    return {
        "drift_warn": overall_warn,
        "bias_warn": bias_warn,
        "worst_bias": worst_bias,
    }


if __name__ == "__main__":
    _quick = None
    if "--quick" in sys.argv:
        idx = sys.argv.index("--quick")
        try:
            _quick = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            _quick = 30
    run(_quick)
    raise SystemExit(0)
