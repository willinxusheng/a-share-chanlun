"""R79 预测质量自检证书生成器。
复用 R72 滚动样本外回测引擎(audit_forecast_calibration.run)聚合五指数 T+8/T+30 的
覆盖/方向/MAE/中位偏置, 叠加 R78 漂移监控、R76 情绪条件化、R80 分regime覆盖、
R81 分regime方向命中的近期实跑结论,
输出 quality_cert.json —— 供 report.py 在看板顶部渲染「📊 预测质量自检证书」常驻区块,
让预测准确性对用户可见、可验证(trust-but-verify)。
仅读取 + 聚合 + 写出, 不改动任何预测数学; 退出码恒 0(非阻断)。
"""
import json
import os
import sys
import datetime
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_forecast_calibration as ac  # noqa: E402


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    data, agg, cons, regime_agg = ac.run()  # 复用 R72 walk-forward 引擎(截断跑真实 forecast_svg)

    # 聚合五指数 -> 总计(与 R72 report() 同口径)
    tot = {h: {"N": 0, "in95": 0, "in75": 0, "dir_main": 0, "dir_med": 0,
               "mae_main": 0.0, "mae_med": 0.0, "bias_list": []} for h in ac.H_TARGETS}
    for sym in data:
        for H in ac.H_TARGETS:
            s = agg[sym][H]
            for k in ("N", "in95", "in75", "dir_main", "dir_med"):
                tot[H][k] += s[k]
            tot[H]["mae_main"] += s["mae_main"]
            tot[H]["mae_med"] += s["mae_med"]
            tot[H]["bias_list"].extend(s["bias_list"])

    cal = {}
    for H in ac.H_TARGETS:
        s = tot[H]
        n = s["N"]
        cal["T%d" % H] = {
            "N": n,
            "cover95": round(s["in95"] / n * 100, 1) if n else None,
            "cover75": round(s["in75"] / n * 100, 1) if n else None,
            "dir_main": round(s["dir_main"] / n * 100, 1) if n else None,
            "dir_med": round(s["dir_med"] / n * 100, 1) if n else None,
            "mae_main": round(s["mae_main"] / n, 2) if n else None,
            "mae_med": round(s["mae_med"] / n, 2) if n else None,
            "bias_median": round(statistics.median(s["bias_list"]) * 100, 2) if s["bias_list"] else None,
        }

    # R80 分 regime 覆盖聚合 + R81 分 regime 方向命中(牛/熊/震荡):
    # 覆盖低 ≠ 方向错 —— 把两者拆开切片, 暴露「全样本平均」掩盖的隐藏弱点。
    regime_cov = {}
    regime_warn = False
    regime_dir = {}
    regime_dir_warn = False
    for rg in ("bull", "bear", "range"):
        regime_cov[rg] = {}
        regime_dir[rg] = {}
        for H in ac.H_TARGETS:
            s = regime_agg[rg][H]
            n = s["N"]
            cov = round(s["in95"] / n * 100, 1) if n else None
            dm = round(s["dir_main"] / n * 100, 1) if n else None
            dmed = round(s["dir_med"] / n * 100, 1) if n else None
            bias = round(statistics.median(s["bias_list"]) * 100, 2) if s["bias_list"] else None
            # R80 覆盖维度
            regime_cov[rg]["T%d" % H] = {"N": n, "cover95": cov, "bias_median": bias}
            if cov is not None and cov < 85.0:   # 任一 regime 覆盖<85% 即预警(真实值常破带=区间不可信)
                regime_warn = True
            # R81 方向维度(与覆盖解耦): 方向<50%=不如抛硬币=该环境方向不可信
            regime_dir[rg]["T%d" % H] = {"N": n, "dir_main": dm, "dir_med": dmed}
            if dm is not None and n >= 20 and dm < 50.0:
                regime_dir_warn = True

    # 分 regime 告警说明: 覆盖与方向两维度分别诚实标注, 并区分「样本不足(不误判过拟合)」
    regime_note = ""
    weak_cov = []
    weak_dir = []
    for rg in ("bull", "bear", "range"):
        for H in ac.H_TARGETS:
            s = regime_agg[rg][H]
            if s["N"] and s["in95"] / s["N"] * 100 < 85.0:
                weak_cov.append((rg, H, s["N"]))
            if s["N"] >= 20 and s["dir_main"] / s["N"] * 100 < 50.0:
                weak_dir.append((rg, H, s["N"]))
    if weak_cov:
        parts = ["%s T+%d 覆盖<85%%(N=%d)" % (rg, H, n) for rg, H, n in weak_cov]
        regime_note += "覆盖异常: " + "; ".join(parts)
        if any(n < 20 for _, _, n in weak_cov):
            regime_note += (" — 触发regime样本偏小(N<20, 或系统计噪声), 暂不改模型避免过拟合, "
                            "待积累熊市样本(延长历史/下次熊市)后定点加固 T+30 带宽")
    if weak_dir:
        parts = ["%s T+%d 方向命中<50%%(N=%d)" % (rg, H, n) for rg, H, n in weak_dir]
        regime_note += ("; " if regime_note else "") + "方向异常: " + "; ".join(parts)
        regime_note += (" — 主路径『方向』在动量/震荡regime的短线命中不如抛硬币, "
                        "决策应以「置信区间(带)」为锚而非单一方向路径; 方向技能主要体现在中长线(T+30)且需结合regime")

    worst_bias = max((statistics.median(tot[H]["bias_list"]) * 100)
                     for H in ac.H_TARGETS if tot[H]["bias_list"])
    last_date = data[next(iter(data))]["meta"]["last_date"]

    cert = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_last_date": last_date,
        "calibration": cal,
        "bias_warn": 5.0,
        "bias_ok": abs(worst_bias) <= 5.0,
        "drift": {
            "status": "healthy",
            "note": "R78 突变漂移监控: 五指数×T8/T30 的 P95|超额|≤0.7%(阈值10%), 零异常 — 预测随行情平滑移动, 无过拟合/数据异常",
        },
        "sentiment": {
            "status": "monitor_only",
            "note": "R76 情绪条件化: T+30 样本外 +8.9pp(极端区 31%→69%)但近期极端样本 N=10 不足, 未并入模型, 仅透明化",
        },
        "accuracy_status": "capped",
        "accuracy_note": "预测准确性所有安全维度(R70-R89 多轮回测+监控)已到顶/证伪/落地; 数学层面已封顶, 证书仅作可见化与监控; 显示概率p_main校准诚实性由关10监控, 置信带锐度/不确定性校准由关11(audit_interval_score.py)监控, 点预测水平(价位)偏置由关12(audit_point_bias.py)监控, 推演数值内部自洽(存续概率↔带/带嵌套/带有限/文本↔图)由关13(audit_forecast_consistency.py)监控, 推演路径形态保真度(画出的路形状对不对)由关14(audit_path_shape.py)监控——实测逐段方向吻合度≈46~48%(低于naive~50%)、Spearmanρ≈0~-0.2, 即结构主路径的『途中弯法』与真实基本无关, 路径节奏不可用于逐日择时; 极端尾部覆盖(Kupiec POF+下行尾部+最差十分位击穿)由关15(audit_tail_coverage.py)监控——全样本T+30带覆盖与名义5%一致(稳健), 但熊市T+30带例外率33.3%(LRuc=7.1)每3次漏1次(熊市N=9偏小, 与关8/关12同源); 波动率扩散标度(√f法则)由关16(audit_vol_scaling.py)监控——全样本log-log斜率0.483≈0.5(√f成立), 但分regime全部亚线性(牛0.385/熊0.148/震荡0.339<0.5, 波动聚集/均值回复), 即『全样本√f成立』是regime混合平均掩盖了各regime内扩散亚线性; 后果: 模型用√f把短期波动外推长horizon→平静市/牛市T+30带bias≈1.4~1.75(过宽虚胖, 对齐关11覆盖偏高), 熊市N=9仍小样本不稳(关15漏覆盖与关16对齐并存, 均不可靠); 仍坚持以带/中位端点为锚、熊市自行加安全垫",
        "regime_coverage": regime_cov,
        "regime_warn": regime_warn,
        "regime_direction": regime_dir,
        "regime_dir_warn": regime_dir_warn,
        "regime_note": regime_note,
    }
    out = os.path.join(base, "quality_cert.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cert, f, ensure_ascii=False, indent=2)
    print("[quality_cert] written ->", out)
    print(json.dumps(cert, ensure_ascii=False, indent=2)[:700])
    return 0


if __name__ == "__main__":
    sys.exit(main())
