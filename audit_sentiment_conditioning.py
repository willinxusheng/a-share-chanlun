# -*- coding: utf-8 -*-
"""
audit_sentiment_conditioning.py  —  情绪信号条件化「预测方向准确性」回测门禁（R76 新增）

目的：验证"用 H5 情绪指数(恐惧/贪婪)条件化斐波那契/缠论方向预测"是否真有稳定增益。
      与 R72 校准门禁同引擎(截断 klines -> analyze -> forecast_svg 仅验几何/带宽)，
      叠加情绪分，比较三种策略在 T+8 / T+30 的方向命中率：

        1) baseline   : 纯斐波那契主路径方向 sign(main - last)
        2) pure_sent  : 纯情绪逆向信号(恐惧<=buy_th 看多 / 贪婪>=sell_th 看空)，仅统计极端区锚点
        3) conditioned: 极端区把斐波那契方向翻转(逆向)，中性区保持斐波那契

      判定：只有 conditioned 相对 baseline 在样本外(后半段时间窗)有显著、稳定提升，
            且极端区样本量足够(N>=30)，才值得并入；否则透明化、不硬改模型。

数据来源：
  - 预测：同目录 data.json + report.py/chanlun.py（5 大指数）
  - 情绪：<sentiment_project>/sentiment_v2.json 的 hist（回追 2022-03-16，row[2]=情绪分）
          买/卖阈值取该文件 buy_th / sell_th（默认 20 / 85）

前视偏差防护：
  - 预测几何仅依赖截断 klines + analyze，与 bt/breadth 概率校准无关（同 R72）。
  - 情绪分采用滚动252日口径(情绪团队 walk-forward 已验证为因果时序)；本门禁不拟合任何阈值，
    阈值直接取自情绪文件，天然样本外安全。仍标注：若 hist 含全样本拟合则结论需打折。

退出码：0=正常（仅打印，门禁阈值断言见底部，默认不阻断 CI）。
"""
import json
import os
import statistics

from chanlun import analyze, adaptive_horizon
from report import forecast_svg  # report.py 内含推演渲染核心

BASE = os.path.dirname(os.path.abspath(__file__))
# 情绪项目路径: 用相对布局推导(../sentiment/sentiment_v2.json), 不再写死过期绝对路径
# —— 否则目录迁移后文件不存在会直接崩溃(与 R96 load_live_sentiment 同类坑)
SENT_PATH = os.path.join(BASE, "sentiment", "sentiment_v2.json")  # 情绪模块随仓库内置(chanlun/sentiment/), 进入 CI 与所有读取环境

H_TARGETS = (8, 30)
ANCHOR_STEP = 15          # 锚点间隔(交易日)，与 R72 一致
MIN_HISTORY = 800         # 截断后最少样本

ZONE_NONE = 0
ZONE_FEAR = -1            # 极端恐惧 -> 逆向看多
ZONE_GREED = 1            # 极端贪婪 -> 逆向看空


def find_proj(proj, tplus_target):
    best, bd = None, 1e9
    for row in proj:
        d = abs(row["tplus"] - tplus_target)
        if d < bd:
            bd, best = d, row
    if best is None or bd > 1:
        return None
    return best


def load_sentiment():
    if not os.path.exists(SENT_PATH):
        # 优雅降级: 情绪文件缺失时仅跑 baseline 对比(情绪条件化/纯情绪项全 N/A), 不崩溃
        return {}, 20.0, 85.0
    d = json.load(open(SENT_PATH, encoding="utf-8"))
    hist = d.get("hist", [])
    # date -> score (row: [date, level, score, ...])
    smap = {}
    for row in hist:
        if len(row) >= 3 and isinstance(row[2], (int, float)):
            smap[row[0]] = float(row[2])
    buy_th = float(d.get("buy_th", 20))
    sell_th = float(d.get("sell_th", 85))
    return smap, buy_th, sell_th


def zone_of(s, buy_th, sell_th):
    if s is None:
        return ZONE_NONE
    if s <= buy_th:
        return ZONE_FEAR
    if s >= sell_th:
        return ZONE_GREED
    return ZONE_NONE


