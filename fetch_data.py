# -*- coding: utf-8 -*-
"""拉取 A 股主要指数日线+周线数据，含完整性校验与新浪交叉验证"""
import json
import os
import sys
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


def fetch_sina_series(symbol, datalen=2000):
    """拉取新浪全量日线（用于序列级交叉验证），返回 {date: close}"""
    try:
        url = SINA_URL.split("datalen=5")[0] + "datalen=%d" % datalen
        arr = json.loads(_get(url % symbol))
        out = {}
        for row in arr:
            out[row["day"]] = float(row["close"])
        return out
    except Exception as e:
        print("WARN 新浪校验拉取失败(双源一致性将标 N/A):", e)
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
    """K线合法性校验，返回问题列表。

    R82 增强：除原有内部一致性(OHLC越界/重复/非升序/数量异常)外，
    新增「点前完整性 / 无未来泄漏」硬校验——任意 bar 日期 > 今天即视为数据泄漏
    （置信带向后取近3年窗口，唯一能造成未来泄漏的入口就是末根/某根 bar 本身是未来日期；
    旧新鲜度护栏只查 last_date 比今天旧、对「未来日期」静默通过）。此项为硬失败，
    命中即阻断部署，确保线上推演锚定的是真实「当下」而非虚构未来。
    """
    issues = []
    seen = set()
    _today = datetime.now().date().isoformat()
    for i, k in enumerate(klines):
        d = k["date"]
        if d > _today:
            issues.append("未来日期(数据泄漏) %s" % d)
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
    # 未来泄漏是硬失败信号，必须优先保留、不被截断吞掉
    # （否则可能被≥12条其它问题挤到 12 名之外，绕过 main 的硬拦）
    future = [x for x in issues if "未来日期" in x]
    others = [x for x in issues if "未来日期" not in x]
    issues = future + others
    return issues[:12]  # 只保留前12条（未来泄漏已优先排在前面）


def main():
    result = {}
    for sym, name in SYMBOLS.items():
        day = fetch_tx(sym, "day")
        if not day:
            print("WARN %s 无日线数据, 跳过该标的" % name)
            continue
        week = fetch_tx(sym, "week")
        month = fetch_tx(sym, "month")
        issues = validate(day)
        # 全序列抽样比值一致性校验（腾讯 qfq ↔ 新浪 裸价）
        sina_series = fetch_sina_series(sym)
        cc = cross_validate(day, sina_series)
        # 双源一致性提升为“可见门禁”：缺校验/超阈值明确标出，避免静默当“干净”
        if cc["n"] == 0:
            cc_status = "N/A(新浪未校验)"
        elif cc["max_rel_dev"] is not None and cc["max_rel_dev"] > 0.02:
            cc_status = "WARN(偏离%.2f%%)" % (cc["max_rel_dev"] * 100)
        else:
            cc_status = "OK"
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
                "consistency_status": cc_status,
            },
        }
        print("%s: 日线%d(%s~%s) 周线%d 校验问题%d 双源比值稳定度 样本%d 最大偏离%s 一致性:%s" % (
            name, len(day), day[0]["date"], day[-1]["date"], len(week),
            len(issues), cc["n"],
            ("%.3f%%" % (cc["max_rel_dev"] * 100)) if cc["max_rel_dev"] is not None else "N/A",
            cc_status))
    # 写盘前整体硬拦：任一指数含未来泄漏即拒绝落盘，避免脏数据进入线上推演
    # （validate 的"未来日期"为硬失败，命中即阻断部署；其余校验问题仅记录不阻断）
    for sym, res in result.items():
        fi = [x for x in res["meta"]["issues"] if "未来日期" in x]
        if fi:
            print("ERROR 检测到未来泄漏(数据异常), 拒绝写入 data.json:", sym, fi)
            sys.exit(1)
    # Bug H 修复：上游 outage / 解析失败时 result 可能为空，若照常 json.dump({})
    # 会把 data.json 覆写成 {}，污染所有只读机并导致线上推演崩溃。空结果直接拒绝落盘。
    if not result:
        print("ERROR 全部标的抓取失败(疑似上游 outage), 拒绝写入空 data.json 以保护既有数据",
              file=sys.stderr)
        sys.exit(1)
    if len(result) < len(SYMBOLS):
        print("WARN 仅抓到 %d/%d 标的, 仍写入(缺失标的将在看板标 N/A)" % (len(result), len(SYMBOLS)))
    # 原子写：先写 .tmp 再 os.replace，避免写入途中崩溃留下半截损坏文件
    out_path = os.path.join(_BASE, "data.json")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp_path, out_path)
    print("saved -> chanlun/data.json (%d 标的)" % len(result))


if __name__ == "__main__":
    main()
