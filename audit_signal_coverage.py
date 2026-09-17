#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁：标注覆盖率（R478 新增）。

**要防的回归**：引擎算出了买卖点，但产物里那条标注**静默消失**。历史上这一类已经咬过两次：
  · R472 之前：`cutoff = n-500` + 「同方向 55 日跳过」两道**按日期砍信息**的过滤，
    让实测 37 条买卖点从未进产物 —— 用户主观感受即「该买该卖的点没标出来」；
  · R476：`_chipObs` 读错 `getOption().graphic` 的形态，障碍框恒为 null（改了等于没改），
    只有靠门禁读数才发现。
两次都不是崩溃、没有异常，**只是图上少了东西**。本门禁把「产出 == 引擎结果」变成硬断言。

判据（对每个主图块）：
  ① 产物 `sigPoints` / `segSigPoints` 里的每一条都必须有非空 label.value 且 coord 是真实日期；
  ② 用产物自带的 OHLC + volume 重建 K 线、跑一次**生产** analyze()，比对
     `len(signals)` == `len(sigPoints)`，以及段级：`len(segSigPoints)` + 合并数 == `len(seg_signals)`；
  ③ (date, price) 集合必须**逐一对应**（不只是条数相等 —— 条数相等但内容错位一样是坏）。
     R479 起段级与笔级**同坐标同向**者会被合并进笔级标签（`08-25 2买` → `08-25 2买·段2买`），
     这类必须在对应笔级 label.value 里**找到自己的级别标记**才算覆盖到 —— 合并逻辑若静默吞掉
     一条，第③步会直接判 FAIL，而不是靠"条数刚好对上"蒙过去。
任一不符 ⇒ 退出码 1，阻断 CI。

独立可跑：python3 audit_signal_coverage.py [report.html]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "report.html")
sys.path.insert(0, HERE)
from chanlun import analyze  # noqa: E402

EXPECT_SYMS = {"sh000001", "sh000300", "sz399001", "sz399006", "sh000905"}


def seg_kind_short():
    """段级 kind[:3] → 图上显示串的映射。

    ★ **单一来源**：直接从 report.py 的 `_KIND_SHORT` 里筛出以「段」开头的键。
    R480 之前这里是**硬编码的 {段一买, 段一卖, 段二买, 段二卖}** ⇒ 新增段三后，18 条
    **已经正确合并**进笔级标签的段三点被误判成「未进产物 / 引擎有产物无」，门禁假 FAIL。
    同义词表一旦硬编码，就必然随新增级别而失效 —— 这类"改 A 漏改 B"是本项目的高频坑。
    导入 report 是安全的：它只有 `if __name__ == "__main__"` 守卫，模块级只定义常量。
    异常时**显式报错**而不是退化成空表 —— 空表会让门禁在错误前提下给出 PASS。
    """
    try:
        import report as _rp            # noqa: PLC0415 —— 延迟导入：避免与门禁自身形成循环
    except Exception as e:              # pragma: no cover
        raise RuntimeError(
            f"无法导入 report.py 读取 _KIND_SHORT（{e}）—— 段级合并识别不可靠，拒绝出结论"
        ) from e
    m = {k: v for k, v in getattr(_rp, "_KIND_SHORT", {}).items() if k.startswith("段")}
    if not m:
        raise RuntimeError("report.py 的 _KIND_SHORT 里没有任何「段*」键 —— 映射表结构已变，门禁需同步")
    return m


def parse_blocks(html):
    """抽出所有 `var D = {...}` 块（主图/预测图交错）。"""
    out = []
    for m in re.finditer(r"var D = ", html):
        try:
            d, _ = json.JSONDecoder().raw_decode(html[m.end():])
            out.append(d)
        except Exception:
            pass
    return out


