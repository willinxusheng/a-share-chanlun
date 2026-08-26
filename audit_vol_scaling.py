# -*- coding: utf-8 -*-
"""
audit_vol_scaling.py — 关16 波动率扩散标度检验 (R89 新增)

目的：验证模型置信带的核心数学假设——日对数收益的 horizon 扩散是否 ∝ √f (square-root-of-time)。
      模型带宽度 = _sp·√f（report.py _bandf），隐含假设：日对数收益近似 i.i.d.、方差恒定。
      若该假设失效（波动聚集/均值回复/跳跃），则带宽度随 horizon 的缩放结构整体错误——
      这比"覆盖好不好"(关11)、"尾部兜不兜住"(关15) 更底层：覆盖好可能因带过宽恰好兜住，
      但标度结构错意味着"长 horizon 带系统性偏窄/偏宽"，是熊市漏覆盖(关15) 的潜在根因。

两项检验（均分牛/熊/震荡切片，与关8/关15 的 regime 框架一致）：
  A. 纯数据 roll 检验（快速、任意 horizon、独立不依赖 forecast_svg）：
     对多个 horizon h 算 realized h 日对数收益 std，log-log 回归 log(std_h) ~ log(h)，
     理想斜率 = 0.5（√f 法则）；<0.5=波动聚集/均值回复(长horizon带偏宽虚胖)，
     >0.5=扩散超线性(长horizon带偏窄风险)。
  C. 锚点级 模型带宽 vs 真实扩散（walk-forward 截断跑真实 forecast_svg，复用校准引擎）：
     取每锚点 T+8/T+30 的 95% 带对数半宽，与锚点后真实 h 日对数收益 std 比对，
     bias = 模型半宽 / (1.645·真实std) ≈ 模型σ / 真实σ；>>1=模型过宽，<<1=模型过窄(漏覆盖根因)。

纪律：仅透明化监控，不改模型数学；退出码恒 0，不阻断 CI。
用法：
  python audit_vol_scaling.py                # 检验A(全样本+分regime) + 检验C(walk-forward, 全量)
  python audit_vol_scaling.py --quick 25     # 检验A + 检验C(仅前25锚点, 冒烟)
  python audit_vol_scaling.py --selftest     # 合成注入校验指标方向/数值正确
"""
import json
import os
import math
import statistics
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000905"]

# 与校准引擎一致, 供检验C 切片
H_TARGETS = (8, 30)
ANCHOR_STEP = 15
MIN_HISTORY = 800
REGIMES = ("bull", "bear", "range")

# 检验A 用的 horizon 集合(交易日), 覆盖短/中/长, 均>=2
HORIZONS = [2, 3, 4, 5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60]

# 单一来源(R108): regime 分档逻辑统一从 chanlun 导入, 禁止本地副本——
# 否则未来改 chanlun.classify_regime 时, 本门禁切片口径会静默分裂(关16 与关8/关15 失配)。
from chanlun import classify_regime


def daily_log_returns(kl):
    rets = []
    for j in range(1, len(kl)):
        c0, c1 = kl[j - 1]["close"], kl[j]["close"]
        if c0 > 0 and c1 > 0:
            rets.append(math.log(c1 / c0))
    return rets


def std_by_horizon(returns, horizons):
    """对各 horizon h: 滑动窗口取 h 个日收益求和 => h 日对数收益, 返回其总体 std。"""
    out = {}
    n = len(returns)
    for h in horizons:
        if n < h + 1:
            out[h] = None
            continue
        rs = [sum(returns[i:i + h]) for i in range(0, n - h + 1)]
        out[h] = statistics.pstdev(rs) if len(rs) >= 2 else None
    return out


def std_by_horizon_regime(kl, returns, horizons):
    """分 regime 的 h 日对数收益 std: 每窗口 regime=锚点前60日累计对数收益分档。"""
    out = {rg: {h: [] for h in horizons} for rg in REGIMES}
    n = len(returns)
    for h in horizons:
        for i in range(0, n - h + 1):
            if i < 60:  # regime 须基于 return 区间之前的完整窗口, 避免用被解释变量本身判定 regime 造成前视污染
                continue
            window = kl[:i]  # 仅取 return 区间(i..i+h-1)之前的数据判定 regime
            rg = classify_regime(window)
            out[rg][h].append(sum(returns[i:i + h]))
    res = {}
    for rg in REGIMES:
        res[rg] = {}
        for h in horizons:
            lst = out[rg][h]
            res[rg][h] = statistics.pstdev(lst) if len(lst) >= 2 else None
    return res


