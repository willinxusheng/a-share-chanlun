#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宽基 ETF 份额日度快照 — 疑似国家队资金流代理 (national/calc_etf.py)
====================================================================
背景: 国家队个股持仓仅季度披露(滞后 1-2 月), 对实操滞后。汇金 2024 起主战场
切至宽基 ETF, 而 ETF 总份额为基金公司**每个交易日披露** → 日度代理通道:
   日度 Δ份额 × 净值 = 当日净申赎资金 → "疑似国家队/大资金" 方向参考
   (博主"ETF 份额看国家队加减仓"同款方法)。

数据源: 东财 push2 行情接口 f84=总份额(份) / f43=现价(fltt=1 为小数) / f116=总市值。
   已双源互证(2026-09-09): 510300 东财 f84=23468887808 份 ≈ 腾讯自选股
   total_shares=23468887700 份, 总市值 1088.25 亿两侧完全相等。
   单次 HTTP 即得, CI 可直连; 多数字子域轮换抗限流。

产物: national/etf_share.json (tracked, CI 提交, 轻量 ~KB 级):
   { "meta": {...},
     "series":  { "510300": [["2026-09-09", shr_份, nav_元, mkt_元], ...] },  日度快照(自建)
     "quarter": { "510300": [["2026-06-30", 期间申_亿份, 期间赎_亿份, 期末份额_亿份, 期末规模_亿元], ...] } }
   quarter = 基金定期报告(季报)披露的份额/申赎长史, 免费接口可回溯至上市日
   (510300 自 2012-05 共 71 期 / 510050 自 2004 共 91 期), 与日度 series 互补:
   季度线看历史复盘(2024 汇金进场/2026 撤离), 日度线看当下动作。

数据源:
  日度 = 东财 push2 行情接口 f84=总份额(份) / f43=现价(fltt=1 为小数) / f116=总市值。
     已双源互证(2026-09-09): 510300 东财 f84=23468887808 份 ≈ 腾讯自选股
     total_shares=23468887700 份, 总市值 1088.25 亿两侧完全相等。
     单次 HTTP 即得, CI 可直连; 多数字子域轮换抗限流。
  季度 = 天天基金 FundArchivesDatas type=gmbd (fundf10.eastmoney.com), 单次请求全量
     列: [报告期, 期间申购(亿份), 期间赎回(亿份), 期末份额(亿份), 期末规模(亿元)]
     实测 510300: 2024-09-30 期末 969.22 亿份(汇金 Q3 进场峰) ↔ 2026-06-30 189.15(撤离)

幂等/防循环守卫(关键, CI 每次 deploy 都会跑本脚本):
  G0 季度: 仅当产物缺 quarter 或 meta.quarter_through 落后于 510300 probe 到的最新报告期
     才全池重拉(日常 probe 510300 一次即 [skip], 不写盘 → git 干净 → 无循环);
  G1 当日(北京)该 code 最后一条已记录 → 跳过(不 append 不 commit);
  G2 份额与最新记录完全一致且非当日 → 非交易日/无申赎 → 跳过 append,
     防止"无变化也写盘 → commit → push → 再触发 deploy"死循环;
  G3 单只份额环比变动 >30% → WARN 照记(拆份额/数据异常可识别);
  G4 全池拉取失败 → exit 0 不阻断 deploy(留给下个时点补), 不写盘。

