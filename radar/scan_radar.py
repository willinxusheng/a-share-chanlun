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
 新鲜度   市场末交易日锚(R272: main 串行段新浪探测沪深龙头末根max) —
           K线源末根 >= 锚 即"不比市场旧"; 周末/长假/当日行情未出自动正确,
           无需维护节假日表; 源落后锚 = CDN陈旧缓存拒用切备源; 锚失效回落窗口表
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


def _vol_share_per_unit(sym):
    """腾讯 fqkline volume 单位换算为「股」的倍数: 主板/深市/ETF 返回 100(volume=手, 1手=100股);
    科创板(sh688/sh689)返回 1(volume=股).
    R300 实证(09-06): 招行 sh600036 volume=845229(手,×100×41≈34.6亿✓), 寒武纪 sh688256 volume=8282775(股,×1072≈88.8亿✓) —
    统一乘 100 会让科创板成交额虚高 100 倍(avg_amt60 虚高、行业合成 K 线成交额列失真)。"""
    return 1 if sym.startswith(("sh688", "sh689")) else 100
ONE_WORD_MAX = 10              # 一字板天数 >= -> 结构失真, 不进信号
# R277: 一字板统计窗口(交易日数) —— "结构失真"语义=近期无量封板无法交易, 与 MIN_BARS
# (120≈半年)门禁同窗口口径。全历史累计会把上市初期连板(次新常态 10~24 个一字)或多年前
# 重组/题材一字永久计入, 使正常交易多年的票被 gate 永久误杀; 次新(n<120)已由"次新"
# gate 先行剔除, 本窗口不会额外放过上市不足半年的连板票。
ONE_WORD_WIN = 120
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
# R374: 阈值 6→15 —— 09-07/09-08 实证 77~82% 裸价降级中, 真实网络/风控失败(err/empty/stale)
# 与票面短历史(tx_short/em_short, 次新股无足够根数)混在 6 连败窗口内即冤停复权主源, 致
# 全市场误转新浪裸价。放宽阈值给偶发抖动容错; 成段故障 15 连败也只空耗 ~5s(0.35s/票), 无损。
TX_FAIL_MAX = 15
EM_FAIL_MAX = 15
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
MARKS_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marks.json")
# R297: mark(笔/中枢/背驰/买卖点标注, ~10.4MB) 从 radar.json 拆到 marks.json —
# radar.json 15.9→5.5MB 首页秒开; marks 由前端异步预取, 点个股时按需就绪。
ETF_KEY = "ETF板块"             # 场内基金/ETF 归为独立板块(P3b), 与申万一级并列展示
# R283: 当日主力资金流(东财 ulist.np 批量, 免key) —— f62=主力净流入(元, 负=净流出,
# 正=净流入), f184=主力净占比%。与标的池同域(push2), CI 境外/本地同链路可达; 限速复用
# _em_th(0.25s/请求); 连续 EM_FF_FAIL_MAX 整批失败即放弃(展示级增强, 不拖慢主 scan)。
EM_FF_HOSTS = ["https://push2.eastmoney.com", "https://push2delay.eastmoney.com",
               "http://push2.eastmoney.com"]
EM_FF_BATCH = 60               # 单请求 secid 数(实测 >60 响应可能被截断)
EM_FF_FAIL_MAX = 5             # 连续整批失败上限(东财不可达快速放弃)

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
# R272: 市场末交易日锚(probe_mkt_last 在 main 串行段探测后写入)。
# 用于 _last_fresh/_should_weekend_skip 的新鲜度判定: 数据末根 >= 该锚 即"不比市场旧",
# 市场无更新的日子(周末/长假/当日行情未出)旧数据即最新 —— 取代硬编码长假窗口的
# 大部分职责(窗口表只作探测失败时的 fallback), 2027+ 节假日无需维护。
_mkt_last = None

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
    """记录一次源失败; 达阈值整段停用(flag=True)。reason 汇入统计供 meta 展示。
    R275: 置停用时把 n 归零 —— 此前 n 在源健康期也持续自增, 停用后首个复探触发点
    被该偏移污染(每 every 次调用才 probe 的语义实际漂移 0~every-1 次); 归零后
    复探节拍严格从停用时刻起算(停用后第 every 次调用轻量复探)。"""
    with lock:
        d["count"] += 1
        if reason:
            d["reasons"][reason] = d["reasons"].get(reason, 0) + 1
        if d["count"] >= d["max"]:
            d["flag"] = True
            d["n"] = 0                     # R275: 复探节拍原点 = 停用时刻
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
                code, name = str(x.get("f12", "")), str(x.get("f14", ""))
                try:
                    mkt = int(x.get("f13", -1))
                except (TypeError, ValueError):
                    mkt = -1            # R289: f13 脏值防御 —— 单条字段异常只弃该行, 不崩整个标的池
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
            # R353: 细分名新增/改名未收录时 sub_to_sw1 返回"其他"——若入库, 行业聚合会出现
            # "其他"行业组, 污染 32 板块键域契约(industries=31 SW1 + ETF)且前端无对应 tab。
            # 用 SW1 白名单收敛: 不在 31 一级内一律归 '-' (与 ETF/未分类同处理, 仅进 universe)。
            if ind not in im.SW1:
                ind = "-"
            uni[sym] = {"code": code, "name": name, "type": typ,
                        "ind": ind, "mcap": mcap}
        return uni, excl, host
    return uni, excl, ""


