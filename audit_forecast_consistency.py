#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R86 关13 推演数值内部自洽性门禁.

验证看板上「显示的预测数字」彼此不自相矛盾 —— 这是前12道门禁(R78-R85)从未覆盖的一层:
预测「准不准」之外, 还得「显示的数字不互相打架」. 四道独立检查(均监控门禁, 退出码恒0):

① 存续概率↔置信带分位自洽: 注释反复声明『结构存续概率(p_hold)与置信带分位严格自洽
   (ZD落带内对应分位即对应概率)』. 本门禁用同一套输入(closes+zd+κ=1.8)独立重算 p_hold,
   与 _note 文本里显示的 p_hold 比对; 偏差>3pp 即 WARN(声明不成立/回归风险).

② 置信带单调嵌套: 每个投影点须满足 l95 ≤ l75 ≤ med ≤ u75 ≤ u95 (容忍≤0.5%浮点噪声);
   任意点违反即 WARN(未来改动破坏带构造/出现反向带时立刻暴露, 此前12门禁从未查).

③ 置信带有限非负: 所有投影价须为有限正数(无 NaN/inf/≤0); 否则 WARN.

④ 文本终点↔图series一致性: _note 里『均值期望终点/趋势外推位/主路径失效位(ZD)』须分别等于
   图 series 末点(proj[-1].med / proj[-1].trend / 中枢zd); 偏差>1%即 WARN(文本与图打架).
   注: 不比 fib 主/次/风险路径终点——它们与图 median 序列本就不同量纲, 比了是误报.

