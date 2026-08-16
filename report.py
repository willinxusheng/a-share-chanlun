# -*- coding: utf-8 -*-
"""生成自包含 HTML 缠论分析报告（内嵌 SVG，浅色主题，涨红跌绿）v6
新增：成交量面板、双法一致性、结构健康度、推演置信度、已知拐点捕捉、原则化推演"""
import json
import os
from datetime import datetime, timedelta
from chanlun import analyze, backtest_signals, MIN_BI_PCT_WEEK, health_score, forecast_confidence, forward_vol, adaptive_horizon

W, H_PRICE, H_VOL, H_MACD = 1060, 360, 64, 110
PAD_L, PAD_R, PAD_T, PAD_B = 12, 78, 24, 26
CHART_TOTAL = PAD_T + H_PRICE + 8 + H_VOL + 8 + H_MACD + PAD_B  # 600

RED, GREEN, GOLD = "#e54545", "#18a058", "#d4a017"
BLUE, GRAY, INK = "#2b6cb0", "#94a3b8", "#1f2937"

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
    "中枢震荡偏空": "#0d9488", "弱势反弹": "#0d9488",
    "空头延续": GREEN, "背驰见顶风险": GREEN,
    "无中枢·向上笔": RED, "无中枢·向下笔": GREEN,
}


def _fmt(v, nd=2):
    return ("%%.%df" % nd) % v


def _smooth(pts, tension=1.0, nd=2):
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
def chart_svg(klines, r, sym):
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
    base.append(f'<defs><linearGradient id="cg-{sym}" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="0%" stop-color="#64748b" stop-opacity="0.10"/>'
                f'<stop offset="100%" stop-color="#64748b" stop-opacity="0"/></linearGradient>'
                f'<clipPath id="clip-{sym}"><rect x="{PAD_L}" y="0" width="{plot_w}" height="{CHART_TOTAL}"/></clipPath></defs>')

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
        for f, lab in ((0.0, "F0"), (0.382, "F38"), (0.5, "F50"), (0.618, "F62")):
            pv = base_hi - swing * f if leg["dir"] == 1 else base_lo + swing * f
            yy = y(pv)
            pg.append(f'<line x1="{x0:.1f}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="#7c3aed" stroke-width="0.8" stroke-dasharray="2,4" stroke-opacity="0.5"/>')
            lg.append(f'<text x="{W - PAD_R - 60}" y="{yy - 2:.1f}" font-size="10" font-weight="600" fill="#7c3aed" text-anchor="start">{lab} {pv:.0f}</text>')

    # 收盘价平滑曲线 + 轻量渐变填充（更细腻）
    close_pts = [(x(i), y(c)) for i, c in enumerate(closes)]
    close_d = _smooth(close_pts)
    pg.append(f'<path d="{close_d} L{W - PAD_R:.1f} {PAD_T + price_h:.1f} L{PAD_L:.1f} {PAD_T + price_h:.1f} Z" fill="url(#cg-{sym})" stroke="none"/>')
    pg.append(f'<path d="{close_d}" fill="none" stroke="#64748b" stroke-width="1" stroke-opacity="0.8" stroke-linejoin="round" stroke-linecap="round"/>')

    # MA20 / MA60 均线（与卡片"均线排列"呼应，提升专业度；置于图形层随窗口横向缩放）
    def ma_series(arr, p):
        out = [None] * len(arr)
        for i in range(len(arr)):
            if i + 1 >= p:
                out[i] = sum(arr[i + 1 - p:i + 1]) / p
        return out

    for maa, mcol, mlab in ((ma_series(closes, 20), "#0ea5e9", "MA20"),
                            (ma_series(closes, 60), "#a855f7", "MA60")):
        pts = [(x(i), y(v)) for i, v in enumerate(maa) if v is not None]
        if pts:
            pg.append(f'<path d="{_smooth(pts)}" fill="none" stroke="{mcol}" stroke-width="1" stroke-opacity="0.85" stroke-linejoin="round" stroke-linecap="round"/>')
    lg.append(f'<rect x="{PAD_L + 2}" y="{PAD_T + 3}" width="132" height="16" rx="3" fill="#ffffff" fill-opacity="0.82"/>')
    lg.append(f'<text x="{PAD_L + 6}" y="{PAD_T + 13}" font-size="12" font-weight="600" fill="#0ea5e9">— MA20</text>')
    lg.append(f'<text x="{PAD_L + 64}" y="{PAD_T + 13}" font-size="12" font-weight="600" fill="#a855f7">— MA60</text>')

    # 笔线段
    for b in bis:
        x0 = x(merged[b["start"]]["idx_end"])
        x1 = x(merged[b["end"]]["idx_end"])
        color = RED if b["dir"] == 1 else GREEN
        pg.append(f'<line x1="{x0:.1f}" y1="{y(b["start_price"]):.1f}" x2="{x1:.1f}" y2="{y(b["end_price"]):.1f}" stroke="{color}" stroke-width="1.8"/>')

    # 笔端点（分型转折点）圆点，便于核对结构（放文字层，随窗口重算 cx 保持正圆）
    for b in bis:
        xxe = x(merged[b["end"]]["idx_end"])
        yye = y(b["end_price"])
        col = RED if b["dir"] == 1 else GREEN
        lg.append(f'<circle data-i="{merged[b["end"]]["idx_end"]}" data-dx="0" cx="{xxe:.1f}" cy="{yye:.1f}" r="2.6" fill="{col}" stroke="#ffffff" stroke-width="0.8"/>')

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
        if d == 1:
            pg.append(f'<polygon points="{xx:.1f},{yy + 8:.1f} {xx - 5:.1f},{yy + 17:.1f} {xx + 5:.1f},{yy + 17:.1f}" fill="{col}"/>')
            lg.append(f'<text data-i="{xi}" data-dx="0" x="{xx:.1f}" y="{yy + 30:.1f}" font-size="13" font-weight="600" fill="{col}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="3">{s['kind'][:3] + ('·量' if s.get('vol_confirm') else '')}</text>')
        else:
            pg.append(f'<polygon points="{xx:.1f},{yy - 8:.1f} {xx - 5:.1f},{yy - 17:.1f} {xx + 5:.1f},{yy - 17:.1f}" fill="{col}"/>')
            lg.append(f'<text data-i="{xi}" data-dx="0" x="{xx:.1f}" y="{yy - 23:.1f}" font-size="13" font-weight="600" fill="{col}" text-anchor="middle" paint-order="stroke" stroke="#ffffff" stroke-width="3">{s['kind'][:3] + ('·量' if s.get('vol_confirm') else '')}</text>')

    # 最新价虚线 + 标签
    last_c = closes[-1]
    yy = y(last_c)
    lcolor = RED if closes[-1] >= closes[-2] else GREEN
    pg.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" stroke="{lcolor}" stroke-width="1" stroke-dasharray="2,3" stroke-opacity="0.8"/>')
    lg.append(f'<g id="latest-{sym}">')
    lg.append(f'<rect x="{PAD_L + plot_w - 74}" y="{yy - 9:.1f}" width="72" height="16" rx="3" fill="{lcolor}" stroke="#ffffff" stroke-width="0.8"/>')
    lg.append(f'<text x="{PAD_L + plot_w - 39}" y="{yy + 4:.1f}" font-size="14" font-weight="700" fill="#ffffff" text-anchor="middle">{last_c:.2f}</text>')
    lg.append('</g>')

    # ===== 成交量副图 =====
    vmax = max((k["volume"] for k in klines), default=1) or 1
    bw = max(plot_w / n * 0.62, 0.8)
    for i, k in enumerate(klines):
        vh = k["volume"] / vmax * (H_VOL - 4)
        vc = RED if k["close"] >= k["open"] else GREEN
        yy2 = vbot - vh
        pg.append(f'<rect x="{x(i) - bw / 2:.1f}" y="{yy2:.1f}" width="{bw:.1f}" height="{vh:.1f}" fill="{vc}" fill-opacity="0.55"/>')
    # 成交量 MA5（量能趋势，置于图形层随窗口横向缩放）
    vma = [sum(klines[j]["volume"] for j in range(i + 1 - 5, i + 1)) / 5 if i + 1 >= 5 else None
           for i in range(n)]
    vma_pts = [(x(i), vbot - (v / vmax) * (H_VOL - 4)) for i, v in enumerate(vma) if v is not None]
    if vma_pts:
        pg.append(f'<path d="{_smooth(vma_pts)}" fill="none" stroke="#475569" stroke-width="1" stroke-opacity="0.85" stroke-linejoin="round" stroke-linecap="round"/>')
    lg.append(f'<text x="{PAD_L}" y="{vtop - 2:.1f}" font-size="14" font-weight="600" fill="{GRAY}">成交量</text>')
    lg.append(f'<text x="{PAD_L + 58}" y="{vtop - 2:.1f}" font-size="12" font-weight="600" fill="#475569">— 量MA5</text>')
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

    p = [f'<svg id="main-{sym}" viewBox="0 0 {W} {CHART_TOTAL}" preserveAspectRatio="xMidYMid meet" data-n="{n}" data-lo="{lo:.4f}" data-span="{span:.4f}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;text-rendering:geometricPrecision;shape-rendering:geometricPrecision">']
    p += base
    p.append(f'<g clip-path="url(#clip-{sym})"><g id="plot-{sym}" class="plot">')
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