# ================= 2. K线抓取(腾讯qfq 主 / 东财qfq 次 / 新浪裸价 备) =================
def _fetch_tx(sym):
    """腾讯 qfq(前复权)主源: 纯 count 形态(R248), 2021-01-01 起裁剪。
    R270: 新鲜度判定由 `>=2024-01-01`(过松, R267 注释自我批评却未在 scan 链路落实)
    收紧为 _last_fresh 分级(gap<=3 常规 / 4~12 仅长假窗口内合法) —— 腾讯 CDN 陈旧缓存
    (R248: 缓存键含日期段曾停 12h+)不再被静默当有效数据吞下。
    R348: ①北交 920 段跳过 —— 腾讯 K线接口对 920 段恒回最新 1 根(param=bj920992,day,,,320,qfq
    实测 2026-09-07 仅 1 根, count 失效), 无历史K线; 请求纯浪费且"新鲜 1 根"假成功会把北交票
    吞成 ks_bad(见 fetch_kline/main 分析循环, <_KS_MIN_BARS 判死无切源), 腾讯健康时北交全丢。
    跳过腾讯与东财北交 skip(_EM_MKT_PFX 无 bj)对称, 由新浪兜底; 腾讯日后支持北交历史再移除。
    ②根数下限 _KS_MIN_BARS —— 与 _fetch_em 的 MIN_BARS 检查(em_short)对称: 非北交票若返回
    短序列(接口截断/异常)不再当"新鲜成功"吞掉, 显式判失败交切源。"""
    if sym.startswith("bj"):
        return [], "tx_skip_bj"
    ks, _dirty = fd.fetch_tx(sym, "day")
    if not ks:
        return [], "tx_empty"
    if not _last_fresh(ks[-1]["date"]):
        return [], "tx_stale"              # CDN 陈旧缓存(R248: 停 12h+), 重试无意义
    if len(ks) < _KS_MIN_BARS:
        return [], "tx_short:%d" % len(ks) # R348: 短序列(接口截断/异常)显式判失败切源, 不假成功吞票
    return ks, "tx"


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
    # R320: 新浪 volume 单位换算须按证券类型分段 —— 实测(2026-09-04 收盘): 新浪
    # sh688256 volume=8282775 与腾讯同值(股), 而 sh510300=841465543(股, 腾讯=8414655手)。
    # 即新浪全市场原生=股; 非科创板 /100 存手 与腾讯/东财一致; 科创板(sh688/sh689) 若也
    # /100 会存成"假手", 而 _vol_share_per_unit(688)=1(期待存储=股) → avg_amt60/行业合成
    # 成交额低估 100× → "低流动性"门禁误杀 + 信号静默丢失 + 日间换源 100× 跳变(R300 只修了
    # 腾讯 688 分支, 新浪兜底路径漏同源). 科创板原生=股, 直接存股对齐腾讯 688 存储口径。
    _sina_sh_kcb = sym.startswith(("sh688", "sh689"))
    for row in arr:
        try:
            out.append({"date": row["day"],
                        "open": float(row["open"]), "high": float(row["high"]),
                        "low": float(row["low"]), "close": float(row["close"]),
                        "volume": (float(row["volume"]) / 100.0 if not _sina_sh_kcb
                                   else float(row["volume"]))})   # 新浪=股 -> 非688存手 / 688存股
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


# ================= 2a2. 月线 MACD 状态(腾讯 qfq 月K, R383 双周期排序键) =================
def _ema(vals, n):
    """标准 EMA(首值=序列首价, 递归平滑) —— MACD 用。"""
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


# R389: 月线补拉网络失败计数。模块级可变容器 —— _month_macd 重试仍失败 +1;
# main 补拉块用前后快照差统计"本次"失败量(免 main 内 global 归零缠绕)。GIL 下 4 线程
# 竞争极小, 即便偶丢一计数仅影响 WARN 阈值精度, 不影响数据正确性。
_mb_net_fail = {"n": 0}


def _month_macd(sym):
    """底背驰个股月线 MACD 状态 —— 逆向观察池/底部信号的双周期排序键数据源(R383)。

    腾讯 qfqmonth(纯 count URL, 自然月对齐, 2021-01 起 ~68 根) → MACD(12,26,9)
    hist 末两根状态:
      red          = hist>=0  月线红柱(多头背景 —— 日线底背驰反弹延续概率高, 排序最前)
      green_shrink = hist<0 且较上月缩短(下跌动能衰减 —— 反转在酝酿, 次前)
      green_grow   = hist<0 且较上月加长(月线下跌中继 —— 日线级反弹易夭折, 排序沉同档尾)
    不可得/失败返回 None(前端=中性档, 不升不降)。北交 920 段腾讯恒假回、指数无底背驰语义,
    由调用方只传个股; 腾讯整段停用(状态机)时不盲打。
    R389 实测(09-09): 短时高频连打 ~200 次后腾讯 WAF 对 ifzq.gtimg.cn 回 501(与主链 day 同款
    风控; 主链 day 有 3 次指数退避, 本函数裸 _get 原零重试, 偶发 501 即整批静默降级)。
    修: 网络层失败轻量重试 1 次(节流时隙自然间隔), 仍失败计 _mb_net_fail 供补拉块汇总 WARN;
    数据不足(<40 根月K, 次新等)不算网络失败, 静默 None 中性。纯展示级增强: 不杀 run。

    R390 对称守卫补漏: 本函数独立解析 qfqmonth, 未继承 fetch_data 主链的"盘中剔除进行中
    月根"(R164/R167 只护日线, R372 补周/月主链时未覆盖此自解析路径) —— 交易日北京 <15:00
    腾讯把进行中月(月初至今已收盘几日+今日实时价并一根, date=当日)半截返回, close 随盘中
    跳变 → 末根 hist h2 失真(state 判定仅依赖最后两柱, 半截月才过几日即被当成完整月)。
    现加同款守卫: 末根 date==今日 且北京 <15:00 时剔除末根(回落最近完整月)。CI radar-scan
    16:00 起(收盘后源给完整当月根, 保留)本无窗口, 但本地盘中调试/提前调度会踩 —— 与
    R372 教训"日线有守卫周月常漏"对称, 独立自解析路径同样要补。"""
    if _src_down(_tx_down, _tx_lock):
        return None
    _tx_th.wait()
    pairs = None
    for _attempt in range(2):      # 初试 + 1 次轻量重试(吸收腾讯偶发 501/超时)
        try:
            raw = _get(fd._tx_url(sym, "month"), timeout=15).decode("utf-8", "ignore")
            node = (json.loads(raw).get("data") or {}).get(sym) or {}
            kl = node.get("qfqmonth") or node.get("month") or []
            pairs = []
            for row in kl:
                try:
                    d, c = str(row[0]), float(row[2])      # 腾讯 [日期,开,收,高,低,量]
                except (IndexError, TypeError, ValueError):
                    continue
                if d >= fd.MIN_DATE and c > 0:             # 对齐 2021 契约起点(与日线同源裁剪)
                    pairs.append((d, c))
            pairs.sort(key=lambda x: x[0])
            # R390: 盘中剔除进行中月根(见 docstring R390 段)。_bj 显式 UTC+8,
            # 避免 UTC runner 跨日窗口误判(与 fetch_data R167/R170 同款口径)。
            _bj = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            if pairs and pairs[-1][0] == _bj.date().isoformat() and _bj.hour < 15:
                pairs = pairs[:-1]
            break                  # 拉取+解析成功; 数据不足(<40)在下方统一返 None, 不属网络失败不重试
        except Exception:   # noqa: BLE001
            if _attempt == 1:      # 末次仍失败 → 计数 + 降级中性(补拉块汇总 WARN)
                _mb_net_fail["n"] += 1
                return None
            _tx_th.wait()          # 重试前让出节流时隙(0.35s 自然间隔吸收瞬时 501)
    if len(pairs) < 40:       # MACD 26+9 EMA 收敛需近 35 根月K(约3年); 不足=结构不可靠
        return None
    closes = [c for _d, c in pairs]
    e12 = _ema(closes, 12)
    e26 = _ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = _ema(dif, 9)
    h1, h2 = (dif[-2] - dea[-2]) * 2, (dif[-1] - dea[-1]) * 2
    if h2 >= 0:
        state = "red"
    elif h2 > h1:
        state = "green_shrink"
    else:
        state = "green_grow"
    return {"state": state, "h1": round(h1, 4), "h2": round(h2, 4), "date": pairs[-1][0]}