def loglog_slope(std_map):
    """log-log 回归: log(std_h) ~ log(h), 返回 (slope, intercept, n_pts)。理想 slope=0.5。"""
    pts = [(math.log(h), math.log(s)) for h, s in std_map.items()
           if s and s > 0 and h >= 2]
    if len(pts) < 3:
        return None, None, len(pts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0:
        return None, None, len(pts)
    slope = sxy / sxx
    return slope, my - slope * mx, len(pts)


def run_roll():
    """检验A: 纯数据 roll 的 √f 标度 log-log 回归(全样本 + 分 regime)。"""
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    symbols = [s for s in SYMBOLS if s in data]
    # 聚合多指数日收益(同长度对齐非必需, 各指数独立算再汇总)
    all_rets = []
    rets_by_sym = {}
    kl_by_sym = {}
    for sym in symbols:
        kl = sorted(data[sym]["klines"], key=lambda k: k["date"])
        kl_by_sym[sym] = kl
        r = daily_log_returns(kl)
        rets_by_sym[sym] = r
        all_rets.extend(r)
    # 全样本斜率
    full_map = std_by_horizon(all_rets, HORIZONS)
    full_slope, _, full_n = loglog_slope(full_map)
    # 分 regime(每指数独立切片后合并)
    reg_all = {rg: {h: [] for h in HORIZONS} for rg in REGIMES}
    for sym in symbols:
        kl = kl_by_sym[sym]
        r = rets_by_sym[sym]
        reg = std_by_horizon_regime(kl, r, HORIZONS)
        for rg in REGIMES:
            for h in HORIZONS:
                if reg[rg][h] is not None:
                    reg_all[rg][h].append(reg[rg][h])
    reg_slope = {}
    reg_n = {}
    for rg in REGIMES:
        # 合并: 各指数同 h 的 std 取中位(避免指数间量纲差), 再 log-log
        merged = {}
        for h in HORIZONS:
            vals = reg_all[rg][h]
            if vals:
                merged[h] = statistics.median(vals)
        sl, _, nn = loglog_slope(merged)
        reg_slope[rg] = sl
        reg_n[rg] = nn
    return full_slope, full_n, reg_slope, reg_n, symbols


def run_deep(quick_n=None):
    """检验C: walk-forward 截断跑真实 forecast_svg, 取 T+8/T+30 的 95% 带半宽 vs 真实 h 日扩散。"""
    from chanlun import analyze, adaptive_horizon
    from report import forecast_svg
    from audit_forecast_calibration import find_proj

    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    symbols = [s for s in SYMBOLS if s in data]
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    base = kls[symbols[0]]
    n_base = len(base)
    # (regime, H) -> {model_hw: [log半宽...], real: [h日对数收益...]}
    rec = {(rg, H): {"hw": [], "real": []} for rg in REGIMES for H in H_TARGETS}
    i = MIN_HISTORY
    anc = 0
    while i < n_base - 35:
        if quick_n is not None and anc >= quick_n:
            break
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
                up = row["f95l"] + row["f95h"]
                lo = row["f95l"]
                med = row["med"]
                if up <= 0 or lo <= 0 or med <= 0:
                    continue
                # 模型 95% 带对数半宽(相对 med, 对称近似)
                hw = (math.log(up) - math.log(lo)) / 2.0
                # 真实: 锚点后 H 日对数收益(已实现)
                if i + H < len(kl):
                    real_h = 0.0
                    for j in range(i + 1, i + H + 1):
                        c0, c1 = kl[j - 1]["close"], kl[j]["close"]
                        if c0 > 0 and c1 > 0:
                            real_h += math.log(c1 / c0)
                    rec[(rg, H)]["hw"].append(hw)
                    rec[(rg, H)]["real"].append(real_h)
        i += ANCHOR_STEP
        anc += 1
    # 汇总: bias = median(model_hw) / (1.645 * stdev(real_h))
    res = {}
    for key, d in rec.items():
        if not d["hw"] or len(d["real"]) < 2:
            res[key] = None
            continue
        mhw = statistics.median(d["hw"])
        rstd = statistics.pstdev(d["real"])
        bias = mhw / (1.645 * rstd) if rstd > 0 else None
        res[key] = (bias, len(d["hw"]))
    return res


def _fmt_slope(sl):
    if sl is None:
        return "  N/A(样本不足)"
    flag = ""
    if sl < 0.45:
        flag = "  ⚠️<0.5 波动聚集/均值回复(长horizon带偏宽虚胖)"
    elif sl > 0.55:
        flag = "  ⚠️>0.5 扩散超线性(长horizon带偏窄风险)"
    else:
        flag = "  ✅≈0.5 √f 法则成立"
    return "%.3f%s" % (sl, flag)


def check(deep=True, quick_n=None):
    print("=" * 96)
    print("关16 波动率扩散标度检验 — 模型置信带核心假设 √f 法则(日对数收益 ∝ √horizon)")
    print("=" * 96)
    # 检验A
    full_slope, full_n, reg_slope, reg_n, symbols = run_roll()
    print("检验A  纯数据 roll 的 √f 标度 log-log 回归 (理想斜率=0.5, 任意 horizon %s)" % HORIZONS)
    print("-" * 96)
    print("  全样本(五指数合并): 斜率=%s   (参与horizon点数=%d, 指数=%d)"
          % (_fmt_slope(full_slope), full_n, len(symbols)))
    for rg in REGIMES:
        sl = reg_slope[rg]
        nn = reg_n[rg]
        label = {"bull": "牛市", "bear": "熊市", "range": "震荡"}[rg]
        print("  分%s(regime):  斜率=%s   (horizon点数=%s)" % (label, _fmt_slope(sl), nn))
    print("-" * 96)
    # 检验C
    if deep:
        print("检验C  锚点级 模型95%带半宽 vs 真实h日扩散 (walk-forward, bias=模型σ/真实σ, ≈1对齐)")
        res = run_deep(quick_n)
        print("  %-8s%-8s%-10s%-8s" % ("regime", "窗口", "bias", "N"))
        for rg in REGIMES:
            for H in H_TARGETS:
                v = res.get((rg, H))
                if v is None:
                    print("  %-8s%-8s%-10s%-8s" % (rg, "T+" + str(H), "  N/A", "-"))
                    continue
                bias, n = v
                tag = ""
                if bias > 1.2:
                    tag = "  ⚠️模型过宽(安全虚胖)"
                elif bias < 0.8:
                    tag = "  ⚠️模型过窄(漏覆盖风险, 关15熊市根因)"
                else:
                    tag = "  ✅带宽对齐真实扩散"
                print("  %-8s%-8s%-10.2f%-8d%s" % (rg, "T+" + str(H), bias, n, tag))
    else:
        print("检验C  跳过(--deep 时跑 walk-forward 比对; 离线默认不跑以保速度)")
    print("=" * 96)
    print("解读: √f 标度错(斜率≠0.5)意味着带宽度随 horizon 缩放结构不正确, 是覆盖/尾部检验的底层根因; "
          "bias<<1 直接解释熊市漏覆盖为何发生(模型σ低估真实波动)。本门禁仅透明化, 不修模型数学。")
    return True


def _selftest():
    """合成注入: 验证 log-log 回归数值正确 + 方向正确(i.i.d.→0.5, 均值回复→<0.5)。"""
    import random
    random.seed(12345)
    # 1) 回归函数对已知斜率正确
    for target, tag in ((0.5, "基准"), (0.65, "超扩散"), (0.40, "亚扩散")):
        smap = {h: 0.02 * (h ** target) for h in HORIZONS}
        sl, _, nn = loglog_slope(smap)
        assert sl is not None and abs(sl - target) < 0.01, "%s 斜率应≈%.2f got %.3f" % (tag, target, sl)
    # 2) i.i.d. 正态日收益 -> 斜率≈0.5 (有限样本滑动窗口估计噪声, 容差放宽至[0.47,0.57])
    rets = [random.gauss(0, 0.02) for _ in range(8000)]
    sl_a, _, _ = loglog_slope(std_by_horizon(rets, HORIZONS))
    assert sl_a is not None and 0.47 <= sl_a <= 0.57, "i.i.d. 应≈0.5 got %.3f" % sl_a
    # 3) AR(1) 均值回复 phi=-0.6 -> 斜率<0.5 (应与 i.i.d. 区间明显分离)
    r = [0.0] * 8000
    for t in range(1, 8000):
        r[t] = -0.6 * r[t - 1] + random.gauss(0, 0.01)
    sl_b, _, _ = loglog_slope(std_by_horizon(r, HORIZONS))
    assert sl_b is not None and sl_b < 0.46, "均值回复应<0.5 got %.3f" % sl_b
    print("[selftest] 波动率扩散标度指标 合成注入校验通过:")
    print("  已知斜率回归: 0.50→%.3f  0.65→%.3f  0.40→%.3f (均<0.01误差)" % (
        loglog_slope({h: 0.02 * (h ** 0.5) for h in HORIZONS})[0],
        loglog_slope({h: 0.02 * (h ** 0.65) for h in HORIZONS})[0],
        loglog_slope({h: 0.02 * (h ** 0.4) for h in HORIZONS})[0]))
    print("  i.i.d.正态日收益: 斜率=%.3f (≈0.5 ✅)" % sl_a)
    print("  AR(1)均值回复phi=-0.6: 斜率=%.3f (<0.5 ✅, 方向正确)" % sl_b)
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    quick_n = None
    if "--quick" in sys.argv:
        idx = sys.argv.index("--quick")
        if idx + 1 < len(sys.argv):
            try:
                quick_n = int(sys.argv[idx + 1])
            except ValueError:
                pass
    # 检验C(walk-forward) 在默认 / --quick N / --deep 下跑; --no-deep 才跳过只跑检验A
    deep = "--no-deep" not in sys.argv
    check(deep=deep, quick_n=quick_n)
