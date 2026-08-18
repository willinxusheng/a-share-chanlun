# -*- coding: utf-8 -*-
"""
audit_point_bias.py  —  缠论推演「点预测水平(价位)偏置」监控门禁（R85 新增，关12）

目的：前 11 道门禁已覆盖——带覆盖(关4/关7)、带锐度(关11)、方向命中(关8)、概率诚实性(关10)、
      点前完整性(关9/关1)、漂移(关6)、情绪(关5)。但有一个用户天天盯着的数字从未被系统性验证：
      **「价位目标」准不准**——即主路径目标(main) 与 统计中位期望(med) 相对后来真实收盘的
      「水平偏置」(level bias)。关8 只验方向(±), 关11 只验带宽, 关10 只验概率;
      本门禁验「价位目标准不准」, 与三者正交。

为什么要验：
  - med = last·exp(近窗口均值对数收益·f), 理论上是「无偏期望」。但若市场有动量/均值回归/漂移,
    近窗口均值会是系统性偏估计 → med 会系统性偏高(乐观)或偏低(悲观)。符号检验可揪出。
  - main = 缠论结构演绎的「方向性目标」, 本就允许偏离; 但若偏离呈系统性单向(永远乐观),
    则「路径偏离提示」(R62, >8%才提示) 不足以覆盖的盲区被暴露。

统计口径（避免前视, 与关4 完全一致）：
  - 每个锚点截断 klines 至 anchor 日(含), realized 取 anchor 之后第 H(8/30) 个交易日收盘。
  - main_bias = (real - main)/main ;  med_bias = (real - med)/med
  - 用中位 + 符号检验(二项双尾正态近似) 判断系统性偏置, 不盲信均值(肥尾虚高)。

退出码：0（仅打印 + 监控, 不阻断 CI）。严守纪律: 不改动任何模型数学, 仅透明化监控。
"""
import json
import math
import os
import statistics
import sys

from chanlun import analyze, adaptive_horizon
from report import forecast_svg
from audit_forecast_calibration import (
    H_TARGETS, ANCHOR_STEP, MIN_HISTORY, find_proj, classify_regime,
)

BASE = os.path.dirname(os.path.abspath(__file__))


def sign_test_p(n_pos, n_neg):
    """二项双尾 p 值(正态近似, 连续校正) 检 H0: p=0.5。返回 p(越小=越系统性偏)。"""
    n = n_pos + n_neg
    if n < 2:
        return 1.0
    # 偏离 0.5 的标准数
    z = (abs(n_pos - n / 2.0) - 0.5) / math.sqrt(n / 4.0)
    z = max(0.0, z)
    # 双尾: 2 * (1 - Φ(z))
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def summarize(vals):
    if not vals:
        return None
    n = len(vals)
    pos = sum(1 for v in vals if v > 0)
    neg = sum(1 for v in vals if v < 0)
    med = statistics.median(vals)
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    p = sign_test_p(pos, neg)
    return {"n": n, "mean": mean, "median": med, "sd": sd,
            "pos": pos, "neg": neg, "p": p}