# ================= 2b. 当日主力资金流(东财 ulist 批量) =================
def _ff_parse(diff, secmap):
    """解析东财 ulist diff 数组 -> {sym: {"net": 元, "pct": %}}。
    secmap={(f13市场,f12代码): sym}。字段缺失/非数值项跳过(不中断整批)。"""
    out = {}
    for x in diff or []:
        code = str(x.get("f12") or "")
        sym = secmap.get((x.get("f13"), code))
        if not sym:
            continue
        try:
            net = float(x.get("f62"))
        except (TypeError, ValueError):
            continue
        if not (abs(net) <= 1e17):           # NaN/inf 防御(round(inf) 会 OverflowError)
            continue
        pct = None
        try:
            pct = round(float(x.get("f184")), 2)
        except (TypeError, ValueError):
            pass
        out[sym] = {"net": round(net), "pct": pct}
    return out


def fetch_fflow_all(syms):
    """全市场当日主力资金流(东财批量): 输入 sym 列表(sh/sz 有效, bj 跳过),
    返回 {sym: {"net": 元, "pct": %}}。东财不可达/停更时缺票直接不返回(前端显示'-')。
    与 _em_down 停用状态机解耦 —— 资金流是展示级增强, 失败不影响 K 线下行源健康判定。
    实测: 股与 ETF/LOF 均返回 f62(510300 等场内基金有主力净额), 全市场约 6500 票
    =110 批 ×0.25s ≈ 30s。"""
    secs = []
    for s in syms:
        sec = _em_secid(s)
        if sec:
            pre, code = sec.split(".")
            secs.append((s, (int(pre), code)))
    out, fail_run = {}, 0
    for i in range(0, len(secs), EM_FF_BATCH):
        chunk = secs[i:i + EM_FF_BATCH]
        if fail_run >= EM_FF_FAIL_MAX:
            break
        ids = ",".join("%d.%s" % (m, c) for _s, (m, c) in chunk)
        done = False
        for host in EM_FF_HOSTS:
            try:
                _em_th.wait()
                u = ("%s/api/qt/ulist.np/get?secids=%s&fields=f12,f13,f14,f62,f184"
                     "&fltt=2&invt=2" % (host, ids))
                d = (json.loads(_get(u, timeout=EM_TIMEOUT,
                                     referer="https://quote.eastmoney.com/")
                                .decode("utf-8", "ignore")).get("data") or {})
                diff = d.get("diff")
                if not diff:
                    continue                  # 空响应 -> 换下一镜像
                secmap = {(_m, _c): _s for _s, (_m, _c) in chunk}
                out.update(_ff_parse(diff, secmap))
                done = True
                break
            except Exception:   # noqa: BLE001
                continue
        if not done:
            fail_run += 1
        elif fail_run > 0:
            fail_run = 0
    return out


def _try_tx(sym):
    """腾讯qfq一次尝试: 成功 (ks,"tx"); 失败(异常/空/陈旧) 计入停用统计后返回 ([],tag)。
    R337: 成功即复位连续失败计数 —— 原实现只在停用后 probe 成功才清零, 健康期的
    `count` 只增不清, "连续失败"退化成"自上次停用以来累计失败": 分散在多轮抖动中的
    失败会被错误加总(如 5 次历史失败 + 恢复后 1 次偶发失败即达阈值停用主源, 全市场
    误转东财/新浪降级)。成功请求本身就是源健康的最强证据, 应清零。"""
    try:
        ks, tag = _fetch_tx(sym)
    except Exception:   # noqa: BLE001
        _src_fail(_tx_down, _tx_lock, "err")     # 归一化(原始消息多变, 不入 reasons)
        return [], ""
    if not ks:
        if tag and tag.startswith("tx_skip"):
            return [], tag          # R348: 北交 920 段腾讯接口缺陷(恒1根) —— 结构性跳过, 非源故障, 不计连败
        if tag and tag.startswith("tx_short"):
            # R374: 短序列(<30根)多为次新股上市不足(票面数据面), 非腾讯源故障 ——
            # 计连败会与真实网络失败(err/empty/stale)加总后冤停主源(09-07/09-08 82%裸价
            # 降级中 tx_short 21 票混入 6 连败窗口即触发整段停用); 切下级源但不动状态机。
            return [], tag
        _src_fail(_tx_down, _tx_lock, tag.split(":")[0])
        return [], tag
    with _tx_lock:
        _tx_down["count"] = 0
    return ks, "tx"


