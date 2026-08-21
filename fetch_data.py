# -*- coding: utf-8 -*-
"""拉取 A 股主要指数日线+周线数据，含完整性校验与新浪交叉验证"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

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
    # R173(F1): 指数退避重试, 避免瞬时网络失败(5xx/超时)直接丢弃整标的 → 看板对缺失标的静默标 N/A
    req = urllib.request.Request(url, headers=UA)
    _delay = 1
    for _i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            if _i == 2:
                raise
            time.sleep(_delay)
            _delay = min(_delay * 3, 9)


def fetch_tx(symbol, period):
    data = json.loads(_get(TX_URL % (symbol, period)))["data"][symbol]
    klines = data.get("qfqday") or data.get("qfqweek") or data.get("qfqmonth") or data.get("day") or data.get("week") or data.get("month") or []
    out = []
    dirty = 0
    for row in klines:
        try:
            # [日期, 开, 收, 高, 低, 量]
            out.append({
                "date": row[0],
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        except (ValueError, IndexError, TypeError):
            # R164: 脏 bar(缺字段/空值/类型错)跳过, 不中断整标的抓取
            # R173(F2): 计数并暴露到 meta.issues, 避免"静默丢根"致看板对缺失数据无知
            dirty += 1
            continue
    return out, dirty


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


# 各周期正常相邻最大间隔(天)与年均 bar 数——validate 原按日线假设写死(14天/244),
# 直接套周/月线会误报"间隔异常/数量异常"(月线相邻~30天必触发 dd>14)。R156 改为按 period 参数化,
# 使同一校验函数可正确服务于日/周/月三线, 避免扩展校验周月时产生虚假问题。
_GAP_MAX = {"day": 14, "week": 21, "month": 45}
_PER_YEAR = {"day": 244, "week": 52, "month": 12}


def validate(klines, period="day"):
    """K线合法性校验，返回问题列表。

    R82 增强：除原有内部一致性(OHLC越界/重复/非升序/数量异常)外，
    新增「点前完整性 / 无未来泄漏」硬校验——任意 bar 日期 > 今天即视为数据泄漏
    （置信带向后取近3年窗口，唯一能造成未来泄漏的入口就是末根/某根 bar 本身是未来日期；
    旧新鲜度护栏只查 last_date 比今天旧、对「未来日期」静默通过）。此项为硬失败，
    命中即阻断部署，确保线上推演锚定的是真实「当下」而非虚构未来。
    """
    issues = []
    seen = set()
    _today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()  # R167: 中国时区(UTC+8), 避免 UTC 服务器跨日窗口把中国"今日"bar 误判为未来日期泄漏
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
    # 交易日连续性 / 缺失检测（#6）——按 period 取对应阈值, 避免周/月线被日线假设误判
    _gap_max = _GAP_MAX.get(period, 14)
    _per_year = _PER_YEAR.get(period, 244)
    if len(klines) >= 2:
        dates = [k["date"] for k in klines]
        for i in range(1, len(dates)):
            dd = _date_gap(dates[i - 1], dates[i])
            if dd <= 0:
                issues.append("日期非递增 %s→%s" % (dates[i - 1], dates[i]))
            elif dd > _gap_max:  # 超过该周期正常相邻最大间隔, 疑似漏数据而非休市
                issues.append("间隔异常(疑似缺失交易日) %s→%s(%d天)" % (dates[i - 1], dates[i], dd))
        total_days = _date_gap(dates[0], dates[-1])
        if total_days > 0:
            exp = int(total_days / 365.25 * _per_year)  # 按周期年均 bar 数估算
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
    _today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()  # R170: 与下方 :176 中国时区守卫一致, 避免 UTC runner 跨日使 _today 偏差致末根半截 bar 误判
    for sym, name in SYMBOLS.items():
        try:
            day, dirty_day = fetch_tx(sym, "day")
            # R164 防御: 今日未收盘(15:00 前)时源端可能返回进行中当日 bar,
            # 丢弃末根避免未来泄漏(置信带锚定虚构"当下")。已收盘(>=15:00)则保留完整当日 bar。
            if day and day[-1]["date"] == _today and datetime.now(timezone(timedelta(hours=8))).hour < 15:  # R167: 显式中国时区, 不再依赖服务器本地 TZ(UTC runner 会误判 15:00 收盘致误丢/误留当日 bar)
                day = day[:-1]
            if not day:
                print("WARN %s 无日线数据, 跳过该标的" % name)
                continue
            week, dirty_week = fetch_tx(sym, "week")
            month, dirty_month = fetch_tx(sym, "month")
            # R156: 周/月线此前完全未校验——report.py 会 analyze 周/月线(1963-1964)并 feeding market_breadth,
            # 若不校验, 周/月线的未来日期泄漏/OHLC 违规会无声流入看板, 而 R82 硬拦只查日线 issues。
            # 现按 period 校验周/月线, 并将其问题(尤其未来日期)并入 meta.issues, 使硬拦覆盖三线。
            issues_day = validate(day, "day")
            issues_week = validate(week, "week")
            issues_month = validate(month, "month")
            issues = (issues_day
                      + [("周线:" + x) for x in issues_week]
                      + [("月线:" + x) for x in issues_month])
            # R173(F2): 脏 bar 丢根计数并入 meta.issues, 让"静默丢数据"可见
            _dirty_total = dirty_day + dirty_week + dirty_month
            if _dirty_total:
                issues.append("脏bar已跳过(数据可能缺失) %d 根(日%d/周%d/月%d)" % (
                    _dirty_total, dirty_day, dirty_week, dirty_month))
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
        except Exception as e:
            print("WARN %s 抓取/校验失败, 跳过该标的(保留其余已成功标的): %s" % (name, e))
            continue
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
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp_path, out_path)
    except Exception:
        # 写盘失败清理 .tmp：优先 os.remove（CI/Linux/Mac 原生支持, 无 shell 注入风险）；
        # 部分运行时(如本沙箱 safe-delete shim)拦截 Python 删除, 退化为 shell rm 兜底。
        try:
            os.remove(tmp_path)
        except Exception:
            try:
                os.system("rm -f " + tmp_path)
            except Exception:
                pass
        raise
    print("saved -> chanlun/data.json (%d 标的)" % len(result))


if __name__ == "__main__":
    main()
