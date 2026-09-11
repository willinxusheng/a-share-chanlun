#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宽基 ETF 份额日度快照 v2 — 官方口径 + 数据日期锚定 (national/calc_etf.py)
=====================================================================
背景: 国家队个股持仓仅季度披露(滞后 1-2 月), 对实操滞后。汇金 2024 起主战场
切至宽基 ETF, 而 ETF 总份额**每交易日披露** → 日度代理通道:
   日度 Δ份额 × 收盘价 = 当日净申赎资金 → "疑似国家队/大资金" 方向参考

R420 重构 (根治"日期戳错位"bug):
  [旧] 16:00 首扫拉东财 f84, 此时交易所尚未发布 T 日份额 → 拿到的是 T-1 值,
       却被标成 T 日 → 整个 series 错位一天; 且 G1 幂等键用"拉取日", 把
       20:00 到货的真 T 日数据挡在门外。
       实证: 旧产物标 2026-09-09 的 510300 = 23,468,887,700 份;
             上交所官方 STAT_DATE=2026-09-08 同为 23,468,887,700 份(逐位一致),
             而 STAT_DATE=2026-09-09 实为 23,185,387,700 份。
  [新] ① 主源换 **上交所官方** (COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L),
         返回自带 STAT_DATE 明确数据日期, 单次请求返全量 ~900 只,
         且可带 STAT_DATE 回溯 (实测 2016-01-04 起);
       ② 幂等键从"拉取日"改 **数据日期** — 数据日期未前进则 skip,
         数据到位才落盘, 不再用拉取时刻冒充数据日期;
       ③ `--backfill N` 用官方源回溯补齐近 N 个自然日的交易日序列;
       ④ 沪市 7 只走官方长史; 深市 3 只官方无覆盖 → 东财实时值逐日累积。

数据源:
  沪市份额 = 上交所 query.sse.com.cn, 字段 TOT_VOL (**单位万份**);
  深市份额 = 东财 push2 f84 (份), 覆盖 159919/159915/159949;
  收盘价   = 新浪日K (给每行补 nav, 保证回溯与日常口径一致);
  季度长史 = 天天基金 FundArchivesDatas type=gmbd (保持原逻辑)。

幂等/防循环守卫 (CI 每次 deploy 都会跑本脚本):
  G0 季度: 仅当产物缺 quarter 或 meta.quarter_through 落后于最新报告期才全池重拉;
  G1 数据日期: 全部 code 的最后记录日期 >= 官方最新数据日期 → skip (不写盘);
  G2 重复: 同一数据日期已记录 → skip (按日期去重; **不再比较份额值** — 实测
     4.5% 的相邻交易日相对变化本就 <1e-4, 按值判"无变化"会误丢合法数据,
     致数据日期停滞/series 缺口/每轮重复拉取, 详见 daily() 内注释);
  G3 异常: 单只份额环比变动 >30% → WARN 照记(拆份额/折算可识别);
  G4 全败: 官方源不可用或无可写数据 → exit 0 不阻断 deploy, 不写盘;
  G5 时段: 北京时间 15:00 前一律 no-op (官方数据盘后才发布, 该时段无新数据);
  G6 价格: 缺当日收盘价(新浪日K 未更新/限流) → 本轮跳过该 code 待下轮,
     **绝不写 0** (写 0 会被数据日期幂等永久锁死, 静默低估资金流)。

产物: national/etf_share.json (tracked, CI 提交, ~KB 级):
   { "meta": {...},
     "series":  { "510300": [["2026-09-09", shr_份, nav_元, mkt_元], ...] },  日度快照
     "quarter": { "510300": [["2026-06-30", 期间申_亿份, 期间赎_亿份, 期末份额_亿份, 期末规模_亿元], ...] } }

用法:
  python3 calc_etf.py                  # 常规: 探测官方最新数据日期并落盘(幂等)
  python3 calc_etf.py --backfill 420   # 回溯: 重建近 420 自然日的日度序列
