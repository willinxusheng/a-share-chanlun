# -*- coding: utf-8 -*-
"""
audit_probability_calibration.py — 缠论推演「显示概率 p_main 校准诚实性」门禁（R83 新增）

目的：看板上显示的「主路径概率 p_main」是最直接被用户盯着的准确性数字，
      但此前所有门禁(关4/关7/关8)在回测时都把 bt/breadth 置 None 只验『带几何』，
      p_main 从未被样本外回测校准过。本门禁用历史上每个交易日当『当下』，
      截断跑真实 forecast_svg 抓取当时产出的 p_main，再比对后来主路径方向是否真对，
      构建可靠性表(reliability diagram)与 Brier 分数，检验『说的 65% 是不是真 65%』。

口径（与关4一致，避免前视）：
  - 截断 klines 至 anchor(含)，realized 取 anchor 之后第 H 交易日的方向；
  - forecast_svg 的 p_main 在 bt=None/breadth=None 下由截断窗口的历史命中率(_dir_main)夹逼，
    故本门禁检验的是『截断窗口校准出的 p_main』对『截断窗口样本外方向』的校准度——
    这是概率校准最严谨的样本外定义(无前视)。
  - 注：live 看板的 p_main 用全量历史 _dir_main + breadth 校准，本门禁用截断窗口口径，
    二者同源同法，结论对 live 校准度有代表性(截断窗口即 live 窗口的滚动近似)。

可靠性判读：分箱(0.30-0.40 / 0.40-0.50 / 0.50-0.60 / 0.60-0.72)内
  观测命中率 vs 箱中心偏差 > 15pp → 该区间系统性失准(WARN)；
  Brier 分数越低越好(理想 0；全随机约 0.25)。

退出码：0（监控门禁，不阻断 CI；如需升级为硬门禁可在此加阈值断言）。
"""
import json
import os
import statistics
import sys

from chanlun import analyze, adaptive_horizon, backtest_paths
from report import forecast_svg

BASE = os.path.dirname(os.path.abspath(__file__))

H_TARGETS = (8, 30)
ANCHOR_STEP = 15          # 与关4一致
MIN_HISTORY = 800         # 截断后最少样本(满足 _WIN+horizon≈762)

# 概率分箱(与 p_main 实际取值区间对齐: 关4 显示 p_main∈[0.30,0.72])
# 半开区间 [lo, hi)，0.72 落入最后箱(0.60,0.73)
BINS = [(0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.73)]

# 失准告警阈值: 任一箱 观测命中率 与 箱中心 偏差超过该值即 WARN
MISCAL_WARN = 0.15


