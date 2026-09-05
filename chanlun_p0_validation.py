# -*- coding: utf-8 -*-
"""P0 全市场缠论雷达 · 算法可靠性验证
================================================
目的: 回答「生产级 chanlun.analyze(chanlun.py, 5指数报告在用) 批量跑个股,
自动缠论分笔/中枢/背驰到底可不可信」——用真实 A 股抽样池量化:
  1) 结构可算率   (K线数/笔数/中枢数达标比例)
  2) 分笔自洽率   (chanlun 内置 bi_agreement 严格口径对照)
  3) 信号分布     (底/顶背驰候选、买卖点信号类型分布)
  4) 极端票表现   (次新/低波动/一字板/长期停牌/ST 区间的误判与门禁必要性)
  5) 人工基准对照 (阳光电源 300274 人工复核结构 + 5 指数)

数据: 腾讯 fqkline 纯 count URL (R248 形态), fetch_data.fetch_tx 复用。
抽样: 沪深代码段均匀抽样(真实代码密度~40-60%, 空号自然被过滤)。
用法: python3 chanlun_p0_validation.py [n_max]
产物: /tmp/p0_universe.json(拉取+结构原始), stdout 汇总(供撰写报告)
"""
import os
import sys
import json
import time
import random
from concurrent.futures import ThreadPoolExecutor

_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)
import fetch_data as fd   # noqa: E402
import chanlun as cl      # noqa: E402

MIN_BARS = 120          # 少于 120 根日线(约半年)视为数据不足(次新/长期停牌)
CONCURRENCY = 6
TIMEOUT_TOTAL = 900

# ---------------- 抽样池 ----------------
def gen_universe(seed=20260905):
    """沪深代码段均匀抽样: 覆盖主板/中小/创业/科创, 空号(退市/未用)由拉取失败自然过滤。
    段设计(含约 35-65% 真实密度), 共 ~379 探活目标命中 ~180-220 有效票。"""
    rng = random.Random(seed)
    syms = []
    # 沪主板 600000-605999 (600/601/603/605 连续编码)
    for code in range(600000, 606000, 35):
        syms.append("sh%d" % code)
    # 沪科创板 688001-688999
    for code in range(688001, 689000, 20):
        syms.append("sh%d" % code)
    # 深主板/中小 000001-003999 (000/001/002/003)
    for code in range(1, 4000, 25):
        syms.append("sz%06d" % code)
    # 深创业板 300001-301999 (300/301)
    for code in range(300001, 302000, 35):
        syms.append("sz%06d" % code)
    rng.shuffle(syms)
    return syms