"""
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "etf_share.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

# 东财行情多域名轮换(抗本机/CI 偶发断连)
HOSTS = ["push2.eastmoney.com", "1.push2.eastmoney.com", "7.push2.eastmoney.com",
         "13.push2.eastmoney.com", "33.push2.eastmoney.com"]

# 国家队主力宽基 ETF 池: (secid, code, market, name) —— market: sh 沪 / sz 深
# 聚焦: 沪深300双地/上证50/180/中证500/1000 + 创业板/科创50(成长) + 红利(险资&国家队)
# 注: 588080 与 588000 同为科创50(指数重复)剔除; 黄金/商品 ETF 非国家队宽基方向剔除
POOL = [
    ("1.510300", "510300", "sh", "沪深300(沪)"),
    ("0.159919", "159919", "sz", "沪深300(深)"),
    ("1.510050", "510050", "sh", "上证50"),
    ("1.510180", "510180", "sh", "上证180"),
    ("1.510500", "510500", "sh", "中证500"),
    ("1.512100", "512100", "sh", "中证1000"),
    ("0.159915", "159915", "sz", "创业板"),
    ("0.159949", "159949", "sz", "创业板50"),
    ("1.588000", "588000", "sh", "科创50"),
    ("1.510880", "510880", "sh", "红利"),
]

CHG_WARN = 0.30   # 单只份额环比变动告警阈值 (G3)

# 上交所官方 ETF 份额接口: 不指定 STAT_DATE 返回最新, 指定则回溯该交易日
SSE_URL = ("http://query.sse.com.cn/commonQuery.do?"
           "sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
           "&pageHelp.pageSize=2000&pageHelp.pageNo=1"
           "&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1")

SINA_K = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
          "CN_MarketData.getKLineData?symbol=%s%s&scale=240&ma=no&datalen=%d")


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bj_now():
    """北京时间 datetime (UTC+8)。"""
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _load():
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "series": {}}


def _save(data):
    """原子写: 先落 .tmp 再 os.replace, 避免 CI 中途被杀留下半个 JSON 产物。"""
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, OUT)


def _pool_meta():
    return [{"code": c, "name": n, "secid": s, "market": mk} for s, c, mk, n in POOL]


# ---------------------------------------------------------------- 源 1: 上交所官方
def fetch_sse(stat_date=None, tries=3):
    """上交所官方 ETF 份额 → (data_date, {code: 份})；无数据返回 (None, None)。

    TOT_VOL 单位为**万份**, 此处统一换算为份。
    stat_date=None 时接口返回**最新**有数据的交易日。
    """
    for k in range(tries):
        try:
            url = SSE_URL + ("&STAT_DATE=" + stat_date if stat_date else "")
            h = {"User-Agent": UA, "Referer": "http://www.sse.com.cn/"}
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=20, context=ssl.create_default_context())
            rows = (json.loads(r.read().decode("utf-8", "ignore")).get("result") or [])
            if not rows:
                return None, None
            dd = rows[0].get("STAT_DATE")
            out = {}
            for x in rows:
                c, v = x.get("SEC_CODE"), x.get("TOT_VOL")
                if c and v not in (None, "", "-", "--"):
                    try:
                        out[c] = int(round(float(str(v).replace(",", "")) * 1e4))
                    except (TypeError, ValueError):
                        pass
            return (dd, out) if out else (None, None)
        except Exception:
            time.sleep(0.8 + k * 0.4)
    return None, None


# ---------------------------------------------------------------- 源 2: 东财 push2
def fetch_em(sid, tries=6):
    """拉单只 ETF {shr份, nav元, mkt元}; 失败返回 None。"""
    for k in range(tries):
        hst = random.choice(HOSTS)
        url = ("https://%s/api/qt/stock/get?secid=%s"
               "&fields=f57,f58,f43,f84,f116&fltt=1" % (hst, sid))
        try:
            h = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
                 "Accept": "*/*", "Connection": "close"}
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=12, context=ssl.create_default_context())
            d = json.loads(r.read().decode("utf-8", "ignore")).get("data") or {}
            if d.get("f84"):
                shr = int(d["f84"])
                nav = float(d.get("f43") or 0)
                mkt = float(d.get("f116") or 0)
                # nav 单位兜底: fltt 在部分子域失效时 f43 返回厘(如 4637=4.637 元),
                # 特征 nav>100 必为厘级 → 用总市值/总份额精确重算 (mkt/shr 恒为元)
                if nav > 100 and shr and mkt:
                    nav = round(mkt / shr, 6)
                return {"shr": shr, "nav": nav, "mkt": mkt}
        except Exception:
            time.sleep(0.8 + k * 0.5)
            continue
        # (R436) 拿到响应却没有 f84 时, 原写法**不退避**直接进下一轮 → 6 次瞬发连打,
        # 反而更容易被 WAF 限流。实测 2026-09-11 16:00 那轮 159915 / 159949 双双
        # [miss] 东财拉取失败(同一轮 159919 成功), 本机重试 15 次×3 只亦全败。
        time.sleep(0.6 + k * 0.5)
    return None


# ---------------------------------------------------------------- 源 3: 新浪日K
def fetch_sina_close(code, market, n=60, tries=3):
    """新浪日K收盘价 → {date: close}；失败返回 {}。"""
    for k in range(tries):
        try:
            url = SINA_K % (market, code, n)
            h = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=20, context=ssl.create_default_context())
            arr = json.loads(r.read().decode("utf-8", "ignore"))
            return {x["day"]: float(x["close"]) for x in arr if x.get("day")}
        except Exception:
            time.sleep(0.8 + k * 0.4)
    return {}


# ---------------------------------------------------------------- 季度长史 (G0)
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
    首条=最近报告期。失败返回 None。
    """
    for _ in range(tries):
        try:
            h = {"User-Agent": UA, "Referer": GMBD_REF}
            r = urllib.request.urlopen(urllib.request.Request(GMBD_URL % code, headers=h),
                                       timeout=15, context=ssl.create_default_context())
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


