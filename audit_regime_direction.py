# -*- coding: utf-8 -*-
"""
audit_regime_direction.py — 缠论推演「分市场环境方向命中」监控门禁（R81 新增）

目的：R80 已暴露「熊市 T+30 覆盖仅 66.7% 被全样本 95.6% 掩盖」——但覆盖低 ≠ 方向错。
      本门禁把 walk-forward 的方向命中率(dir_main/dir_med)也按 牛/熊/震荡 三档切片，
      回答一个对逆向交易者更致命的问题：「**在熊市里，斐波那契投影的『方向』到底准不准？**」

核心洞察（预期结论）：
  - 熊市里 sc 情景天然偏空 → 主路径指向下 → 若真实也下跌，dir_main 反而偏高(方向看空可信)；
  - 但置信带中心用「3年均值(正漂移)」、宽度用「3年分位」，熊市实际波动>3年均值 → 覆盖偏低。
  → 结论将是「**熊市方向看空可信、但置信带太窄且中心偏高，区间不可信**」——
    这比 R80 单纯「覆盖低」更 actionable：用户可信任熊市方向、但别把精确点位当真。

实现：复用 R72 audit_forecast_calibration.run()（截断跑真实 forecast_svg，真样本外），
      仅对已有的 regime_agg 做方向切片统计与判定。不改动任何预测数学。
退出码：恒 0（监控门禁，不阻断 CI）；打印门禁判定供人工复核。
"""
import os
import sys
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_forecast_calibration as ac  # noqa: E402

# 门禁阈值
DIR_WARN = 50.0        # 任一 regime 方向命中<抛硬币(50%)即预警(方向不可信)
COV_WARN = 85.0        # 任一 regime 覆盖<85%即预警(区间不可信)
SMALL_N = 20           # regime 样本<20 视为统计噪声, 标「样本不足」避免误判过拟合


def run():
    data, agg, cons, regime_agg = ac.run()
    print("=" * 110)
    print("R81 分市场环境方向命中门禁 — 暴露「全样本方向命中」掩盖的隐藏弱点(锚点每%d交易日)" % ac.ANCHOR_STEP)
    print("=" * 110)
    hdr = (f"{'环境':<7}{'窗口':>5}{'N':>6}{'P05-P95覆盖':>13}{'主路径方向':>12}{'中线方向':>11}{'中线偏误':>10}"
           f"   结论")
    print(hdr)
    print("-" * 110)

    findings = []  # (regime, H, N, cover, dir_main, dir_med, bias, flag)
    for rg in ("bull", "bear", "range"):
        label = {"bull": "牛市", "bear": "熊市", "range": "震荡"}[rg]
        for H in ac.H_TARGETS:
            s = regime_agg[rg][H]
            n = s["N"]
            if n == 0:
                print(f"{label:<7}{'T+'+str(H):>5}{0:>6}  (样本不足)")
                continue
            cov = s["in95"] / n * 100
            dm = s["dir_main"] / n * 100
            dmed = s["dir_med"] / n * 100
            bias = statistics.median(s["bias_list"]) * 100 if s["bias_list"] else 0.0
            # 判定：方向<抛硬币 或 覆盖<85% → 该档预警
            flags = []
            if dm < DIR_WARN:
                flags.append("方向<50%%(不如抛硬币)")
            if cov < COV_WARN:
                flags.append("覆盖<%d%%" % COV_WARN)
            if n < SMALL_N:
                flags.append("样本不足(N<%d)" % SMALL_N)
            flag_txt = "⚠️" + ";".join(flags) if flags else "✅"
            print(f"{label:<7}{'T+'+str(H):>5}{n:>6}{cov:>12.1f}%{dm:>11.1f}%{dmed:>10.1f}%{bias:>9.1f}%   {flag_txt}")
            findings.append((rg, H, n, cov, dm, dmed, bias, bool(flags)))

    print("-" * 110)
    # 跨 regime 方向对比结论(对用户最有用的一句话)
    print("跨市场环境方向对比(主路径方向命中 T+30):")
    for rg in ("bull", "bear", "range"):
        s = regime_agg[rg][30]
        n = s["N"]
        if n:
            dm = s["dir_main"] / n * 100
            print(f"  {rg:<6} T+30 主路径方向命中 {dm:5.1f}% (N={n})")
    print("=" * 110)

    # 门禁汇总判定
    weak_dir = [(rg, H, n, dm) for (rg, H, n, cov, dm, dmed, bias, f) in findings
                if dm < DIR_WARN and n >= SMALL_N]
    weak_cov = [(rg, H, n, cov) for (rg, H, n, cov, dm, dmed, bias, f) in findings
                if cov < COV_WARN and n >= SMALL_N]
    small = [(rg, H, n) for (rg, H, n, cov, dm, dmed, bias, f) in findings if n < SMALL_N]

    print("R81 门禁判定:")
    if weak_dir:
        for rg, H, n, dm in weak_dir:
            print("  ⚠️ %s T+%d 方向命中 %.1f%% <50%%(N=%d) — 该环境下斐波那契『方向』不可信" % (rg, H, dm, n))
    else:
        print("  ✅ 各市场环境主路径方向命中均 ≥50%%(方向技能在分 regime 下未失效)")
    if weak_cov:
        for rg, H, n, cov in weak_cov:
            print("  ⚠️ %s T+%d 覆盖 %.1f%%<%d%%(N=%d) — 置信带过窄/中心偏移, 区间不可信(方向可能仍准)" % (rg, H, cov, COV_WARN, n))
    else:
        print("  ✅ 各市场环境覆盖均 ≥%d%%" % COV_WARN)
    if small:
        print("  ℹ️ 样本不足档(不计入预警, 避免误判过拟合): " +
              ", ".join("%s T+%d(N=%d)" % (rg, H, n) for rg, H, n in small))
    print("=" * 110)
    print("解读: 本门禁把 R80 的「覆盖低」拆成『方向』与『区间』两件事——熊市常见『方向看空可信 / 区间太窄』,"
          "用户可信任熊市方向、但勿把精确点位当真; 这为后续 regime 自适应带宽加固(待熊市样本积累)提供靶向依据。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
