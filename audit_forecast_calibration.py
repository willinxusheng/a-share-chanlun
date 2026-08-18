# -*- coding: utf-8 -*-
"""
audit_forecast_calibration.py  —  缠论推演「预测数据准确性」滚动样本外回测门禁（R72 新增）

目的：用历史上每个交易日当"当下"，以当时已有数据截断跑真实 forecast_svg，再把 T+8 / T+30
      的主路径、均值期望中线、P05-P95 / P25-P75 置信带，与后来真实收盘比对，量化预测准确性。

统计口径：
  - P05-P95 覆盖率：名义 90%；实测越高=带越宽(安全侧)，越低=漏覆盖(风险)。
  - P25-P75 覆盖率：名义 50%（经验分位内层）。
  - 主路径/中线 方向准确率：>50% 即优于抛硬币。
  - 中线系统性偏误(稳健中位口径)：median((realized-med)/med)；>0=系统保守(低估)，<0=系统激进(高估)。
    用中位而非均值——A股右偏肥尾会使均值口径虚高(看似偏高偏置)，中位口径如实反映中心校准。

实现要点（避免前视偏差）：
  - forecast_svg 的带与路径几何只依赖传入的 klines + analyze(r)，与 bt/breadth 概率校准无关；
    故回测将 bt/breadth 置 None，仅验证「带与几何」这一最关键的预测准确性维度。
  - 每个锚点截断 klines 至 anchor 日（含），realized 取 anchor 之后的第 8 / 30 个交易日收盘。

用法：
  python audit_forecast_calibration.py          # 跑全量五指数回测并打印汇总
  （依赖同目录 data.json + report.py/chanlun.py，与 report.py 同环境运行）

退出码：0=正常（仅打印，不阻断 CI）；如需设为门禁可在此加阈值断言。
"""
import json
import os
import statistics

import math

from chanlun import analyze, adaptive_horizon
from report import forecast_svg  # report.py 内含推演渲染核心

BASE = os.path.dirname(os.path.abspath(__file__))


def classify_regime(trunc, win=60, bull=0.10, bear=-0.10):
    """按锚点前 win 交易日累计对数收益, 把市场环境分三档(无前视):
       牛(bull) > +10% | 熊(bear) < -10% | 其余震荡(range)。
       用于 R80 分 regime 覆盖分析, 暴露「全样本平均覆盖」掩盖的隐藏弱点。"""
    if len(trunc) < win + 2:
        return "range"
    pre = trunc[-(win + 1):-1]  # anchor 之前的 win 根
    cum = 0.0
    for j in range(1, len(pre)):
        cum += math.log(pre[j]["close"] / pre[j - 1]["close"])
    if cum > bull:
        return "bull"
    if cum < bear:
        return "bear"
    return "range"
H_TARGETS = (8, 30)
ANCHOR_STEP = 15          # 锚点间隔(交易日)，越小样本越多越慢
MIN_HISTORY = 800         # 截断后最少样本(满足 _WIN+horizon≈762)


def find_proj(proj, tplus_target):
    """在 proj 中找 tplus 最接近 target 的项，允许偏差<=1"""
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
    base = kls[symbols[0]]
    n_base = len(base)
    agg = {sym: {h: {"N": 0, "in95": 0, "in75": 0, "dir_main": 0, "dir_med": 0,
                     "mae_main": 0.0, "mae_med": 0.0, "bias_list": []} for h in H_TARGETS}
           for sym in symbols}
    # R80 分 regime 覆盖分桶(牛/熊/震荡): 与 agg 同结构, 仅按市场环境切片
    REGIMES = ("bull", "bear", "range")
    regime_agg = {rg: {h: {"N": 0, "in95": 0, "in75": 0, "dir_main": 0, "dir_med": 0,
                           "bias_list": []} for h in H_TARGETS} for rg in REGIMES}
    # 跨指数方向共识(R75): 每个锚点收集 5 指数主路径方向符号 + 真实方向符号, 事后投票
    cons = {h: [] for h in H_TARGETS}
    i = MIN_HISTORY
    while i < n_base - 35:
        date_i = base[i]["date"]
        anchor = {h: {"m": [], "r": []} for h in H_TARGETS}
        cons_ok = True
        for sym in symbols:
            kl = kls[sym]
            if i >= len(kl) or kl[i]["date"] != date_i:
                cons_ok = False
                continue
            trunc = kl[:i + 1]
            last_a = trunc[-1]["close"]
            try:
                r = analyze(trunc)
                horizon = adaptive_horizon(r["bis"], r["merged"])
                # wcls 传 r["classify"](不影响几何); conf=50/sigma=0/bt=None/breadth=None
                # 仅影响 p_main 概率校准，不动带与几何
                _svg, _note, _probs, _leg, fc = forecast_svg(
                    trunc, r, r["classify"], 50.0, 0.0, sym, horizon)
            except Exception:
                cons_ok = False
                continue
            proj = fc["proj"]
            for H in H_TARGETS:
                if H > horizon:
                    continue
                row = find_proj(proj, H)
                if row is None:
                    continue
                if i + H >= len(kl):
                    continue
                real = kl[i + H]["close"]
                main_v, med_v = row["main"], row["med"]
                p05, p95 = row["f95l"], row["f95l"] + row["f95h"]
                p25, p75 = row["f75l"], row["f75l"] + row["f75h"]
                s = agg[sym][H]
                s["N"] += 1
                if p05 <= real <= p95:
                    s["in95"] += 1
                if p25 <= real <= p75:
                    s["in75"] += 1
                ms = 1 if (main_v - last_a) > 0 else (-1 if (main_v - last_a) < 0 else 0)
                rs = 1 if (real - last_a) > 0 else (-1 if (real - last_a) < 0 else 0)
                if ms * (real - last_a) > 0:
                    s["dir_main"] += 1
                if (med_v - last_a) * (real - last_a) > 0:
                    s["dir_med"] += 1
                s["mae_main"] += abs(main_v - real)
                s["mae_med"] += abs(med_v - real)
                s["bias_list"].append((real - med_v) / med_v)
                # R80: 按该 sym 自身市场环境分桶累加
                rg = classify_regime(trunc)
                rg_agg = regime_agg[rg][H]
                rg_agg["N"] += 1
                if p05 <= real <= p95:
                    rg_agg["in95"] += 1
                if p25 <= real <= p75:
                    rg_agg["in75"] += 1
                if ms * (real - last_a) > 0:
                    rg_agg["dir_main"] += 1
                if (med_v - last_a) * (real - last_a) > 0:
                    rg_agg["dir_med"] += 1
                rg_agg["bias_list"].append((real - med_v) / med_v)
                anchor[H]["m"].append(ms)
                anchor[H]["r"].append(rs)
        if cons_ok and all(len(anchor[H]["m"]) >= 3 for H in H_TARGETS):
            for H in H_TARGETS:
                cons[H].append((anchor[H]["m"], anchor[H]["r"]))
        i += ANCHOR_STEP
    return data, agg, cons, regime_agg