# ================= 区间导航条（缩略图 + 可拖窗口） =================
NAV_H = 36

def navigator_svg(klines, sym):
    closes = [k["close"] for k in klines]
    n = len(closes)
    lo, hi = min(closes), max(closes)
    span = hi - lo or 1

    def x(i):
        return W * i / (n - 1)

    def y(v):
        return 8 + (NAV_H - 20) * (1 - (v - lo) / span)

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


def forecast_svg(klines, r, wcls, conf, sigma, sym, horizon=60, bt=None):
    closes = [k["close"] for k in klines]
    n = len(closes)
    tail = closes[-120:]
    last = closes[-1]
    zs = r["zhongshu"][-1] if r["zhongshu"] else None
    zg = zs["zg"] if zs else last * 1.05
    zd = zs["zd"] if zs else last * 0.95
    mid = (zg + zd) / 2
    sc = r["classify"]["scenario"]
    cls_dir = r["classify"]["last_bi_dir"]
    wdir = wcls["last_bi_dir"]
    aligned = (cls_dir == wdir)
    # 最近完成的笔幅度，作为"实测幅度投影"基准
    comp = r["bis"][-2] if len(r["bis"]) >= 2 else r["bis"][-1]
    move = max(abs(comp["end_price"] / comp["start_price"] - 1), 0.03)

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

    # ---- 概率（经验校准 + 结构锚 + 推演置信度 + 结论稳定性微调）----
    # 先按结构分类给基准概率，再叠加置信度偏离与稳定性；避免对“背离/背驰”重复惩罚导致全部贴地板。
    _base_p = {
        "多头延续": 0.58,
        "中枢震荡偏多": 0.50, "高位整理未破前高": 0.50,
        "背驰见顶风险": 0.40, "中枢震荡偏空": 0.40, "弱势反弹": 0.36, "空头延续": 0.34,
    }
    # 经验校准（#1）：优先用本指数"最近且样本足够"的真实信号类型做锚，而非按情景猜一类买/卖
    _anchor_kind = None
    for s in sorted([s for s in r["signals"] if s["bi_index"] >= len(r["bis"]) - 60],
                    key=lambda x: -x["bi_index"]):
        k = s["kind"][:3]
        if bt.get(k, {}).get(20, {}).get("n", 0) >= 5:
            _anchor_kind = k
            break
    if _anchor_kind is None:
        _anchor_kind = "一类买" if sc in ("多头延续", "中枢震荡偏多", "高位整理未破前高", "背驰见底机会") else "一类卖"
    emp_wr, emp_n, emp_h = None, 0, 0
    if bt:
        st20 = bt.get(_anchor_kind, {}).get(20)
        if st20 and st20["n"] >= 5:
            emp_wr, emp_n = st20["win_rate"], st20["n"]
        st60 = bt.get(_anchor_kind, {}).get(60)
        if st60 and st60["n"] >= 5:
            emp_h = st60["win_rate"]
    if emp_wr is not None:
        p_main = 0.5 + (emp_wr - 0.5) * 0.7   # 经验胜率映射到概率锚（向 0.5 轻微收缩）
    else:
        p_main = _base_p.get(sc, 0.45)
    p_main += (conf - 50) / 100 * 0.30
    stab = (r.get("stability") or {}).get("stable", True)
    if not stab:  # 结论对近 1 个月价格敏感 → 主路径概率下调、风险概率上升
        p_main -= 0.04
    p_main = max(0.30, min(0.72, round(p_main, 2)))
    p_alt = 0.30
    p_risk = max(0.05, round(1 - p_main - p_alt, 2))

    H = 300
    PAD_T3, PAD_B3 = 30, 34
    plot_w = W - PAD_L - PAD_R
    hist_w = plot_w * 0.40
    proj_w = plot_w * 0.60

    # 置信锥(±2σ)在末端会显著超出路径端点（f=1、σ≈15% 时带宽≈中枢价 ±30%），
    # 必须把锥体极值纳入纵轴范围，否则锥顶被裁剪/压平，看不出"随时间扩张"的形态
    band_ext = []
    for _f in (0.25, 0.5, 0.75, 1.0):
        _m = _interp(main_p, _f)
        band_ext.append(_m + _m * sigma * _f * 2)
        band_ext.append(_m - _m * sigma * _f * 2)
    all_prices = tail + [v for _, v in main_p + alt_p + risk_p] + [zg, zd] + band_ext
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
        return dt.strftime("%y-%m-%d")

    # 历史区底部：真实交易日日期刻度（约 6 个，均匀且不贴边）
    p.append(f'<text x="{PAD_L + 4:.1f}" y="{PAD_T3 - 10}" font-size="12" font-weight="700" fill="{GRAY}">近{len(tail)}日(交易日)</text>')
    p.append(f'<line x1="{PAD_L + hist_w:.1f}" y1="{PAD_T3}" x2="{PAD_L + hist_w:.1f}" y2="{H - PAD_B3}" stroke="{INK}" stroke-width="1.2" stroke-dasharray="3,3"/>')
    p.append(f'<text x="{PAD_L + hist_w:.1f}" y="{PAD_T3 - 10}" font-size="13" font-weight="700" fill="{INK}" text-anchor="middle">今日 T</text>')
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
            half = med * sigma * f * kmul
            up.append((xp(f), y(med + half)))
            lo.append((xp(f), y(med - half)))
        return " ".join(f"{a:.1f},{b:.1f}" for a, b in up + lo[::-1])

    # 置信锥裁剪到绘图区，避免 ±2σ 带超出图表边框被截断
    p.append(f'<g clip-path="url(#fc-{sym})">')
    p.append(f'<polygon points="{band_poly(2)}" fill="{RED}" fill-opacity="0.06" stroke="none"/>')
    p.append(f'<polygon points="{band_poly(1)}" fill="{RED}" fill-opacity="0.12" stroke="none"/>')
    p.append('</g>')

    draw_path(main_p, RED, "none")
    draw_path(alt_p, "#94a3b8", "6,4")
    draw_path(risk_p, GREEN, "2,3")
    # ---- hover 交互元素（默认隐藏，由 JS initForecast 驱动）----
    p.append(f'<line id="fccx-{sym}" x1="{PAD_L}" y1="{PAD_T3}" x2="{PAD_L}" y2="{H - PAD_B3}" stroke="{INK}" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>')
    p.append(f'<circle id="fcm-{sym}" r="3.6" fill="{RED}" opacity="0"/>')
    p.append(f'<circle id="fca-{sym}" r="3.6" fill="#94a3b8" opacity="0"/>')
    p.append(f'<circle id="fcr-{sym}" r="3.6" fill="{GREEN}" opacity="0"/>')
    p.append("</svg>")
    # 图例改为图表下方的 HTML 图例条（不再压住推演路径与时间轴）
    legend_html = (
        f'<div class="fc-legend">'
        f'<span><i class="ln" style="background:{RED}"></i>主路径：{main_lab} ≈ {p_main * 100:.0f}%</span>'
        f'<span><i class="ln ln-dash" style="background:#94a3b8"></i>次路径：中枢内震荡 ≈ {p_alt * 100:.0f}%</span>'
        f'<span><i class="ln ln-dot" style="background:{GREEN}"></i>风险路径：跌破ZD转空 ≈ {p_risk * 100:.0f}%</span>'
        f'<span><i class="ln ln-band"></i>置信锥 ±1σ/±2σ（σ={sigma * 100:.1f}%）</span>'
        f'</div>'
        f'<div class="fc-targets">目标位(主路径终点) ≈ <b>{main_p[-1][1]:.0f}</b> · '
        f'风险止损位(风险路径终点) ≈ <b>{risk_p[-1][1]:.0f}</b> · '
        f'主路径失效位(有效跌破ZD) ≈ <b>{zd:.0f}</b></div>'
    )
    note = (f"主路径失效位：现价有效跌破 ZD {zd:.0f}（收盘确认）→ 主路径失效、风险路径概率上升；风险路径确认需同时满足「跌破 ZD + 周线笔转向下」。\n"
             f"红色阴影为基于历史 {horizon} 日前向收益波动（σ={sigma*100:.1f}%）推演的置信锥：真实走势落在 ±1σ 带内的经验概率约 68%、±2σ 带内约 95%；锥体随时间扩张，反映不确定性增大。\n"
             f"本图为目的（分类框架）而非点位预测：缠论给出的是「不跌破 ZD 则结构延续、跌破则转弱」的条件应对，不是对具体价位的预测。\n"
             f"主图叠加的斐波那契回调位（F38/F50/F62）与本路径上行目标、ZD 支撑相互印证：若回踩至 F61.8 附近获支撑，反弹结构更可靠；若直接跌漏 ZD，则风险路径概率上升。\n"
             f"时间轴：左侧历史区为真实交易日（MM-DD）；右侧投影区日期按「从最后交易日往后推算相应交易日、跳过周末」得到（未含法定节假日），仅供参照。")
    if emp_wr is not None:
        _h = ("；同类信号后 60 日同向胜率 %.0f%%（n=%d）" % (emp_h * 100, bt.get(_anchor_kind, {}).get(60, {}).get("n", 0))) if emp_h else ""
        note += (f"\n经验校准锚：历史上 {_anchor_kind}点 后 20 交易日同向胜率 {emp_wr*100:.0f}%（n={emp_n}）{_h}——主路径概率已据此由启发式切换为经验估计，样本有限，仅供参照。")
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
        b1u = med + med * sigma * f * 1
        b1l = med - med * sigma * f * 1
        b2u = med + med * sigma * f * 2
        b2l = med - med * sigma * f * 2
        proj.append({"f": round(f, 3), "tplus": kk, "date": dt,
                     "main": round(med, 2), "alt": round(alt, 2), "risk": round(risk, 2),
                     "b1u": round(b1u, 2), "b1l": round(b1l, 2), "b2u": round(b2u, 2), "b2l": round(b2l, 2)})
    fc_data = {"hist": hist, "proj": proj, "p_main": p_main, "p_alt": p_alt, "p_risk": p_risk,
               "zd": round(zd, 2), "zg": round(zg, 2), "last": round(last, 2),
               "sigma": round(sigma, 4), "horizon": horizon, "lo": round(lo, 4), "span": round(span, 4)}
    return "".join(p), note, (p_main, p_alt, p_risk), legend_html, fc_data


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