def refresh_quarter(data):
    """季度真身长史刷新 (G0 幂等)。

    规则: 当 data 无 quarter 字段, 或 data.meta.quarter_through != 最新报告期
    (以池内第一只 probe 为准), 才全池重拉 gmbd 历史。否则返回 False 不写盘。

    返回 (changed: bool, latest_dt: str|None)
    """
    probe_rows = fetch_quarter_history("510300")
    if not probe_rows:
        print("[skip] quarter probe 510300 失败, 本轮不刷 (留待下轮)")
        return False, None
    latest_dt = probe_rows[-1][0]
    cur_through = (data.get("meta") or {}).get("quarter_through")
    if data.get("quarter") and cur_through == latest_dt:
        print("[skip] quarter 已最新 (%s, %d 只) 幂等 G0" % (latest_dt, len(data["quarter"])))
        return False, latest_dt

    quarter = {}
    ok = 0
    for sid, code, mk, name in POOL:
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
        m["pool"] = _pool_meta()
    if not m.get("latest"):
        m["latest"] = latest_dt
    print("quarter 已更新: %d 只, 最新报告期 %s" % (ok, latest_dt))
    return True, latest_dt


# ---------------------------------------------------------------- 常规日更
def daily(force=False):
    """常规日更: 探测官方最新数据日期, 幂等落盘。

    force=True 绕过 G5 深市时段守卫 (仅供本地补数/验证, CI 不传)。
    """
    hhmm = bj_now().strftime("%H%M")
    # ---- G5 时段守卫 (R434 重构: 由「整轮 no-op」改为「仅拦深市」) ----
    # 旧语义: 北京 15:00 前**整轮 return 0**。理由当时写的是「官方数据盘后才发布」,
    #   但该理由经实测不成立 —— 上交所逐日份额实为 **T+1 上午~中午**发布
    #   (09-10 份额 09-11 12:09 已可得; 09-10 21:30 尚不可得)。
    #   副作用: 沪市份额自带权威数据日期 (SSE STAT_DATE), 拿到即可安全落盘, 却被
    #   这道墙连着一起拦住 ⇒ 调度窗口从 16:00 起 + 数据 T+1 才发布 = 看板最长滞后
    #   T+2 个交易日; 用户 3 次误判「看板又没更新」(R430 已加新鲜度条仍未根治)。
    # 新语义: **沪市任意时刻可写**(日期锚定官方 STAT_DATE, 无陈旧/无错戳风险),
    #   深市仍限 >=15:00 —— 深市取东财 push2 f84「实时快照」(不带日期), 盘中该值
    #   随申赎实时变动, 早写会把盘中值打上前一交易日的戳 (R425 记录的
    #   「日期戳跨市场耦合」风险)。深市这 3 只不计入合计/图表/回测, 提前写无收益。
    allow_sz = force or hhmm >= "1500"
    if not allow_sz:
        print("[info] 北京 %s < 15:00: 本轮仅写沪市(官方 STAT_DATE 锚定), 深市跳过 (G5)"
              % hhmm)

    data = _load()
    series = data.setdefault("series", {})

    # ---- G0 季度长史刷新 ----
    q_changed, q_latest = refresh_quarter(data)
    if q_changed:
        data.setdefault("meta", {})["updated_at"] = _utcnow()
        _save(data)

    # ---- 探测官方最新数据日期 (数据日期锚定, 取代原"拉取日"口径) ----
    dd, sse = fetch_sse()
    if not dd:
        print("[skip] 上交所官方源无数据, 本轮不写盘 (留给下轮, G4)")
        return 0
    print("官方最新数据日期: %s (%d 只沪市)" % (dd, len(sse)))

    need = []
    for sid, code, mk, name in POOL:
        if mk != "sh" and not allow_sz:
            continue          # G5: 15:00 前不写深市(东财实时快照, 防盘中值错戳)
        arr = series.get(code) or []
        if not arr or arr[-1][0] < dd:
            need.append((sid, code, mk, name))
    if not need:
        print("[skip] 数据日期 %s 已记录, 无新数据 (数据日期幂等 G1)" % dd)
        return 0
    print("需更新 %d 只: %s" % (len(need), ", ".join(c for _, c, _, _ in need)))

    # ---- 收盘价 (仅拉需要写的 code, 减少新浪请求压力) ----
    px = {}
    for sid, code, mk, name in need:
        px[code] = fetch_sina_close(code, mk, n=60)
        time.sleep(0.15)

    written = 0
    sz_missed = {}      # (R436) 深市本轮未写成功者 {code: 数据日期} —— 落痕用
    for sid, code, mk, name in need:
        arr = series.setdefault(code, [])
        x = None
        if mk == "sh":
            v = sse.get(code)
            if not v:
                print("  [miss] %s 官方源无此代码" % code)
                continue
        else:
            x = fetch_em(sid)
            if not x:
                print("  [miss] %s 东财拉取失败" % code)
                sz_missed[code] = dd
                continue
            v = x["shr"]

        # ---- G2 重复守卫: 只按「数据日期」去重, 不再比较份额值 ----
        # (R421 修复) 旧写法用 |Δ份额|/份额 < 1e-4 判「无变化」并跳过 —— 在官方源
        # 口径下**有害**: 实测 599 个交易日中 4.5% 的相邻日相对变化本就 <1e-4
        # (510180 上证180 高达 22.7%), 会被误判为「无变化」而丢弃该日记录, 后果:
        #   ① 数据日期停滞 → 前端长期显示旧日期; ② 部分 code 被拦 → series 出现
        #   缺口; ③ 每轮 CI 重复拉取同一只。陈旧值(T-1 冒充 T 日)问题已由 G1 的
        # 数据日期幂等根治, 故此处职责只剩「同一数据日期不重复写」。
        if arr and arr[-1][0] >= dd:
            print("  [skip] %s 数据日期 %s 已记录 (G2)" % (code, arr[-1][0]))
            continue

        # ---- 价格守卫: 缺收盘价则本轮跳过该 code, 绝不写 0 ----
        # (R421 修复) 旧写法用 `or 0.0` 兜底 → 新浪日K 未更新/限流时会把
        # nav=0, mkt=0 写进 series; 该日净申赎金额恒为 0 且被数据日期幂等
        # **永久锁死**(G1 认为已记录, 再也不会用正确价格重算) → 静默低估资金流。
        # (R436) 只认**带日期的**收盘价。旧写法缺当日收盘时回退 x["nav"] —— 那是东财
        # **实时快照**(不带日期), 拿它去填一条带历史日期的记录, 日期戳与值不匹配,
        # 与 R425 记录的「跨市场日期戳耦合」是同一类错。两个市场统一: 缺则本轮跳过。
        p = px.get(code, {}).get(dd) or 0.0
        if not p or p <= 0:
            print("  [skip-price] %s %s 缺 %s 收盘价(新浪日K 未更新?), 本轮跳过待下轮"
                  % (code, name, dd))
            if mk != "sh":
                sz_missed[code] = dd
            continue
        arr.append([dd, v, round(p, 6), round(v * p, 2)])
        written += 1
        prev = arr[-2][1] if len(arr) > 1 else None
        dshr = (v - prev) / 1e8 if prev else None
        print("  %s %-10s %10.2f 亿份 %s" % (
            code, name, v / 1e8,
            ("Δ %+.2f 亿份" % dshr) if dshr is not None else "(首条)"))
        if prev and prev > 0 and abs((v - prev) / prev) > CHG_WARN:
            print("  [WARN G3] %s 环比 %+.1f%% (>%d%%, 疑拆份额/折算)"
                  % (code, (v - prev) / prev * 100, int(CHG_WARN * 100)))

    # (R436) 深市未写成功必须**显式留痕**: 否则 meta.data_date 已推进而 series 里
    # 深市仍停在旧日期, 表现为「同一个产物内部日期不一致」却无任何告警。实测
    # 2026-09-11 16:00 那轮即如此(沪市 7 只到 09-10, 深市 2 只停在 09-09)。
    if sz_missed:
        print("  [WARN R436] 深市 %d 只本轮未写成功: %s (目标数据日期 %s) —— "
              "东财快照不带数据日期, 不做跨日补写; 下轮继续重试"
              % (len(sz_missed), ", ".join(sorted(sz_missed)), dd))

    if not written:
        print("[skip] 无可写数据, 不写盘 (G4)")
        return 0

    m = data.setdefault("meta", {})
    m["pool"] = _pool_meta()
    m["latest"] = dd
    m["data_date"] = dd
    m["updated_at"] = _utcnow()
    m["version"] = "e3"
    m["g5"] = "sh-anytime,sz>=1500"
    m["sz_miss"] = sz_missed
    m["source"] = ("沪市=上交所官方 ETF 份额(万份) + 新浪收盘价; "
                   "深市=东财 push2 逐日累积")
    if q_latest:
        m["quarter_through"] = q_latest
    _save(data)
    print("etf_share.json 已更新: 数据日期 %s, 写入 %d 只 (%d KB)"
          % (dd, written, os.path.getsize(OUT) // 1024))
    return 0


# ---------------------------------------------------------------- 回溯重建
def backfill(days=420):
    """用上交所官方源回溯重建近 days 自然日的日度序列 (沪市), 深市取当前值。"""
    data = _load()
    end = bj_now().date()
    start = end - timedelta(days=days)
    dts = []
    d = start
    while d <= end:
        if d.weekday() < 5:          # 粗筛工作日 (法定节假日由官方空返回自动剔除)
            dts.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    print("回溯窗口 %s ~ %s · 候选交易日 %d 个" % (dts[0], dts[-1], len(dts)))

    def work(dt):
        got_dd, m = fetch_sse(dt)
        return dt, (m if got_dd == dt else None)   # 只认与请求日期一致的返回

    got = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, (dt, m) in enumerate(ex.map(work, dts)):
            if m:
                got[dt] = m
            if (i + 1) % 60 == 0:
                print("  ... 已探 %d/%d" % (i + 1, len(dts)))
    print("官方命中 %d 个交易日" % len(got))
    if not got:
        print("[abort] 官方回溯全空, 不写盘")
        return 1

    px = {}
    for sid, code, mk, name in POOL:
        px[code] = fetch_sina_close(code, mk, n=days + 60)
        print("  价 %s %-10s %d 日" % (code, name, len(px[code])))
        time.sleep(0.2)

    dd_latest = max(got)
    series = {}
    for sid, code, mk, name in POOL:
        rows = []
        if mk == "sh":
            for dt in sorted(got):
                v = got[dt].get(code)
                p = px.get(code, {}).get(dt)
                if v and p:
                    rows.append([dt, v, round(p, 6), round(v * p, 2)])
        else:
            # 深市: 官方无覆盖 → 东财实时值 append/更新 (数据日期=官方最新)。
            # (R421 修复) 旧写法每次回溯都把 rows 重置为**单点**, 深市一旦已逐日
            # 累积出多期, 再跑 --backfill 就会把历史压成 1 条 (静默数据丢失)。
            # 改为 **合并已有序列**: 同日则更新、更晚则追加、拉取失败则原样保留。
            x = fetch_em(sid)
            p = px.get(code, {}).get(dd_latest)
            rows = list((data.get("series") or {}).get(code) or [])
            if x and p and p > 0:
                pt = [dd_latest, x["shr"], round(p, 6), round(x["shr"] * p, 2)]
                if rows and rows[-1][0] == dd_latest:
                    rows[-1] = pt
                    print("    (深市 %s 同日 %s 更新)" % (code, dd_latest))
                elif rows and rows[-1][0] > dd_latest:
                    print("    (深市 %s 已有更新记录 %s, 不覆盖)" % (code, rows[-1][0]))
                else:
                    rows.append(pt)
            elif rows:
                print("    (深市 %s 拉取失败, 保留已有 %d 期)" % (code, len(rows)))
            else:
                print("    (深市 %s 无数据: 东财拉取失败且无历史)" % code)
            time.sleep(0.3)
        if rows:
            series[code] = rows
            print("  %s %-10s %4d 期  %s ~ %s" %
                  (code, name, len(rows), rows[0][0], rows[-1][0]))

    if not series:
        print("[abort] 组装后无数据, 不写盘")
        return 1

    data["series"] = series
    m = data.setdefault("meta", {})
    m["pool"] = _pool_meta()
    m["latest"] = dd_latest
    m["data_date"] = dd_latest
    m["updated_at"] = _utcnow()
    m["source"] = ("沪市=上交所官方 ETF 份额(万份) + 新浪收盘价; "
                   "深市=东财 push2 逐日累积")
    m["backfilled"] = "%s ~ %s" % (min(got), dd_latest)
    _save(data)
    print("回溯完成: %d 只 · 区间 %s ~ %s · %d KB"
          % (len(series), min(got), dd_latest, os.path.getsize(OUT) // 1024))
    return 0


def main():
    args = sys.argv[1:]
    if args and args[0] == "--backfill":
        n = int(args[1]) if len(args) > 1 else 420
        return backfill(n)
    return daily(force="--force" in args)


if __name__ == "__main__":
    sys.exit(main())
