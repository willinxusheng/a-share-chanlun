#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R88 关15 极端尾部覆盖检验 (Tail/Extreme Coverage — VaR-style exception backtest).

前14道门禁已覆盖: 历史完整性/双源一致性/归一化/校准回测/情绪条件化/突变漂移/质量证书/
分regime方向/点前完整性/概率校准诚实性/区间锐度(关11, 无条件 Interval Score)/水平偏置/
数值自洽/路径形态保真度。

关11 用 Interval Score 量化了「带盖没盖住 + 带够不够窄」, 但 IS 是 **无条件聚合**——
少数灾难性击穿会被大量平静日平均掉, 看不出「**市场真暴跌时, 我的95%带到底兜没兜住?**」
这一最致命的问题。对逆向/风控型投资者, 这才是预测数据准确性里最该透明化的盲区:

本门禁用 walk-forward(截断跑真实 forecast_svg, 与关4/关14同套无前视引擎)在每历史锚点取
T+8/T+30 的 95% 置信带下沿 f95l、上沿 f95l+f95h, 与后来真实收盘比对, 做三件事:

① 无条件覆盖回测(Kupiec POF 似然比检验):
   95%带名义覆盖90% -> 期望双侧例外率 5%。用 LRuc 检验「实测例外率是否显著偏离5%」,
   并区分 side: 'under'(带太窄/漏覆盖/危险) vs 'over'(带太宽/过保守/安全) vs 'ok'(与名义一致)。
   临界值 LRuc=3.841(95%) / 6.635(99%)。

② 下行尾部条件覆盖(名义 2.5%):
   95%带是双侧的, 期望下行击穿(real<p05)率 ≈2.5%。单列下行例外率,
   这是「暴跌时带是否兜住」的最直接指标; 熊市(bear)单独切片 —— 逆向投资者最关心的情形。

③ 最差十分位条件击穿(worst-decile conditional breach):
   取所有锚点中真实收益最差10%的交易日, 看其中被95%带击穿的比例。
   若带是诚实的, 该比例≈5%(与无条件一致); 若显著>5%, 说明「模型在极端日反而过度自信
   (带收太窄)」—— 正是关11无条件IS掩盖的危险信号, 也是本门禁的核心新增价值。

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
REGIMES = ("bull", "bear", "range")


# ---------- 覆盖统计核心 ----------
def _term(pp, k, m):
    """pp^k * (1-pp)^m, 守卫 0^0=1。"""
    a = pp ** k if k > 0 else 1.0
    b = (1 - pp) ** m if m > 0 else 1.0
    return a * b


def kupiec_lr(x, N, p0=0.05):
    """Kupiec POF 无条件覆盖似然比检验统计量, H0: 覆盖=p0, ~ chi2(1)。
    返回 LRuc; 越大越拒绝 H0(实测覆盖≠名义)。临界值 3.841(95%)/6.635(99%)。"""
    if N == 0:
        return 0.0
    p_hat = x / N
    num = _term(p0, x, N - x)
    den = _term(p_hat, x, N - x)
    if num <= 0 or den <= 0:
        return 0.0
    return -2.0 * math.log(num / den)


def coverage_side(x, N, p0=0.05):
    """返回 (rate, lr, side)。side: 'under'(漏覆盖/危险) | 'over'(过宽/安全) | 'ok'。"""
    if N == 0:
        return 0.0, 0.0, "ok"
    rate = x / N
    lr = kupiec_lr(x, N, p0)
    if rate > p0 * 1.5 or (lr > 3.841 and rate > p0):
        side = "under"
    elif rate < p0 * 0.5 or (lr > 3.841 and rate < p0):
        side = "over"
    else:
        side = "ok"
    return rate, lr, side


