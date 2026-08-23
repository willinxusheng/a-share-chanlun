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
    data, agg, _, regime_agg = ac.run()  # 复用 R72 walk-forward 引擎(截断跑真实 forecast_svg)

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
            # R169: 覆盖率维度补 N>=20 下限(与 R167 C2 的 bias/direction 同口径);
            # N<20 视为统计噪声, cover95 置 None(报告渲染为"-"+⚠️样本不足), 不再写虚假高精度。
            cov = round(s["in95"] / n * 100, 1) if (n and n >= 20) else None
            # R173(F7): 方向/偏置维度与覆盖同口径——N<20 视为统计噪声, 不显精确百分比(避免假精度)。
            dm = round(s["dir_main"] / n * 100, 1) if (n and n >= 20) else None
            dmed = round(s["dir_med"] / n * 100, 1) if (n and n >= 20) else None
            bias = round(statistics.median(s["bias_list"]) * 100, 2) if (s["bias_list"] and n >= 20) else None
            # R80 覆盖维度
            raw_cov = s["in95"] / s["N"] * 100 if (n and n >= 20) else None
            regime_cov[rg]["T%d" % H] = {"N": n, "cover95": cov, "bias_median": bias}
            if raw_cov is not None and raw_cov < 85.0:   # 任一 regime 覆盖<85% 即预警(真实值常破带=区间不可信); 用原始比值与 weak_cov 同口径
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
            # R173(F8): 收集覆盖<85% 的所有 regime(不限 N>=20), 使下方"样本偏小"提示对 N<20 也可达,
            # 修复此前 weak_cov 仅含 N>=20 导致该分支恒为 False 的死代码。
            if s["N"] and s["in95"] / s["N"] * 100 < 85.0:
                weak_cov.append((rg, H, s["N"]))
            if s["N"] >= 20 and s["dir_main"] / s["N"] * 100 < 50.0:
                weak_dir.append((rg, H, s["N"]))
    # R173(F8): 小样本判定基于全部 regime 的 N, 不再依赖 weak_cov(后者不含 N<20)。
    _small_n = [(rg, H, regime_agg[rg][H]["N"]) for rg in ("bull", "bear", "range") for H in ac.H_TARGETS
                if regime_agg[rg][H]["N"] < 20]
    if weak_cov:
        parts = ["%s T+%d 覆盖<85%%(N=%d)" % (rg, H, n) for rg, H, n in weak_cov]
        regime_note += "覆盖异常: " + "; ".join(parts)
        if _small_n:
            regime_note += (" — 部分 regime 样本偏小(N<20, 或系统计噪声), 暂不改模型避免过拟合, "
                            "待积累熊市样本(延长历史/下次熊市)后定点加固 T+30 带宽")
    if weak_dir:
        parts = ["%s T+%d 方向命中<50%%(N=%d)" % (rg, H, n) for rg, H, n in weak_dir]
        regime_note += ("; " if regime_note else "") + "方向异常: " + "; ".join(parts)
        regime_note += (" — 主路径『方向』在动量/震荡regime的短线命中不如抛硬币, "
                        "决策应以「置信区间(带)」为锚而非单一方向路径; 方向技能主要体现在中长线(T+30)且需结合regime")

    # 取 T8/T30 中 |中位偏置| 最大者(signed)作为最差口径: 用于证书标红判定(bias_ok)与
    # 展示(bias_worst)同口径, 避免「标红用 worst、显示却用 T8 偏置」的口径分裂误导用户
    _nonempty = [H for H in ac.H_TARGETS if tot[H]["bias_list"]]
    if _nonempty:
        worst = max(((H, statistics.median(tot[H]["bias_list"]) * 100)
                     for H in _nonempty),
                    key=lambda kv: abs(kv[1]))
        worst_bias = abs(worst[1])
        worst_bias_signed = round(worst[1], 2)
    else:  # R156 防御: 退化数据(全样本 bias_list 为空)时 max() 会抛 ValueError 致证书生成崩溃
        worst_bias = 0.0
        worst_bias_signed = 0.0
    # R165: bias_ok 须覆盖 regime 级偏置——全样本最差偏置(约0.86%)会掩盖某 regime 超阈
    # (如实跑熊市T+30偏置8.34%>5%), 否则证书自相矛盾(标 bias_ok=True 却 regime 漏报偏置)。
    _regime_worst = 0.0
    for _rg, _hs in regime_cov.items():
        for _h, _v in _hs.items():
            _b = _v.get("bias_median")
            # R167: 仅当该 regime 样本充足(N>=20, 与 direction 同阈值)才计入偏置,
            # 避免 N<20 的统计噪声(如实跑熊市 T+30 N=9)污染全局 bias_ok 致误报红标。
            if _b is not None and _v.get("N", 0) >= 20 and abs(_b) > abs(_regime_worst):
                _regime_worst = _b
    if abs(_regime_worst) > abs(worst_bias_signed):
        worst_bias_signed = round(_regime_worst, 2)
    worst_bias = abs(worst_bias_signed)
    bias_ok = worst_bias <= 5.0
    # 取所有标的最新日期的最大值（不同指数末根日期可能差 1 个交易日），避免只取首个标的偏低
    last_date = max((data[s]["meta"]["last_date"] for s in data), default=None) if data else None

    # R173(F9): 诚实分级——方向逆相关或全 regime 样本不足(N<20)时降级标 warn/review
    _any_insufficient = any(regime_agg[rg][H]["N"] < 20
                            for rg in ("bull", "bear", "range") for H in ac.H_TARGETS)

    cert = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_last_date": last_date,
        "calibration": cal,
        "bias_warn": 5.0,
        "bias_ok": bias_ok,
        "bias_worst": worst_bias_signed,  # 与 bias_ok 同口径(最差), 消费端优先显示, 消除口径分裂误导
        "drift": {
            # R170: 原硬编码 "healthy" 改为由实际偏置派生(与 bias_ok/worst_bias 同口径),
            # 使证书不谎称健康——当存在 |中位乘性偏置|>5%(N>=20) 时如实标 warn。
            # (突变漂移维度由 audit_forecast_drift 门禁在 CI 实时监测, 此处锚定系统性偏置口径)
            "status": "warn" if not bias_ok else "healthy",
            "note": ("R78/R170 漂移监控: 系统性乘性偏置最差 %.1f%%(阈值±5%%, N>=20) — %s"
                     % (worst_bias,
                        "超阈值! 模型存在系统性高估/低估, 见 regime_coverage 板块"
                        if not bias_ok
                        else "在阈值内, 预测随行情平滑移动无系统性漂移; 突变漂移维度由 CI 门禁实时监测")),
        },
        "sentiment": {
            "status": "monitor_only",
            "note": "R76 情绪条件化: T+30 样本外 +8.9pp(极端区 31%→69%)但近期极端样本 N=10 不足, 未并入模型, 仅透明化",
        },
        # R173(F9): 诚实分级——方向逆相关(命中<50%, n>=20)比覆盖略窄更严重(作为信号有害), 至少 warn;
        # 全 regime 样本不足(N<20)无法验证校准, 显式 review; 其余 healthy。
        # R172: accuracy_status 由实际回测结果派生, 不再写死 "capped"
        "accuracy_status": ("warn" if (not bias_ok or regime_warn or regime_dir_warn)
                            else ("review" if _any_insufficient else "healthy")),
        "accuracy_note": "预测准确性16道监控门禁(R70-R89)已全部落地, 数学层面封顶: 覆盖良好(关11); 方向/概率/路径形态无技能(关8/关10/关14, 不可作信号); 价位无偏(关12); 数值自洽(关13); 极端尾部平静市兜住、熊市T+30漏覆盖33%(关15全历史回测口径; 当前小样本见regime_coverage板块); 波动率√f全样本成立但regime内亚线性致长horizon带虚胖(关16)。决策仍锚置信带区间+中位路径价位, 熊市自加安全垫。",
        "regime_coverage": regime_cov,
        "regime_warn": regime_warn,
        "regime_direction": regime_dir,
        "regime_dir_warn": regime_dir_warn,
        "regime_note": regime_note,
    }
    out = os.path.join(base, "quality_cert.json")
    # R207: 写文件失败(只读/磁盘满/权限)不得让脚本非0退出——本脚本是 audit_data_accuracy
    # 关7「监控门禁」委托对象, 设计语义为「恒退出0, 仅生成+打印, 不阻断总审计」(见 audit_data_accuracy
    # 注释)。原 open/json.dump 未捕获, 写失败抛 PermissionError -> 退出码1 -> 关7 误判 FAIL ->
    # 总审计 allok=False -> 意外 exit 1 误杀部署。包 try/except, 失败仅 WARN, 仍 return 0。
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(cert, f, ensure_ascii=False, indent=2)
        print("[quality_cert] written ->", out)
    except Exception as _e:
        print("[quality_cert] WARN 写文件失败(不影响监控门禁判定): %s" % _e)
    print(json.dumps(cert, ensure_ascii=False, indent=2)[:700])
    return 0


if __name__ == "__main__":
    sys.exit(main())
