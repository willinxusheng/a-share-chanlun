# -*- coding: utf-8 -*-
"""
audit_interval_score.py  —  缠论推演「置信带锐度 + 不确定性校准」门禁（R84 新增）

为什么需要这一道（与 R72 覆盖门禁的区别）：
  R72 只检查「P05-P95 是否盖住 90% 真实值」——这是二元的 in/out 覆盖。
  但一个从 -99% 到 +99% 的带也能盖 100%，却对决策毫无信息量。
  本门禁用**区间评分 Interval Score（Gneiting & Raftery 2007 标准适当评分规则）**
  同时衡量「覆盖」与「宽度」，回答：『这条置信带是诚实且锐利(有用)的，
  还是只靠够宽才勉强盖住(废带)？』

三个独立诊断：
  ① 锐度(Sharpness)：P05-P95 带宽均值(占价位%)，应落在合理区间——过宽(T+8>18%)≈废带,
     过窄(<2%)≈几乎必漏。
  ② 区间评分 vs 朴素基线：模型 IS 与「同等 90% 覆盖的常数带宽」基线 IS 比较。
     模型 IS ≤ 基线 → 带锐利有用；模型 IS 显著 > 基线 → 带过宽(浪费信息量)。
  ③ 不确定性校准(元校准)：模型「带越宽」是否对应「真实波动越大」？
     - 按带宽分两半, 宽半的真实 |real-med| 应 > 窄半的真实 |real-med|(正相关=模型懂自己的不确定性)；
     - 且窄半(常对应平静期)的经验覆盖仍应≈90%；若窄半覆盖<80% → 平静期模型低估不确定性(危险漏洞)。

实现：与 R72/R83 同款滚动样本外引擎(每个交易日当"当下"截断跑真实 forecast_svg),
      仅读取 band 几何(width/L/U/med)与后来真实收盘, 不触碰任何预测数学。
      复用 chanlun.analyze / adaptive_horizon / report.forecast_svg / 同款常量。

退出码：恒 0（与关5~关10 一致, 监控门禁不阻断）。
"""
import json
import os
import math
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chanlun import analyze, adaptive_horizon
from report import forecast_svg

BASE = os.path.dirname(os.path.abspath(__file__))
H_TARGETS = (8, 30)
ANCHOR_STEP = 15          # 与 R72 一致, 保证可比性
MIN_HISTORY = 800
ALPHA = 0.10              # 名义区间水平(90%)
K = 2.0 / ALPHA           # interval score 惩罚系数 = 20


def find_proj(proj, tplus_target):
    best, bd = None, 1e9
    for row in proj:
        d = abs(row["tplus"] - tplus_target)
        if d < bd:
            bd, best = d, row
    if best is None or bd > 1:
        return None
    return best


def interval_score(L, U, y):
    """标准 interval score (越低越好): width + K*under + K*over"""
    width = U - L
    under = max(L - y, 0.0)
    over = max(y - U, 0.0)
    return width + K * under + K * over


def spearman(xs, ys):
    """简化 Spearman：对两列取秩后 Pearson。样本足够时足以判定正相关方向。"""
    n = len(xs)
    if n < 3:
        return None
    rx = _rank(xs)
    ry = _rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def selftest():
    """区间评分公式正确性自检(玩具数据, 不碰真实引擎)。"""
    # 带 [-10,10], 真实 0 → IS = 20 + 0 = 20
    assert abs(interval_score(-10, 10, 0) - 20.0) < 1e-9, "IS 基础公式错"
    # 真实 -15 (under=5) → IS = 20 + 20*5 = 120
    assert abs(interval_score(-10, 10, -15) - 120.0) < 1e-9, "IS under 公式错"
    # 真实 25 (over=15) → IS = 20 + 20*15 = 320
    assert abs(interval_score(-10, 10, 25) - 320.0) < 1e-9, "IS over 公式错"
    print("✅ 区间评分公式自检通过 (IS 基础=20, under=120, over=320)")
    return True


def run():
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    symbols = list(data.keys())
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    base = kls[symbols[0]]
    n_base = len(base)

    # 每 horizon 收集逐锚点: med, L(P5), U(P95), width(=U-L), real, residual=|real-med|
    recs = {h: [] for h in H_TARGETS}
    i = MIN_HISTORY
    while i < n_base - 35:
        date_i = base[i]["date"]
        ok = True
        step = {}
        for sym in symbols:
            kl = kls[sym]
            if i >= len(kl) or kl[i]["date"] != date_i:
                ok = False
                break
            trunc = kl[:i + 1]
            last_a = trunc[-1]["close"]
            try:
                r = analyze(trunc)
                horizon = adaptive_horizon(r["bis"], r["merged"])
                _svg, _note, _probs, _leg, fc = forecast_svg(
                    trunc, r, r["classify"], 50.0, 0.0, sym, horizon)
            except Exception:
                ok = False
                break
            for H in H_TARGETS:
                if H > horizon:
                    continue
                row = find_proj(fc["proj"], H)
                if row is None:
                    continue
                L = row["f95l"]
                U = row["f95l"] + row["f95h"]
                med = row["med"]
                real = kl[i + H]["close"]
                step.setdefault(H, []).append((med, L, U, U - L, real))
        if ok:
            for H in H_TARGETS:
                for (med, L, U, width, real) in step.get(H, []):
                    recs[H].append((med, L, U, width, real))
        i += ANCHOR_STEP
    return recs


