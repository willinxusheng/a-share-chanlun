# -*- coding: utf-8 -*-
"""拉取 A 股主要指数日线+周线数据，含完整性校验与新浪交叉验证"""
import json
import os
import urllib.request
from datetime import datetime, timedelta

SYMBOLS = {
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000905": "中证500",
}

_END = (datetime.now() + timedelta(days=400)).strftime("%Y-%m-%d")
TX_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,%s,2021-01-01,"
          + _END + ",1600,qfq")
SINA_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=5"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_BASE = os.path.dirname(os.path.abspath(__file__))


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def fetch_tx(symbol, period):
    data = json.loads(_get(TX_URL % (symbol, period)))["data"][symbol]
    klines = data.get("qfqday") or data.get("qfqweek") or data.get("qfqmonth") or data.get("day") or data.get("week") or data.get("month") or []
    out = []
    for row in klines:
        # [日期, 开, 收, 高, 低, 量]
        out.append({
            "date": row[0],
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 else 0.0,
        })
    return out


def fetch_sina_last_close(symbol):
    try:
        arr = json.loads(_get(SINA_URL % symbol))
        return float(arr[-1]["close"]) if arr else None
    except Exception as e:
        return None


def fetch_sina_series(symbol, datalen=1400):
    """拉取新浪全量日线（用于序列级交叉验证），返回 {date: close}"""
    try:
        url = SINA_URL.split("datalen=5")[0] + "datalen=%d" % datalen
        arr = json.loads(_get(url % symbol))
        out = {}
        for row in arr:
            out[row["day"]] = float(row["close"])
        return out
    except Exception as e:
        return {}


def cross_validate(tx_klines, sina_close_map):
    """腾讯(qfq) 与新浪(裸价) 收盘价<b>比值稳定性</b>校验。

    前复权价 = 裸价 × 常数调整因子（相对末日的累计分红/拆股系数），故两源收盘价
    在全序列的比值应基本恒定。若比值漂移过大，说明某源存在分红/拆股口径异常或多日缺口。
    直接比绝对值偏差是错的（qfq 与裸价天然差一个常数倍），比值稳定性才是真正的一致性校验。
    """
    n = len(tx_klines)
    if not sina_close_map or n < 10:
        return {"n": 0, "median_ratio": None, "max_rel_dev": None, "worst_date": None}
    # 抽样：首、尾 + 每年约 2~3 个均匀点
    idxs = set([0, n - 1])
    step = max(1, n // 24)
    for i in range(step // 2, n, step):
        idxs.add(i)
    ratios = []
    for i in sorted(idxs):
        d = tx_klines[i]["date"]
        sc = sina_close_map.get(d)
        if sc and sc > 0 and tx_klines[i]["close"] > 0:
            ratios.append(tx_klines[i]["close"] / sc)
    if len(ratios) < 3:
        return {"n": 0, "median_ratio": None, "max_rel_dev": None, "worst_date": None}
    ratios.sort()
    med = ratios[len(ratios) // 2]
    max_rel = max(abs(r - med) / med for r in ratios)
    worst, worst_date = 0.0, None
    for i in sorted(idxs):
        d = tx_klines[i]["date"]
        sc = sina_close_map.get(d)
        if sc and sc > 0 and tx_klines[i]["close"] > 0:
            rel = abs(tx_klines[i]["close"] / sc - med) / med
            if rel > worst:
                worst, worst_date = rel, d
    return {"n": len(ratios), "median_ratio": round(med, 4),
            "max_rel_dev": round(max_rel, 4), "worst_date": worst_date}


def _date_gap(d1, d2):
    a = datetime.strptime(d1, "%Y-%m-%d")
    b = datetime.strptime(d2, "%Y-%m-%d")
    return (b - a).days


def validate(klines):
    """K线合法性校验，返回问题列表"""
    issues = []
    seen = set()
    for i, k in enumerate(klines):
        d = k["date"]
        if d in seen:
            issues.append("重复日期 %s" % d)
        seen.add(d)
        if k["high"] < k["low"]:
            issues.append("%s high<low" % d)
        if not (k["low"] <= k["close"] <= k["high"]):
            issues.append("%s 收盘价超出高低范围" % d)
        if not (k["low"] <= k["open"] <= k["high"]):
            issues.append("%s 开盘价超出高低范围" % d)
        if k["close"] <= 0:
            issues.append("%s 收盘价非正" % d)
    # 交易日连续性 / 缺失检测（#6）
    if len(klines) >= 2:
        dates = [k["date"] for k in klines]
        for i in range(1, len(dates)):
            dd = _date_gap(dates[i - 1], dates[i])
            if dd <= 0:
                issues.append("日期非递增 %s→%s" % (dates[i - 1], dates[i]))
            elif dd > 14:  # 超过最长法定长假（国庆/春节约 11 天），疑似漏数据而非休市
                issues.append("间隔异常(疑似缺失交易日) %s→%s(%d天)" % (dates[i - 1], dates[i], dd))
        total_days = _date_gap(dates[0], dates[-1])
        if total_days > 0:
            exp = int(total_days / 365.25 * 244)  # A股年均约 244 个交易日
            if abs(len(dates) - exp) > max(5, exp * 0.04):
                issues.append("交易日数量异常：实际%d 预计约%d" % (len(dates), exp))
    return issues[:12]  # 只保留前12条


def main():
    result = {}
    for sym, name in SYMBOLS.items():
        day = fetch_tx(sym, "day")
        week = fetch_tx(sym, "week")
        month = fetch_tx(sym, "month")
        issues = validate(day)
        # 全序列抽样比值一致性校验（腾讯 qfq ↔ 新浪 裸价）
        sina_series = fetch_sina_series(sym)
        cc = cross_validate(day, sina_series)
        result[sym] = {
            "name": name,
            "klines": day,
            "week_klines": week,
            "month_klines": month,
            "meta": {
                "count": len(day),
                "week_count": len(week),
                "month_count": len(month),
                "first_date": day[0]["date"],
                "last_date": day[-1]["date"],
                "issues": issues,
                "consistency": cc,
            },
        }
        print("%s: 日线%d(%s~%s) 周线%d 校验问题%d 双源比值稳定度 样本%d 最大偏离%s" % (
            name, len(day), day[0]["date"], day[-1]["date"], len(week),
            len(issues), cc["n"],
            ("%.3f%%" % (cc["max_rel_dev"] * 100)) if cc["max_rel_dev"] is not None else "N/A"))
    with open(os.path.join(_BASE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print("saved -> chanlun/data.json")


if __name__ == "__main__":
    main()
