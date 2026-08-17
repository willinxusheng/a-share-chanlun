# -*- coding: utf-8 -*-
"""生成自包含 HTML 缠论分析报告（内嵌 SVG，浅色主题，涨红跌绿）v6
新增：成交量面板、双法一致性、结构健康度、推演置信度、已知拐点捕捉、原则化推演"""
import json
import os
import math
from datetime import datetime, timedelta
from chanlun import analyze, backtest_signals, MIN_BI_PCT_WEEK, health_score, forecast_confidence, forward_vol, adaptive_horizon, classify, realized_vol_annualized, KNOWN_PIVOTS, _date_diff, MIN_BI_PCT_MONTH, backtest_robustness, backtest_paths, _path_targets, market_breadth

W, H_PRICE, H_VOL, H_MACD = 1060, 360, 64, 110
PAD_L, PAD_R, PAD_T, PAD_B = 12, 78, 24, 26
CHART_TOTAL = PAD_T + H_PRICE + 8 + H_VOL + 8 + H_MACD + PAD_B  # 600

RED, GREEN, GOLD = "#e54545", "#18a058", "#d4a017"
BLUE, GRAY, INK = "#2b6cb0", "#94a3b8", "#1f2937"


def _label_w(t):
    """估算标签像素宽度（与 verify_overlap.js 的字宽口径一致），用于确定性去重叠。"""
    w = 0.0
    for ch in str(t):
        w += 11.0 if ord(ch) > 0x2e80 else 6.16
    return w + 2.2


def dedup_mark_labels(items, n, y_min, y_max, plot_w, plot_h, grid_l, grid_t,
                      idx_map, default_pos="top"):
    """确定性地去重叠 markPoint 标签：按 x 排序，贪心保留，与已保留标签框重叠的则隐藏。
    仅修改各 item 的 label['show']，不改变标记符号。"""
    if n <= 0 or (y_max - y_min) == 0:
        return
    bar = plot_w / n
    ppx = plot_h / (y_max - y_min)

    def y_of(p, pos):
        yy = grid_t + (y_max - p) * ppx
        return yy - 11 if pos == "top" else yy + 11

    recs = []
    for it in items:
        c = it.get("coord")
        if not c:
            continue
        xi = idx_map.get(c[0]) if isinstance(c[0], str) else c[0]
        if xi is None:
            continue
        lab = it.get("label") or {}
        pos = lab.get("position", default_pos)
        recs.append({"x": grid_l + xi * bar, "yc": y_of(c[1], pos),
                     "w": _label_w(it.get("value", "")), "p": it})
    recs.sort(key=lambda d: d["x"])
    kept = []
    for m in recs:
        if any(abs(m["x"] - k["x"]) < (m["w"] + k["w"]) / 2 + 2 and
               abs(m["yc"] - k["yc"]) < 13 for k in kept):
            m["p"]["label"]["show"] = False
        else:
            kept.append(m)

IDX_COLORS = {
    "sh000001": "#2b6cb0",
    "sh000300": "#7c3aed",
    "sz399001": "#0d9488",
    "sz399006": "#e54545",
    "sh000905": "#d97706",
}

SCENARIO_COLOR = {
    "多头延续": RED, "背驰见底机会": RED,
    "中枢震荡偏多": "#d97706", "高位整理未破前高": "#d97706",
    "中枢震荡偏空": "#0d9488", "弱势反弹": "#0d9488", "反弹未回中枢": "#0d9488",
    "空头延续": GREEN, "背驰见顶风险": GREEN, "震荡待方向": "#64748b",
    "无中枢·向上笔": RED, "无中枢·向下笔": GREEN,
}

# 牛/熊情景集合（用于跨指数市场宽度统计与系统性环境判断）
SC_BULL = ("多头延续", "中枢震荡偏多", "高位整理未破前高", "背驰见底机会")
SC_BEAR = ("背驰见顶风险", "中枢震荡偏空", "弱势反弹", "反弹未回中枢", "空头延续")


def _fmt(v, nd=2):
    return ("%%.%df" % nd) % v


def _smooth(pts, tension=1.0, nd=3):
    """Catmull-Rom 样条 -> 三次贝塞尔路径，穿过所有数据点（细腻且不丢精度）。"""
    if len(pts) < 3:
        return "M" + " L".join(f"{x:.{nd}f} {y:.{nd}f}" for x, y in pts)
    p = pts
    d = f"M{p[0][0]:.{nd}f} {p[0][1]:.{nd}f}"
    for i in range(len(p) - 1):
        x0, y0 = p[i - 1] if i > 0 else p[i]
        x1, y1 = p[i]
        x2, y2 = p[i + 1]
        x3, y3 = p[i + 2] if i + 2 < len(p) else p[i + 1]
        c1x = x1 + (x2 - x0) / 6.0 * tension
        c1y = y1 + (y2 - y0) / 6.0 * tension
        c2x = x2 - (x3 - x1) / 6.0 * tension
        c2y = y2 - (y3 - y1) / 6.0 * tension
        d += f" C{c1x:.{nd}f} {c1y:.{nd}f} {c2x:.{nd}f} {c2y:.{nd}f} {x2:.{nd}f} {y2:.{nd}f}"
    return d