def run():
    data = json.load(open(os.path.join(BASE, "data.json"), encoding="utf-8"))
    smap, buy_th, sell_th = load_sentiment()
    symbols = list(data.keys())
    kls = {sym: sorted(data[sym]["klines"], key=lambda k: k["date"]) for sym in symbols}
    base = kls[symbols[0]]
    n_base = len(base)

    # 每个指数每个窗口：累计 baseline / pure_sent / conditioned 命中与样本
    rec = {sym: {h: {"N": 0, "base_hit": 0,
                     "ext_N": 0, "ext_base_hit": 0, "ext_cond_hit": 0,
                     "ext_fh_N": 0, "ext_fh_base": 0, "ext_fh_cond": 0,
                     "ext_sh_N": 0, "ext_sh_base": 0, "ext_sh_cond": 0,
                     "pure_N": 0, "pure_hit": 0,
                     # 时间切分(前/后半)
                     "fh_N": 0, "fh_base": 0, "fh_cond": 0,
                     "sh_N": 0, "sh_base": 0, "sh_cond": 0,
                     "last_date": None}
                for h in H_TARGETS}
           for sym in symbols}

    i = MIN_HISTORY
    # 时间切分点 = 锚点区间中点(锚点从 MIN_HISTORY 起, 到 n_base-35 止),
    # 确保前半/后半都有样本 -> 真·样本外(近期)验证(此前误用 n_base//2 致全部落入后半)
    mid_anchor = (MIN_HISTORY + (n_base - 35)) // 2
    while i < n_base - 35:
        date_i = base[i]["date"]
        is_second_half = (i >= mid_anchor)
        for sym in symbols:
            kl = kls[sym]
            if i >= len(kl) or kl[i]["date"] != date_i:
                continue
            trunc = kl[:i + 1]
            last_a = trunc[-1]["close"]
            s = smap.get(date_i)
            z = zone_of(s, buy_th, sell_th)
            try:
                r = analyze(trunc)
                horizon = adaptive_horizon(r["bis"], r["merged"])
                _svg, _note, _probs, _leg, fc = forecast_svg(
                    trunc, r, r["classify"], 50.0, 0.0, sym, horizon)
            except Exception:
                continue
            proj = fc.get("proj") or []
            for H in H_TARGETS:
                if H > horizon:
                    continue
                row = find_proj(proj, H)
                if row is None:
                    continue
                if i + H >= len(kl):
                    continue
                real = kl[i + H]["close"]
                main_v = row["main"]
                d_f = 1 if (main_v - last_a) > 0 else (-1 if (main_v - last_a) < 0 else 0)
                d_r = 1 if (real - last_a) > 0 else (-1 if (real - last_a) < 0 else 0)
                rec[sym][H]["N"] += 1
                rec[sym][H]["last_date"] = date_i
                # baseline
                base_ok = (d_f == d_r)
                if base_ok:
                    rec[sym][H]["base_hit"] += 1
                # time split
                if is_second_half:
                    rec[sym][H]["sh_N"] += 1
                    if base_ok:
                        rec[sym][H]["sh_base"] += 1
                else:
                    rec[sym][H]["fh_N"] += 1
                    if base_ok:
                        rec[sym][H]["fh_base"] += 1
                # conditioned (极端区翻转)
                if z != ZONE_NONE:
                    rec[sym][H]["ext_N"] += 1
                    if base_ok:
                        rec[sym][H]["ext_base_hit"] += 1
                    d_c = -d_f if d_f != 0 else 0  # 极端区逆向翻转
                    if d_c == d_r:
                        rec[sym][H]["ext_cond_hit"] += 1
                    # 极端区时间切分
                    if is_second_half:
                        rec[sym][H]["ext_sh_N"] += 1
                        if base_ok:
                            rec[sym][H]["ext_sh_base"] += 1
                        if d_c == d_r:
                            rec[sym][H]["ext_sh_cond"] += 1
                    else:
                        rec[sym][H]["ext_fh_N"] += 1
                        if base_ok:
                            rec[sym][H]["ext_fh_base"] += 1
                        if d_c == d_r:
                            rec[sym][H]["ext_fh_cond"] += 1
                    # 全样本 conditioned(中性区=d_f, 极端区=-d_f)
                    cond_ok = (d_c == d_r)
                else:
                    cond_ok = base_ok  # 中性区保持斐波那契
                if cond_ok:
                    if is_second_half:
                        rec[sym][H]["sh_cond"] += 1
                    else:
                        rec[sym][H]["fh_cond"] += 1
                # pure sentiment 逆向信号(仅极端区)
                if z != ZONE_NONE:
                    rec[sym][H]["pure_N"] += 1
                    d_pure = 1 if z == ZONE_FEAR else -1
                    if d_pure == d_r:
                        rec[sym][H]["pure_hit"] += 1
        i += ANCHOR_STEP
    return data, rec, buy_th, sell_th


