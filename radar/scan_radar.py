# -*- coding: utf-8 -*-
"""P1 全市场缠论雷达 · 每日扫描 (radar/scan_radar.py)
========================================================
全市场(沪深A + 北交所 + 场内基金/ETF)缠论结构每日扫描,
产出 radar/radar.json 供「全市场雷达页 + 首页信号区」消费。

数据链(多源降级, 任一源失败不影响整体):
  标的池  东财 clist (push2delay/push2 镜像轮询, 免key): 名称含 ST/退 直接排除
  K线主源 腾讯 fqkline 纯count qfq (R248 形态, 前复权, ~640根/个股, CI境外最稳)
  K线次源 东财 push2his fqt=1 前复权 (R270: 腾讯不可用时替代裸价; 北交secid未实证跳过)
  K线备源 新浪 CN_MarketDataService 日K (不复权, ~1500根, 本地/境内外双可达)
分析     chanlun.analyze (chanlun.py 生产级, P0 已 200 票抽样验证可信)
门禁     ST/次新(<120根)/低流动性(近60日均额<3000万)/一字板(>=10日)/低自洽/停牌
信号     近端场景 classify ∈ {背驰见底机会, 背驰见顶风险} (P0 定论: 近端口径, 非全历史尾笔)
         强度: 趋势背驰/段级同步 + 量能确认; 排序: 强信号优先 + 新鲜度优先

用法:
  python3 radar/scan_radar.py               # 全量(约7200票, CI 每日跑)
  python3 radar/scan_radar.py --limit 200   # 前200票(本地冒烟)
  python3 radar/scan_radar.py --only sh600000,sz300274,sh510050,bj920000  # 指定票
产物: radar/radar.json (tracked, 每日由 CI radar-scan.yml 提交发布)
"""
import os
import sys
import json
import time
import datetime
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))       # radar/ 内模块
import fetch_data as fd   # noqa: E402   # 复用腾讯 qfq 抓取(纯count R248) 与 UA
import chanlun as cl      # noqa: E402   # 生产级缠论库
import industry_map as im # noqa: E402   # f100细分 -> 申万一级 映射(零遗漏实测)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ---------- 参数 ----------
MIN_BARS = 120                 # 少于120根日线(约半年) -> 次新/数据不足, 不进信号
LOW_AMT60 = 3000.0             # 近60日均成交额(万元) 低于 -> 低流动性
ONE_WORD_MAX = 10              # 一字板天数 >= -> 结构失真, 不进信号
AGREE_TOTAL_MIN = 8
AGREE_RATE_MIN = 0.6
FRESH_MAX_DAYS = 10            # 最近背驰距今天数 <= -> 才算"近端信号"
# R270: 新浪 datalen 实测支持 1500(2020-07 起), 扩至与腾讯qfq窗口(2021至今)同量级,
# 避免新浪兜底时缠论结构起点(原800根≈3.2年自2023-05)与腾讯不一致导致的笔/中枢划分差异。
SINA_LEN = 1500                # 新浪兜底K线根数(~6年, 2021至今全覆盖)
EM_KLINE_LEN = 1600            # 东财前复权K线根数(2021起含裕量)
SPARK_N = 150                  # 信号票内嵌迷你K线根数(前端实操卡用)
IND_KLINE_N = 320              # 行业K线入库根数(画行业走势; 合成全量更长仅用于行业缠论)
CONCURRENCY = 4
TX_INTERVAL = 0.35             # 腾讯全局限速 ~2.9 rps (P0实证突发连发会501)
EM_INTERVAL = 0.25             # 东财全局限速 ~4 rps (K线下行接口, 温和节流)
SINA_INTERVAL = 0.18           # 新浪限速 ~5.5 rps (新浪无501挑战, 温和节流, 实测稳定)
SRC_ONLY = "auto"              # auto=腾讯qfq→东财qfq→新浪 | tx=仅腾讯 | em=仅东财 | sina=仅新浪
# R270: 源停用/复探参数 —— 腾讯(CI境外被风控整段失败)与东财(境外可能不可达)各自独立:
# 连续失败 TX_FAIL_MAX 次 → 整段停用该源(避免逐票空耗 timeout); 停用中每 REPROBE_EVERY 票
# 轻量复探一次, 源恢复即自动切回(一次抖动不再废掉整 run 主源)。
TX_FAIL_MAX = 6
EM_FAIL_MAX = 6
TX_REPROBE_EVERY = 300
EM_REPROBE_EVERY = 300
EM_TIMEOUT = 10                # 东财单请求超时(不可达时快速失败, 不拖全量)
EM_HOSTS = ["https://push2delay.eastmoney.com", "https://push2.eastmoney.com",
            "http://82.push2.eastmoney.com", "http://push2delay.eastmoney.com"]