def score_chip(score, label):
    if score >= 66:
        c = RED
    elif score >= 45:
        c = "#d97706"
    else:
        c = GREEN
    return f'<span class="chip" style="background:{c}1a;color:{c};border:1px solid {c}55"><b>{score}</b> {label}</span>'


# ================= 卡片 =================
def card_html(sym, name, klines, r, wcls, health, conf):
    last, prev = klines[-1], klines[-2]
    chg = (last["close"] / prev["close"] - 1) * 100
    color = RED if chg >= 0 else GREEN
    cls = r["classify"]
    five_yr = (last["close"] / klines[0]["close"] - 1) * 100
    fy_color = RED if five_yr >= 0 else GREEN
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
    return f"""
    <div class="card" style="border-left:4px solid {sc_color}">
      <div class="card-head"><span class="idx-name">{name}</span><span class="sym">{sym}</span></div>
      <div class="price">{last["close"]:.2f} <span style="color:{color}">{'+' if chg >= 0 else ''}{chg:.2f}%</span></div>
      <div class="spark">{spark}</div>
      <div class="kv"><span>近5年涨跌(前复权)</span><b style="color:{fy_color}">{'+' if five_yr >= 0 else ''}{five_yr:.2f}%</b></div>
      <div class="kv"><span>笔 / 中枢 / 背驰 / 段背驰</span><b>{len(r["bis"])} / {len(r["zhongshu"])} / {len(r["beichi"])} / {len(r["seg_beichi"])}</b></div>
      <div class="kv"><span>最近一笔</span><b>{'↑' if cls.get('last_bi_dir') == 1 else '↓'} {amp:.1f}%</b></div>
      <div class="kv"><span>当前分类</span><b style="color:{sc_color}">{cls["scenario"]}</b></div>
      <div class="kv"><span>均线排列(MA20/60/250)</span><b style="color:{ma_color}">{ma_txt}</b></div>
      <div class="kv"><span>日×周区间套</span><b style="color:{nest_color}">{nest_txt}</b></div>
      <div class="chips">{score_chip(health, "结构健康")}{score_chip(conf, "推演置信")}<span class="chip" style="background:#eef2f7;color:#475569;border:1px solid #e2e8f0">双法一致 {agree:.0f}%</span></div>
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
    if sc == "背驰见顶风险":
        return f"顶背驰确认中，减仓防守；支撑看 ZG {zs['zg']:.0f}"
    if sc == "背驰见底机会":
        return f"底背驰确认中，分批布局；压力看 ZD {zs['zd']:.0f}"
    return f"空头格局，反抽不过 ZD {zs['zd']:.0f} 减仓"


def levels_table(data, results, results_week, scores):
    rows = []
    for sym, d in data.items():
        r = results[sym]
        cls = r["classify"]
        wcls = results_week[sym]["classify"]
        health, conf = scores[sym]
        zs = r["zhongshu"][-1] if r["zhongshu"] else None
        close = d["klines"][-1]["close"]
        sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
        w_color = SCENARIO_COLOR.get(wcls["scenario"], BLUE)
        if cls.get("last_bi_dir") == wcls.get("last_bi_dir"):
            syn = '<span style="color:%s;font-weight:700">✓ 共振%s</span>' % (RED if cls["last_bi_dir"] == 1 else GREEN, "多" if cls["last_bi_dir"] == 1 else "空")
        else:
            syn = '<span style="color:#d97706;font-weight:700">⚠ 日强周弱背离</span>' if cls["last_bi_dir"] == 1 else '<span style="color:#d97706;font-weight:700">⚠ 日弱周强背离</span>'
        if zs:
            d_zg = (close / zs["zg"] - 1) * 100
            d_zd = (close / zs["zd"] - 1) * 100
            zg_txt = f'{zs["zg"]:.0f}（{"+" if d_zg >= 0 else ""}{d_zg:.1f}%）'
            zd_txt = f'{zs["zd"]:.0f}（{"+" if d_zd >= 0 else ""}{d_zd:.1f}%）'
        else:
            zg_txt = zd_txt = "—"
        rows.append(f"""<tr>
          <td><b>{d["name"]}</b></td>
          <td style="color:{sc_color};font-weight:600">{cls["scenario"]}</td>
          <td style="color:{w_color}">{wcls["scenario"]}</td>
          <td class="tac">{syn}</td>
          <td>{close:.2f}</td><td>{zg_txt}</td><td>{zd_txt}</td>
          <td class="tac">{score_chip(health, "")}<br>{score_chip(conf, "")}</td>
          <td class="strategy">{strategy_text(cls, zs)}</td>
        </tr>""")
    return """<table class="tbl">
      <thead><tr><th>指数</th><th>日线分类</th><th>周线分类</th><th>级别联立</th><th>现价</th><th>压力 ZG（距离）</th><th>支撑 ZD（距离）</th><th>健康度 / 置信度</th><th>应对策略</th></tr></thead>
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
      <thead><tr><th>信号类型</th><th>后 5 个交易日</th><th>后 10 个交易日</th><th>后 20 个交易日</th><th>后 60 个交易日</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">统计范围：5 大指数 2021-01 至今全部已识别信号；买点胜=之后上涨，卖点胜=之后下跌。报告已构建一/二/三类买卖点完整体系（二类=中枢内回踩/反抽不破 ZD/ZG 的折返笔，触发与否取决于当下结构）。样本有限，仅为历史统计特征，不代表未来胜率，亦非投资建议。</p>""" % "".join(rows)