def diagnose(recs):
    print("=" * 92)
    print("R84 区间锐度 + 不确定性校准门禁（滚动样本外, 与 R72 同款引擎; 监控不阻断）")
    print("=" * 92)
    hdr = (f"{'窗口':>5}{'N':>7}{'P5-P95覆盖':>12}{'均带宽%':>10}{'模型IS':>11}"
           f"{'基线IS':>11}{'IS比':>8}{'宽窄相关':>10}{'窄半覆盖':>10}")
    print(hdr)
    print("-" * 92)
    any_warn = False
    any_crit = False
    for H in H_TARGETS:
        rs = recs[H]
        n = len(rs)
        if n < 20:
            print(f"{'T+'+str(H):>5}{n:>7}  (样本不足)")
            continue
        meds = [r[0] for r in rs]
        Ls = [r[1] for r in rs]
        Us = [r[2] for r in rs]
        Ws = [r[3] for r in rs]
        reals = [r[4] for r in rs]
        # ① 覆盖 + 锐度
        in95 = sum(1 for L, U, y in zip(Ls, Us, reals) if L <= y <= U)
        cov = in95 / n * 100
        mean_w_pct = statistics.mean(Ws) / statistics.mean(meds) * 100
        # ② 区间评分 vs 朴素基线(常数 90% 带宽 = 残差绝对值的 90 分位)
        model_is = statistics.mean(
            [interval_score(L, U, y) for L, U, y in zip(Ls, Us, reals)])
        resid = sorted(abs(y - m) for m, y in zip(meds, reals))
        c90 = resid[int(0.90 * (len(resid) - 1))]
        baseline_is = 2.0 * c90   # 基线带半宽=c90, 覆盖≈90%, 几乎无惩罚 → IS≈2*c90
        is_ratio = model_is / baseline_is if baseline_is else float("inf")
        # ③ 不确定性校准: 带宽 vs 真实 |real-med| 的 Spearman
        real_disp = [abs(y - m) for m, y in zip(meds, reals)]
        rho = spearman(Ws, real_disp)
        # 窄半(平静期)经验覆盖: 按带宽中位数分两半
        wmed = statistics.median(Ws)
        narrow = [(L, U, y) for L, U, y, w in zip(Ls, Us, reals, Ws) if w <= wmed]
        nn = len(narrow)
        narrow_cov = (sum(1 for L, U, y in narrow if L <= y <= U) / nn * 100) if nn else None

        flag = ""
        # 锐度过宽: T+8 带宽>18% 或 T+30>30% → 接近废带
        wide_thr = 18.0 if H == 8 else 30.0
        if mean_w_pct > wide_thr:
            any_warn = True
            flag += " 锐度过宽"
        # 窄半覆盖塌方: 平静期模型低估不确定性(危险)
        if narrow_cov is not None and narrow_cov < 80.0:
            any_crit = True
            flag += " 窄半覆盖塌"
        # IS 比过高: 模型带显著不如朴素基线锐利
        if is_ratio > 1.3:
            any_warn = True
            flag += " IS过宽"
        # 不确定性校准方向: rho 应 > 0 (宽带对应大波动)
        uncal = (rho is not None and rho < 0.10)
        if uncal:
            any_warn = True
            flag += " 不确定性未校准"

        print(f"{'T+'+str(H):>5}{n:>7}{cov:>11.1f}%{mean_w_pct:>9.1f}%{model_is:>11.1f}"
              f"{baseline_is:>11.1f}{is_ratio:>7.2f}{(('%.2f' % rho) if rho is not None else '  n/a'):>10}"
              f"{(('%.1f%%' % narrow_cov) if narrow_cov is not None else ' n/a'):>10}{flag}")
    print("-" * 92)
    print("诊断说明:")
    print("  • 均带宽%%：P5-P95 带宽占价位均值; 过宽(T+8>18%%/T+30>30%%)≈废带, 过窄(<2%%)≈必漏")
    print("  • IS比：模型区间评分 / 朴素常数带宽基线; ≤1.0=锐利有用, 1.0~1.3=略宽可接受, >1.3=过宽浪费")
    print("  • 宽窄相关：带宽 与 真实|real-med| 的 Spearman; >0=模型懂自己的不确定性(宽带宽=大波动)")
    print("  • 窄半覆盖：带宽较窄(常平静期)锚点的经验覆盖; <80%%=平静期低估不确定性(危险漏洞)")
    print("=" * 92)
    verdict = ("❌ CRITICAL — 窄半(平静期)覆盖塌方, 模型在平静期低估不确定性(最有隐蔽性的漏洞)"
               if any_crit else
               ("⚠ WARN — 存在锐度过宽/IS过宽/不确定性未校准(监控, 不阻断)"
                if any_warn else
                "✅ 区间锐度合理 + 不确定性已校准(宽带对应大波动, 窄带仍覆盖≈90%)"))
    print("结论:", verdict)
    print("注: 本门禁退出码恒0(监控不阻断); 若未来要升级为硬门禁, 以『窄半覆盖<80%』为唯一 CRITICAL 触发。")
    sys.exit(0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    recs = run()
    diagnose(recs)