退出码恒0(与关5~关12一致, 监控不阻断). 仅读取+复核, 不改任何模型数学.
"""
import json
import sys
import os
import re
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report import analyze, adaptive_horizon, forecast_svg
from chanlun import classify_regime
import audit_forecast_calibration as ac

# 生产看板使用的 5 大指数(与 report.py main 路径一致)
SYMBOLS = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000905"]


def load():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"), encoding="utf-8") as f:
        data = json.load(f)
    kls = {}
    for s in SYMBOLS:
        if s in data and "klines" in data[s]:
            kls[s] = data[s]["klines"]
    return kls


_KAPPA = {"bull": 1.5, "range": 1.4, "bear": 2.3}  # 与 report.py forecast_svg 内覆盖修正系数一致(R168+R171 标定: 牛1.5/震荡1.4/熊2.3)


def recompute_p_hold(closes, zd, horizon, regime="range"):
    """独立用 forecast_svg 同款口径重算 结构存续概率 P(期末价≥ZD).
    regime 用于选取与 forecast_svg 完全一致的覆盖修正 κ(牛=1.5, 震荡=1.4, 熊=2.3)，
    否则在熊市(κ=2.3)会虚假 Δ>3pp 误报自洽性 WARN。"""
    n = len(closes)
    _WIN = 3 * 244
    _wc = closes[-(_WIN + horizon):] if len(closes) >= _WIN + horizon else closes
    _rets = sorted(math.log(_wc[i + horizon] / _wc[i]) for i in range(len(_wc) - horizon))
    if not _rets:
        return None
    def _q(p):
        k = (len(_rets) - 1) * p
        f0 = int(math.floor(k)); c0 = int(math.ceil(k))
        if f0 == c0:
            return _rets[f0]
        return _rets[f0] * (c0 - k) + _rets[c0] * (k - f0)
    _q50 = _q(0.5); _q05 = _q(0.05); _q95 = _q(0.95)
    _mean = sum(_rets) / len(_rets)
    _kappa = _KAPPA.get(regime, 1.4)
    _sp_up = max((_q95 - _q50) * _kappa, 1e-9)
    _sp_dn = max((_q50 - _q05) * _kappa, 1e-9)
    last = closes[-1]
    if not (last > 0 and zd > 0):
        return None
    _r_star = math.log(zd / last)
    if _r_star >= _mean:
        _p = 0.5 * (1 + math.erf(1.645 * (_mean - _r_star) / (_sp_up * math.sqrt(2))))
    else:
        _p = 0.5 * (1 + math.erf(1.645 * (_mean - _r_star) / (_sp_dn * math.sqrt(2))))
    return max(0.01, min(0.99, _p))


def parse_note_p_hold(note):
    m = re.search(r"结构存续概率[（(]锥[^≈]*≈\s*([\d.]+)%", note)
    return float(m.group(1)) / 100.0 if m else None


def parse_note_endpoint(note, label):
    # label 形如 '均值期望终点' / '趋势外推位' / '主路径失效位(有效跌破ZD)'
    # label 含括号等正则元字符, 必须 re.escape 否则失效(实测 ZD 解析恒为 None)
    m = re.search(re.escape(label) + r"\s*≈\s*<b>([\d.]+)</b>", note)
    return float(m.group(1)) if m else None


def check():
    kls = load()
    print("=" * 92)
    print("R86 推演数值内部自洽性监控(关13) — 显示数字彼此不矛盾")
    print("=" * 92)
    print("检查: ①存续概率↔带自洽 ②带单调嵌套 ③带有限非负 ④文本终点↔图series")
    print("-" * 92)
    any_warn = False
    hdr = "%-9s %-8s %-12s %-14s %-14s %-12s" % (
        "指数", "p_hold", "带嵌套", "带有限", "终点一致", "结论")
    print(hdr)
    print("-" * 92)

    for sym in SYMBOLS:
        kl = kls.get(sym)
        if not kl or len(kl) < ac.MIN_HISTORY:
            print("%-9s %-8s %-12s %-14s %-12s" % (sym, "-", "-", "-", "-", "数据不足跳过"))
            continue
        try:
            r = analyze(kl)
            horizon = adaptive_horizon(r["bis"], r["merged"])
            _svg, _note, _probs, _leg, fc = forecast_svg(
                kl, r, r["classify"], 50.0, 0.0, sym, horizon)
        except Exception as e:
            print("%-9s 调用异常: %s" % (sym, e))
            any_warn = True
            continue

        proj = fc.get("proj") or []
        closes = [k["close"] for k in kl]
        last = closes[-1]
        zs = r["zhongshu"][-1] if r["zhongshu"] else None
        zd = zs["zd"] if zs else last * 0.95

        # ① 存续概率自洽(用与 forecast_svg 完全一致的 regime κ 重算, 避免熊市误报)
        disp = parse_note_p_hold(_note)
        rg = classify_regime(kl)
        recomp = recompute_p_hold(closes, zd, horizon, rg)
        ph_txt = "OK"
        if disp is None or recomp is None:
            ph_txt = "无值"
            any_warn = True
        else:
            diff_pp = abs(disp - recomp) * 100
            if diff_pp > 3.0:
                ph_txt = "WARNΔ%.0fpp" % diff_pp
                any_warn = True

        # ② 带单调嵌套 + ③ 有限非负
        nest_ok = True
        finite_ok = True
        for p in proj:
            l95 = p.get("f95l"); fh95 = p.get("f95h")
            l75 = p.get("f75l"); fh75 = p.get("f75h")
            med = p.get("med")
            # 先判定字段齐全，避免在 f95l/f75l 为 None 时计算 None+number 直接 TypeError 崩溃
            if any(v is None for v in (l95, fh95, l75, fh75, med)):
                finite_ok = False
                continue
            u95 = l95 + fh95; u75 = l75 + fh75
            vals = [l95, l75, med, u75, u95]
            if any(not math.isfinite(v) or v <= 0 for v in vals):
                finite_ok = False
                continue
            tol = max(1.0, abs(med) * 0.005)
            if not (l95 - tol <= l75 and l75 - tol <= med and med - tol <= u75 and u75 - tol <= u95):
                nest_ok = False
        nest_txt = "OK" if nest_ok else "WARN"
        finite_txt = "OK" if finite_ok else "WARN"
        if not nest_ok or not finite_ok:
            any_warn = True

        # ④ 文本终点 ↔ 图 series 末点
        ep_ok = True
        ep_detail = []
        med_ep = parse_note_endpoint(_leg, "均值期望终点")
        if med_ep is not None and proj:
            if abs(med_ep - proj[-1]["med"]) / max(1.0, proj[-1]["med"]) > 0.01:
                ep_ok = False
                ep_detail.append("med")
        tr_ep = parse_note_endpoint(_leg, "趋势外推位")
        if tr_ep is not None and proj:
            if abs(tr_ep - proj[-1]["trend"]) / max(1.0, proj[-1]["trend"]) > 0.01:
                ep_ok = False
                ep_detail.append("trend")
        zd_ep = parse_note_endpoint(_leg, "主路径失效位(有效跌破ZD)")
        if zd_ep is not None:
            if abs(zd_ep - zd) / max(1.0, zd) > 0.01:
                ep_ok = False
                ep_detail.append("zd")
        ep_txt = "OK" if ep_ok else ("WARN:" + ",".join(ep_detail))
        if not ep_ok:
            any_warn = True

        disp_s = ("%.0f%%" % (disp * 100)) if disp is not None else "-"
        print("%-9s %-8s %-12s %-14s %-14s %-12s" % (
            sym, disp_s, ph_txt, nest_txt, finite_txt, ep_txt))

        # 详细: 若自洽偏差, 打印重算值便于核对
        if ph_txt.startswith("WARN"):
            print("      ⚠ 存续概率显示=%.0f%% 独立重算=%.0f%% (Δ=%.1fpp>3pp)"
                  % (disp * 100, recomp * 100, abs(disp - recomp) * 100))
        if not ep_ok:
            m = proj[-1]["med"] if proj else None
            t = proj[-1]["trend"] if proj else None
            print("      ⚠ 终点不一致: 显示 med=%.0f/图末=%.0f | 显示trend=%.0f/图末=%.0f | 显示ZD=%.0f/中枢=%.0f"
                  % (med_ep or -1, m or -1, tr_ep or -1, t or -1, zd_ep or -1, zd))

    print("-" * 92)
    print("结论: %s" % ("⚠ WARN — 存在自洽性异常(监控, 不阻断)" if any_warn
                          else "✅ 推演数值内部自洽(存续概率↔带 / 带嵌套 / 带有限 / 文本↔图 全部一致)"))
    print("注: 仅复核显示数字自洽。")
    sys.exit(0)


if __name__ == "__main__":
    check()
