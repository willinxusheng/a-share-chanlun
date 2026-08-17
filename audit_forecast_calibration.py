# -*- coding: utf-8 -*-
"""
audit_forecast_calibration.py  —  缠论推演「预测数据准确性」滚动样本外回测门禁（R72 新增）

目的：用历史上每个交易日当"当下"，以当时已有数据截断跑真实 forecast_svg，再把 T+8 / T+30
      的主路径、均值期望中线、P05-P95 / P25-P75 置信带，与后来真实收盘比对，量化预测准确性。

统计口径：
  - P05-P95 覆盖率：名义 90%；实测越高=带越宽(安全侧)，越低=漏覆盖(风险)。
  - P25-P75 覆盖率：名义 50%（经验分位内层）。
  - 主路径/中线 方向准确率：>50% 即优于抛硬币。
  - 中线系统性偏误：mean((realized-med)/med)；>0=系统保守(低估)，<0=系统激进(高估)。

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

from chanlun import analyze, adaptive_horizon
from report import forecast_svg  # report.py 内含推演渲染核心

BASE = os.path.dirname(os.path.abspath(__file__))
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
    agg = {}
    for sym, d in data.items():
        kl = sorted(d["klines"], key=lambda k: k["date"])
        n = len(kl)
        rec = {h: {"N": 0, "in95": 0, "in75": 0, "dir_main": 0, "dir_med": 0,
                   "mae_main": 0.0, "mae_med": 0.0, "bias_med": 0.0} for h in H_TARGETS}
        i = MIN_HISTORY
        while i < n - 35:
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
                i += ANCHOR_STEP
                continue
            proj = fc["proj"]
            for H in H_TARGETS:
                if H > horizon:
                    continue
                row = find_proj(proj, H)
                if row is None:
                    continue
                real = kl[i + H]["close"]
                main_v, med_v = row["main"], row["med"]
                p05, p95 = row["f95l"], row["f95l"] + row["f95h"]
                p25, p75 = row["f75l"], row["f75l"] + row["f75h"]
                s = rec[H]
                s["N"] += 1
                if p05 <= real <= p95:
                    s["in95"] += 1
                if p25 <= real <= p75:
                    s["in75"] += 1
                if (main_v - last_a) * (real - last_a) > 0:
                    s["dir_main"] += 1
                if (med_v - last_a) * (real - last_a) > 0:
                    s["dir_med"] += 1
                s["mae_main"] += abs(main_v - real)
                s["mae_med"] += abs(med_v - real)
                s["bias_med"] += (real - med_v) / med_v
            i += ANCHOR_STEP
        agg[sym] = rec
    return data, agg


def report(data, agg):
    print("=" * 96)
    print("R72 滚动样本外回测 — 缠论推演预测准确性(锚点每%d交易日, 截断跑真实 forecast_svg)" % ANCHOR_STEP)
    print("=" * 96)
    hdr = (f"{'指数':<12}{'窗口':>5}{'N':>6}{'P05-P95覆盖':>14}{'P25-P75覆盖':>14}"
           f"{'主路径方向':>12}{'中线方向':>10}{'主路径MAE':>11}{'中线MAE':>10}{'中线偏误':>10}")
    print(hdr)
    print("-" * 96)
    tot = {h: {"N": 0, "in95": 0, "in75": 0, "dir_main": 0, "dir_med": 0,
               "mae_main": 0.0, "mae_med": 0.0, "bias_med": 0.0} for h in H_TARGETS}
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
            bias = s["bias_med"] / s["N"] * 100
            print(f"{nm:<12}{'T+'+str(H):>5}{s['N']:>6}{c95:>13.1f}%{c75:>13.1f}%"
                  f"{dm:>11.1f}%{dmed:>9.1f}%{maem:>11.1f}{maemed:>10.1f}{bias:>9.1f}%")
            for k in ("N", "in95", "in75", "dir_main", "dir_med"):
                tot[H][k] += s[k]
            tot[H]["mae_main"] += s["mae_main"]
            tot[H]["mae_med"] += s["mae_med"]
            tot[H]["bias_med"] += s["bias_med"]
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
        bias = s["bias_med"] / s["N"] * 100
        print(f"{'全部':<12}{'T+'+str(H):>5}{s['N']:>6}{c95:>13.1f}%{c75:>13.1f}%"
              f"{dm:>11.1f}%{dmed:>9.1f}%{maem:>11.1f}{maemed:>10.1f}{bias:>9.1f}%")
    print("=" * 96)
    print("解读: P05-P95 名义90%覆盖(实测≥此=偏保守安全); P25-P75 名义50%; 方向>50%优于抛硬币; "
          "中线偏误>0=系统保守(低估), <0=系统激进(高估)")


if __name__ == "__main__":
    data, agg = run()
    report(data, agg)