# ================= 单指数主图（价格 + 成交量 + MACD） =================
def chart_svg(klines, r, sym, captured=None):
    n = len(klines)
    merged, bis, zss, hist = r["merged"], r["bis"], r["zhongshu"], r["hist"]
    dif, dea = r["dif"], r["dea"]

    closes = [k["close"] for k in klines]
    lo = min(min(k["low"] for k in klines), min(b["low"] for b in bis))
    hi = max(max(k["high"] for k in klines), max(b["high"] for b in bis))
    pad = (hi - lo) * 0.03
    lo, hi = lo - pad, hi + pad
    span = hi - lo or 1

    plot_w = W - PAD_L - PAD_R
    price_h = H_PRICE

    # 三个区域纵向坐标
    vtop = PAD_T + H_PRICE + 8
    vbot = vtop + H_VOL
    mtop = vbot + 8
    mbot = mtop + H_MACD

    def x(i):
        return PAD_L + plot_w * i / (n - 1)

    def y(v):
        return PAD_T + price_h * (1 - (v - lo) / span)

    # 分层：base=最外层(不缩放)；pg=图形(随窗口横向缩放)；lg=文字(位置JS重算,不变形)
    base, pg, lg = [], [], []

    # 背景 + 渐变 + 裁剪定义（最外层）
    base.append(f'<rect width="{W}" height="{CHART_TOTAL}" fill="#ffffff"/>')
    base.append(f'<defs><clipPath id="clip-{sym}"><rect x="{PAD_L}" y="0" width="{plot_w}" height="{CHART_TOTAL}"/></clipPath></defs>')

    # 年份分隔竖线 + 标签；季度细分竖线（更细时间参考）
    prev_q = None
    for i, k in enumerate(klines):
        yr = k["date"][:4]
        mo = int(k["date"][5:7])
        q = (mo - 1) // 3 + 1
        if (yr, q) != prev_q:
            prev_q = (yr, q)
            xx = x(i)
            pg.append(f'<line x1="{xx:.1f}" y1="{PAD_T}" x2="{xx:.1f}" y2="{mbot}" stroke="#eef2f7"/>')
            if q == 1:  # 仅年初标年份，季度线底部加短刻度，保持干净
                lg.append(f'<text data-i="{i}" data-dx="4" x="{xx + 4:.1f}" y="{mbot + 15}" font-size="14" font-weight="600" fill="{GRAY}">{yr}</text>')
            else:
                pg.append(f'<line x1="{xx:.1f}" y1="{mbot + 1:.1f}" x2="{xx:.1f}" y2="{mbot + 7:.1f}" stroke="{GRAY}" stroke-width="0.6"/>')

    # 价格区：横网格（5 主格 + 4 次格）+ 右侧刻度
    for i in range(9):
        v = lo + span * i / 8
        yy = y(v)
        if i % 2 == 0:
            pg.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#eef2f7"/>')
            lg.append(f'<text x="{W - PAD_R + 6}" y="{yy + 4:.1f}" font-size="13" font-weight="600" fill="{GRAY}">{v:.0f}</text>')
        else:
            pg.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#f4f7fb"/>')

    pg.append(f'<rect x="{PAD_L}" y="{PAD_T}" width="{plot_w}" height="{price_h}" fill="none" stroke="#e2e8f0"/>')
    # 现价水平参考线（淡灰虚线 + 右侧标签）：专业图标配，便于一眼比对「现在」相对 MA/中枢/缺口的位置
    _ycl = y(closes[-1])
    pg.append(f'<line x1="{PAD_L}" y1="{_ycl:.1f}" x2="{W - PAD_R}" y2="{_ycl:.1f}" stroke="#94a3b8" stroke-width="0.9" stroke-dasharray="3,3" stroke-opacity="0.65"/>')
    lg.append(f'<text x="{W - PAD_R + 6}" y="{_ycl + 4:.1f}" font-size="12" font-weight="700" fill="#64748b">现价 {closes[-1]:.0f}</text>')
    pg.append(f'<line x1="{PAD_L}" y1="{vtop}" x2="{W - PAD_R}" y2="{vtop}" stroke="#e2e8f0" stroke-dasharray="2,3"/>')

    # 中枢带（最近 8 个）
    for zs in zss[-8:]:
        x0 = x(merged[zs["start"]]["idx_start"])
        x1 = x(merged[zs["end"]]["idx_end"])
        y0, y1 = y(zs["zg"]), y(zs["zd"])
        pg.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{max(x1 - x0, 3):.1f}" height="{y1 - y0:.1f}" fill="{BLUE}" fill-opacity="0.10" stroke="{BLUE}" stroke-opacity="0.45" stroke-dasharray="4,3"/>')

    # 最后中枢 ZG/ZD 金色虚线（标签做垂直防重叠）
    if zss:
        zs = zss[-1]
        _zz = sorted([(y(zs["zg"]), "ZG", zs["zg"]), (y(zs["zd"]), "ZD", zs["zd"])])
        _placed = []
        for yy, lab, val in _zz:
            for py in _placed:
                if abs(yy - py) < 14:
                    yy = (py + 14) if yy >= py else (py - 14)
            _placed.append(yy)
            pg.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="{GOLD}" stroke-width="1.2" stroke-dasharray="6,4"/>')
            lg.append(f'<text x="{PAD_L + 6}" y="{yy - 4:.1f}" font-size="13" font-weight="600" fill="{GOLD}">{lab} {val:.0f}</text>')
        # 中枢 GG/DD（中枢上下极值，刻画中枢真实振幅）+ 延伸标注：count≥9 视为中枢延伸（级别升级）
        if "gg" in zs and "dd" in zs:
            for _vv, _lab in ((zs["gg"], "GG"), (zs["dd"], "DD")):
                _yy = y(_vv)
                pg.append(f'<line x1="{PAD_L}" y1="{_yy:.1f}" x2="{W - PAD_R}" y2="{_yy:.1f}" stroke="#94a3b8" stroke-width="0.7" stroke-dasharray="1,4" stroke-opacity="0.6"/>')
                lg.append(f'<text x="{W - PAD_R - 56}" y="{_yy - 2:.1f}" font-size="9" font-weight="500" fill="#94a3b8">{_lab} {_vv:.0f}</text>')
            if zs.get("extension"):
                lg.append(f'<text x="{PAD_L + 70}" y="{y(zs["zg"]) - 4:.1f}" font-size="11" font-weight="700" fill="#b45309">⟳ 中枢延伸({zs["count"]}笔)</text>')
            # 中枢进入方向（缠论：中枢是上升中还是下降中形成，直接决定三买/三卖的力度）：
            # ↑中枢=进入笔向上(上涨中构筑)、↓中枢=进入笔向下(下跌中构筑)。
            _enter_dir = None
            for _bi in bis:
                if _bi["end"] < zs["start"]:
                    _enter_dir = _bi["dir"]
                else:
                    break
            if _enter_dir == 1:
                lg.append(f'<text x="{PAD_L + 6}" y="{y((zs["zg"] + zs["zd"]) / 2) + 3:.1f}" font-size="10" font-weight="700" fill="#0ea5e9">↑中枢</text>')
            elif _enter_dir == -1:
                lg.append(f'<text x="{PAD_L + 6}" y="{y((zs["zg"] + zs["zd"]) / 2) + 3:.1f}" font-size="10" font-weight="700" fill="#a855f7">↓中枢</text>')

    # 跳空缺口（未补，价格在±15%内最近5个）：横向半透明带 = 强支撑/强压力位。
    # A股「逢缺必补」，未补缺口是中枢之外最重要的价位锚；向上缺口位于下方=红带(突破支撑)，
    # 向下缺口位于上方=绿带(破位压力)。仅显示贴近当前的缺口，避免远处无关缺口污染视图。
    _close = closes[-1]
    _gaps_unf = [g for g in r.get("gaps", []) if not g["filled"]]
    _gaps_view = [g for g in _gaps_unf
                  if abs((g["top"] + g["bottom"]) / 2 / _close - 1) <= 0.15]
    _gaps_view.sort(key=lambda g: g["idx"])
    for g in _gaps_view[-5:]:
        _yt, _yb = y(g["top"]), y(g["bottom"])
        _col = RED if g["type"] == "up" else GREEN  # 涨红跌绿
        _h = max(_yb - _yt, 2.5)
        pg.append(f'<rect x="{PAD_L:.1f}" y="{_yt:.1f}" width="{plot_w:.1f}" height="{_h:.1f}" fill="{_col}" fill-opacity="0.07" stroke="{_col}" stroke-opacity="0.30" stroke-width="0.6"/>')
        _lab = ("▲缺" if g["type"] == "up" else "▼缺") + g["date"][5:]
        lg.append(f'<text x="{W - PAD_R - 64}" y="{_yt - 2:.1f}" font-size="9" font-weight="600" fill="{_col}">{_lab} {g["bottom"]:.0f}~{g["top"]:.0f}</text>')

    # 斐波那契回调位（最近一段已完成 swing；紫虚线，作为预测目标支撑/阻力）
    # 仅在"回撤区"（最近完成笔末端 → 右缘）绘制，避免在无关历史区间上空画悬浮参考线，更专业
    if len(bis) >= 2:
        leg = bis[-2]  # 最近一段已完成的笔（bis[-1] 为进行中的当前笔）
        ls, le = leg["start_price"], leg["end_price"]
        if leg["dir"] == 1:  # 上升腿：从高点向下回调的支撑位
            base_hi, base_lo = le, ls
        else:  # 下降腿：从低点向上反弹的阻力位
            base_hi, base_lo = ls, le
        swing = base_hi - base_lo
        x0 = x(merged[leg["end"]]["idx_end"])  # 回撤区左缘 = 最近完成笔末端（当前笔起点）
        _fib = []
        for f, lab in ((0.0, "F0"), (0.382, "F38"), (0.5, "F50"), (0.618, "F62")):
            pv = base_hi - swing * f if leg["dir"] == 1 else base_lo + swing * f
            yy = y(pv)
            pg.append(f'<line x1="{x0:.1f}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#7c3aed" stroke-width="0.8" stroke-dasharray="2,4" stroke-opacity="0.5"/>')
            _fib.append((yy, lab, pv))
        _fib.sort(key=lambda t: t[0])
        _last_y = -1e9
        for _yy, _lab, _pv in _fib:  # 垂直防重叠：相邻标签过近则错开，避免 F 位文字叠在一起
            if abs(_yy - _last_y) < 12:
                _yy = _last_y + 12 if _yy >= _last_y else _last_y - 12
            _last_y = _yy
            lg.append(f'<text x="{W - PAD_R - 60}" y="{_yy - 2:.1f}" font-size="10" font-weight="600" fill="#7c3aed" text-anchor="start">{_lab} {_pv:.0f}</text>')

    # 蜡烛图（替代原收盘价面积线：开高低收实体+影线，股票图专业度的核心要素；实体宽度随缩放
    # 自适应——全览≈发丝线、放大后展开为完整蜡烛，符合交易终端习惯；影线合并为单 path 高效绘制，
    # 实体用 rect。涨红跌绿遵循 A股惯例）。
    _bw = max(plot_w / n * 0.62, 0.5)
    _wicks, _bodies = [], []
    for i, k in enumerate(klines):
        xc = x(i)
        yo = y(k["open"]); yc = y(k["close"])
        yh = y(k["high"]); yl = y(k["low"])
        _wicks.append(f"M{xc:.1f} {yh:.2f} L{xc:.1f} {yl:.2f}")
        _yt = min(yo, yc); _bh = max(abs(yc - yo), 0.5)
        _bc = RED if k["close"] >= k["open"] else GREEN
        _bodies.append(f'<rect x="{xc - _bw / 2:.2f}" y="{_yt:.2f}" width="{_bw:.2f}" height="{_bh:.2f}" fill="{_bc}" fill-opacity="0.92"/>')
    pg.append(f'<path d="{"".join(_wicks)}" fill="none" stroke="#94a3b8" stroke-width="0.7" stroke-opacity="0.55" vector-effect="non-scaling-stroke"/>')
    pg.extend(_bodies)

    # MA20 / MA60 均线（与卡片"均线排列"呼应，提升专业度；置于图形层随窗口横向缩放）
    def ma_series(arr, p):
        out = [None] * len(arr)
        for i in range(len(arr)):
            if i + 1 >= p:
                out[i] = sum(arr[i + 1 - p:i + 1]) / p
        return out

    for maa, mcol, mlab in ((ma_series(closes, 20), "#0ea5e9", "MA20"),
                            (ma_series(closes, 60), "#a855f7", "MA60"),
                            (ma_series(closes, 250), "#0d9488", "MA250")):
        pts = [(x(i), y(v)) for i, v in enumerate(maa) if v is not None]
        if pts:
            pg.append(f'<path d="{_smooth(pts)}" fill="none" stroke="{mcol}" stroke-width="1" stroke-opacity="0.85" stroke-linejoin="round" stroke-linecap="round"/>')
    lg.append(f'<rect x="{PAD_L + 2}" y="{PAD_T + 3}" width="196" height="16" rx="3" fill="#ffffff" fill-opacity="0.82"/>')
    lg.append(f'<text x="{PAD_L + 6}" y="{PAD_T + 13}" font-size="12" font-weight="600" fill="#0ea5e9">— MA20</text>')
    lg.append(f'<text x="{PAD_L + 64}" y="{PAD_T + 13}" font-size="12" font-weight="600" fill="#a855f7">— MA60</text>')
    lg.append(f'<text x="{PAD_L + 122}" y="{PAD_T + 13}" font-size="12" font-weight="600" fill="#0d9488">— MA250</text>')

    # 笔线段：已完成笔实线、当前进行中的笔用虚线+加粗，直观区分「已确认」与「正在形成」（细腻度）
    for bi_i, b in enumerate(bis):
        x0 = x(merged[b["start"]]["idx_end"])
        x1 = x(merged[b["end"]]["idx_end"])
        color = RED if b["dir"] == 1 else GREEN
        is_cur = (bi_i == len(bis) - 1)
        if is_cur:
            pg.append(f'<line x1="{x0:.1f}" y1="{y(b["start_price"]):.1f}" x2="{x1:.1f}" y2="{y(b["end_price"]):.1f}" stroke="{color}" stroke-width="2.6" stroke-opacity="0.95" stroke-dasharray="6,4"/>')
        else:
            pg.append(f'<line x1="{x0:.1f}" y1="{y(b["start_price"]):.1f}" x2="{x1:.1f}" y2="{y(b["end_price"]):.1f}" stroke="{color}" stroke-width="1.8" stroke-opacity="0.95"/>')

    # 笔端点（分型转折点）圆点，便于核对结构（放文字层，随窗口重算 cx 保持正圆）
    for bi_i, b in enumerate(bis):
        xxe = x(merged[b["end"]]["idx_end"])
        yye = y(b["end_price"])
        col = RED if b["dir"] == 1 else GREEN
        is_cur = (bi_i == len(bis) - 1)
        if is_cur:
            # 当前未完成笔末端（临时分型）：放大圆点 + 虚线方框 + 「未确认」标注，明确与已完成分型区分
            lg.append(f'<circle data-i="{merged[b["end"]]["idx_end"]}" data-dx="0" cx="{xxe:.1f}" cy="{yye:.1f}" r="4" fill="{col}" fill-opacity="0.85" stroke="#ffffff" stroke-width="1.4"/>')
            lg.append(f'<rect data-i="{merged[b["end"]]["idx_end"]}" data-dx="0" x="{xxe - 7:.1f}" y="{yye - 7:.1f}" width="14" height="14" fill="none" stroke="{col}" stroke-width="1" stroke-dasharray="3,2.5" opacity="0.85"/>')
            _ylab = yye - 15 if b["dir"] == 1 else yye + 23
            lg.append(f'<text data-i="{merged[b["end"]]["idx_end"]}" data-dx="0" x="{xxe:.1f}" y="{_ylab:.1f}" font-size="10.5" font-weight="700" fill="{col}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="2.5">未确认</text>')
        else:
            lg.append(f'<circle data-i="{merged[b["end"]]["idx_end"]}" data-dx="0" cx="{xxe:.1f}" cy="{yye:.1f}" r="2.6" fill="{col}" stroke="#ffffff" stroke-width="0.8"/>')

    # 已知历史拐点（算法准确度外部校验·可视化）：抓到的标金色✓菱形，未抓到的标灰色◇。
    # 这是「缠论算法对历史大底/大顶捕捉能力」的诚实展示——高捕获率佐证结构识别可靠，
    # 个别未捕捉的也如实标注，不美化。菱形画图形层(pg)，符号画文字层(lg)随窗口重算保持正立。
    if captured is not None:
        _cap_labels = {c[0] for c in captured}
        _piv = []
        for _pd, (_lab, _dir) in KNOWN_PIVOTS.items():
            _bi, _bd = 0, 1e9
            for _i, _k in enumerate(klines):
                _dd = abs(_date_diff(_k["date"], _pd))
                if _dd < _bd:
                    _bd, _bi = _dd, _i
            _is_cap = _lab in _cap_labels
            _pcol = GOLD if _is_cap else "#94a3b8"
            _piv.append((x(_bi), y(klines[_bi]["close"]), _is_cap, _pcol, _bi))
        _piv.sort(key=lambda t: t[0])
        _last_x, _row = -1e9, 0
        for _xx, _yy, _is_cap, _pcol, _bi in _piv:
            _dd2 = 5
            pg.append(f'<path d="M{_xx:.1f} {_yy-_dd2:.1f} L{_xx+_dd2:.1f} {_yy:.1f} L{_xx:.1f} {_yy+_dd2:.1f} L{_xx-_dd2:.1f} {_yy:.1f} Z" fill="{_pcol}" fill-opacity="0.92" stroke="#ffffff" stroke-width="1"/>')
            if abs(_xx - _last_x) < 80:   # 临近拐点标签水平防重叠：上下错开
                _row = 1 - _row
            else:
                _row = 0
            _last_x = _xx
            _ly = max(PAD_T + 11, min(PAD_T + price_h - 6, _yy - _dd2 - 7 - _row * 13))
            lg.append(f'<text data-i="{_bi}" data-dx="0" x="{_xx:.1f}" y="{_ly:.1f}" font-size="11" font-weight="800" fill="{_pcol}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="3">{("✓" if _is_cap else "◇")}</text>')

    # 线段结构（缠论"笔→线段→走势"高一级递归）：连接相邻线段的摆动极点形成宏观走势折线，
    # 与笔级实线分层——极点用空心小菱形标记（红=线段顶/绿=线段底），仅画最近 ~14 段避免过密。
    # 线段极值定位：在每段 bi 起止 K 线跨度(merged窗口)内取 high/low，极个别跨段边界未命中则退回 bi 末端索引。
    # 既有「线段背驰三角」坐落的极点即宏观折线顶点，二者叠加完整呈现本级别之上的走势层级。
    _segs = r.get("segments", [])
    if len(_segs) >= 2:
        _seg_pts = []
        for _sg in _segs:
            _s0 = merged[_sg["start"]]["idx_start"]
            _e0 = merged[_sg["end"]]["idx_end"]
            if _e0 < _s0:
                _s0, _e0 = _e0, _s0
            if _sg["dir"] == 1:
                _idx = max(range(_s0, _e0 + 1), key=lambda i: klines[i]["high"])
                _pv = klines[_idx]["high"]
                if abs(_pv - _sg["high"]) > 1e-6:
                    _idx, _pv = _e0, _sg["high"]
            else:
                _idx = min(range(_s0, _e0 + 1), key=lambda i: klines[i]["low"])
                _pv = klines[_idx]["low"]
                if abs(_pv - _sg["low"]) > 1e-6:
                    _idx, _pv = _e0, _sg["low"]
            _seg_pts.append((_idx, _pv, _sg["dir"]))
        _recent = _seg_pts[-14:]
        _pline = [(x(_i), y(_v)) for _i, _v, _ in _recent]
        if len(_pline) >= 2:
            pg.append(f'<path d="{_smooth(_pline, tension=0.6)}" fill="none" stroke="#334155" stroke-width="1.5" stroke-opacity="0.5" stroke-dasharray="6,3" stroke-linejoin="round" stroke-linecap="round"/>')
        for _i, _v, _d in _recent:
            _xc, _yc = x(_i), y(_v)
            _mc = RED if _d == 1 else GREEN
            _dd = 4
            lg.append(f'<polygon data-i="{_i}" data-dx="0" points="{_xc:.1f},{_yc-_dd:.1f} {_xc+_dd:.1f},{_yc:.1f} {_xc:.1f},{_yc+_dd:.1f} {_xc-_dd:.1f},{_yc:.1f}" fill="#ffffff" stroke="{_mc}" stroke-width="1.4"/>')
        lg.append(f'<text x="{PAD_L + 6}" y="{PAD_T + 27}" font-size="11" font-weight="700" fill="#334155">⬡ 线段结构(高一级)</text>')

    # 买卖点信号（近 2 年内；同向标签最小间距 55px；过靠右缘会压到最新价标签则跳过）
    cutoff = n - 500
    last_x_by_dir = {1: -999, -1: -999}
    tag_left = PAD_L + plot_w - 96   # 最新价标签左缘，避免信号标签压住它
    for s in r["signals"]:
        b = bis[s["bi_index"]]
        xi = merged[b["end"]]["idx_end"]
        if xi < cutoff:
            continue
        xx, yy = x(xi), y(b["end_price"])
        d = s["dir"]
        if xx - last_x_by_dir[d] < 55:
            continue
        if xx > tag_left:
            continue
        last_x_by_dir[d] = xx
        col = RED if d == 1 else GREEN
        # 信号标签：买卖点类别 + 背驰级别(趋/盘) + 量能确认(量)
        _marker = ""
        if s.get("bc_type") == "趋势背驰":
            _marker += "·趋"
        elif s.get("bc_type") == "盘整背驰":
            _marker += "·盘"
        if s.get("vol_confirm"):
            _marker += "·量"
        _lbl = s["kind"][:3] + _marker
        if d == 1:
            pg.append(f'<polygon points="{xx:.1f},{yy + 8:.1f} {xx - 5:.1f},{yy + 17:.1f} {xx + 5:.1f},{yy + 17:.1f}" fill="{col}"/>')
            lg.append(f'<text data-i="{xi}" data-dx="0" x="{xx:.1f}" y="{yy + 30:.1f}" font-size="13" font-weight="600" fill="{col}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="3">{_lbl}</text>')
        else:
            pg.append(f'<polygon points="{xx:.1f},{yy - 8:.1f} {xx - 5:.1f},{yy - 17:.1f} {xx + 5:.1f},{yy - 17:.1f}" fill="{col}"/>')
            lg.append(f'<text data-i="{xi}" data-dx="0" x="{xx:.1f}" y="{yy - 23:.1f}" font-size="13" font-weight="600" fill="{col}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="3">{_lbl}</text>')
        # R:R 值博率小徽标（缠论实战必备：每个买卖点须有明确止损/目标/盈亏比，此前完全缺失）。
        # 主标签保持简洁，R:R 以独立小字标注于三角旁，紫=优/良(值博)、灰=中、橙=差(慎)。
        _rr = s.get("rr")
        if _rr:
            _rr_col = {"优": "#7c3aed", "良": "#7c3aed", "中": "#64748b", "差": "#b45309"}.get(s.get("quality"), "#94a3b8")
            _ry = (yy + 43) if d == 1 else (yy - 36)
            lg.append(f'<text data-i="{xi}" data-dx="0" x="{xx:.1f}" y="{_ry:.1f}" font-size="10" font-weight="700" fill="{_rr_col}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="2.5">R{_rr:.1f}</text>')

    # 背驰点标注（笔级）：顶背驰=红▼（顶点朝下指向价位）、底背驰=绿▲（顶点朝上指向价位）。
    # 缠论图核心要素——背驰即买卖点触发处，须在价位区直观可见；仅标近 2 年、同一方向最小间距 40px 防过密。
    _bc_cut = n - 500
    _last_x_bc = {1: -999, -1: -999}
    for _bc in r.get("beichi", []):
        _bi = _bc["bi_index"]
        if _bi < 0 or _bi >= len(bis):
            continue
        _b = bis[_bi]
        _xi = merged[_b["end"]]["idx_end"]
        if _xi < _bc_cut:
            continue
        _xb, _yb = x(_xi), y(_b["end_price"])
        _d = 1 if _bc["type"] == "top" else -1
        if _xb - _last_x_bc[_d] < 40:
            continue
        _last_x_bc[_d] = _xb
        _vc = _bc.get("vol_confirm")
        _bt = _bc.get("bc_type", "")
        # 背驰分级描边：趋势背驰=加粗（量能确认金、否则深橙，本级别大级别转折）；
        # 盘整背驰=细描边（量能确认金、否则石灰，级别较小）；与量能确认因子叠加形成 4 级视觉层次。
        if _bt == "趋势背驰":
            _sw = 2.0
            _stroke = GOLD if _vc else "#b45309"
        elif _bt == "盘整背驰":
            _sw = 0.9
            _stroke = GOLD if _vc else "#94a3b8"
        else:
            _sw = 1.0
            _stroke = GOLD if _vc else "#ffffff"
        if _bc["type"] == "top":
            pg.append(f'<polygon points="{_xb:.1f},{_yb - 9:.1f} {_xb - 5:.1f},{_yb - 18:.1f} {_xb + 5:.1f},{_yb - 18:.1f}" fill="{RED}" stroke="{_stroke}" stroke-width="{_sw}"/>')
        else:
            pg.append(f'<polygon points="{_xb:.1f},{_yb + 9:.1f} {_xb - 5:.1f},{_yb + 18:.1f} {_xb + 5:.1f},{_yb + 18:.1f}" fill="{GREEN}" stroke="{_stroke}" stroke-width="{_sw}"/>')

    # 线段级背驰标注（走势段级别，比笔级更高一层）：用空心大三角（白底彩边）与笔级实心小三角区分，
    # 置于更外侧（顶-22~-36 / 底+22~+36）形成双层显示、层级分明；仅标近 2 年、最小间距 60px 防过密。
    # 顶/底背驰分别标定在线段 high/low 对应的 K 线极值点（精确，非近似末端）。
    _sb_cut = n - 500
    _last_x_sb = {1: -999, -1: -999}
    for _sb in r.get("seg_beichi", []):
        _si = _sb["seg_index"]
        if _si < 0 or _si >= len(r["segments"]):
            continue
        _seg = r["segments"][_si]
        _s0 = merged[_seg["start"]]["idx_start"]
        _s1 = merged[_seg["end"]]["idx_end"]
        if _s1 < _s0:
            _s0, _s1 = _s1, _s0
        if _s1 < _sb_cut:
            continue
        if _sb["type"] == "top":
            _idx = max(range(_s0, _s1 + 1), key=lambda i: klines[i]["high"])
            _price = _seg["high"]
        else:
            _idx = min(range(_s0, _s1 + 1), key=lambda i: klines[i]["low"])
            _price = _seg["low"]
        _xb, _yb = x(_idx), y(_price)
        _d = 1 if _sb["type"] == "top" else -1
        if _xb - _last_x_sb[_d] < 60:
            continue
        _last_x_sb[_d] = _xb
        if _sb["type"] == "top":
            pg.append(f'<polygon points="{_xb:.1f},{_yb - 22:.1f} {_xb - 8:.1f},{_yb - 36:.1f} {_xb + 8:.1f},{_yb - 36:.1f}" fill="#ffffff" stroke="{RED}" stroke-width="1.6"/>')
            lg.append(f'<text data-i="{_idx}" data-dx="0" x="{_xb:.1f}" y="{_yb - 46:.1f}" font-size="9" font-weight="700" fill="{RED}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="2.5">{_sb["area_ratio"]:.2f}</text>')
        else:
            pg.append(f'<polygon points="{_xb:.1f},{_yb + 22:.1f} {_xb - 8:.1f},{_yb + 36:.1f} {_xb + 8:.1f},{_yb + 36:.1f}" fill="#ffffff" stroke="{GREEN}" stroke-width="1.6"/>')
            lg.append(f'<text data-i="{_idx}" data-dx="0" x="{_xb:.1f}" y="{_yb + 50:.1f}" font-size="9" font-weight="700" fill="{GREEN}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="2.5">{_sb["area_ratio"]:.2f}</text>')

    # 最新价虚线 + 标签
    last_c = closes[-1]
    yy = y(last_c)
    lcolor = RED if closes[-1] >= closes[-2] else GREEN
    pg.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="{lcolor}" stroke-width="1" stroke-dasharray="2,3" stroke-opacity="0.8"/>')
    lg.append(f'<g id="latest-{sym}">')
    lg.append(f'<rect x="{PAD_L + plot_w - 74}" y="{yy - 9:.1f}" width="72" height="16" rx="3" fill="{lcolor}" stroke="#ffffff" stroke-width="0.8"/>')
    lg.append(f'<text x="{PAD_L + plot_w - 39}" y="{yy + 4:.1f}" font-size="14" font-weight="700" fill="#ffffff" text-anchor="middle">{last_c:.2f}</text>')
    lg.append(f'<text x="{PAD_L + plot_w - 80:.1f}" y="{yy + 4:.1f}" font-size="11" font-weight="600" fill="{INK}" text-anchor="end" opacity="0.85">现价</text>')
    lg.append('</g>')

    # ===== 成交量副图 =====
    vmax = max((k["volume"] for k in klines), default=1) or 1
    bw = max(plot_w / n * 0.62, 0.8)
    # 缩量背驰覆盖的 K 线原始索引集合：背驰笔本身应是价创新高/低、量能却萎缩的背离结构，
    # 把对应成交量柱染金，直观印证「量能确认」因子（缠论核心确认条件），提升图表信息密度。
    _vol_conf_raw = set()
    for _bc in r.get("beichi", []):
        if _bc.get("vol_confirm"):
            _bi = _bc["bi_index"]
            if 0 <= _bi < len(bis):
                _b = bis[_bi]
                _s = merged[_b["start"]]["idx_start"]
                _e = merged[_b["end"]]["idx_end"]
                if _e < _s:
                    _s, _e = _e, _s
                for _ri in range(_s, _e + 1):
                    _vol_conf_raw.add(_ri)
    # 成交量 MA5（量能趋势基准，先整段计算供「放量突破」判定与画线共用）
    vma = [sum(klines[j]["volume"] for j in range(i + 1 - 5, i + 1)) / 5 if i + 1 >= 5 else None
           for i in range(n)]
    # 近20日最高（前20日，不含当日）用于「阶段新高突破」判定
    _hi20 = []
    for i in range(n):
        _lo = max(0, i - 20)
        _hi20.append(max((klines[j]["high"] for j in range(_lo, i)), default=klines[i]["high"]))
    zg_k = zs["zg"] if zs else None
    for i, k in enumerate(klines):
        vh = k["volume"] / vmax * (H_VOL - 4)
        if i in _vol_conf_raw:
            vc_bar, vo = GOLD, 0.92
        else:
            # 放量突破：量能明显放大(>量MA5*1.8)且当日站上中枢上沿或创近20日新高 → 亮蓝高亮。
            # 与缩量背驰金色对称：量价配合的两类极端——放量突破(动能确认) vs 缩量背驰(动能衰竭)。
            _vol_ratio = (k["volume"] / vma[i]) if vma[i] else 0
            _break = (zg_k is not None and k["high"] > zg_k and k["close"] > zg_k) or (k["high"] >= _hi20[i])
            if _vol_ratio > 1.8 and _break:
                vc_bar, vo = "#2563eb", 0.92
            else:
                vc_bar, vo = (RED if k["close"] >= k["open"] else GREEN), 0.55
        yy2 = vbot - vh
        pg.append(f'<rect x="{x(i) - bw / 2:.1f}" y="{yy2:.1f}" width="{bw:.1f}" height="{vh:.1f}" fill="{vc_bar}" fill-opacity="{vo}"/>')
    # 成交量 MA5 线（量能趋势，置于图形层随窗口横向缩放）
    vma_pts = [(x(i), vbot - (v / vmax) * (H_VOL - 4)) for i, v in enumerate(vma) if v is not None]
    if vma_pts:
        pg.append(f'<path d="{_smooth(vma_pts)}" fill="none" stroke="#475569" stroke-width="1" stroke-opacity="0.85" stroke-linejoin="round" stroke-linecap="round"/>')
    # 成交量参考基线（50% / 100% 量能刻度，提升量能读数专业性：此前仅右上角一个总量标注，
    # 量能相对大小难读；叠加参考线后，"放量突破>量MA5*1.8"与"缩量背驰"的高亮更可量化对照）。
    for _frac, _lab in ((1.0, "100%"), (0.5, "50%")):
        _yyv = vbot - (H_VOL - 4) * _frac
        pg.append(f'<line x1="{PAD_L}" y1="{_yyv:.1f}" x2="{W - PAD_R}" y2="{_yyv:.1f}" stroke="#eef2f7" stroke-width="1"/>')
        lg.append(f'<text x="{W - PAD_R + 6}" y="{_yyv + 4:.1f}" font-size="10" fill="{GRAY}">{_lab}</text>')
    lg.append(f'<text x="{PAD_L}" y="{vtop - 2:.1f}" font-size="14" font-weight="600" fill="{GRAY}">成交量</text>')
    lg.append(f'<text x="{PAD_L + 58}" y="{vtop - 2:.1f}" font-size="12" font-weight="600" fill="#475569">— 量MA5</text>')
    lg.append(f'<text x="{PAD_L + 128}" y="{vtop - 2:.1f}" font-size="12" font-weight="700" fill="{GOLD}">◆ 缩量背驰</text>')
    lg.append(f'<text x="{PAD_L + 208}" y="{vtop - 2:.1f}" font-size="12" font-weight="700" fill="#2563eb">■ 放量突破</text>')
    lg.append(f'<text x="{W - PAD_R + 6}" y="{vtop + 14:.1f}" font-size="11" font-weight="500" fill="{GRAY}">{vmax / 1e8:.2f}亿手</text>')

    # ===== MACD 副图 =====
    hmax = max(abs(v) for v in hist) or 1
    mid = mtop + H_MACD / 2
    bw2 = max(plot_w / n * 0.6, 1)
    for i, v in enumerate(hist):
        hh = abs(v) / hmax * (H_MACD / 2 - 6)
        color = RED if v >= 0 else GREEN
        yy2 = mid - hh if v >= 0 else mid
        pg.append(f'<rect x="{x(i) - bw2 / 2:.1f}" y="{yy2:.1f}" width="{bw2:.1f}" height="{hh:.1f}" fill="{color}" fill-opacity="0.65"/>')
    pg.append(f'<line x1="{PAD_L}" y1="{mid:.1f}" x2="{W - PAD_R}" y2="{mid:.1f}" stroke="#cbd5e1"/>')
    dmax = max(max(abs(v) for v in dif), max(abs(v) for v in dea)) or 1

    def ym(v):
        return mid - v / dmax * (H_MACD / 2 - 6)

    dif_pts = [(x(i), ym(dif[i])) for i in range(n)]
    dea_pts = [(x(i), ym(dea[i])) for i in range(n)]
    pg.append(f'<path d="{_smooth(dif_pts)}" fill="none" stroke="{BLUE}" stroke-width="1" stroke-linejoin="round" stroke-linecap="round"/>')
    pg.append(f'<path d="{_smooth(dea_pts)}" fill="none" stroke="#d97706" stroke-width="1" stroke-linejoin="round" stroke-linecap="round"/>')
    # DIF/DEA 零轴上下极值刻度（量纲标注，更细腻）
    lg.append(f'<text x="{W - PAD_R + 6}" y="{mtop + 4:.1f}" font-size="11" fill="{GRAY}">+{dmax:.0f}</text>')
    lg.append(f'<text x="{W - PAD_R + 6}" y="{mbot - 2:.1f}" font-size="11" fill="{GRAY}">-{dmax:.0f}</text>')
    lg.append(f'<text x="{PAD_L}" y="{mtop + 11}" font-size="14" font-weight="600" fill="{GRAY}">MACD(12,26,9)</text>')
    lg.append(f'<text x="{PAD_L + 120}" y="{mtop + 11}" font-size="14" font-weight="600" fill="{BLUE}">— DIF</text>')
    lg.append(f'<text x="{PAD_L + 178}" y="{mtop + 11}" font-size="14" font-weight="600" fill="#d97706">— DEA</text>')
    # MACD 金叉/死叉标记：DIF 上穿 DEA=金叉(▲红)、下穿=死叉(▼绿)；交叉点取 DIF/DEA 中点高度。
    # 仅标近 2 年、最小间距 24px 防过密；白描边保证叠在线条上仍可辨。
    _xo_cut = n - 500
    _last_xo = -999
    for _i in range(1, n):
        if _i < _xo_cut:
            continue
        _up = dif[_i - 1] <= dea[_i - 1] and dif[_i] > dea[_i]
        _dn = dif[_i - 1] >= dea[_i - 1] and dif[_i] < dea[_i]
        if not (_up or _dn):
            continue
        _xxo = x(_i)
        if _xxo - _last_xo < 24:
            continue
        _last_xo = _xxo
        _yyo = ym((dif[_i] + dea[_i]) / 2)
        if _up:
            pg.append(f'<polygon points="{_xxo:.1f},{_yyo - 5:.1f} {_xxo - 3.5:.1f},{_yyo - 1:.1f} {_xxo + 3.5:.1f},{_yyo - 1:.1f}" fill="{RED}" stroke="#ffffff" stroke-width="0.6"/>')
        else:
            pg.append(f'<polygon points="{_xxo:.1f},{_yyo + 5:.1f} {_xxo - 3.5:.1f},{_yyo + 1:.1f} {_xxo + 3.5:.1f},{_yyo + 1:.1f}" fill="{GREEN}" stroke="#ffffff" stroke-width="0.6"/>')

    # 背驰区间 MACD 面积对比（背驰判定的核心可视化证据）：背驰本质是「相邻同向段 MACD 面积萎缩」，
    # 对笔背驰对应 K 线区间在副图画极淡竖条 + 标注面积比(后段/前段)<1 即面积↓，与主图背驰三角
    # 形成「价位背驰 + 动量面积背驰」双证据链。仅标近 2 年、同方向最小间距 46px 防过密。
    _mb_cut = n - 500
    _last_x_mb = {1: -999, -1: -999}
    for _bc in r.get("beichi", []):
        _bi = _bc["bi_index"]
        if _bi < 0 or _bi >= len(bis):
            continue
        _b = bis[_bi]
        _s = merged[_b["start"]]["idx_start"]
        _e = merged[_b["end"]]["idx_end"]
        if _e < _s:
            _s, _e = _e, _s
        if _e < _mb_cut:
            continue
        _xm0, _xm1 = x(_s), x(_e)
        _d = 1 if _bc["type"] == "top" else -1
        if _xm1 - _last_x_mb[_d] < 46:
            continue
        _last_x_mb[_d] = _xm1
        _col = RED if _bc["type"] == "top" else GREEN
        pg.append(f'<rect x="{_xm0:.1f}" y="{mtop:.1f}" width="{max(_xm1 - _xm0, 1):.1f}" height="{H_MACD:.1f}" fill="{_col}" fill-opacity="0.06"/>')
        _ar = _bc.get("area_ratio", 1)
        _lab = f"面积{_ar:.2f}{'↓' if _ar < 1 else '↑'}"
        _midx = (_s + _e) // 2
        _lyy = (mtop + 11) if _bc["type"] == "top" else (mbot - 3)
        lg.append(f'<text data-i="{_midx}" data-dx="0" x="{_xm1:.1f}" y="{_lyy:.1f}" font-size="9" font-weight="700" fill="{_col}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="2.5">{_lab}</text>')

    p = [f'<svg id="main-{sym}" viewBox="0 0 {W} {CHART_TOTAL}" preserveAspectRatio="xMidYMid meet" data-n="{n}" data-lo="{lo:.4f}" data-span="{span:.4f}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;text-rendering:geometricPrecision;shape-rendering:geometricPrecision">']
    p += base
    # vector-effect 保证 dataZoom 横向放大时，线的粗细不会被同步拉粗（只拉伸几何、不拉伸 stroke），
    # 从而避免放大后红绿笔/均线/MACD 线出现横向变粗的“失真”感。
    p.append(f'<g clip-path="url(#clip-{sym})"><g id="plot-{sym}" class="plot" vector-effect="non-scaling-stroke">')
    p += pg
    p.append('</g></g>')
    p.append(f'<g id="lbl-{sym}" class="lbl">')
    p += lg
    p.append('</g>')
    # 交互层：十字光标（竖线 + 横线） + 右侧价格读数 + 透明捕获（最外层，不随窗口缩放）
    p.append(f'<line id="cx-{sym}" x1="0" y1="{PAD_T}" x2="0" y2="{mbot}" stroke="{INK}" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>')
    p.append(f'<line id="cy-{sym}" x1="{PAD_L}" y1="0" x2="{W - PAD_R}" y2="0" stroke="{INK}" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>')
    p.append(f'<g id="pr-{sym}" opacity="0"><rect x="{W - PAD_R + 2}" y="-9" width="72" height="16" rx="3" fill="{INK}"/><text id="prt-{sym}" x="{W - PAD_R + 38}" y="4" font-size="12" font-weight="700" fill="#fff" text-anchor="middle">--</text></g>')
    p.append(f'<rect id="xh-{sym}" x="0" y="0" width="{W}" height="{CHART_TOTAL}" fill="transparent" style="cursor:crosshair"/>')
    p.append('</svg>')
    return "".join(p)