EM_FS_STOCK = "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80"      # 沪深A股(含主板/中小/创业/科创)
EM_FS_BJ = "m:0+t:81+s:2048"                            # 北交所
EM_FS_FUND = "b:MK0021"                                 # 场内基金(ETF/LOF)
# R270: 东财前复权K线下行镜像 —— https 优先(境外 CI 直连可达, R177b 情绪管线实证),
# http 兜底(境内自托管/沙箱, 东财 http 仅境内 CDN 节点可达)。
EM_KLINE_HOSTS = ["https://push2his.eastmoney.com",
                  "http://push2his.eastmoney.com",
                  "http://92.push2his.eastmoney.com"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radar.json")
ETF_KEY = "ETF板块"             # 场内基金/ETF 归为独立板块(P3b), 与申万一级并列展示

# ---------- 全局限速器 ----------
class _Throttle:
    def __init__(self, interval):
        self.interval = interval
        self._next = [0.0]
        self._lk = threading.Lock()
    def wait(self):
        with self._lk:
            now = time.time()
            t = max(now, self._next[0])
            self._next[0] = t + self.interval
            delay = t - now
        if delay > 0:
            time.sleep(delay)

_tx_th = _Throttle(TX_INTERVAL)
_em_th = _Throttle(EM_INTERVAL)
_sina_th = _Throttle(SINA_INTERVAL)
# R270: 源停用状态机(腾讯/东财独立)。字段: flag=整段停用 | count=连续失败数 |
# reasons={失败原因:次数} 供 meta.src_fail 统计 | n=停用后调用计数(复探节拍) |
# max=停用阈值 | every=复探周期 | probe=轻量复探函数(第2节定义后回填)。
_tx_down = {"flag": False, "count": 0, "reasons": {}, "n": 0,
            "max": TX_FAIL_MAX, "every": TX_REPROBE_EVERY, "probe": None}
_em_down = {"flag": False, "count": 0, "reasons": {}, "n": 0,
            "max": EM_FAIL_MAX, "every": EM_REPROBE_EVERY, "probe": None}

def _src_down(d, lock):
    """读停用状态并累计调用; 停用中每 every 次调用执行一次轻量复探, 成功则复位切回。
    (复探为网络请求, 在锁外执行避免阻塞其他取数线程; 状态写回再取锁。)"""
    with lock:
        d["n"] = d.get("n", 0) + 1
        if not d["flag"]:
            return False
        do_probe = (d["n"] % d["every"] == 0 and d["probe"])
    if do_probe and d["probe"]():
        with lock:
            d["flag"] = False
            d["count"] = 0
            d["n"] = 0
        print("[scan_radar] 源复探成功, 已恢复使用", flush=True)
        return False
    return True

def _src_fail(d, lock, reason=""):
    """记录一次源失败; 达阈值整段停用(flag=True)。reason 汇入统计供 meta 展示。"""
    with lock:
        d["count"] += 1
        if reason:
            d["reasons"][reason] = d["reasons"].get(reason, 0) + 1
        if d["count"] >= d["max"]:
            d["flag"] = True
            print("[scan_radar] 源连续失败 >=%d 次, 整段停用 (末因: %s)" % (d["max"], reason or "-"), flush=True)

_tx_lock = threading.Lock()
_em_lock = threading.Lock()


def _get(url, timeout=20, referer=""):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Connection": "close"})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw


# ================= 1. 全市场标的池(东财 clist, 名称门禁) =================
def _em_clist(fs, host):
    """拉取一个板块的全部标的: 返回 [(code, mkt, name, sub, mcap)]。
    sub=f100细分行业名(ETF为'-'), mcap=f20总市值(元)。mkt: 1=沪 0=深/北交。
    实测 push2delay 将 pz 钳制到 100/页, 故用接口 total 字段控制翻页终止。"""
    out, page, total, empty_run = [], 1, None, 0
    while True:
        u = ("%s/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
             "&fs=%s&fields=f12,f13,f14,f100,f20" % (host, page, fs))
        try:
            d = json.loads(_get(u, timeout=20, referer="https://quote.eastmoney.com/")
                           .decode("utf-8", "ignore")).get("data") or {}
        except Exception:
            return None
        if total is None:
            total = d.get("total") or 0
        diff = d.get("diff") or []
        if not diff:
            empty_run += 1
            if empty_run >= 2 or len(out) >= total:
                break
        else:
            empty_run = 0
            for x in diff:
                code, mkt, name = str(x.get("f12", "")), int(x.get("f13", -1)), str(x.get("f14", ""))
                if len(code) == 6 and mkt in (0, 1):
                    sub = str(x.get("f100") or "-").strip()
                    try:
                        mcap = float(x.get("f20") or 0)
                    except (TypeError, ValueError):
                        mcap = 0.0
                    out.append((code, mkt, name, sub, mcap))
        if total and len(out) >= total:
            break
        if page > 250:                                # 防死循环
            break
        page += 1
        time.sleep(0.12)
    return out


def _sym_of(code, mkt, name=""):
    """东财 (code, mkt) -> 腾讯/新浪前缀 symbol。代码段优先于 mkt 字段(北交 f13 亦为0)。"""
    if code.startswith(("60", "68", "69", "51", "56", "58", "50", "90", "11")):
        return "sh" + code
    if code.startswith(("00", "30", "15", "16", "18", "20", "12")):
        return "sz" + code
    if code.startswith(("92", "83", "87", "88", "89", "43", "82")):
        return "bj" + code
    # 兜底按 mkt
    return ("sh" if mkt == 1 else "sz") + code


def _typename(code, name):
    """按代码段分交易所: 沪深A股 / 北交所(92新段+83/87/88/89/43/82/4老段) / 场内基金(兜底)"""
    if code.startswith(("60", "68", "69", "00", "30")):      # 沪深A股(沪主板/科创/深主板/创业)
        return "股"
    if code.startswith(("92", "83", "87", "88", "89", "43", "82", "4")):   # 北交所
        return "北交"
    return "ETF"                                             # 15/16/18/51/56/58/50/90/11/12/20 场内基金


def fetch_universe():
    """东财全市场标的池(多镜像轮询)。返回 {sym: {code,name,type,ind,mcap}}, 及排除计数。
    ind = 申万一级行业(industry_map 映射, ETF/未收录='-')"""
    uni, excl = {}, {"st": 0, "dup": 0}
    for host in EM_HOSTS:
        ok = True
        rows = []
        for fs, tag in ((EM_FS_STOCK, "股票"), (EM_FS_BJ, "北交"), (EM_FS_FUND, "场内基金")):
            r = _em_clist(fs, host)
            if r is None:
                ok = False
                break
            rows.extend(r)
        if not ok:
            continue
        for code, mkt, name, sub, mcap in rows:
            # 退市/ST 名称门禁: 完全剔除(不展示不分析)
            nm = name.upper()
            if "ST" in nm or "退" in name or name.startswith("*"):
                excl["st"] += 1
                continue
            sym = _sym_of(code, mkt, name)
            if sym in uni:
                excl["dup"] += 1
                continue
            typ = _typename(code, name)
            ind = im.sub_to_sw1(sub) if typ != "ETF" and sub != "-" else "-"
            uni[sym] = {"code": code, "name": name, "type": typ,
                        "ind": ind, "mcap": mcap}
        return uni, excl, host
    return uni, excl, ""


# ================= 2. K线抓取(腾讯qfq 主 / 东财qfq 次 / 新浪裸价 备) =================
def _fetch_tx(sym):
    """腾讯 qfq(前复权)主源: 纯 count 形态(R248), 2021-01-01 起裁剪。
    R270: 新鲜度判定由 `>=2024-01-01`(过松, R267 注释自我批评却未在 scan 链路落实)
    收紧为 _last_fresh 分级(gap<=3 常规 / 4~12 仅长假窗口内合法) —— 腾讯 CDN 陈旧缓存
    (R248: 缓存键含日期段曾停 12h+)不再被静默当有效数据吞下。"""
    ks, _dirty = fd.fetch_tx(sym, "day")
    if ks and _last_fresh(ks[-1]["date"]):
        return ks, "tx"
    return [], ("tx_stale" if ks else "tx_empty")


_EM_MKT_PFX = {"sh": "1.", "sz": "0."}   # 东财 secid 市场前缀(沪=1 深=0); 北交段归属未实证, 跳过东财


def _em_secid(sym):
    """sh600000 -> 1.600000; sz300274 -> 0.300274; bj* -> None(北交 K线 secid 归属未实证, 不盲试)。"""
    pre = _EM_MKT_PFX.get(sym[:2])
    return (pre + sym[2:]) if pre else None


def _fetch_em(sym):
    """东财前复权(fqt=1)日K第二复权源: 腾讯不可用时替代新浪裸价(同为前复权, 无除权假跳空)。
    境外 CI 经 https 直连可达(与 fetch_data.fetch_em 同接口家族, R177b 起情绪管线生产验证);
    http 镜像仅供境内网络兜底。返回 (ks, "em") 或 ([], 原因标签)。"""
    sec = _em_secid(sym)
    if not sec:
        return [], "em_skip"
    last_err = "em_empty"
    for host in EM_KLINE_HOSTS:
        u = ("%s/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3,f4,f5,f6"
             "&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&lmt=%d"
             % (host, sec, EM_KLINE_LEN))
        try:
            data = json.loads(_get(u, timeout=EM_TIMEOUT,
                                   referer="https://quote.eastmoney.com/")
                              .decode("utf-8", "ignore")).get("data") or {}
            out = []
            for row in (data.get("klines") or []):
                c = row.split(",")
                if len(c) < 6:
                    continue
                try:
                    # 东财 klines 字段序: date,open,close,high,low,volume(手),amount...
                    out.append({"date": c[0], "open": float(c[1]), "close": float(c[2]),
                                "high": float(c[3]), "low": float(c[4]),
                                "volume": float(c[5])})
                except (ValueError, IndexError):
                    continue
            out = [k for k in out if k["date"] >= fd.MIN_DATE]   # 2021起, 与腾讯契约一致
            out.sort(key=lambda k: k["date"])
            if len(out) >= MIN_BARS:
                if _last_fresh(out[-1]["date"]):                  # R271: 与tx对称 —— em陈旧缓存/CDN滞后同样拒用
                    return out, "em"
                last_err = "em_stale"
            else:
                last_err = "em_short:%d" % len(out)
        except Exception as e:   # noqa: BLE001
            last_err = "em_err:" + str(e)[:60]
    return [], last_err


def _probe_tx():
    """腾讯源轻量复探(整段停用后周期调用): 单票纯count请求 + 新鲜度判定, 成功即复位。"""
    _tx_th.wait()
    try:
        raw = _get(fd._tx_url("sh600000", "day"), timeout=8).decode("utf-8", "ignore")
        node = (json.loads(raw).get("data") or {}).get("sh600000") or {}
        kl = node.get("qfqday") or node.get("day") or []
        return bool(kl and _last_fresh(kl[-1][0]))
    except Exception:
        return False


def _probe_em():
    """东财源轻量复探(https 首选镜像)。"""
    try:
        u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000"
             "&fields1=f1&fields2=f51,f56&klt=101&fqt=1&lmt=3")
        d = json.loads(_get(u, timeout=EM_TIMEOUT, referer="https://quote.eastmoney.com/")
                       .decode("utf-8", "ignore"))
        return bool((d.get("data") or {}).get("klines"))
    except Exception:
        return False