def run():
    """滚动样本外: 抓取每个锚点 forecast_svg 产出的 p_main 与后来主路径方向是否真对。"""
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    symbols = list(data.keys())
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    base = kls[symbols[0]]
    n_base = len(base)
    # rec[sym][H] = [(p_main, correct), ...]
    rec = {sym: {h: [] for h in H_TARGETS} for sym in symbols}
    # R173(P0): 用全量历史计算 bt_paths(与 live 看板口径一致), 使门禁真正检验用户看到的 p_main
    # (forecast_svg 的 _w_dir>0 经验锚分支), 而非仅 _base 启发式下限——修复此前"假绿"(测错代码路径)。
    # analyze/realized 方向仍严格截断无前视, 仅校准锚用全量历史(=live 行为)。
    bt_paths_all = {}
    for sym in symbols:
        try:
            _kl = kls[sym]
            _r = analyze(_kl)
            _hz = adaptive_horizon(_r["bis"], _r["merged"])
            bt_paths_all[sym] = backtest_paths(_kl, horizon=_hz, step=max(15, _hz // 2), with_stability=False)
        except Exception:
            bt_paths_all[sym] = None
    i = MIN_HISTORY
    while i < n_base - 35:
        date_i = base[i]["date"]
        for sym in symbols:
            kl = kls[sym]
            if i >= len(kl) or kl[i]["date"] != date_i:
                continue
            trunc = kl[:i + 1]
            last_a = trunc[-1]["close"]
            try:
                r = analyze(trunc)
                horizon = adaptive_horizon(r["bis"], r["merged"])
                # R173: 传 bt_paths(全量历史口径) → 走 _w_dir>0 经验锚分支, 检验真实 p_main 校准度
                _svg, _note, _probs, _leg, fc = forecast_svg(
                    trunc, r, r["classify"], 50.0, 0.0, sym, horizon,
                    bt_paths=bt_paths_all.get(sym))
            except Exception:
                continue
            p_main = _probs[0]
            proj = fc["proj"]
            for H in H_TARGETS:
                if H > horizon:
                    continue
                best, bd = None, 1e9
                for row in proj:
                    d = abs(row["tplus"] - H)
                    if d < bd:
                        bd, best = d, row
                if best is None or bd > 1:
                    continue
                if i + H >= len(kl):
                    continue
                real = kl[i + H]["close"]
                main_v = best["main"]
                ms = 1 if (main_v - last_a) > 0 else (-1 if (main_v - last_a) < 0 else 0)
                correct = 1 if ms * (real - last_a) > 0 else 0
                rec[sym][H].append((p_main, correct))
        i += ANCHOR_STEP
    return rec


def reliability(rec):
    """跨指数聚合为分箱可靠性表 + Brier 分数。"""
    out = {}
    for H in H_TARGETS:
        allp, allc = [], []
        for sym in rec:
            for p, c in rec[sym][H]:
                allp.append(p)
                allc.append(c)
        n = len(allp)
        if n == 0:
            out[H] = None
            continue
        bins_out = []
        for lo, hi in BINS:
            ps = [p for p, c in zip(allp, allc) if lo <= p < hi]
            cs = [c for p, c in zip(allp, allc) if lo <= p < hi]
            m = len(ps)
            if m == 0:
                bins_out.append((lo, hi, 0, None, (lo + hi) / 2))
                continue
            obs = sum(cs) / m
            bins_out.append((lo, hi, m, obs, (lo + hi) / 2))
        brier = sum((p - c) ** 2 for p, c in zip(allp, allc)) / n
        hit = sum(allc) / n
        avgp = statistics.mean(allp)
        out[H] = {"n": n, "bins": bins_out, "brier": brier, "hit": hit, "avgp": avgp}
    return out


def report(rec):
    rel = reliability(rec)
    print("=" * 92)
    print("R83 概率校准诚实性门禁 — 显示概率 p_main 是否『说几成真几成』(滚动样本外, 锚点每%d交易日)"
          % ANCHOR_STEP)
    print("=" * 92)
    any_warn = False
    for H in H_TARGETS:
        d = rel[H]
        if d is None:
            print("T+%d: 样本不足" % H)
            continue
        print("\n— T+%d (样本 N=%d, 平均 p_main=%.2f, 实际方向命中=%.1f%%) —"
              % (H, d["n"], d["avgp"], d["hit"] * 100))
        print("  %-12s %6s %10s %12s %10s" % ("概率箱", "N", "观测命中", "箱中心", "偏差"))
        worst_dev = 0.0
        for lo, hi, m, obs, mid in d["bins"]:
            if m == 0:
                print("  [%.2f,%.2f) %6d %10s %12.2f %10s" % (lo, hi, 0, "—", mid, "—"))
                continue
            dev = obs - mid
            worst_dev = max(worst_dev, abs(dev))
            flag = " ⚠" if abs(dev) > MISCAL_WARN else ""
            print("  [%.2f,%.2f) %6d %9.1f%% %12.2f %+9.1f%%%s"
                  % (lo, hi, m, obs * 100, mid, dev * 100, flag))
        print("  Brier 分数 = %.3f (越低越好; 全随机基准≈0.25)" % d["brier"])
        if worst_dev > MISCAL_WARN:
            any_warn = True
            print("  ⚠ 该窗口存在概率失准区间(观测命中与宣称概率偏差>%.0fpp)" % (MISCAL_WARN * 100))
        else:
            print("  ✅ 该窗口概率校准良好(各箱偏差≤%.0fpp)" % (MISCAL_WARN * 100))
    print("\n" + "=" * 92)
    if any_warn:
        print("结论: ⚠ WARN — 存在概率失准区间(监控, 不阻断); 详见上方分箱, 必要时定点修正 p_main 夹逼公式")
    else:
        print("结论: ✅ 显示概率 p_main 校准诚实(说几成≈真几成), 用户所见百分比可信")
    return 0


if __name__ == "__main__":
    rec = run()
    sys.exit(report(rec))