# ================= ECharts 主图（参考斐波那契项目，解决放大失真） =================
def echart_main(klines, r, sym, captured=None):
    """用 ECharts 绘制缠论主图（价格+成交量+MACD），缩放后仍清晰细腻。"""
    dates = [k["date"] for k in klines]
    # ECharts 蜡烛图数据格式为 [open, close, low, high]
    ohlc = [[round(k["open"], 2), round(k["close"], 2), round(k["low"], 2), round(k["high"], 2)] for k in klines]
    volumes = [k["volume"] for k in klines]
    closes = [k["close"] for k in klines]
    n = len(klines)
    merged, bis, zss = r["merged"], r["bis"], r["zhongshu"]
    dif, dea, hist = r["dif"], r["dea"], r["hist"]

    def ma_series(arr, p):
        out = [None] * len(arr)
        for i in range(len(arr)):
            if i + 1 >= p:
                out[i] = sum(arr[i + 1 - p:i + 1]) / p
        return out

    ma20 = [round(v, 3) if v is not None else None for v in ma_series(closes, 20)]
    ma60 = [round(v, 3) if v is not None else None for v in ma_series(closes, 60)]
    ma250 = [round(v, 3) if v is not None else None for v in ma_series(closes, 250)]

    date_idx = {d: i for i, d in enumerate(dates)}

    # 中枢 markArea（最近8个）
    mark_areas = []
    for zs in zss[-8:]:
        x0 = merged[zs["start"]]["idx_start"]
        x1 = merged[zs["end"]]["idx_end"]
        if x1 < x0:
            x0, x1 = x1, x0
        mark_areas.append([
            {"xAxis": dates[x0], "yAxis": round(zs["zg"], 2),
             "itemStyle": {"color": "rgba(43,108,176,0.10)"},
             "label": {"show": False}},
            {"xAxis": dates[x1], "yAxis": round(zs["zd"], 2)}
        ])

    # 最后中枢 ZG/ZD 金色虚线（标签移至顶部关键价位条，避免近价重叠）
    last_zs_lines = []
    zg_v = zd_v = None
    if zss:
        zs = zss[-1]
        zg_v, zd_v = round(zs["zg"]), round(zs["zd"])
        for val, lab in [(zs["zg"], "ZG"), (zs["zd"], "ZD")]:
            last_zs_lines.append({
                "yAxis": round(val, 2),
                "lineStyle": {"type": "dashed", "color": GOLD, "width": 1.2},
                "label": {"show": False}
            })

    # 跳空缺口 markArea
    _close = closes[-1]
    gap_areas = []
    for g in sorted(
        [g for g in r.get("gaps", []) if not g["filled"] and abs((g["top"] + g["bottom"]) / 2 / _close - 1) <= 0.15],
        key=lambda x: x["idx"]
    )[-5:]:
        col = RED if g["type"] == "up" else GREEN
        gap_areas.append([
            {"xAxis": dates[g["idx"]], "yAxis": round(g["top"], 2),
             "itemStyle": {"color": f"{col}12"},
             "label": {"show": False}},
            {"xAxis": dates[-1], "yAxis": round(g["bottom"], 2)}
        ])

    # 斐波那契回调位（标签移顶部关键价位条，避免近价重叠）
    fib_lines = []
    fib_pairs = []
    if len(bis) >= 2:
        leg = bis[-2]
        base_hi, base_lo = (leg["end_price"], leg["start_price"]) if leg["dir"] == 1 else (leg["start_price"], leg["end_price"])
        swing = base_hi - base_lo
        x0 = dates[merged[leg["end"]]["idx_end"]]
        for f, lab in ((0.0, "F0"), (0.382, "F38"), (0.5, "F50"), (0.618, "F62")):
            pv = base_hi - swing * f if leg["dir"] == 1 else base_lo + swing * f
            fib_pairs.append((lab, round(pv)))
            fib_lines.append({
                "xAxis": x0, "yAxis": round(pv, 2),
                "lineStyle": {"type": "dashed", "color": "#7c3aed", "width": 0.8},
                "label": {"show": False}
            })

    # 顶部关键价位条（富文本，避免近价标签相互重叠）
    kl = []
    if zg_v is not None:
        kl.append(f"{{zg|ZG {zg_v}}}")
    if zd_v is not None:
        kl.append(f"{{zd|ZD {zd_v}}}")
    if fib_pairs:
        fib_txt = " ".join(f"{lab} {pv}" for lab, pv in fib_pairs)
        kl.append(f"{{fib|Fib {fib_txt}}}")
    key_levels_text = "  ".join(kl)

    # 买卖点 markPoint
    cutoff = n - 500
    sig_points = []
    last_x_by_dir = {1: -999, -1: -999}
    for s in r["signals"]:
        b = bis[s["bi_index"]]
        xi = merged[b["end"]]["idx_end"]
        if xi < cutoff:
            continue
        d = s["dir"]
        if xi - last_x_by_dir[d] < 55:
            continue
        last_x_by_dir[d] = xi
        price = round(klines[xi]["high"], 2) if d == 1 else round(klines[xi]["low"], 2)
        _marker = ""
        if s.get("bc_type") == "趋势背驰":
            _marker += "·趋"
        elif s.get("bc_type") == "盘整背驰":
            _marker += "·盘"
        if s.get("vol_confirm"):
            _marker += "·量"
        lbl = s["kind"][:3] + _marker
        sig_points.append({
            "coord": [dates[xi], price],
            "value": lbl,
            "itemStyle": {"color": RED if d == 1 else GREEN},
            "symbol": "triangle" if d == 1 else "invertedTriangle",
            "symbolSize": 10,
            "label": {"show": True, "position": "top" if d == 1 else "bottom", "color": RED if d == 1 else GREEN, "fontSize": 11, "fontWeight": "bold", "distance": 4}
        })

    # 背驰 markPoint
    bc_points = []
    _bc_cut = n - 500
    _last_x_bc = {1: -999, -1: -999}
    for bc in r.get("beichi", []):
        bi = bc["bi_index"]
        if bi < 0 or bi >= len(bis):
            continue
        b = bis[bi]
        xi = merged[b["end"]]["idx_end"]
        if xi < _bc_cut:
            continue
        d = 1 if bc["type"] == "top" else -1
        if xi - _last_x_bc[d] < 40:
            continue
        _last_x_bc[d] = xi
        bc_points.append({
            "coord": [dates[xi], round(b["end_price"], 2)],
            "value": "顶背驰" if d == 1 else "底背驰",
            "itemStyle": {"color": RED if d == 1 else GREEN},
            "symbol": "pin",
            "symbolSize": 11,
            "label": {"show": True, "position": "top" if d == 1 else "bottom", "color": RED if d == 1 else GREEN, "fontSize": 10, "fontWeight": "bold"}
        })

    # 线段结构 markLine
    seg_lines = []
    segments = r.get("segments", [])
    if len(segments) >= 2:
        seg_pts = []
        for sg in segments:
            s0 = merged[sg["start"]]["idx_start"]
            e0 = merged[sg["end"]]["idx_end"]
            if e0 < s0:
                s0, e0 = e0, s0
            if sg["dir"] == 1:
                idx = max(range(s0, e0 + 1), key=lambda i: klines[i]["high"])
                pv = klines[idx]["high"]
            else:
                idx = min(range(s0, e0 + 1), key=lambda i: klines[i]["low"])
                pv = klines[idx]["low"]
            seg_pts.append((idx, pv, sg["dir"]))
        recent = seg_pts[-14:]
        for i in range(len(recent) - 1):
            idx0, v0, _ = recent[i]
            idx1, v1, _ = recent[i + 1]
            seg_lines.append([
                {"coord": [dates[idx0], round(v0, 2)], "lineStyle": {"color": "#334155", "width": 1.5, "type": "dashed", "opacity": 0.5}},
                {"coord": [dates[idx1], round(v1, 2)]}
            ])

    # 已知历史拐点
    cap_points = []
    if captured is not None:
        _cap_labels = {c[0] for c in captured}
        for _pd, (_lab, _dir) in KNOWN_PIVOTS.items():
            _bi, _bd = 0, 1e9
            for _i, _k in enumerate(klines):
                _dd = abs(_date_diff(_k["date"], _pd))
                if _dd < _bd:
                    _bd, _bi = _dd, _i
            _is_cap = _lab in _cap_labels
            _pcol = GOLD if _is_cap else "#94a3b8"
            cap_points.append({
                "coord": [dates[_bi], round(klines[_bi]["close"], 2)],
                "value": "✓" if _is_cap else "◇",
                "itemStyle": {"color": _pcol},
                "symbol": "diamond",
                "symbolSize": 8,
                "label": {"show": True, "color": _pcol, "fontSize": 11, "fontWeight": "bold"}
            })

    # y 轴范围
    _kMin = min(k["low"] for k in klines)
    _kMax = max(k["high"] for k in klines)
    _yMin, _yMax = _kMin, _kMax
    if zss:
        _yMin = min(_yMin, min(zs["zd"] for zs in zss[-3:]))
        _yMax = max(_yMax, max(zs["zg"] for zs in zss[-3:]))
    for g in r.get("gaps", []):
        if not g["filled"]:
            _yMin = min(_yMin, g["bottom"])
            _yMax = max(_yMax, g["top"])
    _pad = (_yMax - _yMin) * 0.04
    _yMin = math.floor((_yMin - _pad) / 10) * 10
    _yMax = math.ceil((_yMax + _pad) / 10) * 10

    vmax = max(volumes) or 1
    hmax = max(abs(v) for v in hist) or 1

    # 确定性去重叠：买卖点/背驰/拐点标签（ECharts markPoint 的 hideOverlap 在带 position/distance 时不可靠）
    dedup_mark_labels(sig_points + bc_points + cap_points, len(dates), _yMin, _yMax,
                      1100 - 96 - 56, 640 * (1 - 0.40) - 48, 96, 48, date_idx)

    chart_data = {
        "dates": dates,
        "ohlc": ohlc,
        "volume": volumes,
        "ma20": ma20,
        "ma60": ma60,
        "ma250": ma250,
        "dif": dif,
        "dea": dea,
        "hist": hist,
        "yMin": round(_yMin, 2),
        "yMax": round(_yMax, 2),
        "vmax": vmax,
        "hmax": round(hmax, 3),
        "markAreas": mark_areas,
        "lastZsLines": last_zs_lines,
        "gapAreas": gap_areas,
        "fibLines": fib_lines,
        "sigPoints": sig_points,
        "bcPoints": bc_points,
        "segLines": seg_lines,
        "capPoints": cap_points,
        "keyLevelsText": key_levels_text,
    }

    cid = f"echart-{sym}"
    return f"""<div class="echart-toolbar">🔍 滚轮/拖拽缩放 · 拖动底部滑块平移 · 悬停看 OHLC/量能</div>
<div id="{cid}" class="echart-main" style="width:100%;height:640px;"></div>
<script>
(function(){{
  var D = {json.dumps(chart_data, ensure_ascii=False)};
  var chart = echarts.init(document.getElementById('{cid}'));
  var option = {{
    animation: false,
    tooltip: {{
      trigger: 'axis',
      axisPointer: {{ type: 'cross', label: {{ show: false }} }},
      formatter: function(params){{
        var k = params[0];
        if(!k) return '';
        var i = k.dataIndex;
        var d = D.dates[i];
        var o = D.ohlc[i][0], c = D.ohlc[i][1], l = D.ohlc[i][2], h = D.ohlc[i][3];
        var prev = D.ohlc[i-1] ? D.ohlc[i-1][1] : o;
        var chg = (c / prev - 1) * 100;
        var col = chg >= 0 ? '#e54545' : '#18a058';
        return '<b>' + d + '</b><br>开 ' + o.toFixed(2) + ' 收 ' + c.toFixed(2) + '<br>高 ' + h.toFixed(2) + ' 低 ' + l.toFixed(2) + '<br>涨跌 <span style="color:' + col + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</span><br>成交量 ' + (D.volume[i]/1e8).toFixed(2) + ' 亿手';
      }}
    }},
    legend: {{ data: ['日K', 'MA20', 'MA60', 'MA250', '成交量', 'MACD', 'DIF', 'DEA'], top: 2, itemGap: 12, textStyle: {{ fontSize: 11 }} }},
    grid: [
      {{ left: 96, right: 56, top: 48, bottom: '40%' }},
      {{ left: 96, right: 56, top: '62%', height: '11%' }},
      {{ left: 96, right: 56, top: '76%', bottom: 56 }}
    ],
    xAxis: [
      {{ type: 'category', data: D.dates, gridIndex: 0, axisLabel: {{ show: false }} }},
      {{ type: 'category', data: D.dates, gridIndex: 1, axisLabel: {{ show: false }} }},
      {{ type: 'category', data: D.dates, gridIndex: 2, axisLabel: {{ fontSize: 11, margin: 6, hideOverlap: true, showMinLabel: true,
        interval: function(idx, val){{ if (idx === 0) return true; var c = D.dates[idx], p = D.dates[idx-1]; if (!c || !p) return true; if (c.slice(0,4) !== p.slice(0,4)) return true; var m = parseInt(c.slice(5,7),10); return (m % 3 === 1); }},
        formatter: (function(){{ var _py = null; return function(v, i){{ var d = (D.dates && D.dates[i]) ? D.dates[i] : v; if (!d || d.length < 7) return v; var y = d.slice(0,4); if (i === 0 || y !== _py) {{ _py = y; return y; }} return d.slice(5); }}; }})() }} }}
    ],
    yAxis: [
      {{ scale: false, min: D.yMin, max: D.yMax, gridIndex: 0, splitNumber: 6, axisLine: {{ lineStyle: {{ color: '#cbd5e1' }} }}, splitLine: {{ lineStyle: {{ color: '#eef2f7' }} }}, axisLabel: {{ fontSize: 12, hideOverlap: true }} }},
      {{ scale: true, gridIndex: 1, splitNumber: 2, name: '成交量', nameLocation: 'middle', nameGap: 34, nameTextStyle: {{ color: '#94a3b8', fontSize: 11 }}, axisLine: {{ show: false }}, splitLine: {{ show: false }}, axisLabel: {{ show: false }} }},
      {{ scale: true, gridIndex: 2, min: -D.hmax, max: D.hmax, splitNumber: 2, name: 'MACD', nameLocation: 'middle', nameGap: 34, nameTextStyle: {{ color: '#94a3b8', fontSize: 11 }}, axisLine: {{ show: false }}, splitLine: {{ show: false }}, axisLabel: {{ fontSize: 11 }} }}
    ],
    dataZoom: [
      {{ type: 'inside', xAxisIndex: [0, 1, 2], start: 0, end: 100 }},
      {{ type: 'slider', xAxisIndex: [0, 1, 2], start: 0, end: 100, showDetail: false, height: 16, bottom: 12, handleStyle: {{ color: '#2b6cb0' }}, borderColor: '#e2e8f0', fillerColor: 'rgba(43,108,176,0.12)' }}
    ],
    series: [
      {{
        name: '日K', type: 'candlestick', data: D.ohlc,
        itemStyle: {{ color: '#e54545', color0: '#18a058', borderColor: '#e54545', borderColor0: '#18a058' }},
        markArea: {{ data: D.markAreas.concat(D.gapAreas), silent: true }},
        markLine: {{ symbol: 'none', data: D.lastZsLines.concat(D.fibLines).concat(D.segLines), silent: false, labelLayout: {{ moveOverlap: 'shiftY' }} }},
        markPoint: {{ data: D.sigPoints.concat(D.bcPoints).concat(D.capPoints) }}
      }},
      {{ name: 'MA20', type: 'line', data: D.ma20, symbol: 'none', lineStyle: {{ color: '#0ea5e9', width: 1.1 }} }},
      {{ name: 'MA60', type: 'line', data: D.ma60, symbol: 'none', lineStyle: {{ color: '#a855f7', width: 1.2 }} }},
      {{ name: 'MA250', type: 'line', data: D.ma250, symbol: 'none', lineStyle: {{ color: '#0d9488', width: 1.2 }} }},
      {{
        name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: D.volume,
        itemStyle: {{ color: function(p){{ var c = D.ohlc[p.dataIndex]; return c[1] >= c[0] ? '#e54545' : '#18a058'; }} }}
      }},
      {{
        name: 'MACD', type: 'bar', xAxisIndex: 2, yAxisIndex: 2, data: D.hist,
        itemStyle: {{ color: function(p){{ return p.value >= 0 ? '#e54545' : '#18a058'; }} }}
      }},
      {{ name: 'DIF', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: D.dif, symbol: 'none', lineStyle: {{ color: '#2b6cb0', width: 1 }} }},
      {{ name: 'DEA', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: D.dea, symbol: 'none', lineStyle: {{ color: '#d97706', width: 1 }} }}
    ]
  }};
  if (D.keyLevelsText) {{
    option.graphic = [{{
      type: 'text', left: 100, top: 32, z: 100, silent: true,
      style: {{
        text: D.keyLevelsText,
        fontFamily: 'Microsoft YaHei', fontSize: 11,
        rich: {{
          zg:  {{ fill: '{GOLD}', fontWeight: 'bold' }},
          zd:  {{ fill: '{GOLD}', fontWeight: 'bold' }},
          fib: {{ fill: '#7c3aed', fontSize: 10 }}
        }}
      }}
    }}];
  }}
  chart.setOption(option);
}})();
</script>"""


# ================= 区间导航条（缩略图 + 可拖窗口） =================
NAV_H = 24

def navigator_svg(klines, sym):
    closes = [k["close"] for k in klines]
    n = len(closes)
    lo, hi = min(closes), max(closes)
    span = hi - lo or 1

    def x(i):
        return W * i / (n - 1)

    # 上下各留 2px，缩略曲线在 2..18 之间，手柄高度与槽等齐，整体更窄更精致。
    def y(v):
        return 2 + (NAV_H - 6) * (1 - (v - lo) / span)

    pts = [(x(i), y(c)) for i, c in enumerate(closes)]
    nav_d = _smooth(pts, tension=0.8)
    return f'''<svg id="nav-{sym}" viewBox="0 0 {W} {NAV_H}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg"
      style="width:100%;height:auto;display:block;border-top:1px solid #eef2f7;user-select:none;touch-action:none;text-rendering:geometricPrecision;shape-rendering:geometricPrecision">
  <rect width="{W}" height="{NAV_H}" fill="#f8fafc"/>
  <path d="{nav_d}" fill="none" stroke="{GRAY}" stroke-width="1" stroke-linejoin="round" stroke-linecap="round"/>
  <rect id="sl-{sym}" x="0" y="0" width="0" height="{NAV_H}" fill="#cbd5e1" fill-opacity="0.38"/>
  <rect id="sr-{sym}" x="{W}" y="0" width="0" height="{NAV_H}" fill="#cbd5e1" fill-opacity="0.38"/>
  <rect id="wb-{sym}" x="0" y="0" width="{W}" height="{NAV_H}" fill="{BLUE}" fill-opacity="0.05" stroke="{BLUE}" stroke-width="0.8" style="cursor:grab"/>
  <rect id="hl-{sym}" x="-4" y="0" width="8" height="{NAV_H}" fill="{BLUE}" rx="2" style="cursor:ew-resize"/>
  <rect id="hr-{sym}" x="{W - 4}" y="0" width="8" height="{NAV_H}" fill="{BLUE}" rx="2" style="cursor:ew-resize"/>
</svg>'''


NAV_JS = """
function initNav(sym, W, H){
  var main=document.getElementById('main-'+sym);
  var plot=document.getElementById('plot-'+sym);
  var lbl=document.getElementById('lbl-'+sym);
  var nav=document.getElementById('nav-'+sym);
  var hl=document.getElementById('hl-'+sym), hr=document.getElementById('hr-'+sym);
  var wb=document.getElementById('wb-'+sym), sl=document.getElementById('sl-'+sym), sr=document.getElementById('sr-'+sym);
  var n=+main.getAttribute('data-n');
  var PAD_L=12, PAD_R=78, PLOT_W=W-PAD_L-PAD_R;
  var s=0, e=1, mode=null, sx=0, ss=0, se=0;
  // 横向 dataZoom：仅缩放图形层 X 轴，纵向高度固定不变、文字由 JS 重算位置(不变形)
  function apply(){
    var a=1/(e-s), b=(PAD_L*(e-s-1)-PLOT_W*s)/(e-s);
    plot.setAttribute('transform','translate('+b+',0) scale('+a+',1)');
    main.setAttribute('data-s',s); main.setAttribute('data-e',e);
    var xs=lbl.querySelectorAll('[data-i]');
    for(var t=0;t<xs.length;t++){
      var el=xs[t]; var i=+el.getAttribute('data-i'); var dx=+(el.getAttribute('data-dx')||0);
      var xo=PAD_L+PLOT_W*i/(n-1);
      var X=a*xo+b;
      var attr=(el.tagName.toLowerCase()==='circle')?'cx':'x';
      if(X<PAD_L-1||X>W-PAD_R+1){ el.style.display='none'; }
      else { el.style.display=''; el.setAttribute(attr, X+dx); }
    }
    var lt=document.getElementById('latest-'+sym);
    if(lt){ var xl=PAD_L+PLOT_W; var Xl=a*xl+b; lt.style.display=(Xl<PAD_L-1||Xl>W-PAD_R+1)?'none':''; }
    sl.setAttribute('width',s*W);
    sr.setAttribute('x',e*W); sr.setAttribute('width',(1-e)*W);
    wb.setAttribute('x',s*W); wb.setAttribute('width',(e-s)*W);
    hl.setAttribute('x',s*W-4); hr.setAttribute('x',e*W-4);
  }
  function px(ev){ return ev.touches ? ev.touches[0].clientX : ev.clientX; }
  function down(m){ return function(ev){ mode=m; sx=px(ev); ss=s; se=e; ev.preventDefault(); }; }
  hl.addEventListener('mousedown',down('l')); hr.addEventListener('mousedown',down('r')); wb.addEventListener('mousedown',down('m'));
  hl.addEventListener('touchstart',down('l'),{passive:false}); hr.addEventListener('touchstart',down('r'),{passive:false}); wb.addEventListener('touchstart',down('m'),{passive:false});
  function move(ev){
    if(!mode) return;
    var w=nav.getBoundingClientRect().width;
    var dx=(px(ev)-sx)/w;
    if(mode==='l'){ s=Math.min(Math.max(ss+dx,0), se-0.05); }
    else if(mode==='r'){ e=Math.max(Math.min(se+dx,1), ss+0.05); }
    else { var len=se-ss; s=Math.min(Math.max(ss+dx,0),1-len); e=s+len; }
    apply(); ev.preventDefault();
  }
  function up(){ mode=null; }
  document.addEventListener('mousemove',move); document.addEventListener('mouseup',up);
  document.addEventListener('touchmove',move,{passive:false}); document.addEventListener('touchend',up);
  nav.addEventListener('dblclick',function(){ s=0; e=1; apply(); });
  apply();
}
"""


# ================= 未来走势推演图（原则化：实测幅度投影 + ZG/ZD 锚定 + 概率 + 置信锥 + 失效位） =================
def _interp(path, f):
    if f <= path[0][0]:
        return path[0][1]
    if f >= path[-1][0]:
        return path[-1][1]
    for i in range(len(path) - 1):
        f0, v0 = path[i]
        f1, v1 = path[i + 1]
        if f0 <= f <= f1:
            t = (f - f0) / (f1 - f0) if f1 > f0 else 0
            return v0 + (v1 - v0) * t
    return path[-1][1]