def _try_em(sym):
    """东财qfq一次尝试: 成功 (ks,"em"); 失败计入停用统计后返回 ([],tag)。R337: 成功复位计数(见 _try_tx)。"""
    if _src_down(_em_down, _em_lock):
        return [], "em_down"
    _em_th.wait()
    ks, tag = _fetch_em(sym)
    if not ks:
        if tag and tag.startswith("em_short"):
            # R374: 东财短序列(<120根)多为次新股/上市不足(票面), 非东财源故障 —— 不计连败
            # (与 tx_short 对称, 防票面失败混入连败窗口冤停次源致全市场转裸价)。
            return [], tag
        _src_fail(_em_down, _em_lock, tag.split(":")[0])   # em_err:/em_stale 归一化计连败
        return [], tag
    with _em_lock:
        _em_down["count"] = 0
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


# R275: 无需重试的失败标签 —— 源停用/不可用类由状态机或代码段判定, 重试必然同结果
# (R270 整段停用后重试只会再打一次空请求); tx_stale/em_stale 为 CDN 陈旧缓存(R248: 缓存
# 停 12h+), 0.4s 后重试不可能刷新。其余(网络闪断/超时/空响应)才值得重试一次。
_NO_RETRY_SRCS = frozenset(("tx_down", "em_down", "em_unavail", "em_skip",
                            "tx_only", "em_only", "tx_stale", "em_stale",
                            "tx_skip_bj"))   # R348: 北交920段腾讯接口缺陷恒1根 —— 重试必同结果


def _need_retry(src):
    return src not in _NO_RETRY_SRCS


def _fetch_one(sym):
    """单票抓取(worker): 失败且属"可恢复"类才重试一次(网络抖动/限流偶发)。"""
    ks, src = fetch_kline(sym)
    if not ks and _need_retry(src):
        time.sleep(0.4)
        ks, src = fetch_kline(sym)      # 重试一次(网络抖动/限流偶发)
    return sym, (ks, src) if ks else (None, src)


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


# R272: probe 探测的样本(沪深主板流动性龙头, 停牌概率低)。取多只 max 抗单票停牌。
_MKT_PROBE_SYMS = ("sh600000", "sz000001")


def probe_mkt_last():
    """探测市场最新交易日(轻量: 新浪沪深龙头日K末根取 max, CI 上新浪源极稳
    —— 09-04~09-06 全 sina 实证 6736/6736)。成功写入模块全局 _mkt_last 供
    _last_fresh/_should_weekend_skip 作新鲜度锚; 失败返回 None 且保留旧锚(网络
    闪断不倒退锚值)。仅在 main 串行段调用一次, 不进 worker 并发。"""
    global _mkt_last
    dates = []
    for sym in _MKT_PROBE_SYMS:
        try:
            u = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
                 "?symbol=%s&scale=240&ma=no&datalen=5" % sym)
            raw = _get(u, timeout=8).decode("utf-8", "ignore")
            arr = json.loads(raw) or []
            if arr and arr[-1].get("day"):
                dates.append(arr[-1]["day"])
        except Exception:
            continue
    if dates:
        _mkt_last = max(dates)
    return _mkt_last


def _last_fresh(last_date, today=None, mkt_last="__auto__"):
    """末根K线是否够新鲜(相对北京今天)。R270 分级 + R272 市场锚升级:
      gap 0~3            → 新鲜(常规周末/短假/当日, 一律放行)
      gap 4+             → 有市场锚(_mkt_last 或显式 mkt_last): 源末根 >= 锚即放行 ——
                           市场无更新的日子(周末/长假/当日行情未出的首轮 dispatch)里,
                           源停在市场末交易日是"当前可得的最新", 不算陈旧; 源落后于锚
                           (市场已交易到锚日而源停更) = 真陈旧(CDN缓存/CDN滞后), 拒。
                           锚本身停更的极端(>30天)硬拒防御。
                           —— 取代硬编码长假窗口, 2027+ 节假日无需维护
      gap 4~12 无锚      → fallback R270: 仅"今天处于长假窗口"放行(窗口表只兜探测失败)
      未来日期(>今天)    → 判 False(数据泄漏防御)。
    1~3 天内的短滞后无法用自然日区分(真实休市也如此), 由 meta.asof + build_date 的
    次日自动重扫自愈。"""
    try:
        y, m, d = (int(x) for x in last_date.split("-"))
        t = today or _bj_today()
        gap = (t - datetime.date(y, m, d)).days
    except Exception:
        return False
    if gap < 0:
        return False
    if gap <= 3:
        return True
    ml = _mkt_last if mkt_last == "__auto__" else mkt_last
    if ml:
        if last_date >= ml:
            return gap <= 30          # 锚本身停更 >30 天的极端场景硬拒
        return False                  # 源落后于市场末交易日 = 真陈旧
    # ---- 无锚 fallback (R270 窗口表) ----
    if gap > _STALE_GAP_DAYS:
        return False
    return _in_long_holiday(t)