def main():
    if not os.path.exists(HTML):
        print(f"[FATAL] 找不到产物 {HTML}")
        return 1
    html = open(HTML, encoding="utf-8").read()
    mains = [d for d in parse_blocks(html) if "sigPoints" in d]
    print(f"[coverage] 产物 {HTML}")
    print(f"[coverage] 主图块 {len(mains)} 个（期望 {len(EXPECT_SYMS)}）")
    if len(mains) != len(EXPECT_SYMS):
        print(f"[FAIL] 主图块数不符：{len(mains)} != {len(EXPECT_SYMS)}")
        return 1

    total_bad = 0
    grand = {"sig": 0, "seg": 0}
    for bi, d in enumerate(mains):
        dates, ohlc, vol = d["dates"], d["ohlc"], d["volume"]
        n = len(dates)
        if not (len(ohlc) == len(vol) == n):
            print(f"[FAIL] #{bi} 长度不一致 dates={n} ohlc={len(ohlc)} vol={len(vol)}")
            total_bad += 1
            continue
        ds = set(dates)
        sig, seg = d.get("sigPoints") or [], d.get("segSigPoints") or []
        # ① 结构自检
        for nm, arr in (("sigPoints", sig), ("segSigPoints", seg)):
            for p in arr:
                c = p.get("coord") or [None, None]
                if not (p.get("value") or "").strip():
                    print(f"[FAIL] #{bi} {nm} 有空白 label：{c}")
                    total_bad += 1
                if c[0] not in ds:
                    print(f"[FAIL] #{bi} {nm} coord 日期不存在于 dates：{c}")
                    total_bad += 1
        # ② 引擎重算对拍（用产物自带数据重建 K 线）
        kl = [{"date": dates[i], "open": ohlc[i][0], "close": ohlc[i][1],
               "low": ohlc[i][2], "high": ohlc[i][3], "volume": vol[i]} for i in range(n)]
        r = analyze(kl)
        e_sig, e_seg = r["signals"], r.get("seg_signals") or []
        grand["sig"] += len(e_sig); grand["seg"] += len(e_seg)

        def _keyset(ss):
            return {(s["date"], round(s["price"], 2)) for s in ss}

        a = _keyset(e_sig)
        b = {(p["coord"][0], round(p["coord"][1], 2)) for p in sig}
        miss_p, miss_e = sorted(a - b), sorted(b - a)
        # ③a 笔级：(条数 + 集合) 双判 —— 条数相等但内容错位一样是坏
        ok_sig = (len(sig) == len(e_sig)) and not miss_p and not miss_e

        # ③b 段级：引擎结果里**与笔级同坐标同向**的那些，在产物里被**合并进笔级标签**
        # （R479，如 `08-25 2买` + `段2买` → `08-25 2买·段2买`）—— 它们不该各自成点，
        # 但**必须**能在对应笔级标签里找到自己的级别标记；其余仍须各自成点。
        # ★ 这比"只比条数"更强：合并逻辑若静默吞掉一条，这里会直接判 FAIL。
        # ★ 映射取**单一来源**（report.py 的 _KIND_SHORT）—— 硬编码副本会随新增级别静默失效
        _seg_short = seg_kind_short()
        _val_at = {(p["coord"][0], round(p["coord"][1], 2)): (p.get("value") or "") for p in sig}
        own_seg, merged_seg = [], []
        for s in e_seg:
            _k = (s["date"], round(s["price"], 2))
            _tag = _seg_short.get(s["kind"][:3])
            if _tag and _tag in _val_at.get(_k, ""):
                merged_seg.append(s)
            else:
                own_seg.append(s)
        c = _keyset(own_seg)
        d2 = {(p["coord"][0], round(p["coord"][1], 2)) for p in seg}
        mseg_p, mseg_e = sorted(c - d2), sorted(d2 - c)
        ok_seg = (len(seg) == len(own_seg)) and not mseg_p and not mseg_e

        if not ok_sig or not ok_seg:
            total_bad += 1
            print(f"[FAIL] #{bi} 笔级 引擎 {len(e_sig)} vs 产物 {len(sig)}；"
                  f"段级 引擎 {len(e_seg)} vs 产物自带点 {len(seg)} + 合并进笔级 {len(merged_seg)}"
                  f" = {len(seg) + len(merged_seg)}")
            for _nm, _mp, _me in (("笔级", miss_p, miss_e), ("段级", mseg_p, mseg_e)):
                if _mp:
                    print(f"       {_nm} 引擎有/产物无 {len(_mp)} 条：{_mp[:6]}")
                if _me:
                    print(f"       {_nm} 产物有/引擎无 {len(_me)} 条：{_me[:6]}")
        else:
            print(f"[ok ] #{bi} n={n} 笔级 {len(sig)} 条 / 段级 {len(seg)} 条"
                  f"（另 {len(merged_seg)} 条已合并进笔级标签）—— 与引擎逐条对应")

    print(f"[coverage] 引擎合计 笔级 {grand['sig']} 条 / 段级 {grand['seg']} 条")
    if total_bad:
        print(f"[coverage] FAIL 共 {total_bad} 处不一致")
        return 1
    print("[coverage] PASS 标注覆盖完整（产物 == 引擎）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