# 回填 probe(需在 _probe_* 定义之后)
_tx_down["probe"] = _probe_tx
_em_down["probe"] = _probe_em


def _fetch_sina(sym):
    u = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
         "?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, SINA_LEN))
    raw = _get(u).decode("utf-8", "ignore")
    arr = json.loads(raw) or []
    out = []
    for row in arr:
        try:
            out.append({"date": row["day"],
                        "open": float(row["open"]), "high": float(row["high"]),
                        "low": float(row["low"]), "close": float(row["close"]),
                        "volume": float(row["volume"]) / 100.0})   # 新浪=股 -> 统一手
        except (KeyError, ValueError, TypeError):
            continue
    out = [k for k in out if k["date"] >= fd.MIN_DATE]   # R271: 对齐2021契约起点(腾讯/东财已裁), 保跨源结构起点一致
    return out, "sina"


def _fetch_sina_checked(sym):
    """新浪裸价兜底(限速 + 异常兜底)。"""
    _sina_th.wait()
    try:
        ks, src = _fetch_sina(sym)
        if ks:
            return ks, src
    except Exception as e:   # noqa: BLE001
        return None, "sina_err:%s" % str(e)[:60]
    return None, "empty"


def _try_tx(sym):
    """腾讯qfq一次尝试: 成功 (ks,"tx"); 失败(异常/空/陈旧) 计入停用统计后返回 ([],tag)。"""
    try:
        ks, tag = _fetch_tx(sym)
    except Exception:   # noqa: BLE001
        _src_fail(_tx_down, _tx_lock, "err")     # 归一化(原始消息多变, 不入 reasons)
        return [], ""
    if not ks:
        _src_fail(_tx_down, _tx_lock, tag.split(":")[0])
        return [], tag
    return ks, "tx"


def _try_em(sym):
    """东财qfq一次尝试: 成功 (ks,"em"); 失败计入停用统计后返回 ([],tag)。"""
    if _src_down(_em_down, _em_lock):
        return [], "em_down"
    _em_th.wait()
    ks, tag = _fetch_em(sym)
    if not ks:
        _src_fail(_em_down, _em_lock, tag.split(":")[0])   # em_err:/em_short: 归一化
        return [], tag
    return ks, "em"


def fetch_kline(sym):
    """三级源(按 SRC_ONLY):
      auto: 腾讯qfq(复权) → 东财qfq(复权) → 新浪裸价(最后兜底, 除权假跳空风险由黄条提示)
      tx / em / sina: 仅指定源(本地调试/降速场景)。
    R270: 每级失败计入源统计, 连续 >=MAX 次整段停用 + 周期复探自动恢复 —— 单次抖动不再
    废掉整 run 主源, 也避免境外不可达源逐票空耗 timeout。"""
    if SRC_ONLY == "sina":
        return _fetch_sina_checked(sym)
    if SRC_ONLY == "tx":
        if _src_down(_tx_down, _tx_lock):
            return None, "tx_down"
        ks, _tag = _try_tx(sym)
        return (ks, "tx") if ks else (None, "tx_only")
    if SRC_ONLY == "em":
        if _em_secid(sym) is None or _src_down(_em_down, _em_lock):
            return None, "em_unavail"
        ks, _tag = _try_em(sym)
        return (ks, "em") if ks else (None, "em_only")
    # ---- auto ----
    if not _src_down(_tx_down, _tx_lock):
        ks, _tag = _try_tx(sym)
        if ks:
            return ks, "tx"
    if _em_secid(sym) is not None and not _src_down(_em_down, _em_lock):
        ks, _tag = _try_em(sym)
        if ks:
            return ks, "em"
    return _fetch_sina_checked(sym)


# ================= 3. 结构摘要 + 门禁 + 近端信号 =================
# R267: 统一北京时区日期 —— CI runner 本地是 UTC, datetime.date.today() 在 UTC 跨日窗口
# (北京已过午夜而 UTC 未过)会差一天, 使停牌天数/背驰新鲜度/陈旧判定系统性偏移 ±1。
# 与 fetch_data._bj_now (R236/R167) 口径一致。
def _bj_today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()