def forecast_svg(klines, r, wcls, conf, sigma, sym, horizon=60, bt=None, bt_paths=None, breadth_score=None):
    closes = [k["close"] for k in klines]
    n = len(closes)
    tail = closes[-120:]
    last = closes[-1]
    zs = r["zhongshu"][-1] if r["zhongshu"] else None
    zg = zs["zg"] if zs else last * 1.05
    zd = zs["zd"] if zs else last * 0.95
    mid = (zg + zd) / 2
    # 缺口参考线（推演图叠加）：未补且贴近现价的跳空缺口 = 未来支撑/压力位，
    # 与中枢 ZD/ZG、Fib 位共同构成交叉验证的目标/失效锚。仅取最近±15%内最多2条，避免拥挤。
    _gap_refs = [g for g in r.get("gaps", []) if not g["filled"]
                 and abs((g["top"] + g["bottom"]) / 2 / last - 1) <= 0.15]
    _gap_refs.sort(key=lambda g: abs((g["top"] + g["bottom"]) / 2 / last - 1))
    _gap_refs = _gap_refs[:2]
    sc = r["classify"]["scenario"]
    cls_dir = r["classify"]["last_bi_dir"]
    wdir = wcls["last_bi_dir"]
    aligned = (cls_dir == wdir)
    # 最近完成的笔幅度，作为"实测幅度投影"基准
    comp = r["bis"][-2] if len(r["bis"]) >= 2 else r["bis"][-1]
    move = max(abs(comp["end_price"] / comp["start_price"] - 1), 0.03)

    # ---- 趋势外推（独立交叉验证·多窗口 #预测优化·A）：对 20/60/120 日三窗口各做对数线性回归，
    #      以「多窗口斜率方向一致性」判定趋势是否确立（单一窗口易被近期急拉带偏）；主窗口(≤90日)
    #      外推终点仍用于推演图叠加。并以主窗口「前/后半段斜率差」近似加速度衰减（背驰量化佐证）。----
    def _loglin(window):
        n = len(window)
        if n < 10:
            return 0.0, 0.0, None
        xs = list(range(n))
        ys = [math.log(c) for c in window]
        mx = sum(xs) / n; my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        slope = sxy / sxx if sxx else 0.0
        yhat = [my + slope * (x - mx) for x in xs]
        ss_res = sum((y - yh) ** 2 for y, yh in zip(ys, yhat))
        ss_tot = sum((y - my) ** 2 for y in ys)
        r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
        return slope, r2, math.exp(slope * horizon)
    _win_sizes = [20, 60, 120]
    _wins = [closes[-w:] for w in _win_sizes if len(closes) >= w]
    _slopes = []
    for w in _wins:
        sl, _r2w, _ = _loglin(w)
        _slopes.append(sl)
    # 主窗口（保持推演图视觉连续）：最近 min(horizon,90) 日
    _tw = closes[-min(horizon, 90):]
    _main_slope, _r2, trend_end = _loglin(_tw)
    # 多窗口方向共识：所有可用窗口斜率同号（全上行/全下行）
    _agree_dir = (len(_slopes) >= 2) and (all(s > 0 for s in _slopes) or all(s < 0 for s in _slopes))
    # 加速度衰减（主窗口前/后半段斜率差）：上行情景后半段斜率明显低于前半段 → 涨速衰减（背驰信号）
    _decay = 0.0
    if len(_tw) >= 12:
        _h = len(_tw) // 2
        _sla, _, _ = _loglin(_tw[:_h])
        _slb, _, _ = _loglin(_tw[_h:])
        _decay = _slb - _sla

    # ---- 三路径端点（锚定 ZG/ZD/现价/实测幅度）----
    if sc == "多头延续":
        up_tgt = max(zg * 1.01, last * (1 + move))
        main_p = [(0, last), (0.25, zg * 1.003), (0.55, up_tgt), (1.0, up_tgt * 1.03)]
        main_lab = "主路径：多头延续（回踩ZG不破再上）"
        alt_p = [(0, last), (0.3, mid), (0.6, mid), (1.0, zg * 0.99)]
        risk_p = [(0, last), (0.25, zd * 1.01), (0.55, zd * 0.98), (1.0, zd * 0.94)]
    elif sc in ("中枢震荡偏多", "高位整理未破前高"):
        main_p = [(0, last), (0.2, zg * 1.004), (0.45, mid), (0.7, zg), (1.0, zg * 1.03)]
        main_lab = "主路径：震荡偏多（试ZG-回落-突破）"
        alt_p = [(0, last), (0.25, mid), (0.5, zd * 1.01), (0.8, mid), (1.0, mid)]
        risk_p = [(0, last), (0.25, zd), (0.55, zd * 0.98), (1.0, zd * 0.94)]
    elif sc == "背驰见底机会":
        main_p = [(0, last), (0.2, mid), (0.5, zg * 0.99), (1.0, zg * 1.02)]
        main_lab = "主路径：底背驰反弹（向中枢上沿回升）"
        alt_p = [(0, last), (0.3, mid), (1.0, mid)]
        risk_p = [(0, last), (0.25, zd * 0.99), (1.0, zd * 0.93)]
    elif sc in ("背驰见顶风险", "中枢震荡偏空", "弱势反弹", "空头延续"):
        main_p = [(0, last), (0.2, zg), (0.5, mid), (1.0, mid * 0.99)]
        main_lab = "主路径：回落中枢震荡"
        alt_p = [(0, last), (0.3, zd * 1.01), (1.0, zd * 0.98)]
        risk_p = [(0, last), (0.25, zd * 0.99), (1.0, zd * 0.92)]
    else:
        # 数据不足或其余情形：中性中枢震荡（不默认看多）
        main_p = [(0, last), (0.25, mid), (0.5, mid), (1.0, mid)]
        main_lab = "主路径：中枢内中性震荡"
        alt_p = [(0, last), (0.3, zg * 0.99), (1.0, zg * 0.97)]
        risk_p = [(0, last), (0.3, zd * 1.01), (1.0, zd * 0.99)]

    # 趋势外推与主路径（中点）吻合度：两独立方法指向同一区间 → 预测可信度更高。
    # 关键约束：拟合优度 R² 须达标（≥0.25，已收紧 #预测优化·A）且多窗口方向共识，才授予共振增益——
    # 否则低拟合度或方向分歧下"吻合"纯属巧合，据此 +2% 概率属虚增置信度。
    _main_mid = (main_p[0][1] + main_p[-1][1]) / 2
    _R2_TH = 0.25
    trend_agree = bool(_main_mid and abs(trend_end - _main_mid) / _main_mid < 0.06
                       and _r2 >= _R2_TH and _agree_dir)
    trend_weak = _r2 < _R2_TH
    # 趋势衰减提示（多窗口上行共识但主窗口加速度衰减）：背驰可能的量化信号，
    # 共振增益不额外授予（不直接改结构路径几何）
    _trend_decaying = bool(_agree_dir and _main_slope > 0 and _decay < -1e-5 and _r2 > 0.3)

    # 结构存续概率（锥模型）：用与置信锥同款 σ（horizon 日前向收益波动）推导
    # 「期末价 ≥ ZD」的概率 = Φ(ln(现价/ZD) / σ)，μ 取 0（随机游走中性假设）。
    # 这是「主/次/风险」情景概率之外、由置信锥模型直接给出的、纯统计的「结构是否守住失效位」
    # 概率，与置信锥内部自洽，作为预测可信度的独立参照：情景概率衡量方向性演绎（续涨/震荡/跌），
    # 存续概率衡量「不破 ZD」，两者口径不同、互为参照（现价远高于 ZD 时存续概率天然偏高）。
    _p_hold = (0.5 * (1 + math.erf((math.log(last / zd)) / (sigma * math.sqrt(2))))
               if (sigma > 0 and last > 0 and zd > 0) else 0.5)

    # ---- 概率（经验校准 + 结构锚 + 推演置信度 + 结论稳定性微调）----
    # 先按结构分类给基准概率，再叠加置信度偏离与稳定性；避免对“背离/背驰”重复惩罚导致全部贴地板。
    _base_p = {
        "多头延续": 0.58,
        "中枢震荡偏多": 0.50, "高位整理未破前高": 0.50,
        "背驰见顶风险": 0.40, "中枢震荡偏空": 0.40, "弱势反弹": 0.36, "空头延续": 0.34,
    }
    # 经验校准（#1）：优先用本指数"最近且样本足够"的真实信号类型做锚，而非按情景猜一类买/卖。
    # 关键修正：锚点必须与第一/二类买卖方向一致——牛市情景只锚「买点」类信号、熊市只锚「卖点」类，
    # 否则会出现「多头延续的指数却用卖点胜率校准」的方向错配，既削弱准确率又产生自相矛盾的结论文字。
    _bull = sc in ("多头延续", "中枢震荡偏多", "高位整理未破前高", "背驰见底机会")
    _bear = sc in ("背驰见顶风险", "中枢震荡偏空", "弱势反弹", "反弹未回中枢", "空头延续")
    _main_dir = 1 if _bull else (-1 if _bear else 0)
    _buy_kinds = ("一类买", "二类买", "三类买")
    _sell_kinds = ("一类卖", "二类卖", "三类卖")
    _want = _buy_kinds if _bull else (_sell_kinds if _bear else ())
    _anchor_kind = None
    for s in sorted([s for s in r["signals"] if s["bi_index"] >= len(r["bis"]) - 60],
                    key=lambda x: -x["bi_index"]):
        k = s["kind"][:3]
        if k in _want and bt.get(k, {}).get(20, {}).get("n", 0) >= 5:
            _anchor_kind = k
            break
    # 方向一致的信号样本不足时保持 None -> 退回启发式基准概率（避免方向错配）
    emp_wr, emp_n, emp_h, emp_ar, emp_har = None, 0, 0, None, None
    if bt:
        st20 = bt.get(_anchor_kind, {}).get(20)
        if st20 and st20["n"] >= 5:
            emp_wr, emp_n, emp_ar = st20["win_rate"], st20["n"], st20["avg_ret"]
        st60 = bt.get(_anchor_kind, {}).get(60)
        if st60 and st60["n"] >= 5:
            emp_h, emp_har = st60["win_rate"], st60["avg_ret"]
    # 经验校准（双重锚定 + 贝叶斯收缩，#23）：
    #  (a) 买卖点类 20 日同向胜率（backtest_signals）——方向性信号历史兑现；
    #  (b) 路径命中率（backtest_paths·by_dir）——与推演图主/次/风险路径直接对应的历史兑现率，
    #      是最贴合 p_main 定义的经验真值，优先锚定；样本不足时退回(a)、再退回启发式基准。
    #  低样本/宽置信区间时向基准收缩（贝叶斯收缩权重 n/(n+12)），避免小样本噪声过度拉动概率。
    _w = emp_n / (emp_n + 12.0) if (emp_wr is not None and emp_n >= 5) else 0.0
    _dir = (bt_paths or {}).get("by_dir", {}).get(_main_dir) if _main_dir != 0 else None
    _dir_n = _dir.get("n", 0.0) if _dir else 0.0
    _w_dir = min(0.85, _dir_n / (_dir_n + 12.0) * 1.6) if _dir_n >= 8 else 0.0  # 提高命中率锚权重(#预测优化·B)
    _base = _base_p.get(sc, 0.45)
    if _w_dir > 0:
        _dir_main = _dir["main"] / _dir_n
        _dir_alt = _dir["alt"] / _dir_n
        _dir_risk = _dir["risk"] / _dir_n
        p_main = _base * (1 - _w_dir) + _dir_main * _w_dir
    elif _w > 0:
        _emp_p = 0.5 + (emp_wr - 0.5) * 0.7
        p_main = _base * (1 - _w) + _emp_p * _w
        _dir_alt = _dir_risk = None
    else:
        p_main = _base
        _dir_alt = _dir_risk = None
    p_main += (conf - 50) / 100 * 0.30
    # 结论稳健度微调（#29·分级，取代此前"一律-0.04"的粗暴惩罚）：
    #   敏感·待确认(极性翻转·当前方向结论依赖最近年轻笔) → -0.04；
    #   边缘(趋势守住但最后笔年轻) → -0.02；稳健 → 0。
    _stab = r.get("stability") or {}
    _level = _stab.get("level", "稳健")
    if _level == "敏感·待确认":
        p_main -= 0.04
    elif _level == "边缘":
        p_main -= 0.02
    # 共振增益：多种独立方法指向同一结论 → 显式提升主路径概率（仍在夹逼范围内）
    if trend_agree and not _trend_decaying:     # 趋势外推吻合且未现加速度衰减
        p_main += 0.02
    if r["classify"].get("interval_nesting"):    # 日×周区间套共振（已修复生效）
        p_main += 0.03
    # 月度趋势共振（第三层区间套·日×周×月三重）：月线大级别背景与日线情景同向 → 多周期共振、
    # 主路径更可靠(+0.02)；反向 → 大级别压制/支撑、日线可能是反抽/回调，主路径反向微调。
    # 与日×周 nest(+0.03) 相互独立、可叠加，完善缠论「区间套」框架，提升预测准确性。
    _month_dir = r["classify"].get("month_dir", 0)
    if _month_dir != 0:
        _m_bull = (_month_dir == 1)
        if _bull and _m_bull:
            p_main += 0.02
        elif _bull and not _m_bull:
            p_main -= 0.02
        elif _bear and not _m_bull:
            p_main += 0.02
        elif _bear and _m_bull:
            p_main -= 0.02
    # 背驰级别微调（提升预测准确性）：趋势背驰=本级别大级别转折信号，方向更可靠；
    # 盘整背驰=单中枢内折返，转折级别小、可信度低。仅在现有夹逼[0.30,0.72]内小幅修正，
    # 不破坏经验校准+贝叶斯收缩的整体校准框架。
    _rbc = [b for b in r.get("beichi", []) if b["bi_index"] >= len(r["bis"]) - 3]
    _has_trend = any(b.get("bc_type") == "趋势背驰" for b in _rbc)
    _only_chaos = bool(_rbc) and all(b.get("bc_type") == "盘整背驰" for b in _rbc)
    if _bull and _has_trend:
        p_main += 0.03
    elif _bear and _has_trend:
        p_main -= 0.03
    elif _bull and _only_chaos:
        p_main -= 0.02
    elif _bear and _only_chaos:
        p_main += 0.02
    # 背驰强度（area_ratio）微调（#27·提升预测准确性）：在 bc_type 方向修正基础上，用背驰连续强度
    # refining——area_ratio（后段/前段 MACD 面积比）越小=背离越强、转折越可靠。取最近笔背驰与本级别
    # 最近段背驰中的最小 area_ratio 作为最强信号强度；强背驰(≤0.65)额外强化方向 ±0.01、弱背驰(≥0.92)
    # 反向弱化 ±0.01，均在夹逼[0.30,0.72]内小幅修正，不破坏经验校准+贝叶斯收缩的整体框架。
    _ar_bi = [b["area_ratio"] for b in r.get("beichi", []) if b["bi_index"] >= len(r["bis"]) - 5 and b.get("area_ratio")]
    _ar_seg = [b["area_ratio"] for b in r.get("seg_beichi", [])[-3:] if b.get("area_ratio")]
    _arcs = _ar_bi + _ar_seg
    if _arcs:
        _min_ar = min(_arcs)
        if _bull and _min_ar <= 0.65:
            p_main += 0.01
        elif _bear and _min_ar <= 0.65:
            p_main -= 0.01
        elif _bull and _min_ar >= 0.92:
            p_main -= 0.01
        elif _bear and _min_ar >= 0.92:
            p_main += 0.01
    # 量能量化强度（#预测优化·F）：背驰段量能较前段中位数萎缩(vol_ratio<1)→背离更可信；
    # 量增(vol_ratio>1.15)→背离可能不成立（或中继），反向弱化。仅小幅修正。
    _vr = [b.get("vol_ratio") for b in r.get("beichi", []) if b.get("vol_ratio") is not None]
    if _vr:
        _med_vr = sorted(_vr)[len(_vr) // 2]
        if _bull and _med_vr < 0.85:
            p_main += 0.01
        elif _bear and _med_vr < 0.85:
            p_main -= 0.01
        elif _bull and _med_vr > 1.15:
            p_main -= 0.01
        elif _bear and _med_vr > 1.15:
            p_main += 0.01
    # 乖离率（均值回归）微调（#22·提升预测准确性）：现价相对 MA20 乖离过大 → 短线均值回归压力。
    # 超买(涨多了)：多头情景主路径回落概率上升(-0.03)、空头情景反抽/反弹更易(+0.03)；
    # 超卖(跌多了)：反向。与缠论本级别转折信号互为印证，仅在夹逼[0.30,0.72]内小幅修正。
    _bias = r.get("bias") or {}
    if _bias:
        _b20 = _bias.get("bias20", 0) / 100.0
        if _b20 > 0.06:        # 明显/极端超买
            if _bull:
                p_main -= 0.03
            elif _bear:
                p_main += 0.03
        elif _b20 < -0.06:     # 明显/极端超卖
            if _bull:
                p_main += 0.03
            elif _bear:
                p_main -= 0.03
    # 命中率校准门控（#预测优化·B·去硬编码核心）：以上「结构微调」均为二阶修正，最终主路径概率
    # 被路径历史命中率(_dir_main)夹逼——命中率低的情景不允许被微调抬到高位，命中率高的允许上探，
    # 使概率由经验真值主导而非拍脑袋微调。
    if _w_dir > 0:
        _cap = min(0.72, 0.42 + _dir_main * 0.42)
        _floor = max(0.30, _dir_main * 0.55)
        p_main = max(_floor, min(p_main, _cap))
    # 市场广度方向门控（#预测优化·D）：系统性环境真正约束结构推演，不止装饰性微调。
    # 高层级(月/周加权)明确偏空时，买点主路径上限压 0.50；偏多反向放开（不重复增益，避免双计）。
    if breadth_score is not None:
        if _bull and breadth_score <= -0.20:
            p_main = min(p_main, 0.50)
        elif _bear and breadth_score >= 0.20:
            p_main = min(p_main, 0.50)
    p_main = max(0.30, min(0.72, round(p_main, 2)))
    # 概率归一化（修复口径错误）：此前 p_alt 写死 0.30、p_risk 触底 0.05，主路径被夹逼到
    # 高位时三者之和会 >100%（如上证强多头+高置信时 SUM=101%）。现改为从「主路径之外余量」
    # 按比例分配，三者恒和=1。余量分配优先采用路径命中率实测的次/风险比例（#23，更贴合历史），
    # 仅在样本不足时退回 55%/45% 启发式；各自底线 0.05。
    _rem = round(1 - p_main, 2)
    if _w_dir > 0 and (_dir_alt + _dir_risk) > 0:
        _r_alt = _dir_alt / (_dir_alt + _dir_risk)
        _split = round(0.55 * (1 - _w_dir) + _r_alt * _w_dir, 3)
    else:
        _split = 0.55
    p_alt = round(_rem * _split, 2)
    p_risk = round(_rem - p_alt, 2)
    if p_risk < 0.05:
        p_risk = 0.05
        p_alt = round(_rem - p_risk, 2)

    H = 300
    PAD_T3, PAD_B3 = 30, 34
    plot_w = W - PAD_L - PAD_R
    hist_w = plot_w * 0.40
    proj_w = plot_w * 0.60

    # ---- 经验分位扇形置信带（#预测精度·核心）：用真实历史 horizon 对数收益分布的分位，
    # 生成非对称 P05/P25/P75/P95 扇形锥，并以「实测漂移中位路径」为中线锚定——
    # 取代原对称 ±σ 带（A股肥尾/不对称性下，对称带会系统性低估单边极端风险、且中线未锚定统计中位）。
    # 几何口径：horizon 对数收益中位数 q50 随时间线性缩放、离散度按 √f 缩放（GBM 一致性），
    # 95/5 分位 = q50·f ± 1.645·sd·√f（sd 由真实分位反推，天然含肥尾）。
    _rets = sorted(math.log(closes[i + horizon] / closes[i]) for i in range(n - horizon)) if n > horizon else []
    def _q(p):
        if not _rets:
            return 0.0
        k = (len(_rets) - 1) * p
        f0 = int(math.floor(k)); c0 = int(math.ceil(k))
        if f0 == c0:
            return _rets[f0]
        return _rets[f0] * (c0 - k) + _rets[c0] * (k - f0)
    _q50, _q05, _q95 = _q(0.5), _q(0.05), _q(0.95)
    _sd = (_q95 - _q50) / 1.645 if _q95 > _q50 else 0.0
    def _medf(f):
        return last * math.exp(_q50 * f)
    def _bandf(f, z):
        return last * math.exp(_q50 * f + z * _sd * math.sqrt(f))
    band_ext = []
    for _f in (0.25, 0.5, 0.75, 1.0):
        band_ext.append(_bandf(_f, 1.645))   # 经验上沿(P95)
        band_ext.append(_bandf(_f, -1.645))  # 经验下沿(P05)
    band_ext.append(_medf(1.0))
    all_prices = tail + [v for _, v in main_p + alt_p + risk_p] + [zg, zd] + band_ext + [trend_end] \
        + [g["top"] for g in _gap_refs] + [g["bottom"] for g in _gap_refs]
    lo, hi = min(all_prices), max(all_prices)
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad
    span = hi - lo or 1

    def y(v):
        return PAD_T3 + (H - PAD_T3 - PAD_B3) * (1 - (v - lo) / span)

    def xh(i):
        return PAD_L + hist_w * i / (len(tail) - 1)

    def xp(f):
        return PAD_L + hist_w + proj_w * f

    p = [f'<svg id="forecast-{sym}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;text-rendering:geometricPrecision;shape-rendering:geometricPrecision">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         f'<clipPath id="fc-{sym}"><rect x="{PAD_L}" y="{PAD_T3}" width="{plot_w}" height="{H - PAD_T3 - PAD_B3}"/></clipPath>']
    p.append(f'<rect x="{PAD_L + hist_w:.1f}" y="{PAD_T3}" width="{proj_w:.1f}" height="{H - PAD_T3 - PAD_B3}" fill="#f8fafc"/>')
    for i in range(9):
        v = lo + span * i / 8
        yy = y(v)
        if i % 2 == 0:
            p.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#eef2f7"/>')
            p.append(f'<text x="{W - PAD_R + 6}" y="{yy + 4:.1f}" font-size="13" font-weight="600" fill="{GRAY}">{v:.0f}</text>')
        else:
            p.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#f4f7fb"/>')
    _lv = sorted([(y(zg), f"ZG {zg:.0f}", GOLD), (y(zd), f"ZD {zd:.0f}", GOLD), (y(last), f"现价 {last:.0f}", "#64748b")])
    _placed = []
    for yy, lab, c in _lv:
        for py in _placed:
            if abs(yy - py) < 15:
                yy = (py + 15) if yy >= py else (py - 15)
        _placed.append(yy)
        p.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="{c}" stroke-width="1" stroke-dasharray="5,4"/>')
        p.append(f'<text x="{PAD_L + 6}" y="{yy - 4:.1f}" font-size="13" font-weight="600" fill="{c}">{lab}</text>')
    # 缺口参考线（仅投影区，未来支撑/压力位）：灰色细虚线 + 标签，与 ZG/ZD 形成交叉验证
    for g in _gap_refs:
        _yy = y((g["top"] + g["bottom"]) / 2)
        _c = RED if g["type"] == "up" else GREEN  # 涨红跌绿：向上缺口支撑/向下缺口压力（与主图红绿统一）
        p.append(f'<line x1="{PAD_L + hist_w:.1f}" y1="{_yy:.1f}" x2="{W - PAD_R}" y2="{_yy:.1f}" stroke="{_c}" stroke-width="0.8" stroke-dasharray="1,5" stroke-opacity="0.55"/>')
        _glab = ("缺口支撑" if g["type"] == "up" else "缺口压力") + f" {g['bottom']:.0f}-{g['top']:.0f}"
        p.append(f'<text x="{PAD_L + hist_w + 4:.1f}" y="{_yy - 3:.1f}" font-size="10" font-weight="600" fill="{_c}">{_glab}</text>')
    tail_d = _smooth([(xh(i), y(c)) for i, c in enumerate(tail)])
    p.append(f'<path d="{tail_d}" fill="none" stroke="{BLUE}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>')
    # ---- 时间轴：历史区(左)显示真实交易日日期；投影区(右)显示推算交易日日期 ----
    _hist_k = klines[-len(tail):]
    _hd = [k["date"] for k in _hist_k]
    _last_dt = datetime.strptime(klines[-1]["date"], "%Y-%m-%d")

    def _fut(kk):
        """从最后交易日往后推算 kk 个交易日对应的日历日期（跳过周末，忽略法定节假日）"""
        dt = _last_dt
        while kk > 0:
            dt += timedelta(days=1)
            if dt.weekday() < 5:  # 0=周一 … 4=周五
                kk -= 1
        return dt.strftime("%Y-%m-%d")

    # 历史区底部：真实交易日日期刻度（约 6 个，均匀且不贴边）
    p.append(f'<text x="{PAD_L + 4:.1f}" y="{PAD_T3 - 10}" font-size="12" font-weight="700" fill="{GRAY}">近{len(tail)}日(交易日)</text>')
    p.append(f'<line x1="{PAD_L + hist_w:.1f}" y1="{PAD_T3}" x2="{PAD_L + hist_w:.1f}" y2="{H - PAD_B3}" stroke="{INK}" stroke-width="1.2" stroke-dasharray="3,3"/>')
    p.append(f'<text x="{PAD_L + hist_w:.1f}" y="{PAD_T3 - 10}" font-size="13" font-weight="700" fill="{INK}" text-anchor="middle">今日 T</text>')
    # 投影区标题（与左侧"近N日"呼应）：标注推演跨度
    p.append(f'<text x="{PAD_L + hist_w + proj_w / 2:.1f}" y="{PAD_T3 - 10}" font-size="12" font-weight="700" fill="{INK}" text-anchor="middle">未来推演 T+1→T+{horizon}（交易日）</text>')
    L = len(tail)
    _idxs = [max(1, min(L - 2, int(round(L * (j + 0.5) / 6)))) for j in range(6)]
    for i in _idxs:
        xx = xh(i)
        p.append(f'<line x1="{xx:.1f}" y1="{PAD_T3}" x2="{xx:.1f}" y2="{H - PAD_B3}" stroke="#eef2f7"/>')
        p.append(f'<text x="{xx:.1f}" y="{H - 12}" font-size="12" font-weight="600" fill="{GRAY}" text-anchor="middle">{_hd[i][5:10]}</text>')
    # 投影区底部：推算交易日日期(T+X)
    for f, kk in ((0.25, round(horizon * 0.25)), (0.5, round(horizon * 0.5)),
                   (0.75, round(horizon * 0.75)), (1.0, horizon)):
        p.append(f'<text x="{xp(f):.1f}" y="{H - 26}" font-size="11" font-weight="500" fill="{GRAY}" text-anchor="middle">T+{kk}</text>')
        p.append(f'<text x="{xp(f):.1f}" y="{H - 12}" font-size="13" font-weight="700" fill="{INK}" text-anchor="middle">{_fut(kk)}</text>')

    def draw_path(path, color, dash):
        d = _smooth([(xp(f), y(v)) for f, v in path], tension=0.9)
        p.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-dasharray="{dash}" stroke-linejoin="round" stroke-linecap="round"/>')
        p.append(f'<circle cx="{xp(path[-1][0]):.1f}" cy="{y(path[-1][1]):.1f}" r="3" fill="{color}"/>')

    # ---- 置信锥（基于历史60日前向收益波动 σ={sigma*100:.1f}%）----
    frange = [i / 50 for i in range(0, 51)]

    def band_poly(kmul):
        up, lo = [], []
        for f in frange:
            med = _interp(main_p, f)
            half = med * sigma * math.sqrt(f) * kmul
            up.append((xp(f), y(med + half)))
            lo.append((xp(f), y(med - half)))
        return " ".join(f"{a:.1f},{b:.1f}" for a, b in up + lo[::-1])

    # 置信锥裁剪到绘图区，避免 ±2σ 带超出图表边框被截断
    p.append(f'<g clip-path="url(#fc-{sym})">')
    p.append(f'<polygon points="{band_poly(2)}" fill="{RED}" fill-opacity="0.06" stroke="none"/>')
    p.append(f'<polygon points="{band_poly(1)}" fill="{RED}" fill-opacity="0.12" stroke="none"/>')
    p.append('</g>')

    # 趋势外推（独立交叉验证）：对数线性回归外推 horizon 日，青色虚线叠加
    p.append(f'<line x1="{PAD_L + hist_w:.1f}" y1="{y(last):.1f}" x2="{xp(1):.1f}" y2="{y(trend_end):.1f}" stroke="#0891b2" stroke-width="1.3" stroke-dasharray="2,5" stroke-opacity="0.85"/>')
    p.append(f'<circle cx="{xp(1):.1f}" cy="{y(trend_end):.1f}" r="2.8" fill="#0891b2"/>')

    draw_path(main_p, RED, "none")
    draw_path(alt_p, "#94a3b8", "6,4")
    draw_path(risk_p, GREEN, "2,3")
    # 路径末端就近标签（主/次/风险 + 目标位数值）：放大推演图时下方图例条常已滚出视野，
    # 末端直接标注目标价位可就近读数，解决该盲区；白描边使标签在密集线条上仍清晰；
    # 垂直防重叠间距放宽到 18px，三条路径末端 x 相同仍能清晰区分
    _ends = []
    for _p, _t, _c in ((main_p, "主", RED), (alt_p, "次", "#94a3b8"), (risk_p, "风险", GREEN)):
        _ends.append((y(_p[-1][1]), f"{_t} {_p[-1][1]:.0f}", _c, xp(_p[-1][0])))
    _ends.sort(key=lambda t: t[0])
    _ly = -1e9
    for _yy, _lab, _c, _xx in _ends:
        if abs(_yy - _ly) < 18:
            _yy = _ly + 18 if _yy >= _ly else _ly - 18
        _ly = _yy
        p.append(f'<text x="{_xx - 8:.1f}" y="{_yy - 6:.1f}" font-size="12" font-weight="700" fill="{_c}" text-anchor="end" opacity="0.95" paint-order="stroke" stroke="#ffffff" stroke-width="3">{_lab}</text>')
    # ---- hover 交互元素（默认隐藏，由 JS initForecast 驱动）----
    p.append(f'<line id="fccx-{sym}" x1="{PAD_L}" y1="{PAD_T3}" x2="{PAD_L}" y2="{H - PAD_B3}" stroke="{INK}" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>')
    p.append(f'<circle id="fcm-{sym}" r="3.6" fill="{RED}" opacity="0"/>')
    p.append(f'<circle id="fca-{sym}" r="3.6" fill="#94a3b8" opacity="0"/>')
    p.append(f'<circle id="fcr-{sym}" r="3.6" fill="{GREEN}" opacity="0"/>')
    p.append("</svg>")
    # 图例改为图表下方的 HTML 图例条（不再压住推演路径与时间轴）
    legend_html = (
        f'<div class="fc-legend">'
        f'<span><i class="ln" style="background:{RED}"></i>统计中位路径 ≈ {p_main * 100:.0f}%（漂移中位终点 {_medf(1.0):.0f}）</span>'
        f'<span><i class="ln ln-dash" style="background:#94a3b8"></i>次路径：中枢内震荡 ≈ {p_alt * 100:.0f}%</span>'
        f'<span><i class="ln ln-dot" style="background:{GREEN}"></i>风险路径：跌破ZD转空 ≈ {p_risk * 100:.0f}%</span>'
        f'<span><i class="ln ln-band"></i>置信锥 经验分位 P05–P95 / P25–P75（真实分布·非对称）</span>'
        f'<span><i class="ln ln-trend"></i>趋势外推 {trend_end:.0f}（R²={_r2:.2f}{"，弱拟合" if trend_weak else ""}）</span>'
        f'</div>'
        f'<div class="fc-targets">结构演绎目标(主路径终点) ≈ <b>{main_p[-1][1]:.0f}</b> · '
        f'统计中位终点 ≈ <b>{_medf(1.0):.0f}</b> · '
        f'风险止损位(风险路径终点) ≈ <b>{risk_p[-1][1]:.0f}</b> · '
        f'趋势外推位 ≈ <b>{trend_end:.0f}</b> · '
        f'主路径失效位(有效跌破ZD) ≈ <b>{zd:.0f}</b> · '
        f'结构存续概率(锥) ≈ <b>{_p_hold*100:.0f}%</b></div>'
    )
    note = (f"主路径失效位：现价有效跌破 ZD {zd:.0f}（收盘确认）→ 主路径失效、风险路径概率上升；风险路径确认需同时满足「跌破 ZD + 周线笔转向下」。\n"
             f"红色阴影为基于<b>真实历史 {horizon} 日对数收益分布</b>推演的<b>经验分位扇形置信带</b>（P05–P95 外层 / P25–P75 内层）：与对称 ±σ 带不同，它直接由本指数历史兑现统计得出、天然包含 A 股肥尾与涨跌不对称，"
             f"故上下带非对称——单边极端风险（如急跌）被如实反映，而非被对称假设低估。中线路径为「实测漂移中位」（并非手工情景路径），使置信带中线统计诚实；带宽随时间按 √t 扩张（随机游走特性），近月不确定性即已显著，并非线性外推的针状。\n"
             f"本图为目的（分类框架）而非点位预测：缠论给出的是「不跌破 ZD 则结构延续、跌破则转弱」的条件应对，不是对具体价位的预测。\n"
             f"趋势外推（青色虚线，对最近 {min(horizon,90)} 日收盘做对数线性回归外推 {horizon} 日）是与结构路径相互独立的验证方法，"
             + (f"但其拟合优度极低（R²={_r2:.2f}），该独立验证参考性很弱、近乎噪声，不宜据此增减仓位；"
                if trend_weak
                else ("其终点与主路径吻合（误差<6%）且拟合较稳（R²={_r2:.2f}），两法指向同一区间，预测可信度更高；"
                      if trend_agree
                      else f"其终点 ≈ {trend_end:.0f}，与主路径中点存在偏差（R²={_r2:.2f}），提示两种视角对后市节奏判断不完全一致，宜结合仓位管理；"))
             + f"若趋势外推也跌漏 ZD，则风险路径概率进一步上升。\n"
             f"主图叠加的斐波那契回调位（F38/F50/F62）与本路径上行目标、ZD 支撑相互印证：若回踩至 F61.8 附近获支撑，反弹结构更可靠；若直接跌漏 ZD，则风险路径概率上升。\n"
             f"时间轴：左侧历史区为真实交易日（MM-DD）；右侧投影区日期按「从最后交易日往后推算相应交易日、跳过周末」得到（未含法定节假日），仅供参照。")
    note += (f"\n结构存续概率（锥模型）：用与置信锥同款 σ（{sigma*100:.1f}%）推导「期末价 ≥ ZD {zd:.0f}」的概率 ≈ {_p_hold*100:.0f}%（随机游走中性假设 Φ(ln(现价/ZD)/σ)）。该值独立于「主/次/风险」情景概率——情景概率衡量方向性演绎（续涨/震荡/跌），存续概率衡量「结构是否守住失效位」，两者口径不同、互为参照；现价远高于 ZD 时存续概率天然偏高，不应与情景概率混为一谈。")
    if emp_wr is not None:
        _se = math.sqrt(emp_wr * (1 - emp_wr) / emp_n) if emp_n else 0
        _lo = max(0.0, emp_wr - 1.96 * _se)
        _hi = min(1.0, emp_wr + 1.96 * _se)
        _ci = f"95%CI [{_lo*100:.0f}%,{_hi*100:.0f}%]"
        _h = (f"；后 60 日同向胜率 {emp_h*100:.0f}%、均收益 {emp_har*100:+.1f}%（n={bt.get(_anchor_kind, {}).get(60, {}).get('n', 0)}）") if emp_h else ""
        note += (f"\n经验校准锚：历史上 {_anchor_kind}点 后 20 交易日同向胜率 {emp_wr*100:.0f}%（n={emp_n}，{_ci}）、均收益 {emp_ar*100:+.1f}%（n={emp_n}）{_h}——主路径概率据此由启发式基准向经验估计收缩（权重 {_w:.2f}）；置信区间宽、样本有限，仅供参照，不宜简单按胜率高低外推。")
    if _w_dir > 0:
        note += (f"\n路径命中率校准（#23）：历史上同类方向（{'多头' if _main_dir == 1 else '空头'}）结构，主/次/风险路径实际兑现率 "
                 f"{_dir_main*100:.0f}%/{_dir_alt*100:.0f}%/{_dir_risk*100:.0f}%（加权样本≈{_dir_n:.0f}），"
                 f"主路径概率据此由启发式基准向路径命中率收缩（权重 {_w_dir:.2f}）——这是与推演图路径定义直接对应的经验真值，"
                 f"次/风险路径占比也按实测比例分配，使三路径概率整体贴合历史兑现统计。")
    # ---- 悬浮交互数据：历史区真实收盘价 + 投影区密集采样（供 JS initForecast）----
    hist = [[_hd[i][5:10], round(tail[i], 2)] for i in range(len(tail))]
    proj = []
    for fi in range(0, 101):
        f = fi / 100.0
        med = _interp(main_p, f)
        alt = _interp(alt_p, f)
        risk = _interp(risk_p, f)
        kk = round(f * horizon)
        dt = _fut(kk)
        # 经验分位扇形（围绕实测漂移中位路径 medf）：P05/P95 外层、P25/P75 内层
        mdf = _medf(f)
        u95 = _bandf(f, 1.645); l95 = _bandf(f, -1.645)
        u75 = _bandf(f, 0.674); l75 = _bandf(f, -0.674)
        trend = round(last * math.exp(_main_slope * kk), 2)
        proj.append({"f": round(f, 3), "tplus": kk, "date": dt,
                     "main": round(med, 2), "alt": round(alt, 2), "risk": round(risk, 2),
                     "trend": trend, "med": round(mdf, 2),
                     "f95l": round(l95, 2), "f95h": round(u95 - l95, 2),
                     "f75l": round(l75, 2), "f75h": round(u75 - l75, 2)})
    fc_data = {"hist": hist, "proj": proj, "p_main": p_main, "p_alt": p_alt, "p_risk": p_risk,
               "p_hold": round(_p_hold, 3),
               "zd": round(zd, 2), "zg": round(zg, 2), "last": round(last, 2),
               "trend": round(trend_end, 2), "trend_agree": trend_agree, "trend_r2": round(_r2, 3),
               "sigma": round(sigma, 4), "horizon": horizon, "lo": round(lo, 4), "span": round(span, 4),
               "med_term": round(_medf(1.0), 2), "q50": round(_q50, 4), "q_sd": round(_sd, 4),
               "hist_dates": [_hd[i] for i in range(len(tail))],
               "gap_refs": [{"type": g["type"], "top": round(g["top"], 2), "bottom": round(g["bottom"], 2), "date": g["date"]} for g in _gap_refs]}
    return forecast_echart(sym, fc_data), note, (p_main, p_alt, p_risk), legend_html, fc_data


def forecast_echart(sym, fc_data):
    """用 ECharts 重绘缠论未来走势推演图（路径+置信锥+标注），对标主图细腻度、放大矢量清晰。"""
    hist = fc_data["hist"]
    def _norm4(s):
        # 归一化推演日期为四位年： "26-09-25" -> "2026-09-25"，避免 x 轴 formatter 取到 "-25" 负号
        return ("20" + s) if (len(s) == 8 and s[2] == "-") else s
    proj = [dict(p, date=_norm4(p["date"])) for p in fc_data["proj"]]
    zg, zd, last = fc_data["zg"], fc_data["zd"], fc_data["last"]
    p_main, p_alt, p_risk = fc_data["p_main"], fc_data["p_alt"], fc_data["p_risk"]
    gaps = fc_data.get("gap_refs", [])
    x_hist = [h[0] for h in hist]
    x_proj = [p["date"][-5:] for p in proj]   # "YY-MM-DD" -> "MM-DD"
    xcats = x_hist + x_proj
    x_full = list(fc_data.get("hist_dates", [])) + [p["date"] for p in proj]
    n_hist = len(hist)
    n_proj = len(proj)
    hist_s = [h[1] for h in hist] + [None] * n_proj
    main_s = [None] * n_hist + [p["main"] for p in proj]      # 结构演绎路径(参考·虚线)
    med_s = [None] * n_hist + [p["med"] for p in proj]        # 统计中位路径(主·实线)
    alt_s = [None] * n_hist + [p["alt"] for p in proj]
    risk_s = [None] * n_hist + [p["risk"] for p in proj]
    trend_s = [None] * n_hist + [p["trend"] for p in proj]
    f95l = [None] * n_hist + [p["f95l"] for p in proj]
    f95h = [None] * n_hist + [round(p["f95h"], 2) for p in proj]
    f75l = [None] * n_hist + [p["f75l"] for p in proj]
    f75h = [None] * n_hist + [round(p["f75h"], 2) for p in proj]
    lo = fc_data["lo"]
    ymax = round(lo + fc_data["span"], 2)
    tail_prices = [h[1] for h in hist]
    core_prices = tail_prices + [last, zg, zd] + [p["main"] for p in proj] + [p["alt"] for p in proj] + [p["risk"] for p in proj] + [p["trend"] for p in proj] + [p["med"] for p in proj]
    core_lo = min(core_prices)
    core_hi = max(core_prices)
    core_pad = (core_hi - core_lo) * 0.03
    core_lo -= core_pad
    core_hi += core_pad
    zg_v, zd_v, last_v = round(zg), round(zd), round(last)
    hlines = [
        {"yAxis": round(zg, 2), "lineStyle": {"type": "dashed", "color": GOLD, "width": 1.2},
         "label": {"show": False}},
        {"yAxis": round(zd, 2), "lineStyle": {"type": "dashed", "color": GOLD, "width": 1.2},
         "label": {"show": False}},
        {"yAxis": round(last, 2), "lineStyle": {"type": "solid", "color": "#64748b", "width": 1},
         "label": {"show": False}},
    ]
    gap_chips = []
    for g in gaps:
        _mid = (g["top"] + g["bottom"]) / 2
        _c = RED if g["type"] == "up" else GREEN
        _key = "gapup" if g["type"] == "up" else "gapdn"
        _lab = ("缺口支撑" if g["type"] == "up" else "缺口压力") + f" {g['bottom']:.0f}-{g['top']:.0f}"
        gap_chips.append((_key, _lab))
        hlines.append({"yAxis": round(_mid, 2), "lineStyle": {"type": "dashed", "color": _c, "width": 0.8, "opacity": 0.6},
                       "label": {"show": False}})
    vline = [{"xAxis": x_hist[-1], "lineStyle": {"type": "dashed", "color": INK, "width": 1.2},
              "label": {"show": False}}]
    _em, _ea, _er = proj[-1]["med"], proj[-1]["alt"], proj[-1]["risk"]
    end_points = [
        {"coord": [xcats[-1], round(_em, 2)], "value": f"主 {_em:.0f}", "itemStyle": {"color": RED}, "symbol": "circle", "symbolSize": 6,
         "label": {"show": True, "position": "top", "color": RED, "fontSize": 11, "fontWeight": "bold"}},
        {"coord": [xcats[-1], round(_ea, 2)], "value": f"次 {_ea:.0f}", "itemStyle": {"color": "#94a3b8"}, "symbol": "circle", "symbolSize": 6,
         "label": {"show": True, "position": "bottom", "color": "#94a3b8", "fontSize": 11, "fontWeight": "bold"}},
        {"coord": [xcats[-1], round(_er, 2)], "value": f"风险 {_er:.0f}", "itemStyle": {"color": GREEN}, "symbol": "circle", "symbolSize": 6,
         "label": {"show": True, "position": "bottom", "color": GREEN, "fontSize": 11, "fontWeight": "bold"}},
    ]
    f_kl = [f"{{zg|ZG {zg_v}}}", f"{{zd|ZD {zd_v}}}", f"{{last|现价 {last_v}}}"]
    for _key, _lab in gap_chips:
        f_kl.append(f"{{{_key}|{_lab}}}")
    key_levels_text = "  ".join(f_kl)

    # 确定性去重叠：推演端点（主/次/风险）标签
    dedup_mark_labels(end_points, len(xcats), core_lo, core_hi, 1100 - 96 - 64, 440 - 44 - 74, 96, 44,
                      {xcats[-1]: len(xcats) - 1})

    fdata = {
        "keyLevelsText": key_levels_text,
        "xcats": xcats, "xfull": x_full, "n_hist": n_hist, "proj": proj,
        "hist": hist_s, "main": main_s, "alt": alt_s, "risk": risk_s, "trend": trend_s,
        "lo": round(lo, 2), "ymax": ymax, "ymin_core": round(core_lo, 2), "ymax_core": round(core_hi, 2),
        "hlines": hlines, "vline": vline, "endPoints": end_points,
        "med": med_s, "f95l": f95l, "f95h": f95h, "f75l": f75l, "f75h": f75h,
        "p_main": p_main, "p_alt": p_alt, "p_risk": p_risk, "proj_raw": proj,
    }
    cid = f"echart-forecast-{sym}"
    return f'''<div class="echart-toolbar">🔍 滚轮/拖拽缩放 · 拖动底部滑块平移 · 悬停看推演路径/置信锥/趋势</div>
<div id="{cid}" class="echart-main" style="width:100%;height:440px;"></div>
<script>
(function(){{
  var D = {json.dumps(fdata, ensure_ascii=False)};
  var chart = echarts.init(document.getElementById('{cid}'));
  var option = {{
    animation: false,
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'cross', label: {{ show: false }} }},
      formatter: function(params){{
        var i = params[0].dataIndex;
        var x = D.xcats[i];
        if(i < D.n_hist){{
          var hv = D.hist[i];
          if(hv == null) return '<b>'+x+'</b>';
          return '<b>'+x+'</b><br>历史收盘 <b>'+hv.toFixed(2)+'</b>';
        }}
        var pi = i - D.n_hist;
        var p = D.proj[pi];
        if(!p) return '<b>'+x+'</b>';
        var red='#e54545', gray='#94a3b8', grn='#18a058', cyan='#0891b2';
        return '<b>推演 · T+'+p.tplus+' ('+p.date+')</b><br>'
          + '<span style="color:'+red+'">主路径 '+p.main.toFixed(2)+'</span> '+Math.round(D.p_main*100)+'%<br>'
          + '<span style="color:'+gray+'">次路径 '+p.alt.toFixed(2)+'</span> '+Math.round(D.p_alt*100)+'%<br>'
          + '<span style="color:'+grn+'">风险路径 '+p.risk.toFixed(2)+'</span> '+Math.round(D.p_risk*100)+'%<br>'
          + '<span style="color:'+cyan+'">趋势外推 '+p.trend.toFixed(2)+'</span><br>'
          + '<span style="color:#64748b">经验分位 P05~P95 '+(p.f95l).toFixed(0)+'~'+(p.f95l+p.f95h).toFixed(0)+'</span><br>'
          + '<span style="color:#64748b">P25~P75 '+(p.f75l).toFixed(0)+'~'+(p.f75l+p.f75h).toFixed(0)+'</span>';
      }}
    }},
    legend: {{ data: ['历史','统计中位路径','结构演绎路径','次路径','风险路径','趋势外推','置信锥 P05–P95','置信锥 P25–P75'], top: 2, itemGap: 8, textStyle: {{ fontSize: 11 }} }},
    grid: {{ left: 96, right: 64, top: 44, bottom: 80 }},
    xAxis: {{ type: 'category', data: D.xcats, boundaryGap: false, axisLabel: {{ fontSize: 11, margin: 6, hideOverlap: true, showMinLabel: true, showMaxLabel: false,
        interval: function(idx, val){{ if (idx === 0) return true; var c = D.xfull[idx], p = D.xfull[idx-1]; if (!c || !p) return true; if (c.slice(0,4) !== p.slice(0,4)) return true; return (idx % 24 === 0); }},
        formatter: (function(){{ var _py = null; return function(v, i){{ var d = (D.xfull && D.xfull[i]) ? D.xfull[i] : v; if (!d || d.length < 7) return v; var y = d.slice(0,4); if (i === 0 || y !== _py) {{ _py = y; return y; }} return d.slice(5); }}; }})() }} }},
    yAxis: {{ scale: false, min: D.ymin_core, max: D.ymax_core, splitNumber: 6, axisLine: {{ lineStyle: {{ color: '#cbd5e1' }} }}, splitLine: {{ lineStyle: {{ color: '#eef2f7' }} }}, axisLabel: {{ fontSize: 12, hideOverlap: true }} }},
    dataZoom: [
      {{ type: 'inside', xAxisIndex: 0, start: 0, end: 100 }},
      {{ type: 'slider', xAxisIndex: 0, start: 0, end: 100, showDetail: false, height: 16, bottom: 32, handleStyle: {{ color: '#2b6cb0' }}, borderColor: '#e2e8f0', fillerColor: 'rgba(43,108,176,0.12)' }}
    ],
    series: [
      {{ name: '历史', type: 'line', data: D.hist, symbol: 'none', smooth: true, lineStyle: {{ color: '#2b6cb0', width: 1.8 }} }},
      {{ name: '统计中位路径', type: 'line', data: D.med, symbol: 'none', smooth: true, lineStyle: {{ color: '#e54545', width: 2.4 }}, z: 5 }},
      {{ name: '结构演绎路径', type: 'line', data: D.main, symbol: 'none', smooth: true, lineStyle: {{ color: '#e54545', width: 1.4, type: 'dashed', opacity: 0.7 }}, z: 4 }},
      {{ name: '次路径', type: 'line', data: D.alt, symbol: 'none', smooth: true, lineStyle: {{ color: '#94a3b8', width: 1.6, type: 'dashed' }} }},
      {{ name: '风险路径', type: 'line', data: D.risk, symbol: 'none', smooth: true, lineStyle: {{ color: '#18a058', width: 1.6, type: 'dashed' }} }},
      {{ name: '趋势外推', type: 'line', data: D.trend, symbol: 'none', smooth: false, lineStyle: {{ color: '#0891b2', width: 1.3, type: 'dashed' }} }},
      {{ name: '置信锥 P05–P95', type: 'line', data: D.f95l, stack: 'b95', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ opacity: 0 }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '置信锥 P05–P95', type: 'line', data: D.f95h, stack: 'b95', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ color: 'rgba(229,69,69,0.06)' }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '置信锥 P25–P75', type: 'line', data: D.f75l, stack: 'b75', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ opacity: 0 }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '置信锥 P25–P75', type: 'line', data: D.f75h, stack: 'b75', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ color: 'rgba(229,69,69,0.12)' }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '参考', type: 'line', data: [], silent: true,
        markLine: {{ symbol: 'none', data: D.hlines.concat(D.vline), labelLayout: {{ moveOverlap: 'shiftY' }} }},
        markPoint: {{ data: D.endPoints }} }}
    ]
  }};
  if (D.keyLevelsText) {{
    option.graphic = [{{
      type: 'text', left: 100, top: 30, z: 100, silent: true,
      style: {{
        text: D.keyLevelsText,
        fontFamily: 'Microsoft YaHei', fontSize: 11,
        rich: {{
          zg:    {{ fill: '{GOLD}', fontWeight: 'bold' }},
          zd:    {{ fill: '{GOLD}', fontWeight: 'bold' }},
          last:  {{ fill: '#64748b', fontWeight: 'bold' }},
          gapup: {{ fill: '{RED}', fontSize: 10 }},
          gapdn: {{ fill: '{GREEN}', fontSize: 10 }}
        }}
      }}
    }}];
  }}
  chart.setOption(option);
}})();
</script>'''


# ================= 归一化对比图（日历对齐到共同交易日，#7） =================
def compare_svg(data):
    H = 300
    PAD_T2, PAD_B2 = 20, 30
    # 各指数独立拉取，节假日/停牌可能差一两天；归一化对比需对齐到共同交易日（取交集）
    date_close = {}
    common = None
    for sym, d in data.items():
        m = {}
        for k in d["klines"]:
            m[k["date"]] = k["close"]
        date_close[sym] = m
        common = set(m.keys()) if common is None else (common & set(m.keys()))
    common = sorted(common) if common else [k["date"] for k in next(iter(data.values()))["klines"]]
    n = len(common)
    series = {}
    for sym, m in date_close.items():
        base = m[common[0]]
        series[sym] = [m[dt] / base * 100 for dt in common]
    allv = [v for s in series.values() for v in s]
    lo, hi = min(allv), max(allv)
    span = hi - lo or 1
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T2 - PAD_B2

    def x(i):
        return PAD_L + plot_w * i / (n - 1)

    def y(v):
        return PAD_T2 + plot_h * (1 - (v - lo) / span)

    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;text-rendering:geometricPrecision;shape-rendering:geometricPrecision">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
    for i in range(9):
        v = lo + span * i / 8
        yy = y(v)
        if i % 2 == 0:
            p.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#eef2f7"/>')
            p.append(f'<text x="{W - PAD_R + 6}" y="{yy + 4:.1f}" font-size="13" font-weight="600" fill="{GRAY}">{v:.0f}</text>')
        else:
            p.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#f4f7fb"/>')
    # 基准线 100
    p.append(f'<line x1="{PAD_L}" y1="{y(100):.1f}" x2="{W - PAD_R}" y2="{y(100):.1f}" stroke="{INK}" stroke-width="1" stroke-dasharray="4,4" stroke-opacity="0.5"/>')
    # 年份线（基于共同交易日）
    seen = set()
    for i, dt in enumerate(common):
        yr = dt[:4]
        if yr not in seen:
            seen.add(yr)
            p.append(f'<line x1="{x(i):.1f}" y1="{PAD_T2}" x2="{x(i):.1f}" y2="{H - PAD_B2}" stroke="#eef2f7"/>')
            p.append(f'<text x="{x(i) + 4:.1f}" y="{H - 10}" font-size="14" font-weight="600" fill="{GRAY}">{yr}</text>')
    p.append(f'<rect x="{PAD_L}" y="{PAD_T2}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#e2e8f0"/>')
    for sym, s in series.items():
        d = _smooth([(x(i), y(s[i])) for i in range(n)])
        p.append(f'<path d="{d}" fill="none" stroke="{IDX_COLORS[sym]}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>')
    # 终点数值标签（按终值排序防重叠）
    ends = sorted(((s[-1], sym) for sym, s in series.items()), reverse=True)
    placed = []
    for val, sym in ends:
        yy = y(val)
        for py in placed:
            if abs(yy - py) < 13:
                yy = py + 13 if yy <= py else py - 13
        placed.append(yy)
        name = data[sym]["name"]
        p.append(f'<text x="{W - PAD_R + 4}" y="{yy + 4:.1f}" font-size="14" font-weight="600" fill="{IDX_COLORS[sym]}">{name} {val:.0f}</text>')
    # 图例
    lx = PAD_L + 8
    for sym, d in data.items():
        p.append(f'<line x1="{lx}" y1="14" x2="{lx + 18}" y2="14" stroke="{IDX_COLORS[sym]}" stroke-width="2.5"/>')
        p.append(f'<text x="{lx + 23}" y="18" font-size="14" font-weight="600" fill="{INK}">{d["name"]}</text>')
        lx += 23 + len(d["name"]) * 13 + 26
    p.append("</svg>")
    return "".join(p)


# ================= 卡片火花线 / 评分芯片 =================
def sparkline(klines, color, w=150, h=34):
    closes = [k["close"] for k in klines][-60:]
    if len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    span = hi - lo or 1
    sw = w - 4
    sp = [(4 + sw * i / (len(closes) - 1), h - 4 - (h - 8) * (c - lo) / span) for i, c in enumerate(closes)]
    d = _smooth(sp, tension=0.8)
    return f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg"><path d="{d}" fill="none" stroke="{color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>'


def badge(text, color, icon=''):
    """统一实心胶囊标签：白字 + 彩色背景"""
    return f'<span class="badge" style="background:{color}">{icon}{text}</span>'


def score_chip(score, label=''):
    """健康度/置信度等数值评分胶囊"""
    if score >= 66:
        c = RED
    elif score >= 45:
        c = "#d97706"
    else:
        c = GREEN
    txt = f'{score}' if not label else f'{score} {label}'
    return badge(txt, c)


def prob_bar(pct, color):
    """推演概率内嵌水平迷你条（图表细腻度：一眼比较三路径/存续概率高低）。"""
    w = max(0, min(100, pct * 100))
    return (f'<div style="height:5px;width:56px;margin:3px auto 0;border-radius:3px;'
            f'background:#eef2f7;overflow:hidden">'
            f'<i style="display:block;height:100%;width:{w:.0f}%;background:{color}"></i></div>')


def path_hit_html(scenario, pb, p_main, p_alt, p_risk, horizon=60):
    """推演路径历史命中率自校验（预测准确性核心）：把本报告 p_main/p_alt/p_risk 与
    历史上同类方向结构（同 _path_targets 判定的 main_dir）的实际路径兑现率对照，
    暴露校准偏差（偏乐观/偏保守/一致）。pb 由 backtest_paths 预计算（horizon 与推演图一致）。"""
    main_dir = _path_targets(scenario, 0, 0, 0, 0)[2]
    e = pb["by_dir"].get(main_dir)
    if not e or e["n"] < 8:
        e = pb["total"]
    n = e["n"]
    mr = e["main"] / n * 100 if n else 0
    ar = e["alt"] / n * 100 if n else 0
    rr = e["risk"] / n * 100 if n else 0
    rows = (("主路径", mr, p_main * 100, RED),
            ("次路径", ar, p_alt * 100, "#64748b"),
            ("风险路径", rr, p_risk * 100, GREEN))
    body = "".join(
        '<div class="pc-row"><span class="pc-lab" style="color:{c}">{lab}</span>'
        '<span class="pc-bar"><i style="width:{hw:.0f}%;background:{c}"></i></span>'
        '<span class="pc-h">历史 {h:.0f}%</span>'
        '<span class="pc-p">本报告 {p:.0f}%</span></div>'.format(c=c, lab=lab, hw=max(h, 2), h=h, p=p)
        for lab, h, p, c in rows)
    dev = mr - p_main * 100
    if dev < -8:
        calib = '<span style="color:{RED};font-weight:700">偏乐观 — 历史主路径兑现更低，宜谨慎看待主路径</span>'.format(RED=RED)
    elif dev > 8:
        calib = '<span style="color:{GREEN};font-weight:700">偏保守 — 历史主路径兑现更高，可适度乐观</span>'.format(GREEN=GREEN)
    else:
        calib = '<span style="color:#0891b2;font-weight:700">基本一致</span>'
    return ('<div class="pathcheck"><b>推演路径命中率自校验</b>'
            '<span class="pc-sub">历史同类方向结构（h={h}日，N={n}）：未来实际走势落入各路径的比例，与本报告概率对照</span>'
            '{body}<div class="pc-calib">校准结论：{calib}</div></div>').format(h=horizon, n=n, body=body, calib=calib)



# ================= 卡片 =================
def card_html(sym, name, klines, r, wcls, health, conf):
    last, prev = klines[-1], klines[-2]
    closes = [k["close"] for k in klines]
    chg = (last["close"] / prev["close"] - 1) * 100
    color = RED if chg >= 0 else GREEN
    cls = r["classify"]
    five_yr = (last["close"] / klines[0]["close"] - 1) * 100
    fy_color = RED if five_yr >= 0 else GREEN
    one_yr = (last["close"] / klines[max(0, len(klines) - 250)]["close"] - 1) * 100
    oy_color = RED if one_yr >= 0 else GREEN
    ann_vol = realized_vol_annualized(closes)
    vol_txt = ("%.1f%%" % (ann_vol * 100)) if ann_vol else "—"
    m20 = (last["close"] / klines[max(0, len(klines) - 21)]["close"] - 1) * 100
    m20_txt = "%+.2f%%" % m20
    m20_color = RED if m20 >= 0 else GREEN
    sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
    amp = abs(cls.get("last_bi_pct", 0)) * 100
    spark = sparkline(klines, RED if chg >= 0 else GREEN)
    agree = r["agreement"]["rate"] * 100
    ma = cls.get("ma_alignment")
    ma_txt = ma["alignment"] if ma else "—"
    ma_color = {"多头排列": RED, "空头排列": GREEN, "纠缠": "#64748b"}.get(ma_txt, "#64748b")
    nest = cls.get("interval_nesting")
    nest_txt = "区间套✓" if nest else "—"
    nest_color = "#b45309" if nest else "#64748b"
    mctx = cls.get("month_context") or ""
    mctx_txt = (mctx.split("(")[0] if mctx else "—")
    mctx_color = "#7c3aed" if mctx else "#64748b"
    # 多周期共振（日/周/月三层方向联立）：结构化呈现区间套结论，替代原"区间套✓/月线背景"两行简略字段
    _wsc = cls.get("week_scenario") or "—"
    _msc = cls.get("month_scenario") or "—"
    _res = cls.get("resonance") or "—"
    _dd = "↑" if cls.get("last_bi_dir") == 1 else ("↓" if cls.get("last_bi_dir") == -1 else "—")
    _wd = "↑" if cls.get("week_dir") == 1 else ("↓" if cls.get("week_dir") == -1 else "—")
    _md = "↑" if cls.get("month_dir") == 1 else ("↓" if cls.get("month_dir") == -1 else "—")
    w_color = SCENARIO_COLOR.get(_wsc, "#64748b")
    m_color2 = SCENARIO_COLOR.get(_msc, "#64748b")
    _res_color = ("#18a058" if ("共振" in _res and "空" not in _res)
                  else ("#e54545" if ("共振" in _res and "空" in _res)
                        else ("#d97706" if ("背离" in _res or "未确认" in _res) else "#0891b2")))
    _zs = r["zhongshu"][-1] if r["zhongshu"] else None
    _zs_txt = ("%s · %d笔" % ("延伸" if _zs.get("extension") else "标准", _zs["count"])) if _zs else "—"
    _tt = cls.get("trend_type", "—")
    _tt_color = {"上涨走势(趋势)": RED, "下跌走势(趋势)": GREEN, "扩张/盘整走势": "#64748b", "盘整走势": "#64748b"}.get(_tt, "#0f172a")
    # 关键缺口（未补，±18%内最近3个）—— 中枢之外最重要的价位锚，A股「逢缺必补」规律下意义显著
    _gaps_unf = [g for g in r.get("gaps", []) if not g["filled"]]
    _gaps_near = [g for g in _gaps_unf
                  if abs((g["top"] + g["bottom"]) / 2 / last["close"] - 1) <= 0.18]
    _gaps_near.sort(key=lambda g: g["idx"])
    _gap_items = []
    for g in _gaps_near[-3:]:
        _mid = (g["top"] + g["bottom"]) / 2
        _dist = (_mid / last["close"] - 1) * 100
        _arrow = "▲" if g["type"] == "up" else "▼"
        _role = "支撑" if _dist < 0 else "压力"
        _gap_items.append("%s%d-%d(%s%.0f%%)" % (_arrow, g["bottom"], g["top"], _role, _dist))
    _gap_txt = "　".join(_gap_items) if _gap_items else "—"
    # 乖离率（#22）：现价偏离 MA20 的程度，量化短线超买/超卖（均值回归压力）
    _bias = r.get("bias") or {}
    _bias20 = _bias.get("bias20", 0)
    _bias_state = _bias.get("state", "—")
    _bias_level = _bias.get("level", "—")
    _bias_color = {"超买": "#b45309", "超卖": "#2563eb", "中性": "#64748b"}.get(_bias_state, "#64748b")
    # ADX 趋势强度（#专业度）：标准趋势强度指标，与缠论方向判断互补
    _adx = r.get("adx") or {}
    _adx_val = _adx.get("adx")
    _adx_txt = ("%.0f·%s" % (_adx_val, _adx.get("trend", ""))) if _adx_val is not None else "—"
    _adx_color = ("#18a058" if (_adx_val or 0) >= 25 else ("#d97706" if (_adx_val or 0) >= 20 else "#64748b"))
    # 最大回撤（专业风险度量，与年化波动率互补）
    _mdd = r.get("mdd") or {}
    _mdd_txt = ("%.1f%%" % _mdd.get("mdd", 0)) if _mdd else "—"
    # 量能趋势（放量/缩量，与量价背离/背驰缩量确认互为印证）
    _vt = r.get("vol_trend") or {}
    _vt_txt = ("%s %.2fx" % (_vt.get("state", ""), _vt.get("ratio", 1))) if _vt else "—"
    _vt_color = {"放量": "#e54545", "缩量": "#2563eb", "温和": "#64748b"}.get(_vt.get("state"), "#64748b")
    # 信号成熟度（#29·稳健度三级重构）：最后一支已完成笔跨度，年轻信号属"待确认"而非可靠结论
    _stab = r.get("stability") or {}
    _mat = _stab.get("maturity", "established")
    _lv = _stab.get("level", "稳健")
    _lbb = _stab.get("last_bi_bars", 0)
    _mat_txt = ("信号成熟" if _mat == "established" else "信号年轻·待确认")
    _mat_c = ("#18a058" if _mat == "established" else "#d97706")
    _mat_chip = badge(f'{_mat_txt} · 末笔{_lbb}日', _mat_c)
    return f"""
    <div class="card" id="card-{sym}" data-sym="{sym}" data-jump style="border-left:4px solid {sc_color};cursor:pointer">
      <div class="card-head"><span class="idx-name">{name}</span><span class="sym">{sym}</span></div>
      <div class="price">{last["close"]:.2f} <span style="color:{color}">{'+' if chg >= 0 else ''}{chg:.2f}%</span></div>
      <div class="spark">{spark}</div>
      <div class="kv"><span>近5年涨跌(前复权)</span><b style="color:{fy_color}">{'+' if five_yr >= 0 else ''}{five_yr:.2f}%</b></div>
      <div class="kv"><span>近1年涨跌</span><b style="color:{oy_color}">{'+' if one_yr >= 0 else ''}{one_yr:.2f}%</b></div>
      <div class="kv"><span>年化波动率</span><b>{vol_txt}</b></div>
      <div class="kv"><span>近20日涨跌(动量)</span><b style="color:{m20_color}">{m20_txt}</b></div>
      <div class="kv"><span>乖离率(MA20)</span><b style="color:{_bias_color}">{_bias20:+.1f}% {_bias_state}{_bias_level}</b></div>
      <div class="kv"><span>ADX 趋势强度(14)</span><b style="color:{_adx_color}">{_adx_txt}</b></div>
      <div class="kv"><span>最大回撤(全样本)</span><b>{_mdd_txt}</b></div>
      <div class="kv"><span>量能趋势(20/60日)</span><b style="color:{_vt_color}">{_vt_txt}</b></div>
      <div class="kv"><span>笔 / 中枢 / 背驰 / 段背驰</span><b>{len(r["bis"])} / {len(r["zhongshu"])} / {len(r["beichi"])} / {len(r["seg_beichi"])}（顶×{sum(1 for _b in r.get("seg_beichi", []) if _b["type"] == "top")}/底×{sum(1 for _b in r.get("seg_beichi", []) if _b["type"] == "bottom")}）</b></div>
      <div class="kv"><span>最近一笔</span><b>{'↑' if cls.get('last_bi_dir') == 1 else '↓'} {amp:.1f}%</b></div>
      <div class="kv"><span>当前分类</span><b style="color:{sc_color}">{cls["scenario"]}</b></div>
      <div class="kv"><span>走势类型</span><b style="color:{_tt_color}">{_tt}</b></div>
      <div class="kv"><span>最后中枢</span><b>{_zs_txt}</b></div>
      <div class="kv"><span>关键缺口(未补)</span><b style="color:#475569;font-size:11px">{_gap_txt}</b></div>
      <div class="kv"><span>均线排列(MA20/60/250)</span><b style="color:{ma_color}">{ma_txt}</b></div>
      <div class="kv"><span>多周期共振</span><b style="font-size:11px;line-height:1.55">
        <span style="color:{sc_color}">日 {_dd}</span>·
        <span style="color:{w_color}">周 {_wd}</span>·
        <span style="color:{m_color2}">月 {_md}</span>
        <span style="color:#94a3b8">（{cls['scenario']}/{_wsc}/{_msc}）</span><br>
        <span style="color:{_res_color};font-weight:700">{_res}</span></b></div>
      <div class="chips">{score_chip(health, "结构健康")}{score_chip(conf, "推演置信")}{badge(f'双法一致 {agree:.0f}%', '#64748b')}{_mat_chip}</div>
    </div>"""


# ================= 关键位表 =================
def strategy_text(cls, zs):
    if zs is None:
        return "结构数据不足，观望"
    sc = cls["scenario"]
    if sc == "多头延续":
        return f"持股为主；回踩 ZG {zs['zg']:.0f} 不破=三买可加；跌破 ZD {zs['zd']:.0f} 转空"
    if sc in ("中枢震荡偏多", "高位整理未破前高"):
        return f"区间 {zs['zd']:.0f}~{zs['zg']:.0f} 高抛低吸；站稳 ZG 转多，跌破 ZD 转空"
    if sc in ("中枢震荡偏空", "弱势反弹"):
        return f"反抽不过 ZD {zs['zd']:.0f} 减仓；回到中枢内部再观察"
    if sc == "反弹未回中枢":
        return f"反弹未回中枢 ZD {zs['zd']:.0f}，观望；收复 ZD 转震荡，再上破 ZG {zs['zg']:.0f} 转多"
    if sc == "背驰见顶风险":
        return f"顶背驰确认中，减仓防守；支撑看 ZG {zs['zg']:.0f}"
    if sc == "背驰见底机会":
        return f"底背驰确认中，分批布局；压力看 ZD {zs['zd']:.0f}"
    return f"空头格局，反抽不过 ZD {zs['zd']:.0f} 减仓"


def levels_table(data, results, results_week, results_month, scores):
    rows = []
    for sym, d in data.items():
        r = results[sym]
        cls = r["classify"]
        wcls = results_week[sym]["classify"]
        mcls = results_month[sym]["classify"]
        m_color = SCENARIO_COLOR.get(mcls["scenario"], BLUE)
        health, conf = scores[sym]
        zs = r["zhongshu"][-1] if r["zhongshu"] else None
        close = d["klines"][-1]["close"]
        sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
        w_color = SCENARIO_COLOR.get(wcls["scenario"], BLUE)
        if cls.get("last_bi_dir") == wcls.get("last_bi_dir"):
            syn = badge(f'共振{"多" if cls["last_bi_dir"] == 1 else "空"}', RED if cls["last_bi_dir"] == 1 else GREEN, '✓ ')
        else:
            syn = badge('日强周弱背离' if cls["last_bi_dir"] == 1 else '日弱周强背离', '#d97706', '⚠ ')
        if zs:
            d_zg = (close / zs["zg"] - 1) * 100
            d_zd = (close / zs["zd"] - 1) * 100
            zg_txt = f'{zs["zg"]:.0f}（{"+" if d_zg >= 0 else ""}{d_zg:.1f}%）'
            zd_txt = f'{zs["zd"]:.0f}（{"+" if d_zd >= 0 else ""}{d_zd:.1f}%）'
        else:
            zg_txt = zd_txt = "—"
        rows.append(f"""<tr data-sym="{sym}" class="linkrow" data-jump>
          <td><b>{d["name"]}</b></td>
          <td>{badge(cls["scenario"], sc_color)}</td>
          <td>{badge(wcls["scenario"], w_color)}</td>
          <td>{badge(mcls["scenario"], m_color)}</td>
          <td class="tac">{syn}</td>
          <td>{close:.2f}</td><td>{zg_txt}</td><td>{zd_txt}</td>
          <td class="tac"><div style="display:flex;flex-direction:column;align-items:center;gap:4px">{score_chip(health)}{score_chip(conf)}</div></td>
          <td class="strategy">{strategy_text(cls, zs)}</td>
        </tr>""")
    return """<table class="tbl">
      <colgroup><col style="width:90px"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"></colgroup>
      <thead><tr><th>指数</th><th>日线分类</th><th>周线分类</th><th>月线背景</th><th class="tac">级别联立</th><th>现价</th><th>压力 ZG（距离）</th><th>支撑 ZD（距离）</th><th class="tac">健康度 / 置信度</th><th>应对策略</th></tr></thead>
      <tbody>%s</tbody></table>""" % "".join(rows)


def backtest_table(backtests):
    """汇总 5 指数信号回测：{sym: {kind: {h: {n, win_rate, avg_ret}}}}"""
    KINDS = ["一类买", "一类卖", "二类买", "二类卖", "三类买", "三类卖"]
    HORIZONS = [5, 10, 20, 60]
    agg = {}
    for sym, bt in backtests.items():
        for kind, hs in bt.items():
            for h, v in hs.items():
                st = agg.setdefault(kind, {}).setdefault(h, {"n": 0, "wsum": 0.0, "rsum": 0.0})
                st["n"] += v["n"]
                st["wsum"] += v["win_rate"] * v["n"]
                st["rsum"] += v["avg_ret"] * v["n"]
    rows = []
    for kind in KINDS:
        if kind not in agg:
            continue
        tds = [f"<td><b>{kind}点</b></td>"]
        for h in HORIZONS:
            st = agg[kind].get(h)
            if not st or st["n"] == 0:
                tds.append("<td class='tac'>—</td>")
                continue
            wr = st["wsum"] / st["n"] * 100
            ar = st["rsum"] / st["n"] * 100
            c = RED if ar >= 0 else GREEN
            tds.append(f'<td class="tac">{st["n"]} 次 · 胜率 <b>{wr:.0f}%</b> · 均 <span style="color:{c}">{ar:+.1f}%</span></td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")
    return """<table class="tbl">
      <colgroup><col style="width:110px"><col style="width:calc((100%% - 110px)/4)"><col style="width:calc((100%% - 110px)/4)"><col style="width:calc((100%% - 110px)/4)"><col style="width:calc((100%% - 110px)/4)"></colgroup>
      <thead><tr><th>信号类型</th><th class="tac">后 5 个交易日</th><th class="tac">后 10 个交易日</th><th class="tac">后 20 个交易日</th><th class="tac">后 60 个交易日</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">统计 5 大指数 2021-01 至今全部信号（买点胜=之后涨，卖点胜=之后跌）。买卖点按缠论标准：一类=背驰拐点，二类=次低/次高折返，三类=回抽不进中枢。样本有限，历史特征，非投资建议。</p>""" % "".join(rows)


def rr_table(data, results, recent_n=8):
    """近期买卖点值博率（R:R）明细：每个指数最近 N 个买卖点的止损/目标/R:R/值博率——
    缠论实战交易计划必备（每个买卖点须有明确止损位与目标位），此前报告完全缺失该维度。"""
    rows = []
    for sym, d in data.items():
        r = results[sym]
        for s in r["signals"][-recent_n:]:
            _q = s.get("quality", "—")
            _qc = {"优": "#7c3aed", "良": GREEN, "中": "#64748b", "差": "#b45309", "—": "#94a3b8"}.get(_q, "#94a3b8")
            _dir_col = RED if s["dir"] == 1 else GREEN
            _vc = "✓" if s.get("vol_confirm") else "—"
            _rr = ("%.1f" % s["rr"]) if s.get("rr") else "—"
            rows.append(f"""<tr data-sym="{sym}" class="linkrow" data-jump>
              <td><b>{d["name"]}</b></td>
              <td style="color:{_dir_col};font-weight:600">{s["kind"]}</td>
              <td>{s["date"]}</td>
              <td class="tac">{s["price"]:.1f}</td>
              <td class="tac">{s["stop"]:.1f}</td>
              <td class="tac">{s["target"]:.1f}</td>
              <td class="tac" style="color:{_qc};font-weight:700">{_rr}</td>
              <td class="tac">{badge(_q, _qc)}</td>
              <td class="tac">{_vc}</td>
            </tr>""")
    return """<h3 class="fc-title" style="margin-top:22px">近期买卖点值博率（R:R）明细<span class="fc-sub">止损 / 目标 / 风险收益比 —— 缠论实战交易计划必备，此前报告完全缺失</span></h3>
      <table class="tbl">
      <colgroup><col style="width:110px"><col style="width:calc((100%% - 110px)/8)"><col style="width:calc((100%% - 110px)/8)"><col style="width:calc((100%% - 110px)/8)"><col style="width:calc((100%% - 110px)/8)"><col style="width:calc((100%% - 110px)/8)"><col style="width:calc((100%% - 110px)/8)"><col style="width:calc((100%% - 110px)/8)"><col style="width:calc((100%% - 110px)/8)"></colgroup>
      <thead><tr><th>指数</th><th>买卖点</th><th>日期</th><th class="tac">触发价</th><th class="tac">止损位</th><th class="tac">目标位</th><th class="tac">R:R</th><th class="tac">值博率</th><th class="tac">量✓</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">R:R = (目标−触发) / (触发−止损)；值博率：优(RR≥2.5)/良(≥1.5)/中(≥1.0)/差(&lt;1)。止损取局部前低或中枢下沿 ZD，目标取近程摆动极值并封顶 6 倍防失真。结构参考，非交易建议。</p>""" % "".join(rows)


def robustness_table(robust, data):
    """样本外稳健性检验表：早年(2021~split前) vs 近两年(split起) 买方信号胜率对比，检测校准过拟合。"""
    rows = []
    for sym, rb in robust.items():
        name = data[sym]["name"]
        early, recent, _split_t = rb["early"], rb["recent"], rb["split"]
        split = "多切分(" + "/".join(str(s[:4]) for s in _split_t) + ")" if isinstance(_split_t, (tuple, list)) else _split_t
        wf = rb.get("walk_forward", {})
        wf_decay = "%+.0fpt" % (wf.get("decay", 0) * 100) if wf else "—"

        def _pick(d, h=20):
            st = d.get("一类买", {}).get(h) or d.get("三类买", {}).get(h)
            if not st or st["n"] == 0:
                return None
            return st["win_rate"], st["avg_ret"], st["n"]

        em, rm = _pick(early), _pick(recent)
        if em and rm:
            diff_pt = (rm[0] - em[0]) * 100
            if diff_pt <= -15:
                verdict = badge('近两年显著衰减 · 校准或存过拟合', '#d97706', '⚠ ')
            elif diff_pt >= -5:
                verdict = badge('样本外稳定', GREEN, '✓ ')
            else:
                verdict = badge('轻微衰减', '#64748b')
            diff_txt = "%+.0fpt" % diff_pt
        else:
            verdict, diff_txt = "—", "—"

        def _fmt(x):
            return ("%.0f%% (%+.*f%%) n=%d" % (x[0] * 100, 1, x[1] * 100, x[2])) if x else "—"

        rows.append(f"""<tr data-sym="{sym}" class="linkrow" data-jump>
          <td><b>{name}</b>（{sym}）</td>
          <td class="tac">{_fmt(em)}</td>
          <td class="tac">{_fmt(rm)}</td>
          <td class="tac">{diff_txt}</td>
          <td class="tac">{wf_decay}</td>
          <td>{verdict}</td>
        </tr>""")
    _tbl = """<table class="tbl">
      <colgroup><col style="width:140px"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"></colgroup>
      <thead><tr><th>指数</th><th class="tac">早年买方信号胜率(均收益) h=20</th><th class="tac">近两年买方信号胜率(均收益) h=20</th><th class="tac">变化</th><th class="tac">滚动窗口衰减*</th><th>样本外稳健性</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">按 {SPLIT} 切分「早年 / 近两年」买方信号（一类买·三类买，持有 20 日）胜率与均收益对比。近两年显著下滑(≥15pt)提示过拟合风险；持平/更高则样本外稳定。*「滚动窗口衰减」=多个切分点(2022/2023/2024)聚合的两年 vs 早年胜率差均值，比单一切分更稳，刻画样本外稳健性。不构成投资建议。</p>""".replace("{SPLIT}", split)
    return _tbl % "".join(rows)


def forecast_summary_table(data, results, results_week, results_month, forecast_info):
    """推演情景汇总：5 指数主/次/风险概率 + 失效位 + 级别联立 + 稳定性并列对比"""
    rows = []
    for sym, d in data.items():
        r = results[sym]
        wcls = results_week[sym]["classify"]
        mcls = results_month[sym]["classify"]
        m_color = SCENARIO_COLOR.get(mcls["scenario"], BLUE)
        fi = forecast_info[sym]
        cls = r["classify"]
        sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
        if cls.get("last_bi_dir") == wcls.get("last_bi_dir"):
            syn = badge(f'共振{"多" if cls["last_bi_dir"] == 1 else "空"}', RED if cls["last_bi_dir"] == 1 else GREEN, '✓ ')
        else:
            syn = badge('日强周弱背离' if cls["last_bi_dir"] == 1 else '日弱周强背离', '#d97706', '⚠ ')
        _lv = fi.get("level", "稳健")
        _lv_c = {"稳健": GREEN, "边缘": "#d97706", "敏感·待确认": RED}.get(_lv, GREEN)
        stab = _lv
        stab_c = _lv_c
        rows.append(f"""<tr data-sym="{sym}" class="linkrow" data-jump>
          <td><b>{d["name"]}</b></td>
          <td>{badge(cls["scenario"], sc_color)}</td>
          <td>{badge(mcls["scenario"], m_color)}</td>
          <td class="tac">{syn}</td>
          <td class="tac"><b style="color:{RED}">{fi["p_main"]*100:.0f}%</b>{prob_bar(fi["p_main"], RED)}</td>
          <td class="tac"><b style="color:#64748b">{fi["p_alt"]*100:.0f}%</b>{prob_bar(fi["p_alt"], "#64748b")}</td>
          <td class="tac"><b style="color:{GREEN}">{fi["p_risk"]*100:.0f}%</b>{prob_bar(fi["p_risk"], GREEN)}</td>
          <td class="tac"><b style="color:{BLUE}">{fi["p_hold"]*100:.0f}%</b>{prob_bar(fi["p_hold"], BLUE)}</td>
          <td class="tac">{fi["zd"]:.0f}</td>
          <td class="tac">{badge(stab, stab_c)}</td>
        </tr>""")
    return f"""<table class="tbl">
      <colgroup><col style="width:90px"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"></colgroup>
      <thead><tr><th>指数</th><th>日线分类</th><th>月线背景</th><th class="tac">级别联立</th><th class="tac">主路径概率</th><th class="tac">次路径概率</th><th class="tac">风险概率</th><th class="tac">结构存续(锥)</th><th class="tac">失效位 ZD</th><th class="tac">结论稳定性</th></tr></thead>
      <tbody>{"".join(rows)}</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">概率为「级别共振+置信度+回测胜率」启发式估算，主/次/风险已归一(合计100%)，结构存续(锥)为独立参照不计入。稳健度三级：<b style="color:{GREEN}">稳健</b>=极性趋势 20 日内不变；<b style="color:#d97706">边缘</b>=趋势守住但末笔年轻；<b style="color:{RED}">敏感·待确认</b>=极性翻转、已下调主路径概率。末笔仅~7根K线者宜轻仓等周线确认。</p>"""


def data_quality_strip(data, results):
    cards = []
    all_ok = True
    for sym, d in data.items():
        m = d["meta"]
        r = results[sym]
        cc = m.get("consistency", {})
        max_rel = cc.get("max_rel_dev")
        ok_consist = (max_rel is None) or (max_rel < 0.02)
        ok = not m["issues"] and ok_consist
        all_ok = all_ok and ok
        badge = "✓" if ok else "⚠"
        c = "#18a058" if ok else "#d97706"
        cons_txt = ("%.2f%%" % (max_rel * 100)) if max_rel is not None else "—"
        cap = "、".join("%s" % lab for lab, _, _ in r["captured"]) or "—"
        stab = r["stability"].get("level", "稳健")
        cards.append(
            f'<div class="qcard">'
            f'<div class="qtitle" style="color:{c}">{badge} {d["name"]}</div>'
            f'<div class="qbody">样本 {m["first_date"]}~{m["last_date"]} · 日线{m["count"]}根<br>'
            f'前复权(qfq)口径 · 双源比值偏离 {cons_txt}<br>'
            f'双法一致 {r["agreement"]["rate"]*100:.0f}% · 拐点捕捉 {r["capture_rate"]*100:.0f}% · 分类{stab}</div>'
            f'</div>'
        )
    head = f'<div class="qhead"><b>数据源：</b>腾讯(qfq) ↔ 新浪(裸价) 全序列比值一致性校验（前复权=裸价×常数调整因子，比值应恒定）· 综合状态 {"全部正常" if all_ok else "部分需关注"}</div>'
    return head + '<div class="quality-grid">' + "".join(cards) + "</div>"


# ================= 年度收益表 =================
def main():
    _base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_base, "data.json"), encoding="utf-8") as f:
        data = json.load(f)

    results = {sym: analyze(d["klines"]) for sym, d in data.items()}
    results_week = {sym: analyze(d["week_klines"], MIN_BI_PCT_WEEK) for sym, d in data.items()}
    results_month = {sym: analyze(d["month_klines"], MIN_BI_PCT_MONTH) for sym, d in data.items()}
    backtests = {sym: backtest_signals(d["klines"], results[sym], exclude_last=True) for sym, d in data.items()}
    # 样本外稳健性检验：按 2024-01-01 切分早年/近两年，检测校准过拟合
    robust = {sym: backtest_robustness(d["klines"], results[sym],
                                        splits=("2022-01-01", "2023-01-01", "2024-01-01"))
              for sym, d in data.items()}
    # 跨指数市场广度（系统性环境）：日/周/月三级聚合，作为全市场对齐度反馈进推演置信度
    _daily_sc = [results[s]["classify"]["scenario"] for s in data]
    _week_sc = [results_week[s]["classify"]["scenario"] for s in data]
    _month_sc = [results_month[s]["classify"]["scenario"] for s in data]
    bd = market_breadth(_daily_sc, _week_sc, _month_sc)
    _bull_cnt = sum(1 for s in data if results[s]["classify"]["scenario"] in SC_BULL)
    _bear_cnt = sum(1 for s in data if results[s]["classify"]["scenario"] in SC_BEAR)
    _total = len(data)
    last_date = next(iter(data.values()))["meta"]["last_date"]
    _breadth_bias = (_bull_cnt / _total - 0.5) * 2 * 8  # 全看多 +8 / 全看空 -8（0-100 置信度刻度）
    scores = {sym: (health_score(d["klines"], results[sym], results_week[sym]["classify"]),
                    forecast_confidence(results[sym], results_week[sym]["classify"], backtests[sym], breadth_bias=_breadth_bias))
              for sym, d in data.items()}

    _bcolor = {"多头主导": RED, "偏多（高层级有分歧）": GOLD, "分歧震荡": GOLD,
               "偏空": GREEN, "空头主导": GREEN}.get(bd["composite"]["label"], GOLD)
    _rows = ""
    for _lvl, _c in (("日线", bd["daily"]), ("周线", bd["week"]), ("月线", bd["month"])):
        _t = _c["total"]
        _wb = _c["bull"] / _t * 100
        _wn = _c["neutral"] / _t * 100
        _wr = _c["bear"] / _t * 100
        _rows += (f'<div style="display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px">'
                  f'<span style="width:34px;color:#475569;font-weight:600">{_lvl}</span>'
                  f'<div style="flex:1;height:12px;border-radius:6px;overflow:hidden;display:flex;background:#eef2f7">'
                  f'<i style="width:{_wb:.0f}%;background:{RED}"></i>'
                  f'<i style="width:{_wn:.0f}%;background:#94a3b8"></i>'
                  f'<i style="width:{_wr:.0f}%;background:{GREEN}"></i></div>'
                  f'<span style="width:150px;text-align:right;color:#475569;font-variant-numeric:tabular-nums">'
                  f'{_c["bull"]} 多 / {_c["bear"]} 空 / {_c["neutral"]} 中</span></div>')
    breadth_banner = (f'<div class="panel" style="border-left:4px solid {_bcolor};margin:4px 0 16px">'
                      f'<h4 style="font-size:15px;color:#1e40af;margin-bottom:10px">跨指数市场广度综合研判 '
                      f'<span style="font-size:12px;color:#64748b;font-weight:400">日 / 周 / 月三级区间套（数据截至 {last_date}）</span></h4>'
                      f'{_rows}'
                      f'<p style="font-size:13px;color:#334155;line-height:1.75;margin-top:10px;background:#f8fafc;'
                      f'border-radius:6px;padding:8px 12px">{bd["conclusion"]}</p>'
                      f'<p style="font-size:12px;color:#64748b;margin-top:6px">综合广度评分 '
                      f'<b style="color:{_bcolor}">{bd["composite"]["score"]:+.2f}</b>（{bd["composite"]["label"]}）· '
                      f'已折算为「全市场对齐度」±8 反馈进各指数推演置信度。</p>'
                      f'</div>')

    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 日周背离检测（用于结论）
    divergent = [d["name"] for sym, d in data.items()
                 if results[sym]["classify"].get("last_bi_dir") != results_week[sym]["classify"].get("last_bi_dir")]

    # 市场概览 KPI
    n_multi = sum(1 for s in data if results[s]["classify"]["scenario"] in ("多头延续",))
    n_osc = sum(1 for s in data if results[s]["classify"]["scenario"] in ("中枢震荡偏多", "高位整理未破前高"))
    n_bear = sum(1 for s in data if results[s]["classify"]["scenario"] in ("空头延续", "中枢震荡偏空", "弱势反弹", "反弹未回中枢"))
    n_div = len(divergent)
    avg_health = sum(v[0] for v in scores.values()) / len(scores)
    avg_conf = sum(v[1] for v in scores.values()) / len(scores)
    avg_agree = sum(results[s]["agreement"]["rate"] for s in data) / len(data) * 100
    avg_vol = sum((realized_vol_annualized([k["close"] for k in d["klines"]]) or 0) for d in data.values()) / len(data)
    total = len(data)
    n_m_bull = sum(1 for s in data if results_month[s]["classify"]["scenario"] in SC_BULL)
    n_m_bear = sum(1 for s in data if results_month[s]["classify"]["scenario"] in SC_BEAR)

    cards, sections, conclusions = [], [], []
    forecast_info = {}
    paths_bt = {}
    for sym, d in data.items():
        r = results[sym]
        # 推演路径历史命中率回测（预测准确性自校验）：horizon 与推演图自适应 horizon 对齐，
        # 使「路径命中率自校验」对照的是同一时间尺度（此前固定 h=60，而锥图用 30~90 自适应，
        # 口径不一致会让校准对照失真）。step 取 horizon//2 保证样本窗基本不重叠、统计独立。
        horizon = adaptive_horizon(r["bis"], r["merged"])
        _step = max(15, horizon // 2)
        paths_bt[sym] = backtest_paths(d["klines"], horizon=horizon, step=_step, with_stability=False)
        wcls_full = results_week[sym]
        wcls = wcls_full["classify"]
        mcls = results_month[sym]["classify"]
        m_color = SCENARIO_COLOR.get(mcls["scenario"], BLUE)
        # 关键修复：用周线 classify 重算日线 classify，使"日×周区间套"共振判断真正生效
        # （analyze 内部 classify 调用未传 wcls，nest 此前永远为空）。区间套只影响 classify 的
        # interval_nesting/detail 字段，不改变 scenario 与 last_bi_dir，下游 forecast/card 行为不受影响。
        _old_cls = r["classify"]
        r["classify"] = classify(r["bis"], r["zhongshu"], r["beichi"],
                                 d["klines"][-1]["close"], wcls, r["segments"], r["seg_beichi"],
                                 results_month[sym]["classify"])
        # 关键修复：classify() 的返回字典不含 ma_alignment（均线排列由 analyze 单独计算），
        # 此处重赋值会把它丢弃——导致卡片「均线排列」显示"—"、且 health_score/forecast_confidence
        # 里的「均线多空排列交叉验证」惩罚逻辑沦为死代码。回写以恢复显示与交叉验证。
        r["classify"]["ma_alignment"] = _old_cls.get("ma_alignment")
        health, conf = scores[sym]
        horizon = adaptive_horizon(r["bis"], r["merged"])
        sigma = forward_vol([k["close"] for k in d["klines"]], horizon)
        cards.append(card_html(sym, d["name"], d["klines"], r, wcls, health, conf))
        cls = r["classify"]
        sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
        w_color = SCENARIO_COLOR.get(wcls["scenario"], BLUE)
        fs_svg, fs_note, fs_probs, fs_legend, fc_data = forecast_svg(d["klines"], r, wcls, conf, sigma, sym, horizon, backtests[sym], paths_bt[sym], breadth_score=bd["composite"]["score"])
        div_txt = ('⚠️ 周线向下笔运行中，以上路径的兑现以周线底分型确认为前提；若周线续创新低，风险路径概率上升。'
                   if cls.get("last_bi_dir") != wcls.get("last_bi_dir")
                   else "日周级别共振，主路径置信度较高。")
        sections.append(f"""
    <section class="panel" id="sec-{sym}">
      <h2>{d["name"]}（{sym}）{badge(f'日线：{cls["scenario"]}', sc_color)}{badge(f'周线：{wcls["scenario"]}', w_color)}{badge(f'月线：{mcls["scenario"]}', m_color)}{badge(f'健康 {health}', RED)}{badge(f'置信 {conf}', BLUE)}</h2>
      <div class="chartbox">
        {echart_main(d["klines"], r, sym, r["captured"])}
      </div>
      <div class="verdict"><b>结构解读：</b><p>{cls["detail"]}</p>
      <p style="margin-top:4px"><b>周线级别：</b>{wcls["detail"]}</p></div>
      <h3 class="fc-title">未来走势推演</h3>
      <div class="chartbox" id="fcbox-{sym}">
        {fs_svg}
        <div class="xh-tip" id="fctip-{sym}"></div>
      </div>
      {fs_legend}
      {path_hit_html(cls["scenario"], paths_bt[sym], fs_probs[0], fs_probs[1], fs_probs[2], horizon)}
    </section>""")
        conclusions.append(f'<li><b>{d["name"]}</b>：日线 {cls["scenario"]} / 周线 {wcls["scenario"]} —— {cls["detail"]} <a href="#sec-{sym}" data-sym="{sym}" data-jump style="font-size:12px;color:{BLUE}">[查看图解]</a></li>')
        forecast_info[sym] = {"p_main": fs_probs[0], "p_alt": fs_probs[1], "p_risk": fs_probs[2],
                              "p_hold": fc_data["p_hold"],
                              "zd": (r["zhongshu"][-1]["zd"] if r["zhongshu"] else d["klines"][-1]["close"] * 0.95),
                              "stable": r["stability"]["stable"],
                              "level": r["stability"].get("level", "稳健"),
                              "last_bi_bars": r["stability"].get("last_bi_bars", 0),
                              "sigma": sigma, "fc": fc_data}

    fc_blob = {sym: forecast_info[sym]["fc"] for sym in data}
    diverge_note = ""
    if divergent:
        diverge_note = f"""<p style="margin-top:10px;color:#b45309;font-size:14px;line-height:1.8">
    ⚠️ <b>级别背离提示</b>：{"、".join(divergent)} 当前<b>日线向上笔、周线向下笔</b>，属日强周弱背离。
    历史统计上此类组合意味着日线上涨是周线调整中的反弹结构，<b>仓位与预期应低于"日周共振多头"的情形</b>；
    只有周线笔重新转向上（周线底分型确认），日线多头延续的置信度才会提高。</p>"""

    # 全局可信度指标（用于一句话结论）
    avg_cap = sum(results[s]["capture_rate"] for s in data) / len(data) * 100
    avg_agree2 = avg_agree
    worst_rel = max((d["meta"].get("consistency", {}).get("max_rel_dev") or 0) for d in data.values())
    avg_stable = sum(1 for s in data if results[s]["stability"]["stable"]) / len(data) * 100
    n_robust = sum(1 for s in data if results[s]["stability"].get("level") == "稳健")
    n_edge = sum(1 for s in data if results[s]["stability"].get("level") == "边缘")
    n_sens = sum(1 for s in data if results[s]["stability"].get("level") == "敏感·待确认")

    # 数据驱动的市场格局描述（不写死，随每日自动刷新保持准确）
    n_daily_up = sum(1 for s in data if results[s]["classify"]["last_bi_dir"] == 1)
    n_week_up = sum(1 for s in data if results_week[s]["classify"]["last_bi_dir"] == 1)
    n_div = len(divergent)
    total = len(data)
    if n_div == total:
        pat = (f"全部 {total} 个指数日线向上笔、周线向下笔（日强周弱背离），当前上涨在更大级别上属"
               f"<b>反弹中的强势段</b>，而非主升浪")
    elif n_div == 0:
        pat = f"{total} 个指数日线与周线同向（日周共振），结构方向一致性较高"
    else:
        pat = f"{n_div}/{total} 个指数日强周弱背离、{total - n_div} 个日周共振"
    if n_daily_up >= total * 0.6 and n_week_up <= total * 0.4:
        stance = "；仓位与预期应低于\"日周共振多头\"的情形"
    else:
        stance = ""
    n_above = sum(1 for s in data if results[s]["classify"].get("position") == "中枢上方")
    n_inside = sum(1 for s in data if results[s]["classify"].get("position") == "中枢内部")
    exec_summary = f"""
    <div class="panel exec">
      <h4>一句话结论</h4>
      <ul>
        <li><b>市场格局：</b>{pat}{stance}。</li>
        <li><b>推演结论：</b>各指数主路径概率约 {int(min((forecast_info[s]['p_main'] for s in data))*100)}%~{int(max((forecast_info[s]['p_main'] for s in data))*100)}%；跌破中枢 ZD 即主路径失效。详见<a href="#s3">第三节</a>。</li>
      </ul>
    </div>"""

    # 全局指数联动条（模块互联互通：点击聚焦某指数，卡片/表行/图解三向联动）
    sym_rail = ('<nav class="sym-rail" id="symRail" aria-label="指数联动条">'
                + ''.join(f'<a class="chip" href="#sec-{sym}" data-sym="{sym}" data-jump>{d["name"]}</a>' for sym, d in data.items())
                + '</nav>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股缠论结构分析报告 · 2021-2026</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif; background: #f5f7fa; color: {INK}; padding: 24px; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }}
  .wrap {{ max-width: 1120px; margin: 0 auto; }}
  header h1 {{ font-size: 26px; }}
  header p {{ color: #64748b; margin-top: 6px; font-size: 14px; line-height: 1.7; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin: 20px 0; }}
  .card {{ background: #fff; border: 1px solid #e5e9f0; border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(15,23,42,.04); transition: box-shadow .18s ease, transform .18s ease, border-color .18s ease; }}
  .card:hover {{ box-shadow: 0 6px 18px rgba(15,23,42,.10); transform: translateY(-2px); border-color: #cdd7e5; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .idx-name {{ font-weight: 700; }}
  .sym {{ color: {GRAY}; font-size: 12px; }}
  .price {{ font-size: 22px; font-weight: 700; margin: 8px 0; font-variant-numeric: tabular-nums; }}
  .price span {{ font-size: 14px; font-weight: 600; }}
  .kv {{ display: flex; justify-content: space-between; font-size: 13px; color: #64748b; padding: 3px 0; }}
  .kv b {{ color: {INK}; }}
  .panel {{ background: #fff; border: 1px solid #e5e9f0; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(15,23,42,.04); }}
  .panel h2 {{ font-size: 18px; margin-bottom: 10px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }}
  .badge {{ font-size: 12px; color: #fff; padding: 2px 10px; border-radius: 999px; font-weight: 600; white-space: nowrap; font-variant-numeric: tabular-nums; vertical-align: middle; }}
  .verdict {{ background: #f0f6ff; border-left: 4px solid {BLUE}; padding: 10px 14px; margin-top: 12px; font-size: 14px; border-radius: 0 6px 6px 0; }}
  .verdict p {{ margin-top: 4px; color: #475569; line-height: 1.7; }}
  .tbl {{ width: 100%; border-collapse: collapse; font-size: 13px; background: #fff; table-layout: fixed; }}
  .tbl th, .tbl td {{ padding: 9px 10px; text-align: left; vertical-align: middle; border-top: 1px solid #eef2f7; overflow-wrap: break-word; transition: background .12s ease; }}
  .tbl th {{ background: #f1f5f9; color: #475569; font-weight: 600; border-top: none; }}
  .tbl tbody tr:hover td {{ background: #f8fafc; }}
  .tbl tbody tr:last-child td {{ border-bottom: 1px solid #eef2f7; }}
  .tbl .tac {{ text-align: center; }}
  .tac-all th, .tac-all td {{ text-align: center; }}
  .tbl .best {{ font-weight: 700; }}
  .strategy {{ color: #475569; line-height: 1.6; }}
  .conclusion li {{ margin: 8px 0 8px 18px; line-height: 1.8; font-size: 14px; }}
  .disclaimer {{ background: #fff8e6; border: 1px solid #f0d98c; color: #92600a; border-radius: 10px; padding: 14px 18px; font-size: 13px; line-height: 1.8; }}
  .legend {{ font-size: 12px; color: #64748b; margin: 10px 0 18px; line-height: 2; }}
  .legend span {{ margin-right: 16px; white-space: nowrap; }}
  i.dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }}
  h2.sec {{ font-size: 19px; margin: 26px 0 12px; padding-left: 12px; border-left: 4px solid {BLUE}; line-height: 1.3; }}
  nav.toc {{ position: sticky; top: 8px; z-index: 50; background: rgba(255,255,255,0.98); backdrop-filter: blur(8px); border: 1px solid #e2e8f0; border-radius: 999px; padding: 6px 10px; margin: 18px 0 24px; display: flex; flex-wrap: wrap; justify-content: center; gap: 4px; font-size: 13px; box-shadow: 0 4px 14px rgba(15,23,42,0.06); width: 100%; max-width: 100%; }}
  nav.toc a {{ color: #475569; text-decoration: none; padding: 6px 12px; border-radius: 999px; font-weight: 500; transition: all .15s ease; white-space: nowrap; display: inline-flex; align-items: center; flex: 1; justify-content: center; }}
  nav.toc a:hover {{ background: #f1f5f9; color: #1e293b; }}
  nav.toc a.active {{ background: {BLUE}; color: #fff; box-shadow: 0 2px 8px rgba(43,108,176,0.25); }}
  nav.toc a .num {{ display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 50%; background: rgba(0,0,0,0.05); font-size: 11px; font-weight: 600; margin-right: 7px; color: #64748b; transition: all .15s ease; }}
  nav.toc a:hover .num {{ background: rgba(0,0,0,0.08); color: #334155; }}
  nav.toc a.active .num {{ background: rgba(255,255,255,0.25); color: #fff; }}
  .quality {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 12px 16px; font-size: 13px; line-height: 2; }}
  .quality-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin-top: 10px; }}
  .qcard {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 12px; line-height: 1.6; }}
  .qtitle {{ font-weight: 700; font-size: 14px; margin-bottom: 4px; }}
  .qbody {{ font-size: 12px; color: #64748b; }}
  .qhead {{ font-size: 13px; color: #334155; }}
  .chartbox {{ border: 1px solid #eef2f7; border-radius: 8px; overflow: hidden; position: relative; }}
  .echart-toolbar {{ display: flex; align-items: center; gap: 10px; padding: 8px 12px; font-size: 13px; color: #475569; background: #f8fafc; border-bottom: 1px solid #eef2f7; }}
  .toolbar {{ display: flex; align-items: center; gap: 10px; padding: 8px 12px; font-size: 13px; color: #475569; background: #f8fafc; border-bottom: 1px solid #eef2f7; }}
  .fc-title {{ font-size: 15px; margin: 18px 0 8px; display: flex; align-items: baseline; gap: 10px; }}
  .fc-sub {{ font-size: 12px; color: {GRAY}; font-weight: 400; }}
  .fc-legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: #475569; margin: 6px 0 2px; }}
  .fc-targets {{ font-size: 12px; color: #475569; margin: 4px 0 2px; line-height: 1.8; }}
  .fc-targets b {{ color: {INK}; font-variant-numeric: tabular-nums; }}
  .fc-legend span {{ display: inline-flex; align-items: center; }}
  .fc-legend .ln {{ display: inline-block; width: 18px; height: 3px; border-radius: 2px; margin-right: 6px; }}
  .fc-legend .ln-dash {{ background-image: repeating-linear-gradient(90deg, #94a3b8 0 5px, transparent 5px 9px); }}
  .fc-legend .ln-dot {{ background: {GREEN}; }}
  .fc-legend .ln-band {{ width: 18px; height: 11px; background: {RED}; opacity: .18; border-radius: 2px; }}
  .fc-legend .ln-trend {{ width: 18px; height: 0; border-top: 2px dashed #0891b2; }}
  .fc-note {{ font-size: 13px; color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 8px 12px; margin-top: 8px; line-height: 1.7; }}
  .pathcheck {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 14px; margin-top: 10px; }}
  .pathcheck > b {{ font-size: 14px; color: #0f172a; }}
  .pc-sub {{ display: block; font-size: 12px; color: #64748b; margin: 3px 0 8px; line-height: 1.5; }}
  .pc-row {{ display: flex; align-items: center; gap: 8px; margin: 5px 0; font-size: 13px; }}
  .pc-lab {{ width: 64px; flex: none; font-weight: 600; }}
  .pc-bar {{ flex: 1; height: 7px; background: #eef2f7; border-radius: 4px; overflow: hidden; max-width: 320px; }}
  .pc-bar i {{ display: block; height: 100%; border-radius: 4px; }}
  .pc-h {{ width: 70px; flex: none; color: #475569; text-align: right; }}
  .pc-p {{ width: 78px; flex: none; color: #0f172a; font-weight: 600; text-align: right; }}
  .pc-calib {{ margin-top: 8px; font-size: 13px; padding-top: 7px; border-top: 1px dashed #cbd5e1; }}
  .hero {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 4px; }}
  .kpi {{ flex: 1; min-width: 118px; background: #fff; border: 1px solid #e5e9f0; border-radius: 10px; padding: 12px 14px; text-align: center; box-shadow: 0 1px 3px rgba(15,23,42,.04); }}
  .kpi-v {{ font-size: 26px; font-weight: 800; font-variant-numeric: tabular-nums; line-height: 1.1; }}
  .kpi-l {{ font-size: 12px; color: #64748b; margin-top: 3px; }}
  .spark {{ margin: 6px 0 2px; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
  .chip {{ font-size: 11px; padding: 2px 8px; border-radius: 999px; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .chip b {{ font-weight: 800; }}
  .quality .qsub {{ color: #64748b; font-size: 12px; margin-left: 6px; }}
  details.method summary {{ list-style: none; }}
  details.method summary::-webkit-details-marker {{ display: none; }}
  .method h4 {{ margin: 14px 0 6px; font-size: 14px; color: #334155; }}
  .method p, .method li {{ font-size: 13px; color: #475569; line-height: 1.85; }}
  .method ul {{ margin: 0 0 4px 18px; }}
  .method li {{ margin: 4px 0; }}
  .exec {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px; padding: 14px 18px; margin-bottom: 20px; }}
  .exec h4 {{ font-size: 15px; color: #1e40af; margin-bottom: 8px; }}
  .exec ul {{ margin: 0 0 0 18px; }}
  .exec li {{ font-size: 13px; color: #334155; line-height: 1.85; margin: 5px 0; }}
  .xh-tip {{ position: absolute; pointer-events: none; background: rgba(15,23,42,.92); color: #fff; font-size: 13px; font-weight: 500; line-height: 1.55; padding: 8px 12px; border-radius: 6px; display: none; z-index: 20; white-space: nowrap; font-variant-numeric: tabular-nums; box-shadow: 0 2px 10px rgba(0,0,0,.3); }}
  .xh-tip b {{ color: #fbbf24; }}
  .tablescroll {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  @media (max-width: 720px) {{
    body {{ padding: 12px; }}
    .wrap {{ max-width: 100%; }}
    header h1 {{ font-size: 20px; }}
    header p {{ font-size: 13px; line-height: 1.6; }}
    .cards {{ grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
    .kpi {{ min-width: 92px; padding: 9px 8px; }}
    .kpi-v {{ font-size: 21px; }}
    .panel {{ padding: 12px; margin-bottom: 12px; }}
    h2.sec, .panel h2 {{ font-size: 16px; }}
    .tbl {{ font-size: 12px; min-width: 640px; }}
    .tbl th, .tbl td {{ padding: 7px 8px; }}
    nav.toc {{ font-size: 12px; padding: 5px 7px; gap: 3px; border-radius: 14px; margin: 12px 0 18px; }}
    nav.toc a {{ padding: 5px 8px; }}
    nav.toc a .num {{ width: 16px; height: 16px; font-size: 10px; margin-right: 4px; }}
    .legend {{ font-size: 11px; }}
    .hero {{ gap: 8px; }}
  }}
  /* ===== 模块互联互通 ===== */
  nav.sym-rail {{ position: sticky; top: 54px; z-index: 49; background: rgba(255,255,255,0.97); backdrop-filter: blur(8px); border: 1px solid #e2e8f0; border-radius: 12px; padding: 6px 10px; margin: 10px 0 18px; display: flex; flex-wrap: wrap; gap: 6px; box-shadow: 0 4px 14px rgba(15,23,42,0.05); }}
  nav.sym-rail .chip {{ text-decoration: none; font-size: 13px; color: #475569; background: #f1f5f9; padding: 5px 12px; border-radius: 999px; cursor: pointer; transition: all .15s ease; white-space: nowrap; border: 1px solid transparent; }}
  nav.sym-rail .chip:hover {{ background: #e2e8f0; color: #1e293b; }}
  nav.sym-rail .chip.active {{ background: {BLUE}; color: #fff; border-color: {BLUE}; box-shadow: 0 2px 8px rgba(43,108,176,0.25); }}
  .card.linked-active {{ border-color: {BLUE}; box-shadow: 0 0 0 2px rgba(43,108,176,0.25), 0 6px 18px rgba(15,23,42,0.12); transform: translateY(-2px); }}
  tr.linkrow {{ cursor: pointer; }}
  .tbl tbody tr.row-linked td {{ background: #f8fafc; }}
  .tbl tbody tr.row-linked td:first-child {{ border-left: 3px solid {BLUE}; padding-left: 7px; }}
  .tbl tbody tr.row-linked:hover td {{ background: #f1f5f9; }}
  .sec-flash {{ animation: secflash 1.1s ease; }}
  @keyframes secflash {{ 0% {{ box-shadow: 0 0 0 0 rgba(43,108,176,0); }} 25% {{ box-shadow: 0 0 0 4px rgba(43,108,176,0.35); }} 100% {{ box-shadow: 0 1px 3px rgba(15,23,42,0.04); }} }}
  @media (max-width: 720px) {{ nav.sym-rail {{ top: 48px; }} nav.sym-rail .chip {{ padding: 4px 9px; font-size: 12px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>A股主要指数缠论结构分析报告</h1>
    <p>数据区间：2021-01-04 ~ {last_date} · 生成时间：{gen_time}<br>日线+周线+月线 · 前复权</p>
  </header>
  <nav class="toc">
    <a href="#s1"><span class="num">一</span>决策总览</a>
    <a href="#s2"><span class="num">二</span>分指数图解</a>
    <a href="#s3"><span class="num">三</span>关键位与推演</a>
    <a href="#s4"><span class="num">四</span>信号回测对比</a>
    <a href="#s5"><span class="num">五</span>免责说明</a>
  </nav>
  {sym_rail}
  {exec_summary}
  <h2 class="sec" id="s1">一、决策总览</h2>
  <div class="cards">{"".join(cards)}</div>
  {breadth_banner}
  <div class="hero">
    <div class="kpi" style="border-top:3px solid {RED}"><div class="kpi-v" style="color:{RED}">{n_multi}</div><div class="kpi-l">多头延续</div></div>
    <div class="kpi" style="border-top:3px solid #d97706"><div class="kpi-v" style="color:#d97706">{n_osc}</div><div class="kpi-l">震荡偏多</div></div>
    <div class="kpi" style="border-top:3px solid {GREEN}"><div class="kpi-v" style="color:{GREEN}">{n_bear}</div><div class="kpi-l">空头/偏弱</div></div>
    <div class="kpi" style="border-top:3px solid #d97706"><div class="kpi-v" style="color:#d97706">{n_div}</div><div class="kpi-l">日周背离指数</div></div>
    <div class="kpi" style="border-top:3px solid {BLUE}"><div class="kpi-v">{avg_health:.0f}</div><div class="kpi-l">平均结构健康度</div></div>
    <div class="kpi" style="border-top:3px solid {BLUE}"><div class="kpi-v">{avg_conf:.0f}</div><div class="kpi-l">平均推演置信度</div></div>
    <div class="kpi" style="border-top:3px solid {BLUE}"><div class="kpi-v">{avg_agree:.0f}%</div><div class="kpi-l">笔双法一致率</div></div>
  </div>
  <h2 class="sec" id="s2">二、分指数结构图解</h2>
  {"".join(sections)}

  <h2 class="sec" id="s3">三、关键位与推演汇总</h2>
  <div class="panel"><div class="tablescroll">{levels_table(data, results, results_week, results_month, scores)}</div></div>
  <div class="panel"><div class="tablescroll">{forecast_summary_table(data, results, results_week, results_month, forecast_info)}</div></div>
  <div class="panel conclusion">
    <ul>{"".join(conclusions)}</ul>
    {diverge_note}
  </div>

  <h2 class="sec" id="s4">四、信号回测与走势对比</h2>
  <div class="panel"><div class="tablescroll">{backtest_table(backtests)}</div></div>
  <div class="panel"><div class="tablescroll">{rr_table(data, results)}</div></div>
  <div class="panel"><div class="tablescroll">{robustness_table(robust, data)}</div></div>
  <details class="panel" style="cursor:pointer">
    <summary style="font-weight:700;color:#1e40af;cursor:pointer">五指数归一化对比图（2021=100）</summary>
    <div style="margin-top:12px">{compare_svg(data)}</div>
  </details>

  <h2 class="sec" id="s5">五、免责说明</h2>
  <div class="disclaimer">
    <b>免责声明：</b>本报告基于缠论技术分析的自动化结构划分，推演概率为启发式估算，非点位预测，不构成投资建议。市场有风险，决策需独立。
  </div>
</div>
<script>
var FC_DATA = {json.dumps(fc_blob, ensure_ascii=False)};
function initForecast(sym){{
  var svg=document.getElementById('forecast-'+sym);
  if(!svg) return;
  var tip=document.getElementById('fctip-'+sym);
  if(!tip) return;
  var box=svg.closest('.chartbox');
  var PAD_L=12, PAD_R=78, W=1060, plot_w=W-PAD_L-PAD_R;
  var hist_w=plot_w*0.40, proj_w=plot_w*0.60;
  var PAD_T3=30, PAD_B3=34, H=300;
  var D=FC_DATA[sym];
  if(!D) return;
  var cx=document.getElementById('fccx-'+sym);
  var cm=document.getElementById('fcm-'+sym);
  var ca=document.getElementById('fca-'+sym);
  var cr=document.getElementById('fcr-'+sym);
  var lo=D.lo, span=D.span;
  function yf(v){{ return PAD_T3+(H-PAD_T3-PAD_B3)*(1-(v-lo)/span); }}
  function showDots(xf,f){{
    var p=D.proj[Math.max(0,Math.min(D.proj.length-1,Math.round(f*100)))];
    cm.setAttribute('cx',xf); cm.setAttribute('cy',yf(p.main)); cm.setAttribute('opacity','1');
    ca.setAttribute('cx',xf); ca.setAttribute('cy',yf(p.alt)); ca.setAttribute('opacity','1');
    cr.setAttribute('cx',xf); cr.setAttribute('cy',yf(p.risk)); cr.setAttribute('opacity','1');
  }}
  function hideDots(){{ cm.setAttribute('opacity','0'); ca.setAttribute('opacity','0'); cr.setAttribute('opacity','0'); }}
  function move(ev){{
    var ce=ev.touches?ev.touches[0]:ev;
    if(!ce) return;
    var pt=svg.createSVGPoint(); pt.x=ce.clientX; pt.y=ce.clientY;
    var loc=pt.matrixTransform(svg.getScreenCTM().inverse());
    if(loc.x<PAD_L||loc.x>W-PAD_R||loc.y<PAD_T3||loc.y>H-PAD_B3){{
      tip.style.display='none'; cx.setAttribute('opacity','0'); hideDots(); return;
    }}
    cx.setAttribute('x1',loc.x); cx.setAttribute('x2',loc.x); cx.setAttribute('opacity','0.5');
    var html, idx;
    if(loc.x<=PAD_L+hist_w){{
      idx=Math.round((loc.x-PAD_L)/hist_w*(D.hist.length-1));
      idx=Math.max(0,Math.min(D.hist.length-1,idx));
      var h=D.hist[idx];
      html='<b>历史 · '+h[0]+'</b><br>收盘 <b>'+h[1].toFixed(2)+'</b>';
      hideDots();
    }} else {{
      var f=(loc.x-(PAD_L+hist_w))/proj_w;
      idx=Math.max(0,Math.min(D.proj.length-1,Math.round(f*100)));
      var p=D.proj[idx];
      var red='#e54545', gray='#94a3b8', grn='#18a058';
      html='<b>推演 · T+'+p.tplus+' ('+p.date+')</b><br>'
        +'<span style="color:'+red+'">主路径 '+p.main.toFixed(2)+'</span>　'+Math.round(D.p_main*100)+'%<br>'
        +'<span style="color:'+gray+'">次路径 '+p.alt.toFixed(2)+'</span>　'+Math.round(D.p_alt*100)+'%<br>'
        +'<span style="color:'+grn+'">风险路径 '+p.risk.toFixed(2)+'</span>　'+Math.round(D.p_risk*100)+'%<br>'
        +'<span style="color:#0891b2">趋势外推 '+p.trend.toFixed(2)+'</span><br>'
        +'<span style="color:#64748b">±1σ '+p.b1l.toFixed(0)+'~'+p.b1u.toFixed(0)+'</span><br>'
        +'<span style="color:#64748b">±2σ '+p.b2l.toFixed(0)+'~'+p.b2u.toFixed(0)+'</span>';
      showDots(PAD_L+hist_w+proj_w*f, f);
    }}
    tip.innerHTML=html; tip.style.display='block';
    var rect=box.getBoundingClientRect();
    var x=ev.clientX-rect.left+14, y=ev.clientY-rect.top+14;
    if(x+tip.offsetWidth>rect.width) x=rect.width-tip.offsetWidth-6;
    if(y+tip.offsetHeight>rect.height) y=rect.height-tip.offsetHeight-6;
    tip.style.left=x+'px'; tip.style.top=y+'px';
  }}
  function leave(){{ tip.style.display='none'; cx.setAttribute('opacity','0'); hideDots(); }}
  svg.addEventListener('mousemove',move);
  svg.addEventListener('mouseleave',leave);
  svg.addEventListener('touchmove',function(e){{move(e);}},{{passive:true}});
  svg.addEventListener('touchend',leave);
}}
{"".join(f'initForecast("{sym}");' for sym in data)}
(function(){{
  var links = document.querySelectorAll('nav.toc a');
  var sections = Array.from(links).map(function(a){{ return document.querySelector(a.getAttribute('href')); }}).filter(Boolean);
  function onScroll(){{
    if (!sections.length) return;
    var y = window.scrollY + 90;
    var cur = links[0];
    sections.forEach(function(sec, i){{ if (sec && sec.offsetTop <= y) cur = links[i]; }});
    links.forEach(function(a){{ a.classList.remove('active'); }});
    if (cur) cur.classList.add('active');
  }}
  window.addEventListener('scroll', onScroll, {{passive:true}});
  window.addEventListener('resize', onScroll, {{passive:true}});
  onScroll();
}})();
</script>
<script>
(function(){{
  var rail=document.getElementById('symRail');
  var CURRENT=null;
  function offset(){{ return 108; }}
  function secs(){{ return Array.prototype.slice.call(document.querySelectorAll('[id^="sec-"]')); }}
  function setActive(sym, fromScroll){{
    CURRENT=sym;
    if(rail) Array.prototype.forEach.call(rail.querySelectorAll('.chip'), function(c){{ c.classList.toggle('active', c.getAttribute('data-sym')===sym); }});
    Array.prototype.forEach.call(document.querySelectorAll('.card'), function(c){{ c.classList.toggle('linked-active', c.id==='card-'+sym); }});
    Array.prototype.forEach.call(document.querySelectorAll('tr.linkrow'), function(r){{ r.classList.toggle('row-linked', r.getAttribute('data-sym')===sym); }});
    if(!fromScroll){{
      var sec=document.getElementById('sec-'+sym);
      if(sec){{ sec.classList.remove('sec-flash'); void sec.offsetWidth; sec.classList.add('sec-flash'); }}
    }}
  }}
  function focusSymbol(sym){{
    setActive(sym, false);
    var sec=document.getElementById('sec-'+sym);
    if(sec){{ var r=sec.getBoundingClientRect(); window.scrollTo({{ top: r.top + window.scrollY - offset(), behavior:'smooth' }}); }}
  }}
  document.addEventListener('click', function(e){{
    var t=e.target; if(!t||!t.closest) return;
    var el=t.closest('[data-jump]'); if(!el) return;
    var sym=el.getAttribute('data-sym'); if(!sym) return;
    e.preventDefault(); focusSymbol(sym);
  }});
  var SL=secs();
  function spy(){{
    if(!SL.length) return;
    var y=window.scrollY + offset() + 20;
    var cur=null;
    SL.forEach(function(s){{ if(s.offsetTop <= y) cur=s.id.replace('sec-',''); }});
    if(cur && cur!==CURRENT) setActive(cur, true);
  }}
  window.addEventListener('scroll', spy, {{passive:true}});
  window.addEventListener('resize', spy, {{passive:true}});
  spy();
}})();
</script>
</body>
</html>"""

    with open(os.path.join(_base, "report.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("saved -> chanlun/report.html")


if __name__ == "__main__":
    main()