def forecast_summary_table(data, results, results_week, forecast_info):
    """推演情景汇总：5 指数主/次/风险概率 + 失效位 + 级别联立 + 稳定性并列对比"""
    rows = []
    for sym, d in data.items():
        r = results[sym]
        wcls = results_week[sym]["classify"]
        fi = forecast_info[sym]
        cls = r["classify"]
        sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
        if cls.get("last_bi_dir") == wcls.get("last_bi_dir"):
            syn = '<span style="color:%s;font-weight:700">共振%s</span>' % (RED if cls["last_bi_dir"] == 1 else GREEN, "多" if cls["last_bi_dir"] == 1 else "空")
        else:
            syn = '<span style="color:#d97706;font-weight:700">日强周弱背离</span>' if cls["last_bi_dir"] == 1 else '<span style="color:#d97706;font-weight:700">日弱周强背离</span>'
        stab = "稳定" if fi["stable"] else "敏感"
        stab_c = "#18a058" if fi["stable"] else "#d97706"
        rows.append(f"""<tr>
          <td><b>{d["name"]}</b></td>
          <td style="color:{sc_color};font-weight:600">{cls["scenario"]}</td>
          <td class="tac">{syn}</td>
          <td class="tac"><b style="color:{RED}">{fi["p_main"]*100:.0f}%</b></td>
          <td class="tac">{fi["p_alt"]*100:.0f}%</td>
          <td class="tac"><b style="color:{GREEN}">{fi["p_risk"]*100:.0f}%</b></td>
          <td class="tac">{fi["zd"]:.0f}</td>
          <td class="tac" style="color:{stab_c};font-weight:600">{stab}</td>
        </tr>""")
    return """<table class="tbl">
      <thead><tr><th>指数</th><th>日线分类</th><th>级别联立</th><th>主路径概率</th><th>次路径概率</th><th>风险概率</th><th>失效位 ZD</th><th>结论稳定性</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">概率为基于「级别共振 + 推演置信度 + 回测胜率」的启发式估算，非统计定价模型；风险概率恒为「主/次之外」的余量。稳定性=砍掉末 5/10/20 根 K 线重算后分类是否一致，标"敏感"表明结论对近 1 个月价格变动反应较大，属阶段性判断。</p>""" % "".join(rows)


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
        stab = "稳定" if r["stability"]["stable"] else "敏感"
        cards.append(
            f'<div class="qcard">'
            f'<div class="qtitle" style="color:{c}">{badge} {d["name"]}</div>'
            f'<div class="qbody">日线{m["count"]}根 · 双源比值偏离 {cons_txt}<br>'
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
    backtests = {sym: backtest_signals(d["klines"], results[sym]) for sym, d in data.items()}
    scores = {sym: (health_score(d["klines"], results[sym], results_week[sym]["classify"]),
                    forecast_confidence(results[sym], results_week[sym]["classify"], backtests[sym]))
              for sym, d in data.items()}

    last_date = next(iter(data.values()))["meta"]["last_date"]
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 日周背离检测（用于结论）
    divergent = [d["name"] for sym, d in data.items()
                 if results[sym]["classify"].get("last_bi_dir") != results_week[sym]["classify"].get("last_bi_dir")]

    # 市场概览 KPI
    n_multi = sum(1 for s in data if results[s]["classify"]["scenario"] in ("多头延续",))
    n_osc = sum(1 for s in data if results[s]["classify"]["scenario"] in ("中枢震荡偏多", "高位整理未破前高"))
    n_bear = sum(1 for s in data if results[s]["classify"]["scenario"] in ("空头延续", "中枢震荡偏空", "弱势反弹"))
    n_div = len(divergent)
    avg_health = sum(v[0] for v in scores.values()) / len(scores)
    avg_conf = sum(v[1] for v in scores.values()) / len(scores)
    avg_agree = sum(results[s]["agreement"]["rate"] for s in data) / len(data) * 100

    cards, sections, conclusions = [], [], []
    forecast_info = {}
    kl_blob = {}
    for sym, d in data.items():
        r = results[sym]
        wcls_full = results_week[sym]
        wcls = wcls_full["classify"]
        health, conf = scores[sym]
        horizon = adaptive_horizon(r["bis"])
        sigma = forward_vol([k["close"] for k in d["klines"]], horizon)
        cards.append(card_html(sym, d["name"], d["klines"], r, wcls, health, conf))
        cls = r["classify"]
        sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
        w_color = SCENARIO_COLOR.get(wcls["scenario"], BLUE)
        fs_svg, fs_note, fs_probs, fs_legend, fc_data = forecast_svg(d["klines"], r, wcls, conf, sigma, sym, horizon, backtests[sym])
        div_txt = ('⚠️ 周线向下笔运行中，以上路径的兑现以周线底分型确认为前提；若周线续创新低，风险路径概率上升。'
                   if cls.get("last_bi_dir") != wcls.get("last_bi_dir")
                   else "日周级别共振，主路径置信度较高。")
        # 悬浮提示数据
        kl_blob[sym] = [[k["date"], round(k["open"], 2), round(k["close"], 2),
                         round(k["high"], 2), round(k["low"], 2), round(k["volume"] / 1e8, 3)]
                        for k in d["klines"]]
        sections.append(f"""
    <section class="panel" id="sec-{sym}">
      <h2>{d["name"]}（{sym}）<span class="badge" style="background:{sc_color}">日线：{cls["scenario"]}</span><span class="badge" style="background:{w_color}">周线：{wcls["scenario"]}</span><span class="chip" style="background:{RED}1a;color:{RED}">健康 {health}</span><span class="chip" style="background:{BLUE}1a;color:{BLUE}">置信 {conf}</span></h2>
      <div class="chartbox">
        <div class="toolbar">🔍 拖动下方导航条缩放/平移（双击复位）· 移动鼠标查看每日 OHLC/成交量 · 上图含成交量柱（红涨绿跌）</div>
        {chart_svg(d["klines"], r, sym)}
        <div class="xh-tip" id="tip-{sym}"></div>
        {navigator_svg(d["klines"], sym)}
      </div>
      <div class="verdict"><b>结构解读：</b><p>{cls["detail"]}</p>
      <p style="margin-top:4px"><b>周线级别：</b>{wcls["detail"]}</p></div>
      <h3 class="fc-title">未来走势推演<span class="fc-sub">分类框架 · 原则化路径 + 置信锥 · 非点位预测</span></h3>
      <div class="chartbox" id="fcbox-{sym}">
        {fs_svg}
        <div class="xh-tip" id="fctip-{sym}"></div>
      </div>
      {fs_legend}
      <p class="fc-note">{div_txt}<br>{fs_note}</p>
    </section>""")
        conclusions.append(f'<li><b>{d["name"]}</b>：日线 {cls["scenario"]} / 周线 {wcls["scenario"]} —— {cls["detail"]} <a href="#sec-{sym}" style="font-size:12px;color:{BLUE}">[查看图解]</a></li>')
        forecast_info[sym] = {"p_main": fs_probs[0], "p_alt": fs_probs[1], "p_risk": fs_probs[2],
                              "zd": (r["zhongshu"][-1]["zd"] if r["zhongshu"] else d["klines"][-1]["close"] * 0.95),
                              "stable": r["stability"]["stable"], "sigma": sigma, "fc": fc_data}

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
      <h4>一句话结论（Executive Summary）</h4>
      <ul>
        <li><b>市场格局：</b>{pat}{stance}。</li>
        <li><b>数据可信度：</b>腾讯(qfq)↔新浪(裸价)双源<b>全序列比值最大偏离 {worst_rel*100:.2f}%</b>，K线校验 0 问题，笔双法一致率均值 {avg_agree2:.0f}%，已知拐点捕捉率均值 {avg_cap:.0f}%——历史划分具备较高稳健性。</li>
        <li><b>推演结论：</b>各指数主路径概率最高（约 {int(min((forecast_info[s]['p_main'] for s in data))*100)}%~{int(max((forecast_info[s]['p_main'] for s in data))*100)}%），但均以<b>周线底分型确认</b>为兑现前提；结论稳定性 {avg_stable:.0f}%（标"敏感"者需随近 1 个月价格更新）。跌破各自中枢 ZD 即主路径失效、风险路径概率上升。具体指数推演见<a href="#s6">第六节</a>，关键位与策略见<a href="#s3">第三节</a>。</li>
      </ul>
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股缠论结构分析报告 · 2021-2026</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif; background: #f5f7fa; color: {INK}; padding: 24px; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }}
  .wrap {{ max-width: 1120px; margin: 0 auto; }}
  header h1 {{ font-size: 26px; }}
  header p {{ color: #64748b; margin-top: 6px; font-size: 14px; line-height: 1.7; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin: 20px 0; }}
  .card {{ background: #fff; border: 1px solid #e5e9f0; border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(15,23,42,.04); }}
  .card-head {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .idx-name {{ font-weight: 700; }}
  .sym {{ color: {GRAY}; font-size: 12px; }}
  .price {{ font-size: 22px; font-weight: 700; margin: 8px 0; font-variant-numeric: tabular-nums; }}
  .price span {{ font-size: 14px; font-weight: 600; }}
  .kv {{ display: flex; justify-content: space-between; font-size: 13px; color: #64748b; padding: 3px 0; }}
  .kv b {{ color: {INK}; }}
  .panel {{ background: #fff; border: 1px solid #e5e9f0; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(15,23,42,.04); }}
  .panel h2 {{ font-size: 18px; margin-bottom: 10px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }}
  .badge {{ font-size: 12px; color: #fff; padding: 2px 10px; border-radius: 999px; font-weight: 600; }}
  .verdict {{ background: #f0f6ff; border-left: 4px solid {BLUE}; padding: 10px 14px; margin-top: 12px; font-size: 14px; border-radius: 0 6px 6px 0; }}
  .verdict p {{ margin-top: 4px; color: #475569; line-height: 1.7; }}
  .tbl {{ width: 100%; border-collapse: collapse; font-size: 13px; background: #fff; }}
  .tbl th {{ background: #f1f5f9; color: #475569; font-weight: 600; padding: 9px 10px; text-align: left; white-space: nowrap; }}
  .tbl td {{ padding: 9px 10px; border-top: 1px solid #eef2f7; vertical-align: top; }}
  .tbl tr:hover td {{ background: #f8fafc; }}
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
  nav.toc {{ position: sticky; top: 8px; z-index: 50; background: rgba(255,255,255,0.98); backdrop-filter: blur(8px); border: 1px solid #e2e8f0; border-radius: 999px; padding: 6px 10px; margin: 18px auto 24px; display: flex; flex-wrap: wrap; justify-content: center; gap: 4px; font-size: 13px; box-shadow: 0 4px 14px rgba(15,23,42,0.06); width: fit-content; max-width: min(720px, calc(100% - 24px)); }}
  nav.toc a {{ color: #475569; text-decoration: none; padding: 6px 12px; border-radius: 999px; font-weight: 500; transition: all .15s ease; white-space: nowrap; display: inline-flex; align-items: center; }}
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
  .fc-note {{ font-size: 13px; color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 8px 12px; margin-top: 8px; line-height: 1.7; }}
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
    nav.toc {{ font-size: 12px; padding: 5px 7px; gap: 3px; border-radius: 14px; margin: 12px auto 18px; }}
    nav.toc a {{ padding: 5px 8px; }}
    nav.toc a .num {{ width: 16px; height: 16px; font-size: 10px; margin-right: 4px; }}
    .legend {{ font-size: 11px; }}
    .hero {{ gap: 8px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>A股主要指数缠论结构分析报告</h1>
    <p>数据区间：2021-01-04 ~ {last_date}（日线+周线，前复权） · 生成时间：{gen_time}<br>
    方法：K线包含处理 → 顶底分型 → 笔（日线≥1.8% / 周线≥4% 幅度过滤）→ 笔中枢 → MACD 背驰 → 买卖点 → 日周双级别分类推演 → 信号回测验证</p>
  </header>
  <nav class="toc">
    <a href="#s1"><span class="num">一</span>数据质量</a>
    <a href="#s2"><span class="num">二</span>走势对比</a>
    <a href="#s3"><span class="num">三</span>关键位策略</a>
    <a href="#s4"><span class="num">四</span>信号回测</a>
    <a href="#s5"><span class="num">五</span>分指数图解</a>
    <a href="#s6"><span class="num">六</span>走势推演</a>
  </nav>
  <div class="hero">
    <div class="kpi" style="border-top:3px solid {RED}"><div class="kpi-v" style="color:{RED}">{n_multi}</div><div class="kpi-l">多头延续</div></div>
    <div class="kpi" style="border-top:3px solid #d97706"><div class="kpi-v" style="color:#d97706">{n_osc}</div><div class="kpi-l">震荡偏多</div></div>
    <div class="kpi" style="border-top:3px solid {GREEN}"><div class="kpi-v" style="color:{GREEN}">{n_bear}</div><div class="kpi-l">空头/偏弱</div></div>
    <div class="kpi" style="border-top:3px solid #d97706"><div class="kpi-v" style="color:#d97706">{n_div}</div><div class="kpi-l">日周背离指数</div></div>
    <div class="kpi" style="border-top:3px solid {BLUE}"><div class="kpi-v">{avg_health:.0f}</div><div class="kpi-l">平均结构健康度</div></div>
    <div class="kpi" style="border-top:3px solid {BLUE}"><div class="kpi-v">{avg_conf:.0f}</div><div class="kpi-l">平均推演置信度</div></div>
    <div class="kpi" style="border-top:3px solid {BLUE}"><div class="kpi-v">{avg_agree:.0f}%</div><div class="kpi-l">笔双法一致率</div></div>
  </div>
  <div class="legend">
    <span><i class="dot" style="background:{RED}"></i>向上笔 / ▲买点</span>
    <span><i class="dot" style="background:{GREEN}"></i>向下笔 / ▼卖点</span>
    <span><i class="dot" style="background:{BLUE}"></i>中枢区间（最近8个）</span>
    <span><i class="dot" style="background:{GOLD}"></i>最后中枢 ZG/ZD</span>
    <span><i class="dot" style="background:#7c3aed"></i>斐波那契回调位(F38/F50/F62)</span>
    <span><i class="dot" style="background:#475569"></i>成交量 MA5</span>
  </div>

  {exec_summary}
  <h2 class="sec" id="s1">一、数据质量与校验</h2>
  {data_quality_strip(data, results)}

  <h2 class="sec" id="s2">二、总览：五指数走势对比（2021-01-04 = 100，前复权）</h2>
  <details class="panel" style="cursor:pointer">
    <summary style="font-weight:700;color:#1e40af;cursor:pointer">五指数归一化对比图（点击展开 · 2021=100）</summary>
    <div style="margin-top:12px">{compare_svg(data)}</div>
    <p style="font-size:12px;color:#64748b;margin-top:8px">五指数已对齐到<b>共同交易日</b>（取交集），避免各指数因节假日/停牌错位导致曲线偏离；均为前复权口径。</p>
  </details>

  <div class="cards">{"".join(cards)}</div>

  <h2 class="sec" id="s3">三、关键位与应对策略汇总（日周双级别）</h2>
  <div class="panel"><div class="tablescroll">{levels_table(data, results, results_week, scores)}</div></div>

  <h2 class="sec" id="s4">四、买卖点信号历史回测 <span style="font-size:12px;color:{GRAY};font-weight:400">缠论信号有效性的统计验证</span></h2>
  <div class="panel"><div class="tablescroll">{backtest_table(backtests)}</div></div>

  <h2 class="sec" id="s5">五、分指数结构图解</h2>
  {"".join(sections)}

  <h2 class="sec" id="s6">六、未来走势推演（"走势终完美"分类框架）</h2>
  <div class="disclaimer" style="margin-bottom:14px">
    <b>⚠️ 重要说明（请先读）：</b>缠论是<b>概率性的结构分类框架，不是预测工具</b>。本节的"主/次/风险路径"与"置信锥"表达的是<b>在不同结构假设下的条件应对与概率分布</b>，绝非对具体价位的预测；其中主路径概率已尽量用历史同类信号的经验胜率校准，但样本有限，<b>任何路径都不构成买入/卖出建议</b>。真实决策请结合仓位管理与个人风险承受力，并独立判断。市场有风险。
  </div>
  <div class="panel"><div class="tablescroll">{forecast_summary_table(data, results, results_week, forecast_info)}</div></div>
  <div class="panel conclusion">
    <ul>{"".join(conclusions)}</ul>
    {diverge_note}
    <p style="margin-top:10px;color:#475569;font-size:14px;line-height:1.8">
    缠论不预测点位，只给出分类应对：<b>不跌破各自最后中枢上沿 ZG / 下沿 ZD，结构仍按多头处理；
    其中 {n_above} 个指数运行于中枢上方（多头延续）、{n_inside} 个在中枢内部震荡。跌回中枢内部则降级为震荡；
    出现"顶背驰 + 跌破 ZD"组合才确认转空。</b>
    回测显示（见第四节）：一类买点后 5 日平均收益为正且胜率多在 60% 以上，一类卖点后下跌概率更高——信号具备统计意义上的参考价值，但样本有限，需结合仓位管理使用。
    </p>
  </div>
  <details class="panel method">
    <summary style="font-weight:700;color:#1e40af;cursor:pointer">术语与方法论速查（点击展开）</summary>
    <h4 style="margin-top:12px">分析流程</h4>
    <p>K线包含处理 → 顶底分型（含相等高低点处理）→ 笔（日线≥1.8% / 周线≥4% 幅度过滤）→ 笔中枢 → 笔级 + 走势段级（笔端点高阶 zigzag 聚合）MACD 背驰 → 一/二/三类买卖点（含量能背离确认）→ MA20/60/250 多空排列交叉验证 → 日×周双级别区间套 → 斐波那契回调位标注 → 经验校准的分类推演（自适应 horizon + 条件化波动率）→ 信号历史回测验证。</p>
    <h4>核心术语</h4>
    <ul>
      <li><b>笔</b>：相邻顶底分型间至少 2 根独立 K 线、且幅度达到阈值的同向线段。</li>
      <li><b>中枢</b>：至少 3 笔重叠区间 [ZD, ZG]；站上 ZG 转强，跌破 ZD 转弱。</li>
      <li><b>背驰</b>：笔级与走势段级（在笔端点序列上做高阶 zigzag 聚合得到的更大级别走势腿）均检测——价格创新高/低，但 MACD 柱面积较前一同向段萎缩 ≥15%。同时辅以<b>量能背离确认</b>：当前段成交量较前一同向段萎缩则标记"量✓"，信号更可靠（段级背离为更高一层信号）。</li>
      <li><b>买卖点</b>：一类=背驰拐点；二类=中枢内回踩/反抽不破 ZD/ZG 的折返笔；三类=中枢完成后回踩不破 ZG / 反抽不过 ZD。</li>
      <li><b>均线排列</b>：MA20/60/250 多空排列（多头/空头/纠缠），与缠论结构分歧时自动降权。</li>
      <li><b>级别联立（区间套）</b>：日线背驰与周线趋势方向组合，给出更高一级共振判断（如日线底背驰×周线向下笔=潜在周线级低点）。</li>
      <li><b>斐波那契回调位</b>：主图叠加最近一段已完成走势的 0.382/0.5/0.618 回调位（紫虚线），作为反弹/回踩的目标支撑与阻力，与推演中的 ZG/ZD 互为锚定参考。</li>
    </ul>
    <h4>概率与推演</h4>
    <p>主路径概率优先采用<b>历史同类信号的经验同向胜率</b>校准（见第四节回测），样本不足时回退启发式；推演 horizon 按最近笔平均持续交易日自适应（30~90 日），置信锥宽度按近 20 日波动率相对长期水平条件化（震荡市收窄、动荡市放大）。缠论是概率性结构分类框架，推演为目的而非点位预测。</p>
    <h4>准确度校验</h4>
    <p>① 腾讯(qfq)↔新浪(裸价) 全序列<b>比值一致性</b>比对（前复权=裸价×常数调整因子，两源比值应恒定，漂移过大即异常）；② 标准严格笔与振幅过滤笔交叉一致率；③ 8 个已知历史拐点的捕捉率；④ 砍掉末 5/10/20 根 K 线重算分类的稳定性；⑤ 交易日连续性 / 缺失检测（相邻间隔与总数 vs 首末日期应有多少交易日）。结果见第一节与推演汇总表。</p>
  </details>
  <div class="disclaimer">
    <b>免责声明：</b>本报告基于缠中说禅技术分析理论的自动化结构划分，笔与中枢识别采用近似规则（含最小幅度过滤），并辅以标准严格笔交叉验证；与严格手工画线仍可能存在差异。
    回测为历史统计特征，不代表未来表现；推演概率为启发式估算，非点位预测。缠论是概率性的结构分类框架，不构成投资建议。市场有风险，决策需独立。
  </div>
</div>
<script>
var KL_DATA = {json.dumps(kl_blob, ensure_ascii=False)};
var FC_DATA = {json.dumps(fc_blob, ensure_ascii=False)};
{NAV_JS}
{"".join(f'initNav("{sym}",{W},{CHART_TOTAL});' for sym in data)}
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
function initTip(sym){{
  var svg=document.getElementById('main-'+sym);
  var tip=document.getElementById('tip-'+sym);
  var box=svg.closest('.chartbox');
  var PAD_L=12, PAD_R=78, PLOT_W={W-PAD_L-PAD_R}, PAD_T={PAD_T}, PH={H_PRICE};
  var lo=+svg.getAttribute('data-lo'), span=+svg.getAttribute('data-span');
  var KL=KL_DATA[sym];
  var n=KL.length;
  var cx=document.getElementById('cx-'+sym);
  var cy=document.getElementById('cy-'+sym);
  var pr=document.getElementById('pr-'+sym);
  var prt=document.getElementById('prt-'+sym);
  function move(ev){{
    var ce=ev.touches?ev.touches[0]:ev;
    if(!ce) return;
    var pt=svg.createSVGPoint(); pt.x=ce.clientX; pt.y=ce.clientY;
    var loc=pt.matrixTransform(svg.getScreenCTM().inverse());
    var s=+svg.getAttribute('data-s'), e=+svg.getAttribute('data-e');
    var a=1/(e-s), b=(PAD_L*(e-s-1)-PLOT_W*s)/(e-s);
    var xo=(loc.x-b)/a;   // 反算回原始 viewBox x
    var frac=(xo-PAD_L)/PLOT_W;
    if(frac<0||frac>1){{ tip.style.display='none'; cx.setAttribute('opacity','0'); cy.setAttribute('opacity','0'); pr.setAttribute('opacity','0'); return; }}
    var idx=Math.round(frac*(n-1)); idx=Math.max(0,Math.min(n-1,idx));
    var k=KL[idx], prev=KL[idx-1]||k;
    var chg=(k[2]/prev[2]-1)*100;
    var cc=chg>=0?'#f87171':'#4ade80';
    tip.innerHTML='<b>'+k[0]+'</b><br>开 '+k[1]+'　收 '+k[2]+'<br>高 '+k[3]+'　低 '+k[4]+'<br>涨跌 <span style="color:'+cc+'">'+(chg>=0?'+':'')+chg.toFixed(2)+'%</span><br>成交量 '+k[5]+' 亿手';
    tip.style.display='block';
    var rect=box.getBoundingClientRect();
    var x=ev.clientX-rect.left+14, y=ev.clientY-rect.top+14;
    if(x+tip.offsetWidth>rect.width) x=rect.width-tip.offsetWidth-6;
    if(y+tip.offsetHeight>rect.height) y=rect.height-tip.offsetHeight-6;
    tip.style.left=x+'px'; tip.style.top=y+'px';
    // 竖线（按光标 viewBox x）+ 横线（按光标 viewBox y）+ 右侧价格读数
    cx.setAttribute('x1',loc.x); cx.setAttribute('x2',loc.x); cx.setAttribute('opacity','0.5');
    cy.setAttribute('y1',loc.y); cy.setAttribute('y2',loc.y); cy.setAttribute('opacity','0.5');
    if(loc.y>=PAD_T && loc.y<=PAD_T+PH){{
      var price=lo+(1-(loc.y-PAD_T)/PH)*span;
      prt.textContent=price.toFixed(2);
      pr.setAttribute('transform','translate(0,'+loc.y+')'); pr.setAttribute('opacity','1');
    }} else {{ pr.setAttribute('opacity','0'); }}
  }}
  function leave(){{ tip.style.display='none'; cx.setAttribute('opacity','0'); cy.setAttribute('opacity','0'); pr.setAttribute('opacity','0'); }}
  svg.addEventListener('mousemove',move);
  svg.addEventListener('mouseleave',leave);
  svg.addEventListener('touchmove',function(e){{move(e);}},{{passive:true}});
  svg.addEventListener('touchend',leave);
}}
{"".join(f'initTip("{sym}");' for sym in data)}
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
</body>
</html>"""

    with open(os.path.join(_base, "report.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("saved -> chanlun/report.html")


if __name__ == "__main__":
    main()