def _src_degraded(sc):
    """R270: 数据源降级判定(复权源占比视角) —— 新浪裸价(不复权)占主导即降级:
    裸价在除权日留假跳空, 污染缠论结构(R267 曾见 09-04 快照 src_cnt 全 sina=6736 而看板无提示;
    R269 规则"腾讯占比<10%"未覆盖"腾讯/东财部分恢复但裸价仍占 1/3"的混源场景)。
    规则: 新浪票占比 > 30% → 降级 True(>30% 标的无复权保护, 提示精度下降)。"""
    ns = sum(sc.values()) or 1
    sina = sc.get("sina", 0)
    return bool(sina > 0 and sina * 100.0 / ns > 30.0)


def _degraded_reason(sc):
    """降级黄条的具体文案(前端优先展示; 旧数据无此字段时前端回落 R269 默认文案)。"""
    ns = sum(sc.values()) or 1
    sina = sc.get("sina", 0)
    tx = sc.get("tx", 0)
    em = sc.get("em", 0)
    if tx == 0 and em == 0:
        return ("前复权源(腾讯qfq/东财qfq)本次全部不可用, 全市场 %d 票转新浪裸价(不复权)"
                " — 除权日K线可能有假跳空, 结构标注精度下降" % sina)
    if sina:
        return ("%d 票(%.0f%%)无前复权源, 转新浪裸价(不复权) — 除权日K线可能有假跳空"
                % (sina, sina * 100.0 / ns))
    return ""


def _days_ago(date_s, _today=None):
    try:
        y, m, d = (int(x) for x in date_s.split("-"))
        t = _today or _bj_today()
        return (t - datetime.date(y, m, d)).days
    except Exception:
        return 999


_STALE_GAP_DAYS = 12   # 末根距今天数上限: 覆盖最长真实休市(周末2 + 国庆/春节长假≈9), 12 安全裕量
# R270: 2026 超长假窗口(自然日, 含节后周末保守外扩)。gap 4~12 天仅在窗口内合法
# (真实休市); 窗口外出现 4+ 天滞后 = 腾讯 CDN 陈旧缓存(或数据源停更), 拒绝采用。
# 短假(清明/五一/端午/中秋/元旦休市<=5自然日)由 gap<=3 覆盖; 窗口外长假期间误拒只会
# 触发切东财/新浪一次, 次交易日自动恢复 —— 宁切源勿吞陈旧数据。
_LONG_HOLIDAY_WINDOWS = (("2026-02-13", "2026-02-24"),   # 春节(休市2/16~2/22一带)
                         ("2026-10-01", "2026-10-11"))   # 国庆(休市10/1~10/8一带)


def _in_long_holiday(today=None):
    t = (today or _bj_today()).isoformat()
    return any(a <= t <= b for a, b in _LONG_HOLIDAY_WINDOWS)


def _last_fresh(last_date, today=None):
    """末根K线是否够新鲜(相对北京今天)。R270 分级:
      gap 0~3          → 新鲜(常规周末/短假/当日, 一律放行)
      gap 4~12         → 仅"今天处于长假窗口"才放行(真实休市最长≈9自然日+裕量);
                        窗口外此量级滞后 = CDN 陈旧缓存, 判 False 触发切备用源(东财qfq/新浪)。
      未来日期(>今天)  → 判 False(数据泄漏防御)。
    1~3 天内的短滞后无法用自然日区分(真实休市也如此), 由 meta.asof + build_date 的
    次日自动重扫自愈。"""
    try:
        y, m, d = (int(x) for x in last_date.split("-"))
        t = today or _bj_today()
        gap = (t - datetime.date(y, m, d)).days
    except Exception:
        return False
    if not (0 <= gap <= _STALE_GAP_DAYS):
        return False
    if gap <= 3:
        return True
    return _in_long_holiday(t)


def _should_weekend_skip(asof, today):
    """R270: 周末且现有数据已覆盖最近交易日 → 跳过全量重扫。
    (workflow guard 的 shell 兜底 —— 09-06 曾现周日凌晨仍触发全量 sina 扫描拖 15h 的
    反例: guard 失效/排队时序时 scan 内自守卫兜底, 避免周末空跑烧源+CI 额度。)"""
    if today.weekday() < 5:
        return False
    return bool(asof and _last_fresh(asof, today=today))


def _bc_tail(bc, bis, btype, n_last=10):
    """近 n_last 笔内的 type 背驰(正序最后一条), 返回 {bi_date_end, end_price, area_ratio,
    bc_type, vol_confirm, fresh_days} 或 None"""
    cands = [x for x in bc if x["type"] == btype]
    if not cands:
        return None
    x = cands[-1]
    pos = int(x.get("bi_index", -1))
    if not (0 <= pos < len(bis)):
        return None
    bi = bis[pos]
    if len(bis) - 1 - pos >= n_last:      # 超过最近 n_last 笔 -> 不算近端
        return None
    fresh = _days_ago(bi["date_end"])
    if fresh > FRESH_MAX_DAYS * 4:         # 背驰发生在很久前(非当下信号)
        return None
    return {"bi_date_end": bi["date_end"], "end_price": round(bi["end_price"], 3),
            "area_ratio": round(x.get("area_ratio", -1), 3),
            "bc_type": x.get("bc_type", ""), "vol_confirm": bool(x.get("vol_confirm")),
            "fresh_days": fresh}


_KS_MIN_BARS = 30    # R271: 净化绝对底线(防字段错乱/空壳) —— 次新30~120根仍进分析, 由门禁"次新"剔除展示