def run(max_anchors=None):
    data = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"), encoding="utf-8"))
    symbols = [s for s in SYMBOLS if s in data]
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    base = kls[symbols[0]]
    n_base = len(base)
    H_TARGETS = ac.H_TARGETS
    # 每 horizon 收集记录: {logret, breach, down(下行击穿), depth(下行击穿深度%)}
    records = {h: [] for h in H_TARGETS}
    regime_records = {rg: {h: [] for h in H_TARGETS} for rg in REGIMES}

    i = ac.MIN_HISTORY
    _anchors_done = 0
    while i < n_base - 35:
        date_i = base[i]["date"]
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
            rg = ac.classify_regime(trunc)
            for H in H_TARGETS:
                if H > horizon:
                    continue
                if i + H >= len(kl):
                    continue
                row = ac.find_proj(proj, H)
                if row is None:
                    continue
                p05 = row["f95l"]
                p95 = row["f95l"] + row["f95h"]
                real = kl[i + H]["close"]
                if real <= 0 or p05 <= 0 or p95 <= 0 or p95 <= p05:
                    continue
                logret = math.log(max(real, 1e-9) / last_a)
                breach = (real < p05) or (real > p95)
                down = real < p05
                depth = ((p05 - real) / p05 * 100.0) if down else 0.0
                rec = {"logret": logret, "breach": breach, "down": down, "depth": depth}
                records[H].append(rec)
                regime_records[rg][H].append(rec)
        i += ac.ANCHOR_STEP
        _anchors_done += 1
        if max_anchors is not None and _anchors_done >= max_anchors:
            break
    return data, records, regime_records


def _worst_decile_breach(records):
    """最差十分位条件击穿率: 真实收益最差10%的交易日中, 被95%带击穿的比例。"""
    n = len(records)
    if n < 10:
        return None
    srt = sorted(records, key=lambda x: x["logret"])
    k = max(1, int(0.1 * n))
    worst = srt[:k]
    return sum(1 for w in worst if w["breach"]) / k


def _fmt_rate(v):
    return "  -  " if v is None else "%.1f%%" % (v * 100)


def check(max_anchors=None):
    data, records, regime_records = run(max_anchors)
    print("=" * 100)
    print("R88 极端尾部覆盖检验(关15) — 暴跌时, 95%%带到底兜没兜住?")
    print("=" * 100)
    print("① Kupiec POF 无条件覆盖(名义5%%例外) ② 下行尾部(名义2.5%%) ③ 最差十分位条件击穿")
    print("-" * 100)
    print("%-8s %-6s %-7s %-16s %-14s %-16s %-14s" %
          ("窗口", "N", "双侧例外", "LRuc/判定", "下行例外(2.5%)", "最差十分位击穿", "平均下行深度"))
    print("-" * 100)
    findings = []
    for H in ac.H_TARGETS:
        recs = records[H]
        n = len(recs)
        if n == 0:
            print("%-8s %-6d %-16s %-14s %-16s %-14s" % ("T+" + str(H), 0, "  -  ", "  -  ", "  -  ", "  -  "))
            continue
        x = sum(1 for r in recs if r["breach"])
        rate, lr, side = coverage_side(x, n, 0.05)
        dx = sum(1 for r in recs if r["down"])
        drate, _, dside = coverage_side(dx, n, 0.025)
        wd = _worst_decile_breach(recs)
        avg_depth = (sum(r["depth"] for r in recs if r["down"]) / dx) if dx else 0.0
        verdict = {"under": "漏覆盖!", "over": "过宽(安全)", "ok": "与名义一致"}[side]
        print("%-8s %-6d %-16s %-14s %-16s %-14s %-12.1f%%" %
              ("T+" + str(H), n, "%.1f%%/%s" % (rate * 100, verdict),
               "%.1f/%s" % (lr, side), "%.1f%%/%s" % (drate * 100, dside),
               _fmt_rate(wd), avg_depth))
        if side == "under":
            findings.append(("T+%d 双侧漏覆盖" % H, n, rate, lr))
        if wd is not None and wd > 0.05 * 1.5:
            findings.append(("T+%d 最差十分位击穿率过高" % H, n, wd, None))
    print("-" * 100)
    # 分 regime: 熊市单独切片(逆向投资者最关心)
    print("分市场环境(牛/熊/震荡) 95%%带双侧例外率(名义5%%):")
    print("-" * 100)
    for rg in REGIMES:
        for H in ac.H_TARGETS:
            recs = regime_records[rg][H]
            n = len(recs)
            if n == 0:
                continue
            x = sum(1 for r in recs if r["breach"])
            rate, lr, side = coverage_side(x, n, 0.05)
            tag = {"under": "漏覆盖!", "over": "过宽(安全)", "ok": "一致"}[side]
            print("  %-5s T+%-3d N=%-6d 双侧例外=%.1f%%  LRuc=%.1f  %s"
                  % (rg, H, n, rate * 100, lr, tag))
            if rg == "bear" and side == "under":
                findings.append(("熊市 T+%d 漏覆盖" % H, n, rate, lr))
    print("=" * 100)
    if findings:
        parts = []
        for f in findings:
            if f[3] is None:
                parts.append("%s (N=%d, 率=%.1f%%)" % (f[0], f[1], f[2] * 100))
            else:
                parts.append("%s (N=%d, 率=%.1f%%, LRuc=%.1f)" % (f[0], f[1], f[2] * 100, f[3]))
        print("⚠ 尾部覆盖异常: " + "; ".join(parts)
              + " — 该情形下『95%%带』未能按名义覆盖真实波动, 极端日带过窄(过度自信), 风控需自行加安全垫(关11锐度/关12偏置已另测)。")
    else:
        print("✅ 各 horizon / regime 95%%带覆盖均与名义5%%一致(含最差十分位条件击穿未显著升高), 暴跌时带基本兜得住。")
    print("=" * 100)
    print("注: 透明化『极端尾部是否兜得住』盲区。")
    sys.exit(0)