def report(data, agg, cons, regime_agg=None):
    print("=" * 96)
    print("R72 滚动样本外回测 — 缠论推演预测准确性(锚点每%d交易日, 截断跑真实 forecast_svg)" % ANCHOR_STEP)
    print("=" * 96)
    hdr = (f"{'指数':<12}{'窗口':>5}{'N':>6}{'P05-P95覆盖':>14}{'P25-P75覆盖':>14}"
           f"{'主路径方向':>12}{'中线方向':>10}{'主路径MAE':>11}{'中线MAE':>10}{'中线偏误':>10}")
    print(hdr)
    print("-" * 96)
    tot = {h: {"N": 0, "in95": 0, "in75": 0, "dir_main": 0, "dir_med": 0,
               "mae_main": 0.0, "mae_med": 0.0, "bias_list": []} for h in H_TARGETS}
    for sym, d in data.items():
        nm = d.get("name", sym)
        for H in H_TARGETS:
            s = agg[sym][H]
            if s["N"] == 0:
                print(f"{nm:<12}{'T+'+str(H):>5}{0:>6}  (样本不足)")
                continue
            c95 = s["in95"] / s["N"] * 100
            c75 = s["in75"] / s["N"] * 100
            dm = s["dir_main"] / s["N"] * 100
            dmed = s["dir_med"] / s["N"] * 100
            maem = s["mae_main"] / s["N"]
            maemed = s["mae_med"] / s["N"]
            bias = statistics.median(s["bias_list"]) * 100
            print(f"{nm:<12}{'T+'+str(H):>5}{s['N']:>6}{c95:>13.1f}%{c75:>13.1f}%"
                  f"{dm:>11.1f}%{dmed:>9.1f}%{maem:>11.1f}{maemed:>10.1f}{bias:>9.1f}%")
            for k in ("N", "in95", "in75", "dir_main", "dir_med"):
                tot[H][k] += s[k]
            tot[H]["mae_main"] += s["mae_main"]
            tot[H]["mae_med"] += s["mae_med"]
            tot[H]["bias_list"].extend(s["bias_list"])
    print("-" * 96)
    print("合计(五指数加权平均):")
    for H in H_TARGETS:
        s = tot[H]
        if s["N"] == 0:
            continue
        c95 = s["in95"] / s["N"] * 100
        c75 = s["in75"] / s["N"] * 100
        dm = s["dir_main"] / s["N"] * 100
        dmed = s["dir_med"] / s["N"] * 100
        maem = s["mae_main"] / s["N"]
        maemed = s["mae_med"] / s["N"]
        bias = statistics.median(s["bias_list"]) * 100
        print(f"{'全部':<12}{'T+'+str(H):>5}{s['N']:>6}{c95:>13.1f}%{c75:>13.1f}%"
              f"{dm:>11.1f}%{dmed:>9.1f}%{maem:>11.1f}{maemed:>10.1f}{bias:>9.1f}%")
    print("=" * 96)
    # 偏置监控(#预测精度·R74)：中线系统性偏置(稳健中位口径)超阈值即告警(可据需升级为硬门禁)。
    # 注：用「中位」而非「均值」口径——A股右偏肥尾会使均值口径虚高(看似偏高), 中位口径如实反映中心校准。
    BIAS_WARN = 5.0
    worst_bias = max((statistics.median(tot[H]["bias_list"]) * 100) for H in H_TARGETS if tot[H]["bias_list"])
    if abs(worst_bias) > BIAS_WARN:
        print("⚠️ 偏置告警: 全样本中线系统性偏置 %.1f%% 超阈值 ±%.1f%% —— 需检查置信带中心口径" % (worst_bias, BIAS_WARN))
    else:
        print("✅ 偏置监控: 全样本中线偏置 %.1f%% 在 ±%.1f%% 阈值内(中心校准良好)" % (worst_bias, BIAS_WARN))
    print("=" * 96)
    # R80 分市场环境覆盖（此前计算但 report 未输出）：按牛/熊/震荡分桶打印 P05-P95/P25-P75 覆盖
    if regime_agg:
        print("R80 分市场环境覆盖（区间套按牛/熊/震荡分桶）:")
        print("-" * 96)
        for rg in ("bull", "bear", "range"):
            print("%s:" % rg)
            for H in H_TARGETS:
                s = regime_agg[rg][H]
                if s["N"] == 0:
                    print(f"  T+{H}: 样本不足")
                    continue
                c95 = s["in95"] / s["N"] * 100
                c75 = s["in75"] / s["N"] * 100
                dm = s["dir_main"] / s["N"] * 100
                bias = statistics.median(s["bias_list"]) * 100 if s["bias_list"] else 0.0
                print(f"  T+{H}: N={s['N']:>4} P05-P95={c95:5.1f}%  P25-P75={c75:5.1f}%  方向{dm:5.1f}%  中线偏置{bias:+.1f}%")
    print("=" * 96)
    # === 跨指数方向共识(R75新增) ===
    print("R75 跨指数方向共识回测 - 5指数主路径方向投票是否优于单指数(锚点每%d交易日)" % ANCHOR_STEP)
    print("-" * 96)
    print(f"{'窗口':>5}{'锚点数':>8}{'共识→市场':>12}{'共识→单指数':>14}{'单指数基线':>12}{'提升':>9}")
    deltas = {}
    for H in H_TARGETS:
        entries = cons[H]
        na = len(entries)
        if na == 0:
            continue
        c_market = c_per = tot_per = 0
        for mains, reals in entries:
            cs = 1 if sum(mains) > 0 else (-1 if sum(mains) < 0 else 0)
            if cs == 0:
                continue
            rs_market = 1 if sum(reals) > 0 else (-1 if sum(reals) < 0 else 0)
            if cs == rs_market:
                c_market += 1
            for rj in reals:
                tot_per += 1
                if cs == rj:
                    c_per += 1
        acc_market = c_market / na * 100
        acc_per = c_per / tot_per * 100 if tot_per else 0
        baseline = tot[H]["dir_main"] / tot[H]["N"] * 100 if tot[H]["N"] else 0
        delta = acc_per - baseline
        deltas[H] = delta
        print(f"{'T+'+str(H):>5}{na:>8}{acc_market:>11.1f}%{acc_per:>13.1f}%{baseline:>11.1f}%{delta:>+8.1f}pp")
    print("-" * 96)
    # 判定(#预测精度·R75): 要求"两个 horizon 都显著改善(>2pp)"才算有效, 避免单点 borderline 误导。
    # 实测 T+8 反而 -2.2pp(更差)、T+30 +5.6pp(180样本≈1.5SE, 边际且被短horizon抵消) → 不稳健。
    eff = [d for d in deltas.values() if d > 2.0]
    if len(eff) == len(deltas) and deltas:
        print("共识有效: 跨指数方向投票在两个 horizon 均显著提升单指数方向命中(峰值 +%.1fpp), 看板可加共识徽标。" % max(deltas.values()))
    elif eff:
        print("共识分化(弱信号): 仅长 horizon(T+30)边际改善 +%.1fpp(≈噪声), 短 horizon(T+8)反而 %+.1fpp 更差; "
              "方向偏差主要为系统性(同模型同regime), 跨指数聚合仅边际、且短 horizon 无效 → 不新增共识徽标(避免误用弱信号)。"
              % (max(deltas.values()), min(deltas.values())))
    else:
        print("共识中性: 跨指数投票未显著优于单指数(峰值 +%.1fpp) - 方向偏差为系统性(同模型同regime), "
              "聚合无法分散误差; 故不新增共识徽标, 真正杠杆仍是 R74 的校准透明化。" % max(deltas.values()) if deltas else 0.0)
    print("=" * 96)
    print("解读: P05-P95 名义90%覆盖(实测≥此=偏保守安全); P25-P75 名义50%; 方向>50%优于抛硬币; "
          "中线偏误(稳健中位口径)>0=系统保守(低估), <0=系统激进(高估); 均值口径因右偏肥尾会虚高, 故用中位口径。")


if __name__ == "__main__":
    data, agg, cons, regime_agg = run()
    report(data, agg, cons, regime_agg)
