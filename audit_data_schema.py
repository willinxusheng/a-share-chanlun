# -*- coding: utf-8 -*-
"""
audit_data_schema.py — A股缠论看板「data.json 结构契约」门禁 (R169 新增, 补 R168 发现的盲区)。

R168 审计指出: 13 道 audit_* 门禁 + verify_overlap.js 没有任何一道校验 data.json 的**结构契约**
(必填键/类型/日期单调/无未来泄漏/OHLC 合理)。data.json 由云端 CI 独家写、本地只读,
一旦 CI 写出损坏结构(缺键/类型错/日期乱序/未来日期泄漏), report.py 会 KeyError 或渲染错。

本门禁**阻断 CI**(退出码 0=通过 1=失败): 结构损坏 = 报告必然出错, 不应放行。

用法: python audit_data_schema.py  (也可被 audit_data_accuracy.py 以 ads.run() 调用)
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(os.path.abspath(__file__))

REQUIRED_KEYS = ("name", "meta", "klines", "week_klines", "month_klines")
BAR_KEYS = ("date", "open", "high", "low", "close", "volume")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 云端 CI 独家写这 5 个标的; 缺失/多余都视为数据损坏(不应放行)。
EXPECTED_SYMS = ["sh000001", "sh000300", "sz399001", "sz399006", "sh000905"]


def china_today():
    # R167: 显式中国时区(UTC+8), 避免 UTC runner 误判跨日导致未来日期误判。
    return datetime.now(timezone(timedelta(hours=8))).date()


def _is_num(x):
    # R170: 加 math.isfinite 拦截 NaN/Inf —— 否则 float("NaN") 会穿透 _is_num 与下方
    # OHLC 比较(nan<=0 / nan<nan 均为 False), 使坏源注入的 NaN/Inf 漏过契约校验。
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_series(name, bars, today, problems):
    if not isinstance(bars, list) or len(bars) == 0:
        problems.append("%s: 序列为空或非 list" % name)
        return
    prev_date = None
    for i, bar in enumerate(bars):
        if not isinstance(bar, dict):
            problems.append("%s[%d]: 非 dict" % (name, i))
            continue
        for k in BAR_KEYS:
            if k not in bar:
                problems.append("%s[%d]: 缺键 %s" % (name, i, k))
        if "date" in bar:
            d = bar["date"]
            if not (isinstance(d, str) and DATE_RE.match(d)):
                problems.append("%s[%d]: date 格式非法 %r" % (name, i, d))
            else:
                if prev_date is not None and d <= prev_date:
                    problems.append("%s[%d]: 日期非单调(%s<=前%s)" % (name, i, d, prev_date))
                prev_date = d
                if d > today.isoformat():
                    problems.append("%s[%d]: 未来日期泄漏 %s(今日%s)" % (name, i, d, today))
        for k in ("open", "high", "low", "close", "volume"):
            if k in bar and not _is_num(bar[k]):
                problems.append("%s[%d]: %s 非数值 %r" % (name, i, k, bar[k]))
        o, h, l, c = (bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"))
        if _is_num(o) and _is_num(h) and _is_num(l) and _is_num(c):
            if c <= 0:
                problems.append("%s[%d]: close<=0" % (name, i))
            if h < l:
                problems.append("%s[%d]: high<low" % (name, i))
            if h < max(o, c) or l > min(o, c):
                problems.append("%s[%d]: OHLC 不合理(high<max(o,c) 或 low>min(o,c))" % (name, i))
        # R170: 成交量非负校验(此前漏检, 负/异常 volume 会污染 breadth 与渲染)
        v = bar.get("volume")
        if _is_num(v) and v < 0:
            problems.append("%s[%d]: volume<0" % (name, i))


def run():
    print("=== 数据schema契约校验 (audit_data_schema, R169) ===")
    path = os.path.join(BASE, "data.json")
    if not os.path.exists(path):
        print("  FAIL: data.json 不存在")
        return False
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print("  FAIL: data.json 解析失败: %s" % e)
        return False
    if not isinstance(data, dict) or len(data) == 0:
        print("  FAIL: data.json 顶层非非空 dict")
        return False

    problems = []
    for s in EXPECTED_SYMS:
        if s not in data:
            problems.append("缺失预期标的: %s" % s)
    extra = set(data.keys()) - set(EXPECTED_SYMS)
    if extra:
        problems.append("非预期标的: %s" % ", ".join(sorted(extra)))

    today = china_today()
    for sym, d in data.items():
        if not isinstance(d, dict):
            problems.append("%s: 值非 dict" % sym)
            continue
        for k in REQUIRED_KEYS:
            if k not in d:
                problems.append("%s: 缺顶层键 %s" % (sym, k))
        if "name" in d and not (isinstance(d["name"], str) and d["name"]):
            problems.append("%s: name 非非空字符串" % sym)
        if "meta" in d and not isinstance(d["meta"], dict):
            problems.append("%s: meta 非 dict" % sym)
        for sk in ("klines", "week_klines", "month_klines"):
            if sk in d:
                _check_series("%s.%s" % (sym, sk), d[sk], today, problems)
        # 跨序列: 周/月末日期不应晚于日线末日期(否则时序错位)。
        kl = d.get("klines")
        for sk in ("week_klines", "month_klines"):
            s2 = d.get(sk)
            if isinstance(kl, list) and kl and isinstance(s2, list) and s2:
                if s2[-1].get("date", "") > kl[-1].get("date", ""):
                    problems.append("%s: %s 末日期 %s 晚于日线末 %s" % (
                        sym, sk, s2[-1].get("date"), kl[-1].get("date")))

    if problems:
        print("  FAIL(%d 项):" % len(problems))
        for p in problems[:40]:
            print("    - %s" % p)
        return False
    print("  OK: %d 标的, 结构契约全部通过(必填键/类型/日期单调/无未来泄漏/OHLC 合理)" % len(data))
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