def run():
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    symbols = list(data.keys())
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    base = kls[symbols[0]]
    n_base = len(base)

    # 主路径 / 中位 水平偏置聚合
    agg_main = {sym: {h: [] for h in H_TARGETS} for sym in symbols}
    agg_med = {sym: {h: [] for h in H_TARGETS} for sym in symbols}

    # 分 regime（仅 med 期望值做无偏性检验, 这是「无偏期望」是否真无偏的核心问题）
    REGIMES = ("bull", "bear", "range")
    regime_med = {rg: {h: [] for h in H_TARGETS} for rg in REGIMES}

    i = MIN_HISTORY
    while i < n_base - 35:
        date_i = base[i]["date"]
        for sym in symbols:
            kl = kls[sym]
            if i >= len(kl) or kl[i]["date"] != date_i:
                continue
            trunc = kl[:i + 1]
            try:
                r = analyze(trunc)
                horizon = adaptive_horizon(r["bis"], r["merged"])
                _svg, _note, _probs, _leg, fc = forecast_svg(
                    trunc, r, r["classify"], 50.0, 0.0, sym, horizon)
            except Exception:
                continue
            proj = fc["proj"]
            rg = classify_regime(trunc)
            for H in H_TARGETS:
                if H > horizon:
                    continue
                row = find_proj(proj, H)
                if row is None:
                    continue
                real = kl[i + H]["close"]
                main_v, med_v = row["main"], row["med"]
                if main_v and med_v:
                    agg_main[sym][H].append((real - main_v) / main_v)
                    agg_med[sym][H].append((real - med_v) / med_v)
                    regime_med[rg][H].append((real - med_v) / med_v)
        i += ANCHOR_STEP

    # ---- 汇总打印 ----
    print("=" * 92)
    print("R85 点预测水平偏置监控(关12) — 主路径目标 vs 统计中位期望 相对真实收盘的水平偏置")
    print("=" * 92)
    print("口径: bias=(real-forecast)/forecast; 中位稳健; 符号检验 p<0.05 即「系统性偏置」")
    print("-" * 92)

    def fmt(s):
        if s is None:
            return "  n/a"
        return (" N=%4d 中位=%+6.2f%% 均值=%+6.2f%% σ=%5.2f%% "
                "正=%4d/负=%4d 符号p=%.4f" % (
                    s["n"], s["median"] * 100, s["mean"] * 100, s["sd"] * 100,
                    s["pos"], s["neg"], s["p"]))

    any_warn = False
    print("【统计中位期望 med —— 应≈无偏(中位偏置阈值 3%, 符号p<0.05 告警)】")
    for sym in symbols:
        nm = data[sym].get("name", sym)
        for H in H_TARGETS:
            s = summarize(agg_med[sym][H])
            warn = (s is not None and (abs(s["median"]) > 0.03 or s["p"] < 0.05))
            any_warn = any_warn or warn
            print("  %-9s T+%-2d %s %s" % (nm, H, fmt(s), "  ⚠偏置" if warn else ""))
    print("-" * 92)
    print("【缠论主路径目标 main —— 允许偏离(方向性目标), 阈值 5%】")
    for sym in symbols:
        nm = data[sym].get("name", sym)
        for H in H_TARGETS:
            s = summarize(agg_main[sym][H])
            warn = (s is not None and abs(s["median"]) > 0.05)
            any_warn = any_warn or warn
            print("  %-9s T+%-2d %s %s" % (nm, H, fmt(s), "  ⚠偏置" if warn else ""))
    print("-" * 92)
    print("【分市场环境 med 无偏性(暴露平均掩盖的弱点)】")
    for rg in REGIMES:
        for H in H_TARGETS:
            s = summarize(regime_med[rg][H])
            warn = (s is not None and (abs(s["median"]) > 0.03 or s["p"] < 0.05))
            any_warn = any_warn or warn
            lab = {"bull": "牛", "bear": "熊", "range": "震荡"}[rg]
            print("  %-4s T+%-2d %s %s" % (lab, H, fmt(s), "  ⚠偏置" if warn else ""))
    print("=" * 92)
    print("结论: %s" % (
        "⚠ WARN — 存在系统性水平偏置(监控, 不阻断; 如实标注给用户)"
        if any_warn else
        "✅ 点预测水平无显著系统性偏置(中位≈0 且符号检验不显著)"))
    print("注: 退出码恒0; 仅透明化监控, 不改动模型数学。med 代表「无偏期望」若告警,"
          "说明近窗口均值不是好无偏估计(动量/均值回归/漂移), 可作未来数学改进线索。")
    return any_warn


def selftest():
    """验证符号检验与聚合: 100 样本全正 → p≈0; 50/50 → p≈1; 70正30负 → p<0.05。"""
    ok = True
    # (名称, 正数, 负数, 期望: 'near0' | 'near1' | 'lt', 阈值)
    cases = [
        ("全正(应 p≈0)", 100, 0, "near0", 0.001),
        ("均衡(应 p≈1)", 50, 50, "near1", 0.9),
        ("70正30负(应 p<0.05)", 70, 30, "lt", 0.05),
    ]
    for name, p_, n_, kind, th in cases:
        got = sign_test_p(p_, n_)
        if kind == "near0":
            good = got < th
        elif kind == "near1":
            good = got > th
        else:
            good = got < th
        ok = ok and good
        print("  %-22s p=%.4f %s" % (name, got, "OK" if good else "FAIL"))
    # 聚合中位
    s = summarize([0.01, -0.02, 0.03, -0.01, 0.0])
    print("  聚合中位校验: %.4f (期望~0.0) %s" % (s["median"], "OK" if abs(s["median"]) < 0.001 else "FAIL"))
    ok = ok and abs(s["median"]) < 0.001
    print("SELFTEST:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    run()
    sys.exit(0)