# ---------------- 拉取 ----------------
def _fetch_one(sym):
    t0 = time.time()
    try:
        ks, dirty = fd.fetch_tx(sym, "day")
        if len(ks) < MIN_BARS:
            return sym, {"ok": False, "reason": "bars<%d:%d" % (MIN_BARS, len(ks)), "sec": round(time.time() - t0, 2)}
        return sym, {"ok": True, "klines": ks, "dirty": dirty, "sec": round(time.time() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return sym, {"ok": False, "reason": "err:%s" % str(e)[:70], "sec": round(time.time() - t0, 2)}

def fetch_universe(syms):
    got, fails = {}, {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for sym, res in ex.map(_fetch_one, syms):
            if res["ok"]:
                got[sym] = res
            else:
                fails[sym] = res["reason"]
            if time.time() - t0 > TIMEOUT_TOTAL:
                print("!! 总超时, 提前终止拉取", file=sys.stderr)
                break
    return got, fails

# ---------------- 结构计算 ----------------
def _bi_stats(klines, r):
    merged, bis, zss = r["merged"], r["bis"], r["zhongshu"]
    bc = r["beichi"]
    signals = r["signals"]
    cls = r["classify"]
    closes = [k["close"] for k in klines]
    lows = [k["low"] for k in klines]
    highs = [k["high"] for k in klines]
    vols = [k["volume"] for k in klines]
    n = len(klines)
    last = klines[-1]
    # 上市时长粗估: 首根日期(2021-01-01 裁剪)距最后一根的自然日数; 首根=2021-01-04 说明>=2021
    d0 = klines[0]["date"]; d1 = last["date"]
    def _days(a, b):
        try:
            import datetime
            da = datetime.date(*map(int, a.split("-")))
            db = datetime.date(*map(int, b.split("-")))
            return (db - da).days
        except Exception:
            return -1
    span_days = _days(d0, d1)
    # 近60日粗成交额(元): volume(手)*100*close
    tail60 = klines[-60:]
    avg_amt = sum(k["volume"] * 100 * k["close"] for k in tail60) / max(1, len(tail60)) if tail60 else 0.0
    # 振幅与一字板
    amp = [h / l - 1 for h, l in zip(highs, lows) if l > 0]
    one_word = sum(1 for h, l in zip(highs, lows) if l > 0 and abs(h / l - 1) < 1e-9)
    med_amp = sorted(amp)[len(amp) // 2] if amp else 0.0
    # 近端 bottom/top 背驰
    def _last_bc(t):
        bb = [x for x in bc if x["type"] == t]
        if not bb:
            return None
        x = bb[-1]
        b = bis[x["bi_index"]]
        return {"bi_date_end": b["date_end"], "end_price": round(b["end_price"], 2),
                "area_ratio": round(x.get("area_ratio", -1), 3), "bc_type": x.get("bc_type", ""),
                "vol_confirm": bool(x.get("vol_confirm"))}
    # 最近两个中枢区间(合并口径合并到K线日期)
    def _zs_simple(z):
        return {"date_start": z["date_start"], "date_end": z["date_end"],
                "zd": round(z["zd"], 2), "zg": round(z["zg"], 2),
                "count": z["count"], "ext": bool(z.get("extension"))}
    zs_tail = [_zs_simple(z) for z in zss[-3:]]
    sig_kinds = [s.get("kind", "") for s in signals]
    from collections import Counter
    sig_cnt = dict(Counter(sig_kinds))
    return {
        "n_bars": n, "first_date": d0, "last_date": d1, "span_days": span_days,
        "last_close": round(last["close"], 2),
        "merged_n": len(merged), "bi_n": len(bis), "zs_n": len(zss),
        "beichi_n": len(bc), "sig_n": len(signals),
        "bottom_bc": _last_bc("bottom"), "top_bc": _last_bc("top"),
        "agreement_rate": round(r["agreement"]["rate"], 3),
        "agreement_total": r["agreement"]["total"],
        "zs_tail": zs_tail, "sig_cnt": sig_cnt,
        "scenario": cls.get("scenario", ""), "position": cls.get("position", ""),
        "trend_type": cls.get("trend_type", ""),
        "seg_bc_bottom": bool(cls.get("seg_bc_bottom")), "seg_bc_top": bool(cls.get("seg_bc_top")),
        "med_amp": round(med_amp * 100, 2), "one_word_days": one_word,
        "avg_amt60": round(avg_amt / 1e4, 1),   # 万元
        "dd_from_hi": round(last["close"] / max(highs) - 1, 3) if highs else 0,
        "last_bi_dir": bis[-1]["dir"] if bis else 0,
    }

def analyze_one(sym, item):
    ks = item["klines"]
    try:
        r = cl.analyze(ks, with_stability=False)
        return sym, {"ok": True, "st": _bi_stats(ks, r)}
    except Exception as e:  # noqa: BLE001
        return sym, {"ok": False, "err": str(e)[:100]}

def analyze_batch(got):
    out = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for sym, res in ex.map(analyze_one, got.keys(), (got[s] for s in got)):
            if res["ok"]:
                out[sym] = res["st"]
            else:
                out[sym] = {"ok": False, "err": res["err"]}
    return out

# ---------------- 汇总 ----------------
def summarize(uni, got, fails, st):
    n_try = len(uni)
    n_got = len(got)
    n_fail = len(fails)
    # 成功样本: analyze_batch 成功分支存扁平 stats(无 "err" 键), 失败分支存 {"ok":False,"err"}
    def _is_ok(v):
        return "err" not in v
    n_ok = sum(1 for v in st.values() if _is_ok(v))
    n_err = sum(1 for v in st.values() if not _is_ok(v))
    rows = [v for v in st.values() if _is_ok(v)]
    if not rows:
        return {"n_valid": 0, "n_try": n_try, "n_got": n_got, "n_fail": n_fail,
                "n_ok": n_ok, "n_err": n_err}
    from statistics import mean
    def _pct(x):
        return round(100.0 * x / len(rows), 1)
    def _cnt(cond):
        return sum(1 for v in rows if cond(v))
    zs_ok = [v for v in rows if v["bi_n"] >= 6 and v["zs_n"] >= 1]
    bi_ok = [v for v in rows if v["bi_n"] >= 4]
    agree_hi = [v for v in rows if v["agreement_total"] >= 8 and v["agreement_rate"] >= 0.8]
    agree_lo = [v for v in rows if v["agreement_total"] >= 8 and v["agreement_rate"] < 0.6]
    bot_bc = [v for v in rows if v["bottom_bc"]]
    top_bc = [v for v in rows if v["top_bc"]]
    bot_trend = [v for v in bot_bc if v["bottom_bc"]["bc_type"] == "趋势背驰"]
    sig_any = [v for v in rows if v["sig_n"] > 0]
    low_liq = [v for v in rows if v["avg_amt60"] < 3000]          # 3000万以下(门禁候选)
    one_word = [v for v in rows if v["one_word_days"] >= 10]      # 一字板多(难算结构)
    flat = [v for v in rows if v["med_amp"] < 0.8]                # 极低波动(难算)
    suspect = [v for v in rows if v["bi_n"] >= 4 and (v["agreement_total"] >= 8 and v["agreement_rate"] < 0.6)]
    return {
        "n_try": n_try, "n_got": n_got, "n_fail": len(fails), "n_ok": n_ok, "n_err": n_err,
        "n_valid": len(rows),
        "bi_ok": len(bi_ok), "bi_ok_pct": _pct(len(bi_ok)),
        "zs_ok": len(zs_ok), "zs_ok_pct": _pct(len(zs_ok)),
        "agree_hi": len(agree_hi), "agree_hi_pct": _pct(len(agree_hi)),
        "agree_lo": len(agree_lo), "agree_lo_pct": _pct(len(agree_lo)),
        "bot_bc": len(bot_bc), "bot_bc_pct": _pct(len(bot_bc)),
        "bot_trend": len(bot_trend), "top_bc": len(top_bc),
        "sig_any": len(sig_any), "sig_any_pct": _pct(len(sig_any)),
        "low_liq": len(low_liq), "one_word": len(one_word), "flat": len(flat),
        "suspect": len(suspect),
        "med_bi": round(mean(v["bi_n"] for v in rows), 1),
        "med_zs": round(mean(v["zs_n"] for v in rows), 1),
        "med_agree": round(mean(v["agreement_rate"] for v in rows), 3),
        "beichi_kinds": {},
    }

def main():
    n_max = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    t0 = time.time()
    bench_syms = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000905", "sz300274"]
    uni = gen_universe()
    if n_max:
        uni = uni[:n_max]
    print("抽样池 %d 个(seed=20260905) + 基准%d票, 并发%d" % (len(uni), len(bench_syms), CONCURRENCY))
    got, fails = fetch_universe(uni)
    bgot, bfails = fetch_universe(bench_syms)
    got.update(bgot)
    fails.update(bfails)
    print("拉取完成: 有效 %d, 失败 %d, 耗时 %.0fs" % (len(got), len(fails), time.time() - t0))
    _fr = {}
    for _r in fails.values():
        _k = _r.split(":")[0][:24]
        _fr[_k] = _fr.get(_k, 0) + 1
    print("失败原因分布:", json.dumps(_fr, ensure_ascii=False))
    st = analyze_batch(got)
    _errs = [(s, str(v.get("err"))[:90]) for s, v in st.items() if not v.get("ok")][:3]
    if _errs:
        print("analyze 异常样例:", _errs, file=sys.stderr)
    summ = summarize(uni, got, fails, st)
    print("结构计算完成, 有效 %d, 耗时 %.0fs" % (summ.get("n_ok", 0), time.time() - t0))
    json.dump({"universe": uni, "got": {s: {"dirty": v["dirty"], "sec": v["sec"]} for s, v in got.items()},
               "fails": fails, "st": st, "summ": summ},
              open("/tmp/p0_universe.json", "w"), ensure_ascii=False, indent=1)
    # ---- 打印汇总 ----
    print("\n================ P0 汇总 (有效 %d 票) ================" % summ["n_valid"])
    for k in ("bi_ok", "bi_ok_pct", "zs_ok", "zs_ok_pct", "agree_hi", "agree_hi_pct",
              "agree_lo", "agree_lo_pct", "bot_bc", "bot_bc_pct", "bot_trend", "top_bc",
              "sig_any", "sig_any_pct", "low_liq", "one_word", "flat", "suspect",
              "med_bi", "med_zs", "med_agree"):
        print("  %-14s %s" % (k, summ.get(k)))
    # ---- 人工基准: 5 指数 + 阳光电源 ----
    print("\n================ 人工基准对照 ================")
    bench = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000905", "sz300274"]
    nm = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
          "sh000300": "沪深300", "sh000905": "中证500", "sz300274": "阳光电源"}
    for s in bench:
        if s not in st or "err" in st[s]:
            print("  %-10s 不在有效池或失败" % nm.get(s, s)); continue
        v = st[s]
        print("  %-8s n=%d 笔%d 中枢%d agree=%.2f | 场景[%s] 位置[%s] 方向%s | 尾底背驰%s" % (
            nm.get(s, s), v["n_bars"], v["bi_n"], v["zs_n"], v["agreement_rate"],
            v["scenario"], v["position"], v["last_bi_dir"] if "last_bi_dir" in v else "?",
            json.dumps(v["bottom_bc"], ensure_ascii=False)))
        print("          中枢尾:", json.dumps(v["zs_tail"], ensure_ascii=False))
        print("          seg背驰: bottom=%s top=%s | %s" % (v["seg_bc_bottom"], v["seg_bc_top"], v["trend_type"]))
    # ---- 抽样打印 ----
    print("\n================ 随机 6 票详情抽样 ================")
    rng = random.Random(7)
    ks = [s for s in st if "err" not in st[s]]
    rng.shuffle(ks)
    for s in ks[:6]:
        v = st[s]
        print("  %s n=%d(%s~%s) 笔%d 中枢%d agree=%.2f | 场景[%s] | 尾底背驰=%s 顶背驰=%s | 近60日均额%.0f万" % (
            s, v["n_bars"], v["first_date"], v["last_date"], v["bi_n"], v["zs_n"],
            v["agreement_rate"], v["scenario"],
            json.dumps(v["bottom_bc"], ensure_ascii=False) or "-",
            json.dumps(v["top_bc"], ensure_ascii=False) or "-", v["avg_amt60"]))
    print("\n完成, 总耗时 %.0fs; 明细 /tmp/p0_universe.json" % (time.time() - t0))

if __name__ == "__main__":
    main()
