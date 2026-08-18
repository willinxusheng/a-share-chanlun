# -*- coding: utf-8 -*-
"""
audit_data_accuracy.py — A股缠论看板「数据准确性」总审计（一键16关）。

关1-3 离线(历史完整性/双源一致性/预测归一化); 关4-16 需 --deep(校准回测/情绪条件化/突变漂移/质量证书/分regime方向/点前完整性/概率校准/区间锐度/水平偏置/数值自洽/路径形态保真/极端尾部覆盖/波动率扩散标度)。
用法: python audit_data_accuracy.py [--deep] [--online]; 退出码 0=全通过 1=失败项。
"""
import json, sys, os, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(os.path.abspath(__file__))
from chanlun import (analyze, adaptive_horizon, classify, forward_vol,
                     health_score, forecast_confidence, backtest_signals,
                     market_breadth, MIN_BI_PCT_WEEK, MIN_BI_PCT_MONTH)
from report import forecast_svg, SC_BULL
import fetch_data as fd

SYMS = ["sh000001", "sh000300", "sz399001", "sz399006", "sh000905"]
TOL_SUM = 0.001  # 概率和偏离容忍（归一化代码保证=1.00，此处防回归）


def load():
    return json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))


def audit_history(data):
    print("\n=== 关1 历史完整性 (validate) ===")
    ok = True
    for sym, d in data.items():
        kl = d["klines"]
        issues = fd.validate(kl)
        status = "OK" if not issues else "FAIL(%d)" % len(issues)
        if issues:
            ok = False
        print("  %-9s %-7s 条数%-5d 问题%d %s (首%s~末%s)" % (
            sym, d["name"], len(kl), len(issues), status, kl[0]["date"], kl[-1]["date"]))
        for i in issues[:5]:
            print("      - %s" % i)
    return ok


def audit_consistency(data, online=False):
    print("\n=== 关2 双源一致性 (腾讯qfq ↔ 新浪裸价) ===")
    ok = True
    for sym, d in data.items():
        meta = d.get("meta", {})
        cstat = meta.get("consistency_status")
        if cstat is None:  # 旧数据无 status 字段
            if online:
                sina = fd.fetch_sina_series(sym)
                cc = fd.cross_validate(d["klines"], sina)
                if cc["n"] == 0:
                    cstat = "N/A(新浪未校验)"
                elif cc["max_rel_dev"] is not None and cc["max_rel_dev"] > 0.02:
                    cstat = "WARN(偏离%.2f%%)" % (cc["max_rel_dev"] * 100)
                else:
                    cstat = "OK"
            else:
                cstat = "N/A(未记录, 加 --online 重算)"
        if cstat.startswith("OK"):
            flag = "OK"
        elif cstat.startswith("N/A"):
            flag = "N/A"   # 未记录/无法校验(中性, 不阻断); 加 --online 可重算
        elif cstat.startswith("WARN"):
            flag = "WARN"
        else:
            flag = "FAIL"
        if flag in ("WARN", "FAIL"):   # 仅 WARN/FAIL 阻断; N/A 中性不阻断(修复语义矛盾)
            ok = False
        print("  %-9s %-7s 一致性:%s" % (sym, d["name"], cstat))
    return ok


def audit_forecast(data):
    print("\n=== 关3 预测归一化 + trend 有效性 (复刻 main 推演管线) ===")
    # —— 复刻 report.main() 的准备工作（保证审计的是真实推演路径，非简化版）——
    results = {s: analyze(d["klines"]) for s, d in data.items()}
    results_week = {s: analyze(d["week_klines"], MIN_BI_PCT_WEEK) for s, d in data.items()}
    results_month = {s: analyze(d["month_klines"], MIN_BI_PCT_MONTH) for s, d in data.items()}
    backtests = {s: backtest_signals(d["klines"], results[s], exclude_last=True) for s, d in data.items()}
    # market_breadth 接收情景字符串列表
    _daily_sc = [results[s]["classify"]["scenario"] for s in data]
    _week_sc = [results_week[s]["classify"]["scenario"] for s in data]
    _month_sc = [results_month[s]["classify"]["scenario"] for s in data]
    bd = market_breadth(_daily_sc, _week_sc, _month_sc)
    _bull_cnt = sum(1 for s in data if results[s]["classify"]["scenario"] in SC_BULL)
    _total = len(data)
    _breadth_bias = (_bull_cnt / _total - 0.5) * 2 * 8

    ok = True
    for sym, d in data.items():
        r = results[sym]
        horizon = adaptive_horizon(r["bis"], r["merged"])
        wcls = results_week[sym]["classify"]
        mcls = results_month[sym]["classify"]
        # 同 main 的关键修复：周线 nested 重算日线 classify
        r["classify"] = classify(r["bis"], r["zhongshu"], r["beichi"],
                                 d["klines"][-1]["close"], wcls, r["segments"],
                                 r["seg_beichi"], mcls)
        health, conf = (health_score(d["klines"], r, wcls),
                        forecast_confidence(r, wcls, backtests[sym], breadth_bias=_breadth_bias))
        sigma = forward_vol([k["close"] for k in d["klines"]], horizon)
        _svg, _note, probs, _leg, fc = forecast_svg(
            d["klines"], r, wcls, conf, sigma, sym, horizon,
            backtests[sym], None, breadth_score=bd["composite"]["score"])
        p_main, p_alt, p_risk = probs
        s = p_main + p_alt + p_risk
        trend = fc["trend"]
        last = d["klines"][-1]["close"]
        sum_ok = abs(s - 1.0) <= TOL_SUM
        # trend 必须是真实价格（R71 bug: 曾存 0.99 比值），落在 last 的 ±50% 内
        trend_ok = (trend > last * 0.5) and (trend < last * 1.5)
        if not sum_ok or not trend_ok:
            ok = False
        print("  %-9s %-7s SUM=%.4f(%s) p=%.2f/%.2f/%.2f trend=%s last=%s(%s)" % (
            sym, d["name"], s, "OK" if sum_ok else "FAIL",
            p_main, p_alt, p_risk, trend, last, "OK" if trend_ok else "FAIL"))
    return ok


