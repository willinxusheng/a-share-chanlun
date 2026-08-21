# -*- coding: utf-8 -*-
"""R82 关9 · 点前完整性 + 无未来泄漏 + 带宽抗污染守卫（监控门禁, 不阻断, 退出码恒0）。

为什么需要这一道（R78~R81 的盲区）:
  - 关1(fd.validate)只做「内部一致性」(OHLC越界/重复/非升序/数量异常)，从不对照墙钟；
  - R79 新鲜度护栏只查 last_date 比今天「旧」，对 last_date 在「未来」静默通过
    （_gap_days<0 → 循环为空 → _gap_td=0 → 无预警）。
  而置信带 `_wc = closes[-(732+horizon):]` 天然向后看，唯一能造成「未来泄漏」的入口，
  就是末根/某根 bar 本身日期在未来——旧护栏完全漏检。

本门禁三道独立检查:
  ① 无未来泄漏: 任意 bar 日期 > 今天 → 数据泄漏(CRITICAL)。(关1 已并此硬失败, 此处复核可见)
  ② 带宽抗污染: 校准窗口(近732+horizon根)内若混入「单根异常收益」bar，会毒化经验分位带宽
     (P05/P95 被拉歪 → 你看的置信带失真)。A股指数单日真实波动几乎从不超过±12%，
     故 |日对数收益|≥0.30(≈35%, 远超任何真实情形)判「疑似脏数据」; 0.10≤|lr|<0.30 判
     「真实极端日(核实, 如2024-09政策牛)」仅提示不告警。
  ③ 带宽窗口充分性: 生产需 ≥732+horizon 根, 否则早期截断使校准口径不一致(回测早期锚点已存在此问题)。

退出码恒0(监控), 但清晰打印 CRITICAL/WARN/OK, 与关5~关8 一致的「不阻断、可验证」纪律。
"""
import json
import sys
import os
import math
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(os.path.abspath(__file__))
from chanlun import analyze, adaptive_horizon
from report import _is_trading_day

SYMS = ["sh000001", "sh000300", "sz399001", "sz399006", "sh000905"]
WIN = 3 * 244          # 与 report.py forecast_svg 的 _WIN 一致(近3年≈732交易日)
LR_EXTREME = 0.10      # |日对数收益|≥此值: 真实极端日(提示核实)
LR_CORRUPT = 0.30      # |日对数收益|≥此值: 远超任何真实指数单日波动 → 疑似脏数据


def main():
    print("R82 关9 点前完整性 + 无未来泄漏 + 带宽抗污染守卫 (监控门禁, 不阻断)")
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    today = datetime.now(timezone(timedelta(hours=8))).date()
    today_s = today.isoformat()
    any_critical = False
    warn_stale = False
    warn_window = False
    warn_extreme = False

    print("%-9s %-7s %-11s %-7s %-7s %-14s %-10s" %
          ("代码", "名称", "末根日期", "泄漏", "滞后(TD)", "带宽污染", "窗口"))
    print("-" * 78)

    for sym, d in data.items():
        kl = d["klines"]
        name = d["name"]
        closes = [k["close"] for k in kl]
        last_date = kl[-1]["date"]

        # ① 无未来泄漏
        future_bars = [k["date"] for k in kl if k["date"] > today_s]
        leak = "CRIT" if future_bars else "OK"

        # ② 滞后交易日(复用 R79 口径 _is_trading_day, 与看板一致)
        last_d = datetime.strptime(last_date, "%Y-%m-%d").date()
        gap_days = (today - last_d).days
        if gap_days < 0:
            gap_td = -1  # last_date 在未来(已被①捕获)
        else:
            gap_td = sum(1 for i in range(1, gap_days + 1)
                         if _is_trading_day(last_d + timedelta(days=i)))
        stale = "OK" if gap_td <= 2 else ("W%d" % gap_td)

        # ③ 带宽抗污染: 先看自适应 horizon, 再扫描校准窗口内单根异常收益
        r = analyze(kl)
        horizon = adaptive_horizon(r["bis"], r["merged"])
        need = WIN + horizon
        win_closes = closes[-need:] if len(closes) >= need else closes
        worst_lr = 0.0
        worst_i = -1
        for i in range(1, len(win_closes)):
            if win_closes[i - 1] > 0 and win_closes[i] > 0:
                lr = abs(math.log(win_closes[i] / win_closes[i - 1]))
                if lr > worst_lr:
                    worst_lr = lr
                    worst_i = i
        if worst_lr >= LR_CORRUPT:
            poison = "CORRUPT(%.0f%%)" % ((math.exp(worst_lr) - 1) * 100)
            any_critical = True
        elif worst_lr >= LR_EXTREME:
            poison = "极端%.0f%%" % ((math.exp(worst_lr) - 1) * 100)
            warn_extreme = True
        else:
            poison = "OK"

        # 带宽窗口充分性
        full = len(closes) >= need
        wstat = ("满(%d)" % len(closes)) if full else ("不足%d/%d" % (len(closes), need))
        if not full:
            warn_window = True

        flag = "CRIT" if (future_bars or worst_lr >= LR_CORRUPT) else ("WARN" if (gap_td > 2 or not full or worst_lr >= LR_EXTREME) else "OK")
        print("%-9s %-7s %-11s %-7s %-7s %-14s %-10s" %
              (sym, name, last_date, leak, (str(gap_td) if gap_td >= 0 else "未来"),
               poison, wstat))
        if future_bars:
            print("      ⚠ 未来日期泄漏(数据异常): %s ... 末根=%s 今天=%s" %
                  (", ".join(future_bars[:3]), last_date, today_s))
        if worst_lr >= LR_CORRUPT:
            print("      ⚠ 带宽窗口内疑似脏数据: 第%d根 |日收益|≈%.0f%% ≥ %.0f%% 阈值, 经验分位带(P05/P95)或被毒化" %
                  (worst_i, (math.exp(worst_lr) - 1) * 100, LR_CORRUPT * 100))
        elif worst_lr >= LR_EXTREME:
            print("      ℹ 带宽窗口含真实极端日: |日收益|≈%.0f%%(<%.0f%% 脏数据阈值), 如2024-09政策牛, 经验分位带已自然吸纳, 仅提示" %
                  ((math.exp(worst_lr) - 1) * 100, LR_CORRUPT * 100))
            warn_extreme = True
        if gap_td > 2:
            print("      ⚠ 行情滞后 %d 交易日(≥3触发看板预警), 推演基于旧数据" % gap_td)
            warn_stale = True

    print("-" * 78)
    if any_critical:
        print("结论: ❌ CRITICAL — 存在未来泄漏或带宽污染(关1已硬阻断; 此处复核)")
    elif warn_stale or warn_window or warn_extreme:
        _rs = []
        if warn_stale:
            _rs.append("行情滞后")
        if warn_window:
            _rs.append("带宽窗口不足")
        if warn_extreme:
            _rs.append("含真实极端日(提示)")
        print("结论: ⚠ WARN — %s(均为监控项, 不阻断)" % " / ".join(_rs))
    else:
        print("结论: ✅ 点前完整 / 无未来泄漏 / 带宽未被污染 / 窗口充分")
    print("注: 未来泄漏硬阻断由关1(fd.validate)负责。")
    sys.exit(0)


if __name__ == "__main__":
    main()