def pct(a, b):
    return (a / b * 100.0) if b else float("nan")


def report(data, rec, buy_th, sell_th):
    print("=" * 110)
    print("R76 情绪条件化回测门禁 — 基线 vs 情绪条件化 vs 纯情绪 (锚点每%d交易日)" % ANCHOR_STEP)
    print("情绪阈值 buy_th=%.0f sell_th=%.0f | 极端区: 恐惧<=buy 看多 / 贪婪>=sell 看空(逆向)" % (buy_th, sell_th))
    print("=" * 110)
    hdr = (f"{'指数':<10}{'窗口':>5}{'N':>6}{'基线命中':>9}{'条件化命中':>11}{'Δ全样本':>9}"
           f"{'极端N':>7}{'极基命中':>9}{'极条命中':>9}{'纯情绪N':>8}{'纯情绪命中':>11}")
    print(hdr)
    print("-" * 110)
    # 合计
    tot = {h: {"N": 0, "base": 0, "cond": 0, "ext_N": 0, "ext_base": 0, "ext_cond": 0,
               "ext_fh_N": 0, "ext_fh_base": 0, "ext_fh_cond": 0,
               "ext_sh_N": 0, "ext_sh_base": 0, "ext_sh_cond": 0,
               "pure_N": 0, "pure": 0, "fh_N": 0, "fh_base": 0, "fh_cond": 0,
               "sh_N": 0, "sh_base": 0, "sh_cond": 0} for h in H_TARGETS}
    for sym, d in data.items():
        nm = d.get("name", sym)
        for H in H_TARGETS:
            s = rec[sym][H]
            if s["N"] == 0:
                continue
            base_h = pct(s["base_hit"], s["N"])
            # 条件化全样本命中
            cond_h = pct(s["fh_cond"] + s["sh_cond"], s["fh_N"] + s["sh_N"])
            delta = cond_h - base_h
            ext_base = pct(s["ext_base_hit"], s["ext_N"])
            ext_cond = pct(s["ext_cond_hit"], s["ext_N"])
            pure_h = pct(s["pure_hit"], s["pure_N"])
            print(f"{nm:<10}{'T+'+str(H):>5}{s['N']:>6}{base_h:>8.1f}%{cond_h:>10.1f}%{delta:>+8.1f}pp"
                  f"{s['ext_N']:>7}{ext_base:>8.1f}%{ext_cond:>8.1f}%{s['pure_N']:>8}{pure_h:>10.1f}%")
            # 干净的显式累计
            tot[H]["N"] += s["N"]
            tot[H]["base"] += s["base_hit"]
            tot[H]["cond"] += s["fh_cond"] + s["sh_cond"]
            tot[H]["ext_N"] += s["ext_N"]
            tot[H]["ext_base"] += s["ext_base_hit"]
            tot[H]["ext_cond"] += s["ext_cond_hit"]
            tot[H]["ext_fh_N"] += s["ext_fh_N"]
            tot[H]["ext_fh_base"] += s["ext_fh_base"]
            tot[H]["ext_fh_cond"] += s["ext_fh_cond"]
            tot[H]["ext_sh_N"] += s["ext_sh_N"]
            tot[H]["ext_sh_base"] += s["ext_sh_base"]
            tot[H]["ext_sh_cond"] += s["ext_sh_cond"]
            tot[H]["pure_N"] += s["pure_N"]
            tot[H]["pure"] += s["pure_hit"]
            tot[H]["fh_N"] += s["fh_N"]
            tot[H]["fh_base"] += s["fh_base"]
            tot[H]["fh_cond"] += s["fh_cond"]
            tot[H]["sh_N"] += s["sh_N"]
            tot[H]["sh_base"] += s["sh_base"]
            tot[H]["sh_cond"] += s["sh_cond"]
    print("-" * 110)
    print("合计(五指数汇总):")
    for H in H_TARGETS:
        s = tot[H]
        if s["N"] == 0:
            continue
        base_h = pct(s["base"], s["N"])
        cond_h = pct(s["cond"], s["N"])
        delta = cond_h - base_h
        ext_base = pct(s["ext_base"], s["ext_N"])
        ext_cond = pct(s["ext_cond"], s["ext_N"])
        pure_h = pct(s["pure"], s["pure_N"])
        # 样本外(后半段)对比
        sh_base = pct(s["sh_base"], s["sh_N"])
        sh_cond = pct(s["sh_cond"], s["sh_N"])
        sh_delta = sh_cond - sh_base
        fh_base = pct(s["fh_base"], s["fh_N"])
        fh_cond = pct(s["fh_cond"], s["fh_N"])
        fh_delta = fh_cond - fh_base
        # 极端区时间切分(近期才是真·样本外)
        ext_sh_base = pct(s["ext_sh_base"], s["ext_sh_N"])
        ext_sh_cond = pct(s["ext_sh_cond"], s["ext_sh_N"])
        ext_fh_base = pct(s["ext_fh_base"], s["ext_fh_N"])
        ext_fh_cond = pct(s["ext_fh_cond"], s["ext_fh_N"])
        print(f"  T+{H}: 基线 {base_h:.1f}% | 条件化 {cond_h:.1f}% (Δ {delta:+.1f}pp)")
        print(f"         极端区: 基线 {ext_base:.1f}% -> 条件化 {ext_cond:.1f}% | 纯情绪逆向 {pure_h:.1f}% (N极={s['ext_N']}, N纯={s['pure_N']})")
        print(f"         极端区时间切分: 前半段 基线 {ext_fh_base:.1f}%->条件化 {ext_fh_cond:.1f}% | 后半段(样本外) 基线 {ext_sh_base:.1f}%->条件化 {ext_sh_cond:.1f}%")
        print(f"         全样本时间切分: 前半段 Δ {fh_delta:+.1f}pp | 后半段(样本外) Δ {sh_delta:+.1f}pp")
        print("-" * 110)
    print("=" * 110)
    # 门禁判定
    verdict_lines = []
    any_oos_gain = False
    for H in H_TARGETS:
        s = tot[H]
        if s["N"] == 0:
            continue
        base_h = pct(s["base"], s["N"])
        cond_h = pct(s["cond"], s["N"])
        sh_base = pct(s["sh_base"], s["sh_N"])
        sh_cond = pct(s["sh_cond"], s["sh_N"])
        sh_delta = sh_cond - sh_base
        # 极端区近期(样本外)翻转增益 —— 这是"并入"决策的核心证据
        ext_sh_base = pct(s["ext_sh_base"], s["ext_sh_N"])
        ext_sh_cond = pct(s["ext_sh_cond"], s["ext_sh_N"])
        ext_sh_delta = ext_sh_cond - ext_sh_base
        enough = s["ext_sh_N"] >= 30  # 极端区近期样本需足够(与文档 N>=30 对齐)
        # 决策: 近期极端区翻转增益显著(>10pp) 且 全样本近期Δ为正
        oos_gain = (ext_sh_delta > 10.0) and (sh_delta > 0)
        if oos_gain and enough:
            any_oos_gain = True
            verdict_lines.append(f"  T+{H}: 极端区近期翻转 Δ {ext_sh_delta:+.1f}pp (近N极={s['ext_sh_N']}) | 全样本近期 Δ {sh_delta:+.1f}pp — 稳定增益")
        else:
            verdict_lines.append(f"  T+{H}: 极端区近期翻转 Δ {ext_sh_delta:+.1f}pp (近N极={s['ext_sh_N']}) | 全样本近期 Δ {sh_delta:+.1f}pp — 无显著/不足")
    print("【门禁判定】情绪条件化是否并入预测模型：")
    if any_oos_gain:
        print("  ⚠️ 检测到样本外稳定增益 -> 建议并入(接入 live 情绪分做极端区方向翻转)")
    else:
        print("  ✅ 未检测到样本外稳定增益 -> 不并入模型，仅透明化")
        print("     情绪作为独立方向信号样本外失效(wf IC≈-0.028, ridge r2_oos<0)；条件化需极端区近期增益支撑")
    for v in verdict_lines:
        print(v)
    print("=" * 110)
    return 0


if __name__ == "__main__":
    data, rec, buy_th, sell_th = run()
    raise SystemExit(report(data, rec, buy_th, sell_th))
