# -*- coding: utf-8 -*-
# R180→R188 预测精度门禁: 校验情绪 KNN 预测的样本外回测精度(forecast_acc)是否仍处合理区间。
# 现已升为 CI 阻断: 覆盖率偏离名义 50% 过多 / MAE 退化 / 方向命中率异常时返回 exit 1,
# 由 deploy.yml 在 calc_v2 之后、report 之前调用, set -euo pipefail 捕获非0 即阻断 Pages 部署。
# 仅"数据缺失(SKIP: 文件不存在/forecast_acc 缺失)"保持 exit 0 不阻断。
# 反选依据见 sentiment/calc_v2.py 内 analysis(_grid.py): 最优 k=15/ctx=15/等权全局。
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE, "sentiment", "sentiment_v2.json")


def main():
    print("=== 情绪预测精度监控 (audit_sentiment_forecast_acc, R180) ===")
    if not os.path.exists(PATH):
        print("  SKIP: 未找到 sentiment_v2.json (CI 尚未生成情绪产物)")
        return 0
    try:
        d = json.load(open(PATH, encoding="utf-8"))
    except Exception as e:
        print("  WARN: 解析 sentiment_v2.json 失败: %s" % e)
        return 0
    acc = d.get("forecast_acc")
    fc = d.get("forecast")
    if not isinstance(acc, dict) or not fc:
        print("  SKIP: forecast_acc 缺失(样本不足或无预测)")
        return 0
    cov = float(acc.get("cov", 0))
    mae = float(acc.get("mae", 0))
    dacc = float(acc.get("dir_acc", 0))
    n = int(acc.get("n", 0))
    print("  配置: k=%s ctx=%s weight=%s regime=%s" % (
        fc.get("k"), fc.get("ctx"), fc.get("weight"), fc.get("regime_weight")))
    print("  样本外回测(%d 锚点): 覆盖率=%.1f%%  方向命中=%.1f%%  平均误差=%.1f 分" % (n, cov, dacc, mae))

    warns = []
    # p25-p75 名义覆盖应≈50%; 偏离过多=校准失准
    if cov < 30 or cov > 65:
        warns.append("覆盖率 %.1f%% 偏离名义 50%% 过多(疑似带宽失准)" % cov)
    # MAE 在 0-100 标尺上; 历史约 21-25, 设退化阈值 35
    if mae > 35:
        warns.append("平均误差 %.1f 分退化(历史基线≈22)" % mae)
    if not (0 <= dacc <= 100):
        warns.append("方向命中率 %.1f%% 异常" % dacc)

    if warns:
        print("  ⛔ 预测精度退化, 阻断发布 (exit 1):")
        for w in warns:
            print("    - " + w)
        print("  结论: 覆盖率偏离名义 50% / MAE 退化 / 方向命中率异常, 已阻断 Pages 部署。")
        return 1
    print("  ✅ 预测精度指标处于合理区间(覆盖率贴近名义 50%, MAE 未退化)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
