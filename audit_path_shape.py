#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R87 关14 推演路径形态保真度门禁 (Path Shape Fidelity).

前13道门禁(R70-R86)从多个角度量化了「预测准不准」:
  关8/关12 只验了「端点」方向/价位(终点对不对);
  关11 只验了「带」覆盖/锐度(区间盖没盖住);
  关10 只验了 p_main 概率诚实性;
  关13 只验了显示数字内部自洽。
但从未有人回答: 「画出来的那条路, 形状对不对?」 —— 即预测路径在途中是怎么弯的,
是否和真实走势的弯法一致。一条主路径可能在终点价位上"差不多", 但途中节奏(何时上冲、
何时回踩、是否走出预期的之字形)完全错, 这对「按路径节奏择时」的用户是致命的盲区。

本门禁用 walk-forward(截断跑真实 forecast_svg, 与关4同一套无前视引擎)把每个历史锚点的
「结构主路径(main, 缠论演绎路径, 唯一带真实形态/会之字形的路径; med 是单调指数曲线无形态)」
逐交易日(tplus=0..H) 与后来真实收盘对齐, 计算两个形态指标:

① 逐段方向吻合度(step-direction agreement):
   对相邻交易日 t-1→t, 比较 预测主路径 与 真实收盘 的涨跌符号是否一致; 命中占比。
   预测主路径若是之字形态, 能正确抓到真实回踩/反抽的拐点, 吻合度才高;
   若只是单调漂移, 吻合度≈真实路径主导符号占比(≈抛硬币)。
   判定阈值 53%: 高于≈naive(随机符号~50%), 才说明路径形态有技能; 低于则「画的形状没用」。

② 形态秩相关(Spearman ρ, 对累计对数收益):
   把预测主路径与真实路径都转成「相对锚点的累计对数收益序列」, 取秩相关。
   ρ 衡量整体弯法是否一致(单调漂移/回踩节奏), 不受绝对点位缩放影响。
   判定阈值 0.15: 弱相关以下=路径形状与真实基本无关。

按 T+8 / T+30 与 牛/熊/震荡 分桶切片(沿用关8/关9 的 classify_regime), 暴露「全样本形态尚可但
某regime画图完全错」的隐藏弱点。例如若熊市主路径总画成"先跌后V反"而真实是"阴跌不止",
吻合度会显著低于震荡市 —— 这正是关8「熊市方向看空可信」之外、更细一层的预警。

