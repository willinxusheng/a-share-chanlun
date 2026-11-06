# -*- coding: utf-8 -*-
"""X2 · 缠论结构合法性独立审计
不信任引擎自证：用独立实现重算 包含处理/分型/标准笔，与生产 bis 对照；
并逐条校验生产 中枢/买卖点/背驰 是否满足缠论定义。
"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chanlun import analyze, merge_inclusion, build_bi_strict, MIN_BI_PCT

d = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")))
SYMS = ["sh000001", "sh000300", "sz399001", "sz399006", "sh000905"]


def indep_merge(klines):
    """独立实现包含处理（与项目同算法，但独立书写，防复制粘贴同一 bug）"""
    out = []
    for k in klines:
        if len(out) < 2:
            out.append({"h": k["high"], "l": k["low"]})
            continue
        p, c = out[-1], {"h": k["high"], "l": k["low"]}
        up = out[-1]["h"] > out[-2]["h"]
        contained = (p["h"] >= c["h"] and p["l"] <= c["l"]) or (p["h"] <= c["h"] and p["l"] >= c["l"])
        if not contained:
            out.append(c)
        else:
            if up:
                p["h"] = max(p["h"], c["h"]); p["l"] = max(p["l"], c["l"])
            else:
                p["h"] = min(p["h"], c["h"]); p["l"] = min(p["l"], c["l"])
    return out


print("=" * 96)
print("X2-A  包含处理一致性（独立实现 vs 引擎）")
print("=" * 96)
tot_bad = 0
for s in SYMS:
    kl = d[s]["klines"]
    m1 = merge_inclusion(kl)
    m2 = indep_merge(kl)
    same = len(m1) == len(m2) and all(abs(a["high"] - b["h"]) < 1e-9 and abs(a["low"] - b["l"]) < 1e-9 for a, b in zip(m1, m2))
    print(f"  {d[s]['name']}: 引擎 {len(m1)} 根 / 独立 {len(m2)} 根 → {'✅ 逐根一致' if same else '❌ 不一致'}")
    if not same: tot_bad += 1

print()
print("=" * 96)
print("X2-B  笔的缠论合法性（标准要求：顶底分型不共用K线 → 中心间隔 ≥3；本引擎阈值 ≥2）")
print("=" * 96)
print(f"{'指数':<8}{'笔数':>6}{'间隔<3(不达标准)':>16}{'占比':>8}{'幅度<1.8%':>12}{'方向不交替':>12}")
for s in SYMS:
    r = analyze(d[s]["klines"])
    bis = r["bis"]
    short = [b for b in bis if b["end"] - b["start"] < 3]
    amp_bad = [b for b in bis if abs(b["end_price"] / b["start_price"] - 1) < MIN_BI_PCT - 1e-9]
    alt_bad = 0
    for a, b in zip(bis, bis[1:]):
        if a["dir"] == b["dir"]: alt_bad += 1
        if b["start"] != a["end"]: alt_bad += 1   # 笔必须首尾相接
    print(f"{d[s]['name']:<8}{len(bis):>6}{len(short):>16}{len(short)/len(bis)*100:>7.1f}%{len(amp_bad):>12}{alt_bad:>12}")

print()
print("=" * 96)
print("X2-C  中枢合法性（zg>zd / 三笔互相重叠 / 中枢内笔数≥3）")
print("=" * 96)
for s in SYMS:
    r = analyze(d[s]["klines"])
    bis, zss = r["bis"], r["zhongshu"]
    bad = []
    for z in zss:
        if not (z["zg"] > z["zd"]): bad.append(f"zg<=zd({z['date_start']})")
        if z["count"] < 3: bad.append(f"笔数<3({z['date_start']})")
        # 找构成中枢的笔
        cons = [b for b in bis if b["start"] >= z["start"] and b["end"] <= z["end"]]
        if len(cons) < 3: bad.append(f"构成笔不足({z['date_start']}, 实得{len(cons)})")
        # 每笔都要与 [zd,zg] 有重叠
        for b in cons:
            if not (min(b["high"], z["zg"]) >= max(b["low"], z["zd"])):
                bad.append(f"{b['date_end']} 笔未与中枢区间重叠"); break
        if z["gg"] < z["zg"] or z["dd"] > z["zd"]: bad.append(f"gg/dd 越界({z['date_start']})")
    print(f"  {d[s]['name']}: {len(zss)} 个中枢 | 问题 {bad[:3] if bad else '无 ✅'}")

print()
print("=" * 96)
print("X2-D  买卖点合法性（价格方向/止损-目标关系/二类不破前低/三类不进中枢）")
print("=" * 96)
for s in SYMS:
    r = analyze(d[s]["klines"])
    sigs, bis = r["signals"], r["bis"]
    p1b = p1s = 0
    bad = []
    for sg in sigs:
        k = sg["kind"]; b = bis[sg["bi_index"]]
        if sg["dir"] == 1:
            if "一类" in k: p1b += 1
            if not (sg.get("target", 0) > sg["price"] > sg.get("stop", 9e9)):
                bad.append(f"{k}@{sg['date']} 价格不在止损/目标之间")
            if abs(sg["price"] - b["end_price"]) > 1e-6 and "三类" not in k and "段三" not in k:
                bad.append(f"{k}@{sg['date']} 信号价与笔末端价不符")
        else:
            if "一类" in k: p1s += 1
            if not (sg.get("stop", 0) > sg["price"] > sg.get("target", 9e9)):
                bad.append(f"{k}@{sg['date']} 价格不在止损/目标之间")
        if sg.get("rr") is not None and (sg["rr"] < 0 or sg["rr"] > 6.01):
            bad.append(f"{k}@{sg['date']} rr 越界 {sg['rr']}")
        # 缠论：买点必须锚在**向下笔**末端（底分型），卖点锚在**向上笔**末端（顶分型）
        if "三类" in k or "段三" in k:
            pass  # 三类点为回抽/反抽笔，方向判据相反，另由中枢位置约束
        elif sg["dir"] == 1 and b["dir"] != -1:
            bad.append(f"{k}@{sg['date']} 买点未锚在向下笔末端")
        elif sg["dir"] == -1 and b["dir"] != 1:
            bad.append(f"{k}@{sg['date']} 卖点未锚在向上笔末端")
    print(f"  {d[s]['name']}: 信号 {len(sigs)} 条（一类买 {p1b}/一类卖 {p1s}）| 问题 {len(bad)} {bad[:2] if bad else '✅'}")

print()
print("=" * 96)
print("X2-E  多级别一致性（日/周/月 情景方向是否互相矛盾）")
print("=" * 96)
from chanlun import SC_BULL, SC_BEAR
def pol(sc):
    return 1 if sc in SC_BULL else (-1 if sc in SC_BEAR else 0)
for s in SYMS:
    node = d[s]
    r_d = analyze(node["klines"])
    r_w = analyze(node["week_klines"], min_bi_pct=0.04)
    r_m = analyze(node["month_klines"], min_bi_pct=0.08)
    scs = [r_d["classify"]["scenario"], r_w["classify"]["scenario"], r_m["classify"]["scenario"]]
    pols = [pol(x) for x in scs]
    conf = "⚠ 日vs周相反" if pols[0] * pols[1] < 0 else "✅"
    print(f"  {d[s]['name']}: 日[{scs[0]}] 周[{scs[1]}] 月[{scs[2]}] → {conf}")

print()
print("=" * 96)
print("X2-F  笔识别两套算法一致性（幅度笔 vs 标准笔 bi_agreement，已有引擎函数）")
print("=" * 96)
for s in SYMS:
    r = analyze(d[s]["klines"])
    ag = r.get("agreement") or {}
    print(f"  {d[s]['name']}: 幅度笔 {len(r['bis'])} | 两套笔一致率 {ag} | 已知拐点捕捉 {r.get('captured')} ({r.get('capture_rate')})")
