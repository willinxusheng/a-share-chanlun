# -*- coding: utf-8 -*-
"""P1 全市场缠论雷达 · 每日扫描 (radar/scan_radar.py)
========================================================
全市场(沪深A + 北交所 + 场内基金/ETF)缠论结构每日扫描,
产出 radar/radar.json 供「全市场雷达页 + 首页信号区」消费。

数据链(多源降级, 任一源失败不影响整体):
  标的池  东财 clist (push2delay/push2 镜像轮询, 免key): 名称含 ST/退 直接排除
  K线主源 腾讯 fqkline 纯count qfq (R248 形态, 前复权, ~640根/个股, CI境外最稳)
  K线备源 新浪 CN_MarketDataService 日K (不复权, ~800根, 本地/境内外双可达)
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
import fetch_data as fd   # noqa: E402   # 复用腾讯 qfq 抓取(纯count R248) 与 UA
import chanlun as cl      # noqa: E402   # 生产级缠论库

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ---------- 参数 ----------
MIN_BARS = 120                 # 少于120根日线(约半年) -> 次新/数据不足, 不进信号
LOW_AMT60 = 3000.0             # 近60日均成交额(万元) 低于 -> 低流动性
ONE_WORD_MAX = 10              # 一字板天数 >= -> 结构失真, 不进信号
AGREE_TOTAL_MIN = 8
AGREE_RATE_MIN = 0.6
FRESH_MAX_DAYS = 10            # 最近背驰距今天数 <= -> 才算"近端信号"
SINA_LEN = 800                 # 新浪兜底K线根数(约3.2年, 与腾讯qfq窗口同量级)
SPARK_N = 150                  # 信号票内嵌迷你K线根数(前端实操卡用)
CONCURRENCY = 4
TX_INTERVAL = 0.35             # 腾讯全局限速 ~2.9 rps (P0实证突发连发会501)
SINA_INTERVAL = 0.18           # 新浪限速 ~5.5 rps (新浪无501挑战, 温和节流, 实测稳定)
SRC_ONLY = "auto"              # auto=腾讯优先失败切新浪 | tx=仅腾讯 | sina=仅新浪(本地被腾讯WAF降速时)
EM_HOSTS = ["https://push2delay.eastmoney.com", "https://push2.eastmoney.com",
            "http://82.push2.eastmoney.com", "http://push2delay.eastmoney.com"]
EM_FS_STOCK = "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80"      # 沪深A股(含主板/中小/创业/科创)
EM_FS_BJ = "m:0+t:81+s:2048"                            # 北交所
EM_FS_FUND = "b:MK0021"                                 # 场内基金(ETF/LOF)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radar.json")

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
_sina_th = _Throttle(SINA_INTERVAL)
_tx_down = {"flag": False, "count": 0}
_tx_lock = threading.Lock()


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
    """拉取一个板块的全部标的: 返回 [(code, mkt, name)]。mkt: 1=沪 0=深/北交(用代码段细分)。
    实测 push2delay 将 pz 钳制到 100/页, 故用接口 total 字段控制翻页终止。"""
    out, page, total, empty_run = [], 1, None, 0
    while True:
        u = ("%s/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
             "&fs=%s&fields=f12,f13,f14" % (host, page, fs))
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
                    out.append((code, mkt, name))
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
    """东财全市场标的池(多镜像轮询)。返回 {sym: {code,name,type}}, 及排除计数。"""
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
        for code, mkt, name in rows:
            # 退市/ST 名称门禁: 完全剔除(不展示不分析)
            nm = name.upper()
            if "ST" in nm or "退" in name or name.startswith("*"):
                excl["st"] += 1
                continue
            sym = _sym_of(code, mkt, name)
            if sym in uni:
                excl["dup"] += 1
                continue
            uni[sym] = {"code": code, "name": name, "type": _typename(code, name)}
        return uni, excl, host
    return uni, excl, ""


# ================= 2. K线抓取(腾讯 qfq 主 / 新浪 备) =================
def _fetch_tx(sym):
    ks, dirty = fd.fetch_tx(sym, "day")   # 纯count qfq (R248), 返回2021-01-01起
    if ks and ks[-1]["date"] >= "2024-01-01":
        return ks, "tx"
    return [], "tx"


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
    return out, "sina"


def _is_waf(text):
    return (not text.lstrip().startswith("{")
            and ("waf" in text.lower() or "<!doctype" in text.lower()
                 or "<html" in text.lower()))


def fetch_kline(sym):
    """按 SRC_ONLY: 腾讯 qfq 主源(默认, CI用) -> 新浪 备源; --src sina 时仅新浪(本地WAF降速场景)。"""
    # 腾讯主源
    with _tx_lock:
        tx_down = _tx_down["flag"]
    if SRC_ONLY != "sina" and not tx_down:
        _tx_th.wait()
        try:
            ks, src = _fetch_tx(sym)
            if ks:
                return ks, src
        except Exception as e:   # noqa: BLE001
            msg = str(e)[:200]
            if "json" in msg.lower() or "waf" in msg.lower() or "501" in msg:
                # 疑似腾讯 WAF/风控: 累计触发则整段切新浪源
                _tx_th.wait()
                try:
                    _probe = _get(fd._tx_url(sym, "day"), timeout=8).decode("utf-8", "ignore")
                    if _is_waf(_probe):
                        with _tx_lock:
                            _tx_down["count"] += 1
                            if _tx_down["count"] >= 6:
                                _tx_down["flag"] = True
                except Exception:
                    pass
    if SRC_ONLY == "tx":
        return None, "tx_only"
    # 新浪备源
    _sina_th.wait()
    try:
        ks, src = _fetch_sina(sym)
        if ks:
            return ks, src
    except Exception as e:   # noqa: BLE001
        return None, "sina_err:%s" % str(e)[:60]
    return None, "empty"


# ================= 3. 结构摘要 + 门禁 + 近端信号 =================
def _days_ago(date_s):
    try:
        y, m, d = (int(x) for x in date_s.split("-"))
        return (datetime.date.today() - datetime.date(y, m, d)).days
    except Exception:
        return 999


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


def analyze_one(sym, ks):
    """chanlun.analyze -> 精简摘要(雷达schema)"""
    try:
        r = cl.analyze(ks, with_stability=False)
    except Exception as e:   # noqa: BLE001
        return None, "analyze_err:%s" % str(e)[:80]
    try:
        merged, bis, zss = r["merged"], r["bis"], r["zhongshu"]
        bc, signals = r["beichi"], r["signals"]
        cls, agree = r["classify"], r["agreement"]
    except (KeyError, TypeError) as e:
        return None, "schema_err:%s" % e
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
    return st, None


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
                "scenario": scen}
    return None


def _spark_of(ks):
    """最近 SPARK_N 根压缩 [o,h,l,c] (内含首根日期便于前端比例)"""
    ks2 = ks[-SPARK_N:]
    return {"d0": ks2[0]["date"], "d1": ks2[-1]["date"],
            "data": [[round(k["open"], 3), round(k["high"], 3),
                      round(k["low"], 3), round(k["close"], 3)] for k in ks2]}


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
    sts, errs = {}, {}
    t_a = time.time()
    for i, (sym, (ks, src)) in enumerate(got.items()):
        st, err = analyze_one(sym, ks)
        if st:
            st["src"] = src
            sts[sym] = st
        else:
            errs[sym] = err
    if sts:
        per = (time.time() - t_a) / len(sts)
        print("  分析完成 %d 票, 均耗时 %.2fs/票, 失败 %d" % (len(sts), per, len(errs)))

    # --- 门禁 + 信号 ---
    signals, universe = [], {}
    for sym, st in sts.items():
        gate, gdesc = gate_of(st)
        sig = None
        if not gate:
            sig = signal_of(sym, uni[sym]["name"], uni[sym]["type"], st)
        row = {"name": uni[sym]["name"], "type": uni[sym]["type"],
               "code": uni[sym]["code"], "src": st.pop("src", ""), "gate": gate, "gd": gdesc}
        row["st"] = st
        universe[sym] = row
        if sig:
            signals.append((sym, sig))
    # 信号票补 spark 快照(从 got 拿原始K线)与关键位
    for sym, sig in signals:
        ks, _src = got[sym]
        sig["spark"] = _spark_of(ks)
        sig["st"] = sts[sym]
        zs = sts[sym].get("zs_last")
        sig["levels"] = {"zd": zs["zd"], "zg": zs["zg"]} if zs else {}

    signals.sort(key=lambda x: (-x[1]["strong"], x[1]["fresh"], -x[1]["area"] if x[1]["area"] > 0 else 0))

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
    meta = {
        "title": "A股全市场缠论雷达",
        "asof": asof, "build_time": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "version": "P1-r1",
        "n_universe": len(uni), "n_fetch": len(got), "n_fail": len(fails),
        "n_ok": len(sts), "n_gate": sum(gate_cnt.values()) - gate_cnt.get("", 0),
        "n_signal": len(signals),
        "scen_cnt": scen_cnt, "gate_cnt": gate_cnt, "src_cnt": src_cnt,
        "excl_st": excl.get("st", 0),
        "note": ("信号=近端背驰场景(背驰见底/见顶) 距背驰日<=%d天; 门禁剔除项仅展示不进信号; "
                 "K线源 腾讯qfq优先/新浪兜底" % FRESH_MAX_DAYS),
    }
    out = {"meta": meta,
           "signals": [{"sym": s, **sig} for s, sig in signals],
           "universe": {s: {k: v for k, v in row.items() if k in ("name", "type", "code", "gate", "gd", "st")}
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