监控门禁, 不阻断 CI。
"""
import json
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report import analyze, adaptive_horizon, forecast_svg  # 复用真实推演管线
import audit_forecast_calibration as ac  # 复用 walk-forward 引擎常量与 classify_regime

SYMBOLS = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000905"]


# ---------- 形态指标 ----------
def _sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)


def step_agreement(forecast, realized):
    """相邻段涨跌符号吻合占比。forecast/realized 等长(list of price), 索引即 tplus。"""
    n = len(forecast)
    if n < 2:
        return None
    ag = 0
    tot = 0
    for t in range(1, n):
        fa = _sign(forecast[t] - forecast[t - 1])
        fb = _sign(realized[t] - realized[t - 1])
        if fa == 0 and fb == 0:
            continue
        tot += 1
        if fa == fb:
            ag += 1
    return ag / tot if tot else None


def _rank(a):
    n = len(a)
    order = sorted(range(n), key=lambda i: a[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 平均秩(1-based)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x, y):
    """两序列 Spearman 秩相关(手动实现, 不依赖 scipy)。"""
    n = len(x)
    if n < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    r = num / (dx * dy)
    return max(-1.0, min(1.0, r))


def path_at(proj, horizon, key="main"):
    """把 proj(101 dense points) 抽成整数 tplus -> 价格 的字典, 取离 t/horizon 最近者。"""
    d = {}
    for p in proj:
        t = p["tplus"]
        f = p["f"]
        if t not in d or abs(f - t / horizon) < abs(d[t][0] - t / horizon):
            d[t] = (f, p[key])
    return d


def run(max_anchors=None):
    data = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"), encoding="utf-8"))
    symbols = [s for s in SYMBOLS if s in data]
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    base = kls[symbols[0]]
    n_base = len(base)
    H_TARGETS = ac.H_TARGETS
    agg = {sym: {h: {"N": 0, "step_sum": 0.0, "sp_sum": 0.0} for h in H_TARGETS} for sym in symbols}
    REGIMES = ("bull", "bear", "range")
    regime_agg = {rg: {h: {"N": 0, "step_sum": 0.0, "sp_sum": 0.0} for h in H_TARGETS} for rg in REGIMES}

    i = ac.MIN_HISTORY
    _anchors_done = 0
    while i < n_base - 35:
        date_i = base[i]["date"]
        # 仅当所有 sym 在该锚点对齐(同日期), 才纳入, 与关4一致
        aligned = True
        for sym in symbols:
            kl = kls[sym]
            if i >= len(kl) or kl[i]["date"] != date_i:
                aligned = False
                break
        if not aligned:
            i += ac.ANCHOR_STEP
            continue
        for sym in symbols:
            kl = kls[sym]
            trunc = kl[:i + 1]
            last_a = trunc[-1]["close"]
            try:
                r = analyze(trunc)
                horizon = adaptive_horizon(r["bis"], r["merged"])
                _svg, _note, _probs, _leg, fc = forecast_svg(
                    trunc, r, r["classify"], 50.0, 0.0, sym, horizon)
            except Exception:
                continue
            proj = fc.get("proj") or []
            main_d = path_at(proj, horizon, "main")
            rg = ac.classify_regime(trunc)
            for H in H_TARGETS:
                if H > horizon:
                    continue
                if i + H >= len(kl):
                    continue
                # 构建 t=0..H 的整数路径
                fc_path = [main_d[t][1] if t in main_d else None for t in range(0, H + 1)]
                if any(v is None for v in fc_path):
                    continue
                real_path = [kl[i + t]["close"] for t in range(0, H + 1)]
                if any(v is None or v <= 0 for v in real_path):
                    continue
                sa = step_agreement(fc_path, real_path)
                # Spearman 对累计对数收益(相对锚点 last_a), 形态稳健
                fc_lr = [math.log(max(v, 1e-9) / last_a) for v in fc_path]
                rl_lr = [math.log(max(v, 1e-9) / last_a) for v in real_path]
                sp = spearman(fc_lr, rl_lr)
                if sa is None or sp is None:
                    continue
                s = agg[sym][H]
                s["N"] += 1
                s["step_sum"] += sa
                s["sp_sum"] += sp
                rs = regime_agg[rg][H]
                rs["N"] += 1
                rs["sp_sum"] += sp
                rs["step_sum"] += sa
        i += ac.ANCHOR_STEP
        _anchors_done += 1
        if max_anchors is not None and _anchors_done >= max_anchors:
            break
    return data, agg, regime_agg


def _fmt(v, pct=False, nd=1):
    if v is None:
        return "  -  "
    return ("%.1f%%" % (v * 100)) if pct else ("%.2f" % v)


def check(max_anchors=None):
    data, agg, regime_agg = run(max_anchors)
    print("=" * 96)
    print("R87 推演路径形态保真度(关14) — 画出来的路, 形状对不对?")
    print("=" * 96)
    print("指标: ①逐段方向吻合度(预测主路径 vs 真实收盘 相邻段涨跌符号命中%) ")
    print("      ②形态秩相关 Spearman ρ(累计对数收益序列秩相关, 看整体弯法是否一致)")
    print("判定: 吻合度>53%(≈优于naive随机符号~50%) 且 ρ>0.15 才算『路径形态有技能』")
    print("-" * 96)
    hdr = "%-10s %-6s %-6s %-16s %-14s" % ("指数", "窗口", "N", "逐段方向吻合", "形态ρ(Spearman)")
    print(hdr)
    print("-" * 96)
    tot = {h: {"N": 0, "step_sum": 0.0, "sp_sum": 0.0} for h in ac.H_TARGETS}
    for sym, d in data.items():
        nm = d.get("name", sym)
        for H in ac.H_TARGETS:
            s = agg[sym][H]
            if s["N"] == 0:
                print("%-10s %-6s %-6d %-16s %-14s" % (nm, "T+" + str(H), 0, "  -  ", "  -  "))
                continue
            sa = s["step_sum"] / s["N"]
            sp = s["sp_sum"] / s["N"]
            flag = "OK" if (sa > 0.53 and sp > 0.15) else "弱"
            print("%-10s %-6s %-6d %-16s %-14s %s" % (nm, "T+" + str(H), s["N"],
                                                     _fmt(sa, True), _fmt(sp), flag))
            tot[H]["N"] += s["N"]
            tot[H]["step_sum"] += s["step_sum"]
            tot[H]["sp_sum"] += s["sp_sum"]
    print("-" * 96)
    print("合计(五指数):")
    for H in ac.H_TARGETS:
        s = tot[H]
        if s["N"] == 0:
            continue
        sa = s["step_sum"] / s["N"]
        sp = s["sp_sum"] / s["N"]
        flag = "OK" if (sa > 0.53 and sp > 0.15) else "弱(无形态技能)"
        print("  T+%-3d N=%-6d 逐段方向吻合=%.1f%%  形态ρ=%.2f  -> %s"
              % (H, s["N"], sa * 100, sp, flag))
    print("=" * 96)
    # 分 regime 切片: 暴露「全样本尚可但某regime画错」的隐藏弱点
    print("分市场环境(牛/熊/震荡)切片:")
    print("-" * 96)
    weak = []
    for rg in ("bull", "bear", "range"):
        for H in ac.H_TARGETS:
            s = regime_agg[rg][H]
            if s["N"] == 0:
                continue
            sa = s["step_sum"] / s["N"]
            sp = s["sp_sum"] / s["N"]
            tag = "OK" if (sa > 0.53 and sp > 0.15) else "弱"
            print("  %-5s T+%-3d N=%-6d 吻合=%.1f%% ρ=%.2f %s" % (rg, H, s["N"], sa * 100, sp, tag))
            if sa <= 0.53 or sp <= 0.15:
                weak.append((rg, H, s["N"], sa * 100, sp))
    print("-" * 96)
    if weak:
        parts = ["%s T+%d 吻合%.0f%%/ρ%.2f(N=%d)" % (rg, H, sa, sp, n) for rg, H, n, sa, sp in weak]
        print("⚠ 形态弱区: " + "; ".join(parts)
              + " — 该regime下『画出的路径形状』与真实弯法基本无关, 路径节奏不可信(但端点价位/带覆盖仍由关11/关12监控)。")
    else:
        print("✅ 各regime 路径形态均达技能阈值(吻合>53% 且 ρ>0.15), 画出的路形状整体可信。")
    print("=" * 96)
    print("注: 透明化『路径形状准不准』盲区。")
    sys.exit(0)


def _selftest():
    """合成注入验证指标函数正确性(不依赖网络/数据规模)。"""
    # 案例A: 完美之字形吻合 —— 预测与真实同形(先涨后跌)
    fc_a = [100, 102, 104, 103, 101, 99]
    rl_a = [100, 102, 105, 104, 102, 100]
    sa_a = step_agreement(fc_a, rl_a)
    sp_a = spearman([math.log(v / 100) for v in fc_a], [math.log(v / 100) for v in rl_a])
    # 案例B: 完全反形 —— 预测涨真实跌(之字相反)
    fc_b = [100, 102, 104, 103, 101, 99]   # 先涨后跌
    rl_b = [100, 98, 96, 97, 99, 101]      # 先跌后涨(反)
    sa_b = step_agreement(fc_b, rl_b)
    sp_b = spearman([math.log(v / 100) for v in fc_b], [math.log(v / 100) for v in rl_b])
    # 案例C: monotone 预测 vs 之字真实 —— 吻合度应≈主导符号占比(≈抛硬币), 暴露『单调画形状没用』
    fc_c = [100, 101, 102, 103, 104, 105]  # 单调涨
    rl_c = [100, 102, 101, 103, 100, 104]  # 之字
    sa_c = step_agreement(fc_c, rl_c)
    ok = True
    assert sa_a is not None and sa_a > 0.8, "A 应高吻合, got %s" % sa_a
    assert sp_a is not None and sp_a > 0.8, "A 应高ρ, got %s" % sp_a
    assert sa_b is not None and sa_b < 0.4, "B 应低吻合, got %s" % sa_b
    assert sp_b is not None and sp_b < 0.0, "B 应负ρ, got %s" % sp_b
    # C: monotone 预测对之字真实, 吻合度应在 [0.3,0.7](≈符号随机), 不应虚高
    assert sa_c is not None and 0.2 <= sa_c <= 0.8, "C 应≈抛硬币, got %s" % sa_c
    sp_c = spearman([math.log(v / 100) for v in fc_c], [math.log(v / 100) for v in rl_c])
    print("[selftest] 路径形态指标 合成注入校验通过:")
    print("  完美同形 A:  吻合=%.0f%%  ρ=%.2f" % (sa_a * 100, sp_a))
    print("  完全反形 B:  吻合=%.0f%%  ρ=%.2f" % (sa_b * 100, sp_b))
    print("  单调预测C:   吻合=%.0f%%  ρ=%.2f (≈抛硬币, 暴露形状无技能)" % (sa_c * 100, sp_c))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _quick = None
    if "--quick" in sys.argv:
        idx = sys.argv.index("--quick")
        try:
            _quick = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            _quick = 30
    check(_quick)
