# -*- coding: utf-8 -*-
"""
audit_data_accuracy.py — A股缠论看板「数据准确性」总审计（一键四关）

把 R70~R73 四轮审计验证过的方法固化成单次可复跑的资产：
  关1 历史完整性 : fetch_data.validate(klines) -> issues（OHLC越界/重复/非升序/异常缺口）
  关2 双源一致性 : meta.consistency_status（fetch_data 已算；--online 可在线重算腾讯qfq↔新浪裸价）
  关3 预测归一化 : 复刻 main() 每符号推演管线，断言 p_main+p_alt+p_risk≈1.00 且 fc['trend'] 为真实价格(非~1.0)
  关4 校准回测   : --deep 时委托 audit_forecast_calibration.py（walk-forward 样本外, 验证风险带/方向技能）
  关5 情绪条件化 : --deep 时委托 audit_sentiment_conditioning.py（验证 H5 情绪指数条件化能否提升方向命中；监控门禁, 不阻断）
  关6 突变漂移   : --deep 时委托 audit_forecast_drift.py（监控相邻刷新预测移动 vs 行情移动, 检测过拟合/数据异常；不阻断）
  关7 质量证书   : --deep 时委托 gen_quality_cert.py（聚合 R72 校准 + R78 漂移 + R76 情绪 + R80/R81 分regime覆盖/方向结论, 生成 quality_cert.json 供看板顶部证书区块；不阻断）
  关8 分regime方向: --deep 时委托 audit_regime_direction.py（把方向命中率按牛/熊/震荡切片, 暴露「全样本方向命中」掩盖的弱点——熊市常『方向看空可信/区间太窄』；监控门禁, 不阻断）
  关9 点前完整性 : --deep 时委托 audit_point_in_time.py（无未来泄漏/带宽抗污染/窗口充分性；未来日期泄漏的硬阻断由关1负责, 此处监控复核；不阻断）

用法:
  python audit_data_accuracy.py            # 关1+2+3（离线，约30~60s）
  python audit_data_accuracy.py --deep     # 再加关4+关5+关6+关7（约10~20min）
  python audit_data_accuracy.py --online   # 关2 在线重算双源一致性（需网络）
退出码: 0=全通过, 1=存在失败项
"""
import json, sys, os, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chanlun import (analyze, adaptive_horizon, classify, forward_vol,
                     health_score, forecast_confidence, backtest_signals,
                     market_breadth, MIN_BI_PCT_WEEK, MIN_BI_PCT_MONTH)
from report import forecast_svg, SC_BULL
import fetch_data as fd

SYMS = ["sh000001", "sh000300", "sz399001", "sz399006", "sh000905"]
TOL_SUM = 0.001  # 概率和偏离容忍（归一化代码保证=1.00，此处防回归）


def load():
    return json.load(open("data.json", encoding="utf-8"))


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
        flag = "OK" if cstat.startswith("OK") else ("WARN" if cstat.startswith("WARN") else "FAIL")
        if flag != "OK":
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
    print("\n" + "=" * 60)
    print("汇总: 关1历史完整性=%s  关2双源一致性=%s  关3预测归一化=%s  关4校准回测=%s  关5情绪条件化=%s  关6突变漂移=%s  关7质量证书=%s  关8分regime方向=%s  关9点前完整性=%s"
          % (["FAIL", "OK"][ok1], ["FAIL", "OK"][ok2], ["FAIL", "OK"][ok3],
             (["SKIP", "OK"][ok4] if deep else "SKIP"),
             (["SKIP", "OK"][ok5] if deep else "SKIP"),
             (["SKIP", "OK"][ok6] if deep else "SKIP"),
             (["SKIP", "OK"][ok7] if deep else "SKIP"),
             (["SKIP", "OK"][ok8] if deep else "SKIP"),
             (["SKIP", "OK"][ok9] if deep else "SKIP")))
    allok = ok1 and ok2 and ok3 and (ok4 if deep else True) and (ok5 if deep else True) and (ok6 if deep else True) and (ok7 if deep else True) and (ok8 if deep else True) and (ok9 if deep else True)
    print("结论: %s" % ("✅ 全部通过" if allok else "❌ 存在失败项"))
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
