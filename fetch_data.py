# -*- coding: utf-8 -*-
"""拉取 A 股主要指数日线+周线数据，含完整性校验与新浪交叉验证"""
import json
import urllib.request

SYMBOLS = {
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000905": "中证500",
}

TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,%s,2021-01-01,2026-12-31,1600,qfq"
SINA_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=5"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def fetch_tx(symbol, period):
    data = json.loads(_get(TX_URL % (symbol, period)))["data"][symbol]
    klines = data.get("qfqday") or data.get("qfqweek") or data.get("day") or data.get("week") or []
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
    """在腾讯序列上均匀抽样若干交易日，与新浪比对收盘价，返回偏差统计"""
    n = len(tx_klines)
    if not sina_close_map or n < 10:
        return {"n": 0, "max_dev": None, "mean_dev": None, "worst_date": None}
    # 抽样：首、尾 + 每年约 2~3 个均匀点
    idxs = set([0, n - 1])
    step = max(1, n // 24)
    for i in range(step // 2, n, step):
        idxs.add(i)
    devs, worst, worst_date = [], 0.0, None
    compared = 0
    for i in sorted(idxs):
        d = tx_klines[i]["date"]
        if d in sina_close_map and sina_close_map[d] > 0:
            dev = abs(tx_klines[i]["close"] / sina_close_map[d] - 1) * 100
            devs.append(dev)
            compared += 1
            if dev > worst:
                worst, worst_date = dev, d
    if not devs:
        return {"n": 0, "max_dev": None, "mean_dev": None, "worst_date": None}
    return {"n": compared, "max_dev": round(max(devs), 4),
            "mean_dev": round(sum(devs) / len(devs), 4), "worst_date": worst_date}


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
    return issues[:10]  # 只保留前10条


def main():
    result = {}
    for sym, name in SYMBOLS.items():
        day = fetch_tx(sym, "day")
        week = fetch_tx(sym, "week")
        issues = validate(day)
        sina_close = fetch_sina_last_close(sym)
        tx_close = day[-1]["close"] if day else None
        dev = abs(sina_close / tx_close - 1) * 100 if (sina_close and tx_close) else None
        # 全序列抽样交叉验证
        sina_series = fetch_sina_series(sym)
        cc = cross_validate(day, sina_series)
        result[sym] = {
            "name": name,
            "klines": day,
            "week_klines": week,
            "meta": {
                "count": len(day),
                "week_count": len(week),
                "first_date": day[0]["date"],
                "last_date": day[-1]["date"],
                "issues": issues,
                "tx_close": tx_close,
                "sina_close": sina_close,
                "dev_pct": round(dev, 4) if dev is not None else None,
                "cross_check": cc,
            },
        }
        print("%s: 日线%d(%s~%s) 周线%d 校验问题%d 末值偏差%s 序列抽样%d点 最大偏差%s" % (
            name, len(day), day[0]["date"], day[-1]["date"], len(week),
            len(issues), ("%.3f%%" % dev) if dev is not None else "N/A",
            cc["n"], ("%.3f%%" % cc["max_dev"]) if cc["max_dev"] is not None else "N/A"))
    with open("chanlun/data.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print("saved -> chanlun/data.json")


if __name__ == "__main__":
    main()