def _selftest():
    """合成注入验证覆盖统计核心函数正确性(不依赖网络/数据规模)。"""
    # 案例A: 名义5%例外(10/200) -> LRuc≈0, side=ok
    lr_a = kupiec_lr(10, 200)
    side_a = coverage_side(10, 200)[2]
    # 案例B: 20%例外(40/200, 漏覆盖危险) -> LRuc大, side=under
    lr_b = kupiec_lr(40, 200)
    side_b = coverage_side(40, 200)[2]
    # 案例C: 0%例外(过宽安全) -> LRuc大但 side=over
    lr_c = kupiec_lr(0, 200)
    side_c = coverage_side(0, 200)[2]
    assert side_a == "ok", "A 应 ok, got %s" % side_a
    assert lr_a < 3.841, "A LRuc应<3.84, got %s" % lr_a
    assert side_b == "under", "B 应 under, got %s" % side_b
    assert lr_b > 3.841, "B LRuc应>3.84, got %s" % lr_b
    assert side_c == "over", "C 应 over, got %s" % side_c
    # 尾部条件击穿注入: 180平静日(全在带内) + 20最差日(真实暴跌且击穿下沿)
    recs = []
    for _ in range(180):
        recs.append({"logret": 0.0, "breach": False, "down": False, "depth": 0.0})
    for _ in range(20):
        recs.append({"logret": -0.3, "breach": True, "down": True, "depth": 25.0})
    cond = _worst_decile_breach(recs)  # 最差10% (20/200) 应全击穿 -> 100%
    assert cond > 0.5, "最差十分位条件击穿应显著>5%%, got %.0f%%" % (cond * 100)
    srt = sorted(recs, key=lambda x: x["logret"])
    k = max(1, int(0.1 * len(srt)))
    rest = srt[k:]
    rest_rate = sum(1 for w in rest if w["breach"]) / len(rest)
    assert rest_rate == 0.0, "平静日击穿率应0, got %s" % rest_rate
    print("[selftest] 极端尾部覆盖指标 合成注入校验通过:")
    print("  名义5%%例外 A: LRuc=%.2f side=%s (ok)" % (lr_a, side_a))
    print("  20%%例外 B:   LRuc=%.2f side=%s (under, 漏覆盖危险)" % (lr_b, side_b))
    print("  0%%例外 C:    LRuc=%.2f side=%s (over, 过宽安全)" % (lr_c, side_c))
    print("  最差十分位条件击穿: %.0f%% (远高于名义5%%, 暴露极端日过度自信)" % (cond * 100))
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