def _should_weekend_skip(asof, today, mkt_last="__auto__"):
    """R270: 周末/长假且现有数据已覆盖最近交易日 → 跳过全量重扫。
    R272 升级: 有市场锚时按锚判定(数据 asof 已覆盖市场末交易日即跳过, 周末/长假/
    当日行情未出的首轮 dispatch 通用); 无锚回退周末 + 窗口表。
    (workflow guard 的 shell 兜底 —— 09-06 曾现周日凌晨仍触发全量 sina 扫描拖 15h 的
    反例: guard 失效/排队时序时 scan 内自守卫兜底, 避免空跑烧源+CI 额度。)"""
    if not asof:
        return False
    ml = _mkt_last if mkt_last == "__auto__" else mkt_last
    if ml:
        return asof >= ml
    if today.weekday() >= 5:
        return bool(_last_fresh(asof, today=today))
    return False


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
# R275: 净化丢弃 bar 累计(诊断用) —— 坏根是 R265~R269 多轮结构错位里的隐性来源, R271 加净化防线后
# 却无任何丢弃量化; 此计数汇入 meta.sanit_drop_bars, 供排查"某日结构错位=数据坏根激增"直接对照。
_sanit_drop_bars = 0


def _sanitize_ks(ks):
    """R271: K线净化防线 —— 任何源进缠论引擎前统一过滤。
    脏/坏 bar(字段截断、重复日期、OHLC 矛盾、越出 2021 契约窗/未来日期)会静默污染
    笔/中枢/背驰结构(R265~R269 多轮结构错位里数据层坏根是隐性来源), 宁缺毋滥:
    净化后不足 _KS_MIN_BARS 根即整段作废(交上层切备用源/记失败)。"""
    global _sanit_drop_bars
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
            _sanit_drop_bars += 1          # 字段缺失/非数值
            continue
        if d in seen or not (len(d) == 10 and fd.MIN_DATE <= d <= t_today):
            _sanit_drop_bars += 1          # 重复日期 / 出契约窗 / 未来日期
            continue
        if not (o > 0 and h > 0 and l > 0 and c > 0 and v >= 0):
            _sanit_drop_bars += 1          # 非正价/负量
            continue
        if h < max(o, c) or l > min(o, c):     # OHLC 自洽: high>=max(o,c) 且 low<=min(o,c)
            _sanit_drop_bars += 1          # OHLC 矛盾
            continue
        seen.add(d)
        out.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
    out.sort(key=lambda x: x["date"])
    return out if len(out) >= _KS_MIN_BARS else []


def _one_word_count(ks, win=ONE_WORD_WIN):
    """近端 win 根内一字板天数(high==low, 无量封板无法成交)。R277 窗口化 ——
    全历史累计会把 2021~2023 的上市初期连板/旧一字永久计入, 详见 ONE_WORD_WIN 注释。"""
    seg = ks[-win:] if len(ks) > win else ks
    return sum(1 for k in seg if k["low"] > 0 and abs(k["high"] / k["low"] - 1) < 1e-9)


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
    try:
        closes = [k["close"] for k in ks]
        highs = [k["high"] for k in ks]
        lows = [k["low"] for k in ks]
        n = len(ks)
        last = ks[-1]
        d0, d1 = ks[0]["date"], last["date"]
        _a0 = _days_ago(d0)                       # 首根距今(自然日); 复用避免双算(7000票级)
        span_days = _a0 - _days_ago(d1) if _a0 < 30000 else -1
        tail60 = ks[-60:]
        avg_amt = sum(k["volume"] * _vol_share_per_unit(sym) * k["close"] for k in tail60) / max(1, len(tail60)) / 1e4  # R300: 科创板 volume 单位=股, 勿再 ×100
        amp = [h / l - 1 for h, l in zip(highs, lows) if l > 0]
        one_word = _one_word_count(ks)   # R277: 近端120根窗口(原全历史累计误杀上市初期连板的正常票)
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
    except Exception as e:   # noqa: BLE001
        # R274: st/mark 构造段异常保护 —— chanlun 输出 schema 基本稳定, 但个别极端数据
        # (某元素缺键/类型异常)一旦命中会把全量 run 崩掉无产物(单票坏数据杀死全市场)。
        # 单票降级为 summ_err 进 errs, 其余票照常。
        return None, "summ_err:%s" % str(e)[:80], {}


def _price_limit(sym):
    """板块单日涨跌幅上限(涨跌停制度, 比例): 主板0.10 / 创业·科创0.20 / 北交0.30 /
    场内基金0.10。R273: 用于裸价源(新浪)除权假跳空识别 —— 真实单日涨跌不可能超上限,
    超上限 = 除权除息日(送转/大比例分红/份额折算)的假跳空。
    R274: 补场内基金段 —— R273 注释"ETF/LOF 无涨跌停跳过"为错误认知: 沪深交易所场内
    ETF/LOF/REITs 现行制度均有 ±10% 涨跌幅(跨境ETF 2024-02 起统一 10%; REITs 上市首日
    30%/此后 10%)。腾讯对 ETF 只回 day(不回撤, radar.html L633 注释), 新浪更是裸价,
    大比例份额折算/极端分红的假跳空此前因 lim=None 完全漏检。1.05 缓冲(_EXDIV_BUF)
    防真实 10% 涨停误伤; REITs 首日 30% 超阈仅触发展示级免疫, 次新 gate 兜底不进信号。"""
    m, code = (sym or "")[:2], (sym or "")[2:]
    if m == "bj":
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return 0.10
    # 场内基金代码段: 沪 5xx(510/511/512/513/515/516/517/518/560/561/562/563/588/589/508REITs),
    # 深 1xx(159ETF/16x LOF/180REITs; 11x/12x 可转债不在股票池, 不会进入)。可转债/分级基金已退
    # 场, 场内品种 2020 后统一 10%(上市首日除外的 REITs 30% 见上)。
    if m in ("sh", "sz") and code.startswith(("5", "1")):
        return 0.10
    return None


_EXDIV_BUF = 1.05    # 缓冲: 涨停收盘可因四舍五入略超上限, *1.05 防误伤真实涨停