def _sanitize_ks(ks):
    """R271: K线净化防线 —— 任何源进缠论引擎前统一过滤。
    脏/坏 bar(字段截断、重复日期、OHLC 矛盾、越出 2021 契约窗/未来日期)会静默污染
    笔/中枢/背驰结构(R265~R269 多轮结构错位里数据层坏根是隐性来源), 宁缺毋滥:
    净化后不足 _KS_MIN_BARS 根即整段作废(交上层切备用源/记失败)。"""
    if not ks:
        return []
    seen, out = set(), []
    t_today = _bj_today().isoformat()
    for k in ks:
        d = k.get("date", "")
        try:
            o, h, l, c = (float(k[x]) for x in ("open", "high", "low", "close"))
            v = float(k.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if d in seen or not (len(d) == 10 and fd.MIN_DATE <= d <= t_today):
            continue
        if not (o > 0 and h > 0 and l > 0 and c > 0 and v >= 0):
            continue
        if h < max(o, c) or l > min(o, c):     # OHLC 自洽: high>=max(o,c) 且 low<=min(o,c)
            continue
        seen.add(d)
        out.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
    out.sort(key=lambda x: x["date"])
    return out if len(out) >= _KS_MIN_BARS else []


def analyze_one(sym, ks):
    """chanlun.analyze -> 精简摘要(雷达schema) + 轻量绘图标注 mark。
    返回 (st, err, mark)。mark 仅供前端详情页叠画, 门禁剔除票也尽量给(可点看结构)。
    R271: 入口统一过 _sanitize_ks 净化(日期有序去重/OHLC自洽/契约窗), 坏数据不进引擎。"""
    ks = _sanitize_ks(ks)
    if not ks:
        return None, "ks_bad", {}
    try:
        r = cl.analyze(ks, with_stability=False)
    except Exception as e:   # noqa: BLE001
        return None, "analyze_err:%s" % str(e)[:80], {}
    try:
        merged, bis, zss = r["merged"], r["bis"], r["zhongshu"]
        bc, signals = r["beichi"], r["signals"]
        cls, agree = r["classify"], r["agreement"]
    except (KeyError, TypeError) as e:
        return None, "schema_err:%s" % e, {}
    closes = [k["close"] for k in ks]
    highs = [k["high"] for k in ks]
    lows = [k["low"] for k in ks]
    n = len(ks)
    last = ks[-1]
    d0, d1 = ks[0]["date"], last["date"]
    span_days = _days_ago(d0) - _days_ago(d1) if _days_ago(d0) < 30000 else -1
    tail60 = ks[-60:]
    avg_amt = sum(k["volume"] * 100 * k["close"] for k in tail60) / max(1, len(tail60)) / 1e4
    amp = [h / l - 1 for h, l in zip(highs, lows) if l > 0]
    one_word = sum(1 for h, l in zip(highs, lows) if l > 0 and abs(h / l - 1) < 1e-9)
    med_amp = (sorted(amp)[len(amp) // 2] if amp else 0.0) * 100
    stop_days = _days_ago(d1)   # 距今天数(停牌判定: 明显大于3)
    zs_last = ({"zd": round(zss[-1]["zd"], 2), "zg": round(zss[-1]["zg"], 2),
                "date_end": zss[-1]["date_end"]} if zss else None)
    scenario = cls.get("scenario", "")
    bottom = _bc_tail(bc, bis, "bottom")
    top = _bc_tail(bc, bis, "top")
    st = {
        "n_bars": n, "first": d0, "last": d1, "span_days": span_days,
        "close": round(last["close"], 3), "chg1d": round(last["close"] / closes[-2] - 1, 4) if n >= 2 else 0,
        "bi_n": len(bis), "zs_n": len(zss), "bc_n": len(bc), "sig_n": len(signals),
        "agree": round(agree["rate"], 3), "agree_n": agree["total"],
        "scenario": scenario, "trend": cls.get("trend_type", ""),
        "last_bi_dir": bis[-1]["dir"] if bis else 0,
        "avg_amt60": round(avg_amt, 1), "med_amp": round(med_amp, 2),
        "one_word": one_word, "dd": round(last["close"] / max(highs) - 1, 3) if highs else 0,
        "zs_last": zs_last, "stop_days": stop_days,
        "bottom_bc": bottom, "top_bc": top,
        "seg_bot": bool(cls.get("seg_bc_bottom")), "seg_top": bool(cls.get("seg_bc_top")),
    }
    # 轻量绘图标注(详情页叠画用): 近中枢矩形 + 近背驰点 + 近笔折线(限通过票)
    mark = {"zs": [], "bc": [], "line": [], "sig": []}
    for z in zss[-3:]:
        mark["zs"].append([round(z["zg"], 2), round(z["zd"], 2), z["date_start"], z["date_end"]])
    for b in bc[-6:]:
        i = b["bi_index"]
        if 0 <= i < len(bis):
            mark["bc"].append([b["type"], bis[i]["date_end"], round(bis[i]["end_price"], 2)])
    for b in bis[-10:]:
        mark["line"].append([b["date_start"], round(b["start_price"], 2),
                             b["date_end"], round(b["end_price"], 2), b["dir"]])
    # R271: 分类买卖点(一/二/三类) — 前端个股/行业K线叠画「一买/二买/三买/一卖/二卖/三卖」。
    # signals 已由 find_signals 去重(每笔仅一类信号, 优先级 一类>三类>二类), 携带 date/price,
    # 直接取近端 14 条入库; 一类点与 bc 背驰点同源, 前端有 sig 时不再重复画通用背驰三角。
    # 格式 [dir(1买/-1卖), kind全名, date_end, price] — 与 zs/bc/line 一致的紧凑数组。
    mark["sig"] = [[s["dir"], s["kind"], s["date"], round(s["price"], 2)]
                   for s in signals[-14:]]
    return st, None, mark


def gate_of(st):
    """门禁: 返回 (gate_code, desc)。gate="" 表示可通过。"""
    if st is None:
        return "fail", "分析失败"
    if st["n_bars"] < MIN_BARS:
        return "次新", "数据不足(%d根<%d)" % (st["n_bars"], MIN_BARS)
    if st["stop_days"] > 20:
        return "停牌", "最后K线距今%d天" % st["stop_days"]
    if st["avg_amt60"] < LOW_AMT60:
        return "低流动性", "近60日均额%.0f万" % st["avg_amt60"]
    if st["one_word"] >= ONE_WORD_MAX:
        return "一字板", "一字板%d天" % st["one_word"]
    if st["agree_n"] >= AGREE_TOTAL_MIN and st["agree"] < AGREE_RATE_MIN:
        return "低自洽", "分笔自洽%.2f" % st["agree"]
    if st["bi_n"] < 4:
        return "次新", "笔数不足(%d)" % st["bi_n"]
    return "", ""


def signal_of(sym, name, typ, st):
    """近端信号: classify 背驰场景 + 最近背驰新鲜度 <= FRESH_MAX_DAYS"""
    scen = st["scenario"]
    if scen == "背驰见底机会":
        b = st["bottom_bc"]
        if not b or b["fresh_days"] > FRESH_MAX_DAYS:
            return None
        strong = 2 if (b["bc_type"] == "趋势背驰" or st["seg_bot"]) else 1
        return {"sym": sym, "name": name, "type": typ, "dir": "bottom",
                "sig": "背驰见底", "strong": strong,
                "vol": b["vol_confirm"], "fresh": b["fresh_days"],
                "area": b["area_ratio"], "bc_date": b["bi_date_end"],
                "bc_type": b.get("bc_type", ""),
                "scenario": scen}
    if scen == "背驰见顶风险":
        b = st["top_bc"]
        if not b or b["fresh_days"] > FRESH_MAX_DAYS:
            return None
        strong = 2 if (b["bc_type"] == "趋势背驰" or st["seg_top"]) else 1
        return {"sym": sym, "name": name, "type": typ, "dir": "top",
                "sig": "背驰见顶", "strong": strong,
                "vol": b["vol_confirm"], "fresh": b["fresh_days"],
                "area": b["area_ratio"], "bc_date": b["bi_date_end"],
                "bc_type": b.get("bc_type", ""),
                "scenario": scen}
    return None


def _spark_of(ks):
    """最近 SPARK_N 根压缩 [o,h,l,c] (内含首根日期便于前端比例)"""
    ks2 = ks[-SPARK_N:]
    return {"d0": ks2[0]["date"], "d1": ks2[-1]["date"],
            "data": [[round(k["open"], 3), round(k["high"], 3),
                      round(k["low"], 3), round(k["close"], 3)] for k in ks2]}


# ================= 4b. 行业K线合成(总市值加权) =================
def synth_industry_kline(members, got):
    """行业成分股(市值加权收益率链式)合成行业日K [o,h,l,c,v,date]。
    members: [(sym, mcap)]; got: {sym: (ks, src)}。市值缺失票剔除。
    权重 w_i = 最新总市值占比; 当日停牌(缺K线/昨收)剔除并重归一。"""
    rows = []
    for sym, mcap in members:
        if mcap <= 0 or sym not in got:
            continue
        ks = got[sym][0]
        if len(ks) < 60:
            continue
        rows.append((mcap, ks))
    if len(rows) < 3:                       # 成分太少 -> 无行业K线
        return None
    # 建日期->各股 map: date -> [(mcap, k), ...]
    days = {}
    for mcap, ks in rows:
        prev_c = None
        for k in ks:
            c = k["close"]
            if prev_c and prev_c > 0 and c > 0 and k["open"] > 0 and k["high"] > 0 and k["low"] > 0:
                days.setdefault(k["date"], []).append(
                    (mcap, k["open"] / prev_c - 1, k["high"] / prev_c - 1,
                     k["low"] / prev_c - 1, c / prev_c - 1, k.get("volume", 0) * c))
            prev_c = c
    dates = sorted(days)
    if len(dates) < 120:
        return None
    out = []
    px = 1000.0
    for d in dates:
        bucket = days[d]
        wsum = sum(m for m, *_ in bucket)
        if wsum <= 0:
            continue
        ro = sum(m * o for m, o, *_ in bucket) / wsum
        rh = sum(m * h for m, _o, h, *_ in bucket) / wsum
        rl = sum(m * l for m, _o, _h, l, *_ in bucket) / wsum
        rc = sum(m * c for m, _o, _h, _l, c, _v in bucket) / wsum
        vol = sum(v for m, _o, _h, _l, _c, v in bucket)      # 行业成交额(元)合计
        if len(bucket) < 3:                 # 当日在场成分太少, 跳过
            continue
        o = px * (1 + ro)
        hi = px * (1 + rh)
        lo = px * (1 + rl)
        cl = px * (1 + rc)
        out.append({"date": d, "open": round(o, 2), "high": round(max(hi, o, cl), 2),
                    "low": round(min(lo, o, cl), 2), "close": round(cl, 2),
                    # R267 注释更正: vol=Σ(成分手×价), 单位=手·元, 非元(差100倍=手→股)。
                    # 存值 = vol/1e8 = 板块成交额(亿元)/100 —— 前端 radar.html amtYi ×100 还原为亿元,
                    # 勿把本字段当"亿元"直接用(会小100倍)。
                    "volume": round(vol / 1e8, 2)})          # = 成交额(亿元)/100
        px = cl
    return out if len(out) >= 120 else None


# ================= 4c. 行业专业指标(P3 雷达平铺用) =================
def _rsi14(kline, n=14):
    """Wilder 平滑 RSI(14)。输入 [o,h,l,c,...] 日线数组; 不足 n+1 返回 None。"""
    closes = [k["close"] for k in kline if k.get("close")]
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    # 初始均值(前 n 期简单平均)
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    # Wilder: avg = (prev_avg*(n-1) + cur) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l <= 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def _amp20(kline, win=20):
    """近 win 日振幅 = (区间内 max(high) - min(low)) / 期初 open * 100。"""
    seg = kline[-win:] if len(kline) >= win else kline
    if len(seg) < 5:
        return None
    hi = max(k["high"] for k in seg)
    lo = min(k["low"] for k in seg)
    o0 = seg[0]["open"]
    if o0 <= 0:
        return None
    return round((hi - lo) / o0 * 100, 2)


def _regime_of(ist, n_top, n_bot, rsi14=None):
    """行业走势状态标签: 成分顶/底背驰计数 + 板块自身位置(收盘 vs 末中枢 + RSI)。

    [R260] 修复 2 处:
    1) bug: 原代码读 ist["bis"]——但 analyze_one 构造 st 时从未写入 "bis" 键,
       该键恒空 → last_dir 永远 "" → "上涨趋势/下跌趋势" 两个分支永不命中,
       regime 退化成纯成分计数(全市场只见 顶背驰区/震荡中 两种, CSS 预留的趋势配色从未生效)。
       实际方向字段为 ist["last_bi_dir"](-1 末笔向下 / 1 末笔向上)。
    2) 板块位置门控: "顶背驰区" 的语义是"板块处相对高位、上攻动能衰竭"。
       当板块自身已跌破末中枢下沿(close<zd)、或回落至中枢下半部且 RSI<45 时,
       成分的顶背驰预警多半已经兑现(信号后普跌), 继续标"顶背驰区"会误导逆向判断,
       故前者按末笔给"下跌趋势/震荡中", 后者给"震荡中"(除非成分底背驰显著集中)。
       反之板块处中枢上半/上方时, 成分顶背驰计数占优 → "顶背驰区" 语义成立, 保留。"""
    # 末笔方向: last_bi_dir=-1 末笔向下 / 1 末笔向上 / 0 缺省
    last_dir = {1: "up", -1: "down"}.get(ist.get("last_bi_dir"), "")
    close = ist.get("close")
    zs = ist.get("zs_last") or {}
    zd, zg = zs.get("zd"), zs.get("zg")
    # —— 位置门控 1: 收盘已跌破末中枢下沿 = 破位下行区 ——
    if close is not None and zd is not None and close < zd:
        return "下跌趋势" if last_dir == "down" else "震荡中"
    # —— 位置门控 2: 中枢下半部 且 RSI<45 = 弱势回落区(顶背驰预警已兑现) ——
    if close is not None and zd is not None and zg is not None and close < (zd + zg) / 2:
        if rsi14 is not None and rsi14 < 45:
            if n_bot >= 2 and n_bot > n_top * 2:   # 成分底背驰集中仍提示
                return "底背驰区"
            return "震荡中"
    # —— 成分信号计数(板块处中枢上半/上方时, 位置语义成立) ——
    if n_top >= 2 and n_top > n_bot * 2:
        return "顶背驰区"
    if n_bot >= 2 and n_bot > n_top * 2:
        return "底背驰区"
    if n_top >= 2 and n_bot == 0 and last_dir == "down":
        return "顶背驰区"
    if n_bot >= 2 and n_top == 0 and last_dir == "up":
        return "底背驰区"
    # —— 位置门控 3 [R264]: 趋势词必须与收盘位置自洽 ——
    # 末笔方向只反映最近一笔(约3~5根K线)的端点方向, 高位钝化时一笔向下的小回调
    # 会把"明明在主升/高位横盘"的板块误标成"下跌趋势" (实证: 农林牧渔 RSI67.6、
    # 收盘超末中枢上沿9.6%、近10日+10.1% 却被标"下跌趋势", 商贸零售同型)。
    # 趋势语义=结构方向: 需收盘位置同向支持 ——
    #   close<mid 末笔向下 → 中枢下半回落, 结构偏空, 才是"下跌趋势";
    #   close>mid 末笔向下 → 高位回调, 只算"震荡中"(防误导);
    #   close>mid 末笔向上 → 中枢上半推进, 结构向上, 才是"上涨趋势";
    #   close<mid 末笔向上 → 低位反抽, 只算"震荡中"。
    if close is not None and zd is not None and zg is not None:
        mid = (zd + zg) / 2
        if last_dir == "down":
            return "下跌趋势" if close < mid else "震荡中"
        if last_dir == "up":
            return "上涨趋势" if close > mid else "震荡中"
    if last_dir == "down":
        return "下跌趋势"
    if last_dir == "up":
        return "上涨趋势"
    return "震荡中"


def _qual_rate(ind_name, ind_total, ind_qual):
    """门禁合格率 = 行业过门禁 / 行业总成分(扫描后); 无成分时 None。"""
    t = ind_total.get(ind_name, 0)
    q = ind_qual.get(ind_name, 0)
    if t <= 0:
        return None
    return round(q / t * 100, 1)


# ================= 4. 主流程 =================
def main():
    global SRC_ONLY
    argv = sys.argv[1:]
    limit = 0
    only = ""
    for i, a in enumerate(argv):
        if a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
        elif a == "--only" and i + 1 < len(argv):
            only = argv[i + 1]
        elif a == "--src" and i + 1 < len(argv):
            SRC_ONLY = argv[i + 1]
    t0 = time.time()
    # R270: 周末自守卫(workflow guard 的 shell 兜底 —— 曾现 guard 失效/排队时序下周末凌晨
    # 触发全量 sina 扫描拖 15h 的反例): 周末且现有数据已含最近交易日 → 直接跳过。
    # 人工调试(--only/--limit/--src)不受限, 照常可跑。
    _old = {}
    if not only and not limit and SRC_ONLY == "auto":
        try:
            _old = json.load(open(OUT, encoding="utf-8")).get("meta") or {}
        except Exception:
            _old = {}
        if _should_weekend_skip(_old.get("asof", ""), _bj_today()):
            print("[scan_radar] 周末(%s)且数据已覆盖最近交易日(asof=%s), 跳过全量重扫"
                  % (_bj_today().isoformat(), _old.get("asof", "")))
            return
    print("[scan_radar] 拉取全市场标的池(东财 clist)... src模式=%s" % SRC_ONLY)
    uni, excl, host = fetch_universe()
    if not uni:
        print("!! 东财全镜像不可达, 无法构建标的池", file=sys.stderr)
        sys.exit(2)
    syms = list(uni.keys())
    print("  标的池 %d (ST/退排除 %d) 源host=%s" % (len(uni), excl.get("st", 0), host))
    if only:
        syms = [s.strip() for s in only.split(",") if s.strip()]
        for s in syms:                      # 允许指数/自定标的(不在池内)参与, 便于人工基准对照
            if s not in uni:
                uni[s] = {"code": s[-6:] if len(s) >= 6 else s, "name": s, "type": "基准"}
    elif limit:
        syms = syms[:limit]

    # --- 抓K线(并发, 全局限速由 fetch_kline 内 throttle 保证; 失败重试一轮) ---
    got, fails = {}, {}

    def _fetch_one(sym):
        ks, src = fetch_kline(sym)
        if not ks:
            time.sleep(0.4)
            ks, src = fetch_kline(sym)      # 重试一次(网络抖动/限流偶发)
        return sym, (ks, src) if ks else (None, src)

    t_f = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for n_done, (sym, res) in enumerate(ex.map(_fetch_one, syms), 1):
            ks, src = res
            if ks:
                got[sym] = (ks, src)
            else:
                fails[sym] = src
            if n_done % 200 == 0:
                print("  拉取 %d/%d  有效%d 失败%d  %.0fs" % (
                    n_done, len(syms), len(got), len(fails), time.time() - t0), flush=True)
    print("  拉取完成: 有效 %d / %d, 失败 %d, %.0fs" % (len(got), len(syms), len(fails), time.time() - t_f))

    # --- 分析 ---
    sts, marks, errs = {}, {}, {}
    t_a = time.time()
    for i, (sym, (ks, src)) in enumerate(got.items()):
        st, err, mark = analyze_one(sym, ks)
        if st:
            st["src"] = src
            sts[sym] = st
            if mark:
                marks[sym] = mark
        else:
            errs[sym] = err
    if sts:
        per = (time.time() - t_a) / len(sts)
        print("  分析完成 %d 票, 均耗时 %.2fs/票, 失败 %d" % (len(sts), per, len(errs)))

    # --- 门禁 + 信号 + 行业聚合(同时攒成分) ---
    signals, universe, ind_members, ind_total, ind_qual = [], {}, {}, {}, {}
    for sym, st in sts.items():
        gate, gdesc = gate_of(st)
        sig = None
        if not gate:
            sig = signal_of(sym, uni[sym]["name"], uni[sym]["type"], st)
        uind = uni[sym].get("ind", "-") if sym in uni else "-"
        if uind not in ("", "-"):
            ind_total[uind] = ind_total.get(uind, 0) + 1
        if not gate and uind not in ("", "-") and uni[sym]["type"] in ("股", "北交"):
            ind_qual[uind] = ind_qual.get(uind, 0) + 1
        # ETF/场内基金 -> 聚合为「ETF板块」并列(按总规模加权合成板块K线)
        if uni[sym]["type"] == "ETF":
            ind_total[ETF_KEY] = ind_total.get(ETF_KEY, 0) + 1
            if not gate:
                ind_qual[ETF_KEY] = ind_qual.get(ETF_KEY, 0) + 1
                ind_members.setdefault(ETF_KEY, []).append((sym, uni[sym]["mcap"]))
        uind = uni[sym].get("ind", "-") if sym in uni else "-"
        row = {"name": uni[sym]["name"], "type": uni[sym]["type"],
               "code": uni[sym]["code"], "src": st.pop("src", ""),
               "gate": gate, "gd": gdesc, "ind": uind,
               "mcap": uni[sym].get("mcap", 0)}
        row["st"] = st
        if sym in marks:
            row["mark"] = marks[sym]
        universe[sym] = row
        if sig:
            # ETF 信号归到 ETF板块 (行业计数/分组用)
            sig["ind"] = ETF_KEY if uni[sym]["type"] == "ETF" else uind
            signals.append((sym, sig))
        if uind not in ("-", "") and uni[sym]["type"] in ("股", "北交") and gate == "":
            ind_members.setdefault(uind, []).append((sym, uni[sym]["mcap"]))
    # 信号票补 spark 快照(从 got 拿原始K线)与关键位
    for sym, sig in signals:
        ks, _src = got[sym]
        sig["spark"] = _spark_of(ks)
        sig["st"] = sts[sym]
        zs = sts[sym].get("zs_last")
        sig["levels"] = {"zd": zs["zd"], "zg": zs["zg"]} if zs else {}
        if sym in marks:
            sig["mark"] = marks[sym]

    signals.sort(key=lambda x: (-x[1]["strong"], x[1]["fresh"], -x[1]["area"] if x[1]["area"] > 0 else 0))

    # --- 行业K线合成 + 行业自身缠论 + 行业聚合 ---
    industries = {}
    t_ind = time.time()
    for ind, members in sorted(ind_members.items()):
        iks = synth_industry_kline(members, got)
        if not iks:
            continue
        ist, ierr, imark = analyze_one("ind_" + ind, iks)
        if not ist:
            continue
        ist["src"] = "synth"
        ind_sig = [x for x in signals if x[1].get("ind") == ind]
        n_top = sum(1 for _s, sg in ind_sig if sg["dir"] == "top")
        n_bot = sum(1 for _s, sg in ind_sig if sg["dir"] == "bottom")
        # 行业当日加权涨跌(合成K线末两根)
        chg1d = round(iks[-1]["close"] / iks[-2]["close"] - 1, 4) if len(iks) >= 2 else 0
        total_cap = sum(m for _s, m in members if m)
        rsi14 = _rsi14(iks) if len(iks) >= 15 else None   # R260: regime 位置门控需 RSI 弱态信号
        industries[ind] = {
            "n_member": len(members),
            "n_total": ind_total.get(ind, len(members)),
            "n_sig_top": n_top, "n_sig_bot": n_bot,
            "cap": round(total_cap / 1e8, 0),      # 亿元
            "chg1d": chg1d,
            "is_etf": 1 if ind == ETF_KEY else 0,
            "rsi14": rsi14,
            "amp20": _amp20(iks),
            "qual_rate": _qual_rate(ind, ind_total, ind_qual),
            "regime": _regime_of(ist, n_top, n_bot, rsi14),
            "st": ist, "mark": imark,
            "kline": iks[-IND_KLINE_N:],           # 最近 N 根(画行业K线)
            "spark": _spark_of(iks[-SPARK_N:])["data"],
        }
    print("  行业合成/分析 %d 个, %.0fs" % (len(industries), time.time() - t_ind))

    # --- meta ---
    # asof = 全市场最新交易日: 取 last 众数(set去重后取中位会落到日期值域正中, 曾误得2014)
    from collections import Counter as _Counter
    _lc = _Counter(s["last"] for s in sts.values() if s.get("last"))
    asof = _lc.most_common(1)[0][0] if _lc else ""
    scen_cnt, gate_cnt = {}, {}
    for s in sts.values():
        scen_cnt[s["scenario"]] = scen_cnt.get(s["scenario"], 0) + 1
    for r in universe.values():
        gate_cnt[r["gate"]] = gate_cnt.get(r["gate"], 0) + 1
    src_cnt = {}
    for _s, (_ks, _src) in got.items():
        src_cnt[_src] = src_cnt.get(_src, 0) + 1
    ind_cnt = {}
    for r in universe.values():
        i = r["ind"]
        ind_cnt[i] = ind_cnt.get(i, 0) + 1
    deg = _src_degraded(src_cnt)
    deg_reason = _degraded_reason(src_cnt) if deg else ""
    # R270: 各源失败原因统计(停用状态机 reasons) -> meta.src_fail, 前端/人工可查腾讯为何不可用
    _tx_r = dict(_tx_down.get("reasons") or {})
    _em_r = dict(_em_down.get("reasons") or {})
    src_fail = {}
    if _tx_r:
        src_fail["tx"] = _tx_r
    if _em_r:
        src_fail["em"] = _em_r
    meta = {
        "title": "A股全市场缠论雷达",
        "asof": asof, "build_time": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "version": "P3b-r1",
        "n_universe": len(uni), "n_fetch": len(got), "n_fail": len(fails),
        "n_ok": len(sts), "n_gate": sum(gate_cnt.values()) - gate_cnt.get("", 0),
        "n_signal": len(signals), "n_ind": len(industries),
        "scen_cnt": scen_cnt, "gate_cnt": gate_cnt, "src_cnt": src_cnt,
        "degraded": deg,                       # R270: 新浪裸价占比>30% 即降级(复权源占比视角)
        "degraded_reason": deg_reason,         # 降级黄条文案(前端优先展示)
        "src_fail": src_fail,                  # 各源失败原因计数(诊断腾讯/东财为何不可用)
        "ind_cnt": ind_cnt,
        "excl_st": excl.get("st", 0),
        "note": ("信号=近端背驰场景(背驰见底/见顶) 距背驰日<=%d天; 门禁剔除项仅展示不进信号; "
                 "K线源 腾讯qfq优先/东财qfq次之/新浪兜底; 行业=申万一级31个, K线=成分股总市值加权合成"
                 % FRESH_MAX_DAYS),
    }
    out = {"meta": meta,
           "signals": [{"sym": s, **sig} for s, sig in signals],
           "industries": industries,
           "universe": {s: {k: v for k, v in row.items()
                            if k in ("name", "type", "code", "gate", "gd", "ind", "mcap", "st", "mark")}
                        for s, row in universe.items()}}
    json.dump(out, open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    print("\n======== 雷达产物 %s ========" % OUT)
    print(json.dumps({k: v for k, v in meta.items() if not isinstance(v, dict)}, ensure_ascii=False, indent=1))
    print("场景分布:", json.dumps(scen_cnt, ensure_ascii=False))
    print("门禁分布:", json.dumps(gate_cnt, ensure_ascii=False))
    print("K线源分布:", json.dumps(src_cnt, ensure_ascii=False))
    print("\n==== 信号清单 (%d) ====" % len(signals))
    for s, sig in signals[:40]:
        print("  %-11s %-10s %s %s 强%s 新鲜%d天 面积%.2f %s" % (
            s, uni[s]["name"], sig["sig"], "底" if sig["dir"] == "bottom" else "顶",
            sig["strong"], sig["fresh"], sig["area"],
            "量能确认" if sig["vol"] else ""))
    print("\n完成 %.0fs; 文件 %.1f KB" % (time.time() - t0, os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