def audit_calibration(deep):
    if not deep:
        return True
    print("\n=== 关4 校准回测 (walk-forward, 委托 audit_forecast_calibration.py) ===")
    r = subprocess.run([sys.executable, "audit_forecast_calibration.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0


def audit_sentiment(deep):
    if not deep:
        return True
    print("\n=== 关5 情绪条件化 (委托 audit_sentiment_conditioning.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_sentiment_conditioning.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印门禁判定


def audit_drift(deep):
    if not deep:
        return True
    print("\n=== 关6 突变漂移监控 (委托 audit_forecast_drift.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_forecast_drift.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印门禁判定


def audit_quality_cert(deep):
    if not deep:
        return True
    print("\n=== 关7 预测质量证书 (委托 gen_quality_cert.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "gen_quality_cert.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅生成 quality_cert.json + 打印


def audit_regime_direction(deep):
    if not deep:
        return True
    print("\n=== 关8 分regime方向命中 (委托 audit_regime_direction.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_regime_direction.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印门禁判定


def audit_point_in_time(deep):
    if not deep:
        return True
    print("\n=== 关9 点前完整性+无未来泄漏+带宽抗污染 (委托 audit_point_in_time.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_point_in_time.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印门禁判定


def audit_probability_calibration(deep):
    if not deep:
        return True
    print("\n=== 关10 概率校准诚实性 (委托 audit_probability_calibration.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_probability_calibration.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印可靠性表+Brier


def audit_interval_score(deep):
    if not deep:
        return True
    print("\n=== 关11 区间锐度+不确定性校准 (委托 audit_interval_score.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_interval_score.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印区间评分+锐度诊断


def audit_point_bias(deep):
    if not deep:
        return True
    print("\n=== 关12 点预测水平(价位)偏置 (委托 audit_point_bias.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_point_bias.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印水平偏置+符号检验


def audit_forecast_consistency(deep):
    if not deep:
        return True
    print("\n=== 关13 推演数值内部自洽 (委托 audit_forecast_consistency.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_forecast_consistency.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印自洽性判定


def audit_path_shape(deep):
    if not deep:
        return True
    print("\n=== 关14 推演路径形态保真度 (委托 audit_path_shape.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_path_shape.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印路径形态吻合度+Spearmanρ


def audit_tail_coverage(deep):
    if not deep:
        return True
    print("\n=== 关15 极端尾部覆盖检验 (委托 audit_tail_coverage.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_tail_coverage.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印 Kupiec POF/下行尾部/最差十分位击穿


def audit_vol_scaling(deep):
    if not deep:
        return True
    print("\n=== 关16 波动率扩散标度检验 (委托 audit_vol_scaling.py, 监控门禁不阻断) ===")
    r = subprocess.run([sys.executable, "audit_vol_scaling.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    return r.returncode == 0  # 该脚本恒退出0, 仅打印 √f 标度 log-log 斜率 + 模型带宽 vs 真实扩散 bias


def main():
    deep = "--deep" in sys.argv
    online = "--online" in sys.argv
    print("A股缠论看板 数据准确性总审计  (deep=%s online=%s)" % (deep, online))
    data = load()
    ok1 = audit_history(data)
    ok2 = audit_consistency(data, online)
    ok3 = audit_forecast(data)
    ok4 = audit_calibration(deep)
    ok5 = audit_sentiment(deep)
    ok6 = audit_drift(deep)
    ok7 = audit_quality_cert(deep)
    ok8 = audit_regime_direction(deep)
    ok9 = audit_point_in_time(deep)
    ok10 = audit_probability_calibration(deep)
    ok11 = audit_interval_score(deep)
    ok12 = audit_point_bias(deep)
    ok13 = audit_forecast_consistency(deep)
    ok14 = audit_path_shape(deep)
    ok15 = audit_tail_coverage(deep)
    ok16 = audit_vol_scaling(deep)
    print("\n" + "=" * 60)
    print("汇总: 关1历史完整性=%s  关2双源一致性=%s  关3预测归一化=%s  关4校准回测=%s  关5情绪条件化=%s  关6突变漂移=%s  关7质量证书=%s  关8分regime方向=%s  关9点前完整性=%s  关10概率校准=%s  关11区间锐度=%s  关12水平偏置=%s  关13数值自洽=%s  关14路径形态=%s  关15尾部覆盖=%s  关16扩散标度=%s"
          % (["FAIL", "OK"][ok1], ["FAIL", "OK"][ok2], ["FAIL", "OK"][ok3],
             # 关4-16 为监控门禁(子脚本恒退出0, 不阻断), 其告警由各自子报告打印;
             # 总审计不再冒充 OK, 浅层标 SKIP、深层标 MON(已监测, 告警见上方子报告)
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON"),
             ("SKIP" if not deep else "MON")))
    allok = ok1 and ok2 and ok3 and (ok4 if deep else True) and (ok5 if deep else True) and (ok6 if deep else True) and (ok7 if deep else True) and (ok8 if deep else True) and (ok9 if deep else True) and (ok10 if deep else True) and (ok11 if deep else True) and (ok12 if deep else True) and (ok13 if deep else True) and (ok14 if deep else True) and (ok15 if deep else True) and (ok16 if deep else True)
    print("结论: %s" % ("✅ 全部通过" if allok else "❌ 存在失败项"))
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
