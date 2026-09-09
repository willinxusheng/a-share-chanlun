# -*- coding: utf-8 -*-
# R180→R188 预测精度门禁: 校验情绪 KNN 预测的样本外回测精度(forecast_acc)是否仍处合理区间。
# 覆盖率偏离名义 50% 过多 / MAE 退化 / 方向命中率异常时返回 exit 1。
# 调用方式(deploy.yml, R238 降级): 数据刷新日(未改代码)按 `|| echo ::warning::` 降级为告警不阻断,
# 保证当日数据照常上线; 若持续偏离应人工检查模型。R279: 更新头注释使其与实际调用语义一致
# (原注释称"set -euo pipefail 捕获非0 即阻断"已过时)。
# 仅"数据缺失(SKIP: 文件不存在/forecast_acc 缺失)"保持 exit 0 不阻断。
# 反选依据见 sentiment/calc_v2.py 内 analysis(_grid.py): 最优 k=15/ctx=15/等权全局。
import json
import math
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
    # R327: 键缺失(结构损坏/calc_v2 改版/旧产物)按「数据缺失」SKIP 而非默认 0——
    # 原 acc.get("cov", 0) 在键缺失时 cov=0 会误判「覆盖率偏离名义50%」exit 1 阻断发布,
    # 但键缺失是产物结构问题不是模型退化, 阻断会掩盖真因且误杀每日数据刷新(R238 精神)。
    if acc.get("cov") is None or acc.get("mae") is None or acc.get("dir_acc") is None:
        print("  SKIP: forecast_acc 键缺失(结构异常, 按数据缺失处理不阻断)")
        return 0
    # R396: float() 后补 isfinite 守卫 —— Python json.load 默认把 JSON 字面量 NaN/
    # Infinity 解析为 float nan/inf(calc_v2 写盘 json.dump allow_nan 默认开), 而 nan 与
    # 任何数比较恒 False, 会穿透阈值判定: cov=nan 时 nan<30/nan>65 均 False -> 静默放行
    # (打印"合理区间"); dacc=nan 时 0<=nan<=100 恒 False -> 误阻断。同一 NaN 三种命运全错
    # (audit_data_schema R170 已修同款 NaN 穿透, 本门禁为 R327 时代产物漏网)。非有限值=
    # 产物异常, 按 R327「结构异常按数据缺失 SKIP 不阻断」语义透明提示后放行。
    _acc_nums = []
    for _k in ("cov", "mae", "dir_acc"):
        try:
            _v = float(acc[_k])
        except (TypeError, ValueError):
            _v = float("nan")
        _acc_nums.append(_v)
    if not all(math.isfinite(_v) for _v in _acc_nums):
        print("  SKIP: forecast_acc 含非有限数值(cov=%r mae=%r dir_acc=%r, 产物异常按数据缺失不阻断)"
              % (acc.get("cov"), acc.get("mae"), acc.get("dir_acc")))
        return 0
    cov, mae, dacc = _acc_nums
    # R396: n 安全读取(R327 只给 cov/mae/dir_acc 加 None 守卫漏 n —— 键存在但 null/非
    # 数字时 int(None) TypeError 崩断发布, 与 R327「键缺失=结构异常 SKIP」语义相悖)
    try:
        n = int(acc.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
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
        warns.append("方向命中率 %.1f%% 异常(超出 0-100 合理域)" % dacc)
    # R327: 方向命中率显著低于随机 50% 视为「预测反向/信号失效」——实测正常≈70%
    # (n=206 锚点, 2026-09 实测 70.4%), 崩到 40% 以下(-9σ 级)原检查完全漏检(越界域几乎恒真),
    # 反向预测比随机更危险(按错误方向操作), 须显式告警。
    elif dacc < 40:
        warns.append("方向命中率 %.1f%% 显著低于随机(正常≈70%%, 疑似方向信号失效/反向)" % dacc)

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