def _exdiv_dates(ks, sym):
    """扫描 K 线中疑似除权日(裸价源): 单日 |chg| 超 板块上限*缓冲 = 假跳空。
    返回升序 [date,...]; 无涨跌停段(ETF等)/无命中返回 []。复权源(qfq)不会命中
    (除权已被平滑), 故检测天然只作用于裸价降级票。"""
    lim = _price_limit(sym)
    if not lim:
        return []
    th = lim * _EXDIV_BUF
    out, prev_c = [], None
    for k in ks:
        c = k["close"]
        if prev_c and prev_c > 0 and c > 0 and abs(c / prev_c - 1) > th:
            out.append(k["date"])
        prev_c = c
    return out


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


def _apply_exdiv_immune(st, ks, sym, fresh_days=None):
    """R273: 裸价源(新浪)除权假跳空免疫(在 analyze_one 后、信号生成前调用)。
    除权日单日|chg|超板块涨跌停上限是复权源不会出现的假跳空(真实涨跌被制度锁死),
    会伪造笔/背驰与当日涨跌幅:
      1) 末根为除权日 -> chg1d 不可信置 null(前端显示"-", 免误读为真实暴跌)
      2) 近端(末根前 <= fresh_days+2 交易日)有除权 -> 结构/背驰可能被假跳空污染,
         scenario 免疫(灰标"疑似除权·结构失真"), 清近端背驰字段(防详情页误导)。
    返回是否命中近端免疫。"""
    ex = _exdiv_dates(ks, sym)
    if not ex:
        return False
    fd = FRESH_MAX_DAYS if fresh_days is None else fresh_days
    st["exdiv_d"] = ex[-1]                       # 最近疑似除权日(诊断/展示)
    if ks and ks[-1]["date"] == ex[-1]:
        st["chg1d"] = None                       # 末根除权: 当日涨跌幅不可信
    near = False
    for j in range(len(ks) - 1, -1, -1):     # 含末根本身(除权日可为末根)
        if ks[j]["date"] == ex[-1]:
            near = (len(ks) - 1 - j) <= fd + 2
            break
    if near:
        st["exdiv"] = 1
        st["scenario"] = "疑似除权·结构失真"
        st["bottom_bc"] = None
        st["top_bc"] = None
        st["seg_bot"] = False
        st["seg_top"] = False
    return near