用法: python3 calc_etf.py            # 读/写 national/etf_share.json
"""
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "etf_share.json")

# 东财行情多域名轮换(抗本机/CI 偶发断连)
HOSTS = ["push2.eastmoney.com", "1.push2.eastmoney.com", "7.push2.eastmoney.com",
         "13.push2.eastmoney.com", "33.push2.eastmoney.com"]

# 国家队主力宽基 ETF 池: (secid, 名称) —— 沪 1. / 深 0.
# 聚焦: 沪深300双地/上证50/180/中证500/1000 + 创业板/科创50(成长) + 红利(险资&国家队)
# 注: 588080 与 588000 同为科创50(指数重复)剔除; 黄金/商品 ETF 非国家队宽基方向剔除
POOL = [
    ("1.510300", "沪深300(沪)"),
    ("0.159919", "沪深300(深)"),
    ("1.510050", "上证50"),
    ("1.510180", "上证180"),
    ("1.510500", "中证500"),
    ("1.512100", "中证1000"),
    ("0.159915", "创业板"),
    ("0.159949", "创业板50"),
    ("1.588000", "科创50"),
    ("1.510880", "红利"),
]

CHG_WARN = 0.30  # 单只份额环比变动告警阈值


def fetch_one(sid, tries=4):
    """拉单只 ETF {shr份, nav元, mkt元}; 失败返回 None。"""
    for _ in range(tries):
        hst = random.choice(HOSTS)
        url = ("https://%s/api/qt/stock/get?secid=%s"
               "&fields=f57,f58,f43,f84,f116&fltt=1" % (hst, sid))
        try:
            h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 Chrome/120",
                 "Referer": "https://quote.eastmoney.com/", "Accept": "*/*",
                 "Connection": "close"}
            ctx = ssl.create_default_context()
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=12, context=ctx)
            d = json.loads(r.read().decode("utf-8", "ignore")).get("data") or {}
            if d.get("f84"):
                shr = int(d["f84"])
                nav = float(d.get("f43") or 0)
                mkt = float(d.get("f116") or 0)
                # R414: nav 单位兜底 — fltt 在部分子域失效时 f43 返回厘(如 4637=4.637 元),
                # 特征 nav>100 必为厘级 → 用总市值/总份额精确重算 (mkt/shr 恒为元)
                if nav > 100 and shr and mkt:
                    nav = round(mkt / shr, 6)
                return {"shr": shr, "nav": nav, "mkt": mkt}
        except Exception:
            time.sleep(1.2)
    return None


GMBD_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code=%s&rt=0.1"
GMBD_REF = "http://fundf10.eastmoney.com/"


def _num(s):
    """'1,999.14' → 1999.14; '---'/''/'-' → None"""
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "---", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_quarter_history(code, tries=3):
    """天天基金 gmbd: 单次请求全量季度份额历史(自上市日至今)。

    返回升序 [[报告期, 期间申(亿份), 期间赎(亿份), 期末份额(亿份), 期末规模(亿元)], ...]
    首条=最近报告期(2026-06-30)。失败返回 None。
    """
    for _ in range(tries):
        try:
            h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 Chrome/120",
                 "Referer": GMBD_REF}
            ctx = ssl.create_default_context()
            r = urllib.request.urlopen(urllib.request.Request(GMBD_URL % code, headers=h),
                                       timeout=15, context=ctx)
            html = r.read().decode("utf-8", "ignore")
            out = []
            for m in re.finditer(r"<tr>(.*?)</tr>", html, re.S):
                tds = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
                if len(tds) < 5:
                    continue
                dt = re.sub(r"<[^>]+>", "", tds[0]).strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", dt):
                    continue  # 表头
                buy = _num(re.sub(r"<[^>]+>", "", tds[1]))
                red = _num(re.sub(r"<[^>]+>", "", tds[2]))
                end = _num(re.sub(r"<[^>]+>", "", tds[3]))
                mkt = _num(re.sub(r"<[^>]+>", "", tds[4]))
                if end is None:
                    continue
                out.append([dt, buy, red, end, mkt])
            out.sort(key=lambda x: x[0])
            if out:
                return out
        except Exception:
            time.sleep(1.2)
    return None


def bj_today():
    """北京时间日期 (UTC+8)。"""
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def refresh_quarter(data):
    """季度真身长史刷新 (G0 幂等)。

    规则: 当 data 无 quarter 字段, 或 data.meta.quarter_through != 最新报告期
    (以池内第一只 probe 为准), 才全池重拉 gmbd 历史。否则返回 False 不写盘
    (防 commit→push→deploy 死循环)。

    返回 (changed: bool, latest_dt: str|None)
    """
    # probe: 池内第一只(510300)看最新报告期, 同时缓存其历史
    probe_rows = fetch_quarter_history("510300")
    if not probe_rows:
        print("[skip] quarter probe 510300 失败, 本轮不刷 (留待下轮)")
        return False, None
    latest_dt = probe_rows[-1][0]
    cur_through = (data.get("meta") or {}).get("quarter_through")
    if data.get("quarter") and cur_through == latest_dt:
        print("[skip] quarter 已最新 (%s, %d 只) 幂等 G0" % (latest_dt, len(data["quarter"])))
        return False, latest_dt

    # 全池拉季度历史
    quarter = {}
    ok = 0
    for sid, name in POOL:
        code = sid.split(".")[1]
        rows = probe_rows if code == "510300" else fetch_quarter_history(code)
        if rows:
            quarter[code] = rows
            ok += 1
            print("  [Q] %s %-10s %4d 期  %s ~ %s" %
                  (code, name, len(rows), rows[0][0], rows[-1][0]))
        else:
            print("  [Q-FAIL] %s %s" % (code, name))
        time.sleep(0.25)
    if not quarter:
        print("[skip] quarter 全池拉取失败, 不写盘")
        return False, latest_dt
    data["quarter"] = quarter
    m = data.setdefault("meta", {})
    m["quarter_through"] = latest_dt
    if not m.get("pool"):
        m["pool"] = [{"code": s.split(".")[1], "name": n, "secid": s}
                     for s, n in POOL]
    if not m.get("latest"):
        m["latest"] = latest_dt  # 只有季度时也至少给报告期
    print("quarter 已更新: %d 只, 最新报告期 %s" % (ok, latest_dt))
    return True, latest_dt


def _save(data):
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def main():
    today = bj_today()

    # ---- 读旧产物 ----
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"meta": {}, "series": {}}
    series = data.setdefault("series", {})

    # ---- G0 季度长史刷新(独立于日度; 报告期更新才写盘) ----
    q_changed, q_latest = refresh_quarter(data)
    if q_changed:
        data.setdefault("meta", {})["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _save(data)

    # ---- G1 当日已记? ----
    any_today = False
    for code, _ in POOL:
        arr = series.get(code) or []
        if arr and arr[-1][0] == today:
            any_today = True
            break
    if any_today:
        print("[skip] 当日 %s 已有快照 (幂等 G1)" % today)
        return 0

    # ---- 拉取全池 ----
    got = {}
    failed = []
    for sid, name in POOL:
        x = fetch_one(sid)
        code = sid.split(".")[1]
        if x:
            got[code] = x
        else:
            failed.append(code)
        time.sleep(0.35)
    print("拉取完成: %d/%d, 失败 %s" % (len(got), len(POOL), failed or "无"))

    # ---- G4 全败不写盘 ----
    if not got:
        print("[skip] 全池拉取失败, 不写盘 (留给下个时点补, G4)")
        return 0

    # ---- G2 无变化守卫(防 commit→push→deploy 死循环) ----
    no_change_all = True
    for code, x in got.items():
        arr = series.get(code) or []
        if arr:  # 与最新记录比较
            if arr[-1][1] != x["shr"]:
                no_change_all = False
                break
        else:
            no_change_all = False  # 新 code 首次记录
    if no_change_all:
        print("[skip] 全池份额与最新记录一致(非交易日/无申赎), 不 append (G2)")
        return 0

    # ---- append 当日快照 ----
    changed = []
    for sid, name in POOL:
        code = sid.split(".")[1]
        if code not in got:
            continue
        x = got[code]
        arr = series.setdefault(code, [])
        prev = arr[-1][1] if arr else None
        arr.append([today, x["shr"], x["nav"], x["mkt"]])
        if prev is not None and prev:
            chg = (x["shr"] - prev) / prev
            if abs(chg) > CHG_WARN:
                print("[WARN G3] %s %s 份额环比 %+.1f%% (>%d%%, 疑拆份额/异常)"
                      % (code, name, chg * 100, int(CHG_WARN * 100)))
        changed.append((code, name, prev, x["shr"]))

    data["meta"] = {
        "pool": [{"code": s.split(".")[1], "name": n, "secid": s}
                 for s, n in POOL],
        "latest": today,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quarter_through": q_latest or (data.get("meta") or {}).get("quarter_through"),
    }
    _save(data)
    size_kb = os.path.getsize(OUT) // 1024
    print("etf_share.json 已更新 (%d KB, 快照期 %s)" % (size_kb, today))
    for code, name, prev, cur in changed:
        dshr = (cur - prev) / 1e8 if prev else float("nan")
        print("  %s %-10s 份额 %10.2f 亿份 (Δ %s 亿份)" %
              (code, name, cur / 1e8, ("%+.2f" % dshr) if prev else "首次"))
    return 0


if __name__ == "__main__":
    sys.exit(main())