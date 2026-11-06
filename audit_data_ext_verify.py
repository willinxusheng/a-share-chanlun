# -*- coding: utf-8 -*-
"""独立核验看板数据准确性（不 import 项目代码，纯外部源比对）"""
import json, urllib.request, zlib, sys
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0"}

def tx(sym, period="day", count=30):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},{period},,,{count},qfq"
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20)
    raw = r.read()
    try: raw = zlib.decompress(raw, 16+zlib.MAX_WBITS)
    except Exception: pass
    j = json.loads(raw.decode("utf-8"))
    node = j["data"][sym]
    arr = node.get("qfqday") or node.get("day")
    out = []
    for it in arr:
        out.append(dict(date=it[0], open=float(it[1]), close=float(it[2]),
                        high=float(it[3]), low=float(it[4]), volume=float(it[5])))
    return out

def sina(sym):
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=10"
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20)
    j = json.loads(r.read().decode("utf-8"))
    return [dict(date=x["day"], open=float(x["open"]), close=float(x["close"]),
                 high=float(x["high"]), low=float(x["low"]), volume=float(x["volume"])) for x in j]

d = json.load(open("data.json"))
print("=" * 78)
print("A. 日线 OHLCV 与腾讯原始接口逐根比对（最近 15 个交易日）")
print("=" * 78)
bad = 0
for sym, node in d.items():
    K = {k["date"]: k for k in node["klines"]}
    t = tx(sym, "day", 40)
    checked = 0
    for row in t[-15:]:
        if row["date"] not in K:
            print(f"  ❌ {node['name']} 看板缺 {row['date']}"); bad += 1; continue
        k = K[row["date"]]
        for f in ("open", "close", "high", "low"):
            if abs(k[f] - row[f]) > 0.011:
                print(f"  ❌ {node['name']} {row['date']} {f}: 看板 {k[f]} vs 腾讯 {row[f]}"); bad += 1
        rv = k["volume"] / row["volume"] if row["volume"] else 0
        if not (0.98 < rv < 1.02):
            print(f"  ⚠ {node['name']} {row['date']} volume 比 {rv:.3f}（看板 {k['volume']:.0f} vs 腾讯 {row['volume']:.0f}）")
        checked += 1
    print(f"  {node['name']}: 核对 {checked} 根，末根 {t[-1]['date']} close={t[-1]['close']} / 看板 {node['klines'][-1]['date']} close={node['klines'][-1]['close']}")

print()
print("=" * 78)
print("B. 新浪交叉验证（最近 8 根）")
print("=" * 78)
for sym, node in d.items():
    ss = sym if sym.startswith("sh") or sym.startswith("sz") else sym
    try:
        s = sina(ss)
    except Exception as e:
        print(f"  {node['name']}: 新浪取数失败 {e}"); continue
    K = {k["date"]: k for k in node["klines"]}
    devs = []
    for row in s[-8:]:
        k = K.get(row["date"])
        if not k: continue
        devs.append(abs(k["close"] - row["close"]) / row["close"] * 100)
    print(f"  {node['name']}: {len(devs)} 根比对，最大收盘偏离 {max(devs):.4f}%" if devs else f"  {node['name']}: 无可比对")

print()
print("=" * 78)
print("C. 周线聚合正确性（周 OHLC = 该周日内聚合）")
print("=" * 78)
from collections import OrderedDict
for sym, node in d.items():
    daily = node["klines"]
    wk = node["week_klines"]
    # 按 ISO 周聚合日线
    agg = OrderedDict()
    for k in daily:
        y, w, _ = datetime.strptime(k["date"], "%Y-%m-%d").isocalendar()
        key = f"{y}-W{w:02d}"
        a = agg.setdefault(key, dict(open=k["open"], close=k["close"], high=k["high"],
                                     low=k["low"], volume=0.0, date=k["date"], n=0))
        a["close"] = k["close"]; a["date"] = k["date"]; a["n"] += 1
        a["high"] = max(a["high"], k["high"]); a["low"] = min(a["low"], k["low"])
        a["volume"] += k["volume"]
    agg = list(agg.values())
    errs = 0
    for i in range(1, 4):  # 最近 3 个完整度已知的周
        if len(agg) - i < 0 or len(wk) - i < 0: break
        a, w = agg[-i], wk[-i]
        dc = abs(a["close"] - w["close"]) / a["close"] * 100
        dh = abs(a["high"] - w["high"]) / a["high"] * 100
        dl = abs(a["low"] - w["low"]) / a["low"] * 100
        tag = "✅" if max(dc, dh, dl) < 0.02 else "❌"
        if tag == "❌": errs += 1
        print(f"  {tag} {node['name']} 周{i}({w['date']}) close {w['close']} vs 聚合 {a['close']:.2f} | 高 {w['high']} vs {a['high']:.2f} | 低 {w['low']} vs {a['low']:.2f} | 日数 {a['n']}")
    if errs: bad += errs

print()
print("=" * 78)
print("D. 数据完整性结构检查（全 5 指数全历史）")
print("=" * 78)
for sym, node in d.items():
    ks = node["klines"]
    dates = [k["date"] for k in ks]
    iss = []
    if len(set(dates)) != len(dates):
        dup = [x for x in set(dates) if dates.count(x) > 1]
        iss.append(f"重复日期 {dup[:3]}")
    for k in ks:
        if not (k["low"] <= min(k["open"], k["close"]) and k["high"] >= max(k["open"], k["close"])):
            iss.append(f"{k['date']} OHLC 矛盾(O={k['open']} H={k['high']} L={k['low']} C={k['close']})"); break
        if k["volume"] <= 0:
            iss.append(f"{k['date']} 成交量<=0"); break
    # 交易日间隔检查
    gaps = []
    for a, b in zip(ks, ks[1:]):
        dd = (datetime.strptime(b["date"], "%Y-%m-%d") - datetime.strptime(a["date"], "%Y-%m-%d")).days
        if dd > 12: gaps.append(f"{a['date']}→{b['date']}({dd}天)")
    print(f"  {node['name']}: {len(ks)} 根 {ks[0]['date']}~{ks[-1]['date']} | 结构问题 {iss if iss else '无'} | 长休市缺口 {gaps if gaps else '无'}")

print()
print("A/B/C 严重不符计数:", bad)