def signal_of(sym, name, typ, st):
    """近端信号: classify 背驰场景 + 最近背驰新鲜度 <= FRESH_MAX_DAYS"""
    scen = st["scenario"]
    if scen == "背驰见底机会":
        b = st["bottom_bc"]
        if not b or b["fresh_days"] > FRESH_MAX_DAYS:
            return None
        strong = 2 if (b["bc_type"] == "趋势背驰" or st["seg_bot"]) else 1
        # R275: 去掉冗余 "sym" 键 —— 外层 JSON 展开 {"sym": s, **sig} 已带同值 sym,
        # 双写同键纯增体积(信号多时 JSON 膨胀); 后端下游全部经元组 (sym, sig) 取 sym。
        return {"name": name, "type": typ, "dir": "bottom",
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
        return {"name": name, "type": typ, "dir": "top",
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
    权重 w_i = 最新总市值占比; 当日停牌(缺K线/昨收)剔除并重归一。
    R273: 裸价成分的除权假跳空日(单日|chg|超板块上限)当日剔除 —— 防行业K线
    被单只成分的除权大跳空打出假阴线/假结构(除权不改变行业真实收益率)。"""
    rows = []
    for sym, mcap in members:
        if mcap <= 0 or sym not in got:
            continue
        ks = got[sym][0]
        if len(ks) < 60:
            continue
        rows.append((sym, mcap, ks))
    if len(rows) < 3:                       # 成分太少 -> 无行业K线
        return None
    # 建日期->各股 map: date -> [(mcap, k), ...]
    days = {}
    for sym, mcap, ks in rows:
        lim = _price_limit(sym) or 1.0      # 无涨跌停段(如 4xx/8xx 老三板等池外段, _price_limit=None): 阈值1.0单日翻倍才剔除
                                            # R274: 场内 ETF/LOF 有 0.10 涨跌停(_price_limit 已含 5xx/1xx 段), 与股票同口径免疫
        th = lim * _EXDIV_BUF
        prev_c = None
        for k in ks:
            c = k["close"]
            if prev_c and prev_c > 0 and c > 0 and k["open"] > 0 and k["high"] > 0 and k["low"] > 0:
                if abs(c / prev_c - 1) <= th:      # 正常日 -> 入桶; 除权假跳空日 -> 剔除该成分当日
                    days.setdefault(k["date"], []).append(
                        (mcap, k["open"] / prev_c - 1, k["high"] / prev_c - 1,
                         k["low"] / prev_c - 1, c / prev_c - 1,
                         k.get("volume", 0) * _vol_share_per_unit(sym) * c / 100.0))   # R300: 行业合成按手·元当量(volume×股数/手×价/100), 科创板 volume=股不虚高
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
    # R270+R272: 全量重扫自守卫(workflow guard 的 shell 兜底 —— 曾现 guard 失效/排队
    # 时序下周末凌晨触发全量 sina 扫描拖 15h 的反例)。R272 升级为市场末交易日锚:
    # 先探测市场最新交易日(新浪, 轻量), 现有数据(asof)已覆盖锚 → 周末/长假/当日行情
    # 未出的首轮 dispatch 一律直接跳过, 不再空跑烧源; 探测失败回落周末+窗口表判定。
    # 人工调试(--only/--limit/--src)不受限, 照常可跑。
    _old = {}
    if not only and not limit and SRC_ONLY == "auto":
        try:
            _old = json.load(open(OUT, encoding="utf-8")).get("meta") or {}
        except Exception:
            _old = {}
        _ml = probe_mkt_last()
        if _ml:
            print("[scan_radar] 市场末交易日锚=%s (无新行情 run 自动跳过)" % _ml)
        else:
            print("[scan_radar] 市场锚探测失败(新浪不可达), 回落周末/窗口表守卫")
        if _should_weekend_skip(_old.get("asof", ""), _bj_today(), mkt_last=_ml):
            print("[scan_radar] 数据已覆盖市场末交易日(asof=%s), 跳过全量重扫"
                  % (_old.get("asof", "")))
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

    # --- R283: 当日主力资金流(全市场 sh/sz 批量; 与 K 线/源健康解耦, 失败只缺字段不崩产物) ---
    t_ff = time.time()
    ffmap = fetch_fflow_all(syms)
    print("  资金流 %d 票, %.0fs (东财主力净额口径)" % (len(ffmap), time.time() - t_ff))

    # --- 分析 ---
    sts, marks, errs = {}, {}, {}
    t_a = time.time()
    for sym in list(got.keys()):
        ks, src = got[sym]
        # R274: 净化前置并写回 —— analyze_one 内部净化(幂等)与免疫/行业合成/spark 此前各自
        # 用不同数据(分析=净化后, 免疫/spark/synth=原始), 坏根在免疫检测与迷你图上会产生
        # 与缠论分析不一致的判定(如未来日期根/重复根造成假除权或 spark 错画)。统一为净化后
        # 同一份; 净化失败的票照旧记 errs(不参与免疫/spark, got 保留原始仅影响 synth 存量同旧)。
        ks2 = _sanitize_ks(ks)
        if not ks2:
            errs[sym] = "ks_bad"
            continue
        got[sym] = (ks2, src)
        st, err, mark = analyze_one(sym, ks2)
        if st:
            st["src"] = src
            # R273: 裸价源(新浪)除权假跳空免疫(见 _apply_exdiv_immune) —— 复权源
            # 不会命中(除权已平滑), 无涨跌停段(基准等)自动跳过。
            _apply_exdiv_immune(st, ks2, sym)
            sts[sym] = st
            if mark:
                marks[sym] = mark
        else:
            errs[sym] = err
    if not sts:
        # R373: 全市场分析零成功的显式中止 —— 原实现会继续走到下方
        # `_Counter(...).most_common(1)[0][0]` 对空 Counter 抛 IndexError 才退出,
        # 靠"偶然崩溃"阻止空产物写盘(radar.json 被覆盖成空壳; CI 侧自检 n_ok>4000
        # 兜底但本地跑无自检)。语义应为显式守卫: 全源失败/数据全坏时中止且不写盘,
        # 保留现有产物(CI runner 上旧 radar.json 由 checkout 提供, 下次 run 自动恢复)。
        print("!! 全市场有效分析 0 票(抓取有效 %d 失败 %d), 中止且不写盘(保留现有产物)"
              % (len(got), len(fails)), file=sys.stderr)
        sys.exit(1)
    per = (time.time() - t_a) / len(sts)
    print("  分析完成 %d 票, 均耗时 %.2fs/票, 失败 %d" % (len(sts), per, len(errs)))

    # --- R383: 底背驰个股月线 MACD 状态(双周期排序键: 逆向观察池/实操信号雷达共用) ---
    # 凡有 bottom_bc 的个股补拉腾讯 qfq 月K(自然月) → st.m_macd={state,h1,h2,date}。
    # 前端 revpool pick 键链按 state 插档(red>green_shrink>无数据>green_grow沉同档尾);
    # 无 state 的票 st["m_macd"]=null(JSON), 前端读到缺省即中性。ETF bottom_bc 只汇入
    # ETF板块聚合无个股行、北交腾讯无可用月K(920恒假回), 一并 null 中性(不惩罚)。
    _mb_syms = [s for s, st in sts.items()
                if st.get("scenario") == "背驰见底机会" and st.get("bottom_bc")
                and not s.startswith("bj")
                and uni.get(s, {}).get("type") not in ("ETF",)]
    if _mb_syms:
        _t_mb = time.time()
        _mb_f0 = _mb_net_fail["n"]
        with ThreadPoolExecutor(max_workers=4) as _mb_ex:
            _mb_res = list(_mb_ex.map(_month_macd, _mb_syms))
        for s, mm in zip(_mb_syms, _mb_res):
            sts[s]["m_macd"] = mm
        _mb_net = _mb_net_fail["n"] - _mb_f0
        _mb_none = sum(1 for mm in _mb_res if mm is None)
        _mb_line = ("  月线MACD状态 %d 票(成功 %d / 数据不足 %d / 网络失败 %d) %.0fs; null=中性"
                    % (len(_mb_syms), len(_mb_syms) - _mb_none, _mb_none - _mb_net, _mb_net,
                       time.time() - _t_mb))
        if _mb_net and (_mb_none == len(_mb_syms) or _mb_net >= max(3, len(_mb_syms) // 5)):
            _mb_line += "  ⚠ 腾讯月K大量网络失败, 月线排序键整键失效风险(前端全员中性)!"
        print(_mb_line)

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
        # (uind 已于上方 L1259 计算, 勿重复赋值)
        row = {"name": uni[sym]["name"], "type": uni[sym]["type"],
               "code": uni[sym]["code"], "src": st.pop("src", ""),
               "gate": gate, "gd": gdesc, "ind": uind,
               "mcap": uni[sym].get("mcap", 0)}
        row["st"] = st
        if sym in marks:
            row["mark"] = marks[sym]
        ffd = ffmap.get(sym)                 # R283: 当日主力净流入(元/占比%) 展示级
        if ffd:
            row["ff"] = ffd
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
        # R297: sig 不再内嵌 mark(与 universe.mark 重复, 前端统一从 MARKS[sym] 取)

    # R392: area 键反转 —— 原 -area(降序) 把弱背离(面积比趋近 0.85 门槛)顶前, 与全局口径
    # (chanlun.py a_cur/a_prev 越小=动能衰竭越狠=背离越强; report.py 注释同) 相悖;
    # 升序小=强背离优先, area<=0(无值) 用 999 沉底(与前端 fresh null->999 兜底同款)。
    signals.sort(key=lambda x: (-x[1]["strong"], x[1]["fresh"], x[1]["area"] if x[1]["area"] > 0 else 999))

    # --- R283: 行业龙头(成分市值最大; ETF板块=场内规模最大) + 行业当日资金流(全成分Σ) ---
    # 口径: 龙头/资金流用 universe 全成分(含门禁票) —— 市值权重最大者即大众认知的行业龙头,
    # 资金流全量加总才是"行业当日净流入"; 与行业K线合成(过门禁成分)口径分开说明。
    ind_lead, ind_ff, ind_ffn = {}, {}, {}
    for _sym, row in universe.items():
        if row["type"] == "ETF":
            key = ETF_KEY
        else:
            key = row["ind"]
            if key in ("", "-"):
                continue
        _mc = row.get("mcap") or 0
        _cur = ind_lead.get(key)
        if _mc > 0 and (not _cur or _mc > _cur["mcap"]):
            ind_lead[key] = {"sym": _sym, "name": row["name"], "mcap": _mc}
        _f = row.get("ff")
        if _f and _f.get("net") is not None:
            ind_ff[key] = ind_ff.get(key, 0) + _f["net"]
            ind_ffn[key] = ind_ffn.get(key, 0) + 1

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
            "leader": ind_lead.get(ind),           # R283: 行业龙头 {sym,name,mcap}(市值最大)
            "netflow": ind_ff.get(ind),            # R283: 当日主力净流入Σ(元, 全成分口径; 负=净流出)
            "netflow_n": ind_ffn.get(ind, 0),      # 参与加总的成分数(诊断口径完整性)
            "st": ist, "mark": imark,
            "kline": iks[-IND_KLINE_N:],           # 最近 N 根(画行业K线)
            "spark": _spark_of(iks[-SPARK_N:])["data"],
        }
    # R283: 龙头行回填 lead=1(前端成分列表/个股详情给"行业龙头"徽标)
    for _ind, _ld in ind_lead.items():
        _lu = universe.get(_ld["sym"])
        if _lu:
            _lu["lead"] = 1
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
    # R374: 降级严重度百分比(新浪裸价占比, 前端按阈值分级渲染: <50% 普通黄条 / >=50% 红条)
    _src_tot = sum(src_cnt.values()) or 1
    deg_pct = round(src_cnt.get("sina", 0) * 100.0 / _src_tot)
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
        "version": "P3b-r11",   # r11=R393: radar.html .stk-grid 加 760px 窄屏适配(5 关键列, 桌面 11 列零影响) — 全文件唯一无 980 变体的 grid 列表, 11列min宽~650px 在手机容器被 overflow:hidden 直裁右侧 4-5 列
                               # r10=R392: signals area 键反转(强背离优先, 原降序把弱背离顶前; R283 引入方向未审)
                               # r6 覆盖 R320(新浪科创板volume 单位=股)/R348(北交920段tx跳过来新浪兜底)/R352(缺员冻结)/
                               # R361(行业映射收敛)等 20+ 轮口径变更, 版本号如实反映当前 schema
        "n_universe": len(uni), "n_fetch": len(got), "n_fail": len(fails),
        "n_ok": len(sts), "n_gate": sum(gate_cnt.values()) - gate_cnt.get("", 0),
        "n_signal": len(signals), "n_ind": len(industries),
        "scen_cnt": scen_cnt, "gate_cnt": gate_cnt, "src_cnt": src_cnt,
        "degraded": deg,                       # R270: 新浪裸价占比>30% 即降级(复权源占比视角)
        "degraded_pct": deg_pct,               # R374: 新浪占比%(前端严重度分级渲染)
        "degraded_reason": deg_reason,         # 降级黄条文案(前端优先展示)
        "src_fail": src_fail,                  # 各源失败原因计数(诊断腾讯/东财为何不可用)
        "mkt_last": _mkt_last or "",           # R272: 市场末交易日锚(新浪探测; 空=探测失败回落窗口表)
        "sanit_drop_bars": _sanit_drop_bars,   # R275: 净化丢弃 bar 数(坏根量化诊断; 正常≈0, 激增=源数据异常)
        "ind_cnt": ind_cnt,
        "excl_st": excl.get("st", 0),
        "note": ("信号=近端背驰场景(背驰见底/见顶) 距背驰日<=%d天; 门禁剔除项仅展示不进信号; "
                 "K线源 腾讯qfq优先/东财qfq次之/新浪兜底; 行业=申万一级31个, K线=成分股总市值加权合成; "
                 "龙头=行业内总市值最大成分(ETF板块=规模最大场内基金); "
                 "资金流=东财当日主力净额(超大+大单, 元), 正=净流入红 负=净流出绿"
                 % FRESH_MAX_DAYS),
    }
    # R297: mark 拆分 — universe 各标的的 mark(笔/中枢/背驰/买卖点, ~10.4MB)独立成 marks.json,
    # radar.json 白名单不再含 "mark"; 行业合成标的(industries.*.mark, 仅32个)保留在 radar.json。
    marks_out = {s: row["mark"] for s, row in universe.items() if row.get("mark")}
    meta["marks_n"] = len(marks_out)
    out = {"meta": meta,
           "signals": [{"sym": s, **sig} for s, sig in signals],
           "industries": industries,
           # R319: 白名单补 "src"(票级K线源 tx/em/sina) —— 此前漏写导致前端 srcTag(u.src)
           # 恒 undefined, R271 票级源徽标从未生效(R276 注释"已在顶层"系假修复, 后端从未写出)。
           # src 值域见 _src_degraded: tx/em=复权(绿标), sina=新浪裸价(橙标⚠, 除权日假跳空风险)。
           "universe": {s: {k: v for k, v in row.items()
                            if k in ("name", "type", "code", "gate", "gd", "ind", "mcap",
                                     "st", "ff", "lead", "src")}
                        for s, row in universe.items()}}
    # R275: 显式 UTF-8 —— 读侧(L1002)已带 encoding, 写侧遗漏; CI runner 若 locale 非 UTF-8
    # (如 C/POSIX), ensure_ascii=False 写中文 meta 文案会 UnicodeEncodeError 崩掉全量 run 无产物。
    json.dump(marks_out, open(MARKS_OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
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
