# -*- coding: utf-8 -*-
"""生成自包含 HTML 缠论分析报告（内嵌 SVG，浅色主题，涨红跌绿）v6
新增：成交量面板、双法一致性、结构健康度、推演置信度、已知拐点捕捉、原则化推演"""
import json
import os
import sys
import math
import ast
from datetime import datetime, timedelta, timezone
from chanlun import analyze, backtest_signals, MIN_BI_PCT_WEEK, health_score, forecast_confidence, forward_vol, adaptive_horizon, classify, realized_vol_annualized, KNOWN_PIVOTS, _date_diff, MIN_BI_PCT_MONTH, backtest_robustness, backtest_paths, _path_targets, market_breadth, regime_factor, classify_regime, SC_BULL, SC_BEAR, build_seg_zhongshu

W, H_PRICE, H_VOL, H_MACD = 1060, 360, 64, 110
PAD_L, PAD_R, PAD_T, PAD_B = 12, 78, 24, 26
CHART_TOTAL = PAD_T + H_PRICE + 8 + H_VOL + 8 + H_MACD + PAD_B  # 600

RED, GREEN, GOLD = "#e54545", "#18a058", "#d4a017"
BLUE, GRAY, INK = "#2b6cb0", "#94a3b8", "#1f2937"

# ---------- A 股交易日历（用于推演图投影日期推算）----------
# 节假日来源：国务院办公厅《关于2026年部分节假日安排的通知》(2025-11-04 发布，已核实)；
# R345: 2027 全年条目(春节/清明/劳动/端午/中秋/国庆)为「农历历法公历日期(历法事实) +
# 惯例连休」的前瞻估算, 非官方安排——国务院 2027 安排约 2026-11 发布, 发布后须核对本表
# (尤其官方多放/少放的休市日, 周末恒休 R247 天然覆盖调休补班)。
# 当前数据日期起 horizon≤90 交易日推演触及约 2027-01 中旬, 春节(2/5 起)尚未进入触及范围,
# 属前瞻预留; sentiment/calc_v2.py 的 _CN_HOLIDAYS 已同步同源估算 2027 工作日休市表。
# R247: A股周末恒休（含调休补班日）——交易所不随调休补班开市，腾讯 K线实测 2021 至今
# 无任何周末 K线（2026-01-04/02-14/02-28/05-09/09-20/10-10 等官方补班日均休市）。
_A_SHARE_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",   # 中秋（周五~周日）
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",   # 国庆
    # 2027（节日本身公历日期为确定历法事实；调休/补班细节待国务院年底公布，MAKEUP 暂留空）
    "2027-01-01",
    # 春节：正月 2.6(初一)~2.8(初三)，除夕 2.5 + 年初四 2.9 补假
    "2027-02-05", "2027-02-06", "2027-02-07", "2027-02-08", "2027-02-09",
    # 清明：4.5(周一) 连休周末
    "2027-04-04", "2027-04-05", "2027-04-06",
    # 劳动节：5.1 连休
    "2027-05-01", "2027-05-02", "2027-05-03", "2027-05-04", "2027-05-05",
    # 端午：6.9(周三)
    "2027-06-09", "2027-06-10", "2027-06-11",
    # 中秋：9.15(周三)
    "2027-09-15", "2027-09-16", "2027-09-17",
    # 国庆：10.1~10.7
    "2027-10-01", "2027-10-02", "2027-10-03", "2027-10-04",
    "2027-10-05", "2027-10-06", "2027-10-07",
}
# R247: A股周末恒休（含调休补班日）——沪深交易所不随国务院调休补班开市。
# 腾讯 fqkline 实测 2021 至今无任何周末 K线：2026-01-04(周日)/02-14(周六)/02-28(周六)/
# 05-09(周六)/09-20(周日)/10-10(周六) 等官方补班日均无交易，故删除原 _A_SHARE_MAKEUP
# 补班交易表（R78 曾误把补班日当交易日，导致推演图预测段混入周末、日期错位）。


def _is_trading_day(dt):
    """判断某日历日是否为 A 股交易日：周末恒休（含调休补班日）+ 法定节假日休市。"""
    s = dt.strftime("%Y-%m-%d")
    if s in _A_SHARE_HOLIDAYS:
        return False
    return dt.weekday() < 5  # 0=周一 … 4=周五


# R469: 买卖点图上短名 —— "一类买点(底背驰)" → "1买"。标签宽度是去重叠的硬约束：
# 实测主图窗口 1383 根压进 948px（0.685px/日），而单标签宽 56~74px ⇒ 一个标签的宽度
# 相当于 80~110 个交易日（约 4~5 个月），故"数字+方向"两字比中文序号三字省约 33px/条
# 直接转化为可见条数。键取 kind[:3]；未登记的 kind 原样输出（不静默变空）。
_KIND_SHORT = {
    "一类买": "1买", "一类卖": "1卖",
    "二类买": "2买", "二类卖": "2卖",
    "三类买": "3买", "三类卖": "3卖",
    # R478: 段级（线段级别）买卖点 —— 显示串必须与笔级**一眼可分**（段前缀），
    # 否则用户会把段级二类当成笔级二类，两个级别的止损/目标完全不同。
    "段一买": "段1买", "段一卖": "段1卖",
    "段二买": "段2买", "段二卖": "段2卖",
    # R480: 段级三类点（段级中枢的离开-回抽）。
    "段三买": "段3买", "段三卖": "段3卖",
    # R483: 段级「类二点」（一类点之后**第 2 次**未破位的反向折返）。命名刻意让
    # `kind[:3]` **自带方向**（`段类买`/`段类卖`）—— 本表正是用 `kind[:3]` 查的，
    # 若取名「段二类买」则前 3 字「段二类」买卖同串、静默撞同一个键（R480 同类坑）。
    "段类买": "段类2买", "段类卖": "段类2卖",
    # R484: 笔级「类二点」（一类点之后**第 2 次**未破位的反向折返）—— 与 R483 段级
    # 「段类点」同律（量化见 chanlun.find_signals 的注释：第 2 次 68.2%/73.1%，
    # 不弱于首个 59.3%/62.9%；第 3 次退化）。命名让 `kind[:3]` 自带方向。
    "类二买": "类2买", "类二卖": "类2卖",
}

# R478: 段级买卖点在图上的标签后缀（与笔级 sig_points 的 ·趋/·盘/·量 同位置的语义补充）。
_SEG_MARKER = {"段级底背驰": "·背驰", "段级顶背驰": "·背驰"}

# R476: 顶部筹码条（graphic rich 文本）的排版常量 —— 与 JS 侧 option.graphic 的 style/rich
# **单一来源**（JS 里的 fontSize 由这里的值插值，_chip_obs_rect() 也用同一份，不会漂移）。
#   _KL_BASE_FS = 外层 style.fontSize，只作用于**非 rich 的字面文本**（即片段间的两个空格）
#   _KL_FS_*    = 各 rich 片段的**实际** fontSize。★ zg/zd 原先没写 fontSize，实测按 **12**
#   渲染（ECharts 的 rich 片段**不继承外层** fontSize/fontFamily，回落到默认 12px sans-serif，
#   不是外层的 11）；现补成显式值让配置说真话 —— 渲染结果逐位不变，已由 _dbg/r476/obs_api.js
#   用 zrender 外接矩形复核（47.04 / 212.8 / 右缘 431.52 前后一致）。
#   （用独立的裸名而非 dict 下标，是为了在 f-string 里直接插值，不依赖 PEP 701 的嵌套引号。）
_KL_BASE_FS = 11
_KL_FS_ZG = 12
_KL_FS_ZD = 12
_KL_FS_FIB = 10
_KL_RICH_FS = {"zg": _KL_FS_ZG, "zd": _KL_FS_ZD, "fib": _KL_FS_FIB}


def _label_w(t, extra=0.0):
    """估算标签像素宽度（与 verify_overlap.js 的字宽口径一致），用于确定性去重叠。
    extra = 背景/边框造成的额外外扩（R476：新信号标签带 padding[1,3]+borderWidth 1
    ⇒ 水平外扩 (3+1)*2 = 8px）。必须计入，否则门禁（按真实渲染算）会报重叠。"""
    w = 0.0
    for ch in str(t):
        w += 11.0 if ord(ch) > 0x2e80 else 6.16
    return w + 2.2 + extra


def _chip_obs_rect(parts, left, top, base_fs, rich_fs):
    """筹码条（顶部 graphic rich 文本）的**障碍矩形** —— 供去重叠判据把标注框与它做相交检测。

    R476 实测（两路互证：`_dbg/r476/dump_svg.js` 落盘 SVG 直读 + `obs_api.js` 用 zrender
    自身 API 取外接矩形）；样本 = 5 指数主图：

      · **片段 x 偏移严格累加**，每片段宽 = Σ字符（ASCII 0.56·fs / CJK 1.0·fs）。
        实测 "ZG 3884" = 7×0.56×12 = 47.04、段落间隔 "  " = 2×0.56×11 = 12.32、
        "Fib F0 …3939"（38 字符）= 38×0.56×10 = 212.8 ⇒ 累加右缘 = 100+331.52 = **431.52**，
        与 zrender `getBoundingRect()` + `getComputedTransform()` 报出的 431.52 **逐位相等**。
      · ★ 反直觉但已实证：**rich 片段不继承外层 style 的 fontSize/fontFamily**。外层写的是
        `fontSize: 11 / Microsoft YaHei`，而 zg/zd 实际渲染成 **12px sans-serif**（ECharts
        默认字号），只有**非 rich 的字面文本**（片段间空格）才用外层的 11。⇒ rich 未显式给
        fontSize 的一律按 **12** 算；照字面按 11 算会把宽度低估 1/12。
      · 纵向：各片段以 `dominant-baseline="central"` 居中于同一行盒（盒高 = 各片段最大 fs）
        ⇒ 并集恒为 `[top, top + max_fs]`（实测 local y ∈ [0,12] / [1,10]，加 top=32 → [32,44]）。

    ★ 返回值用**门禁 verify_overlap.js 的 textBoxes 模型**，而非 ECharts 真值框：门禁对每条
      `<text>` 按 `top = ty - 0.8·fs`、`bottom = ty + 0.3·fs`、右缘再加 `fs·0.2` 框定，相对
      真值（central 基线 ⇒ `[ty-0.5fs, ty+0.5fs]`）整体上移 0.3·fs。**关键**：门禁对筹码条
      与标签用的是同一套模型 ⇒ 两者同向平移，**相对重叠量几乎精确**（残差仅
      0.3·|fs_标签 − fs_筹码条| ≈ 0.3px）。故取门禁模型既不误删、又保证门禁结果为 0。
      （试过「真值框 ∪ 门禁框」的并集：会把上缘再抬 0.3·fs，实测制造出「贴下缘 3.3px 内
      的标签被误删」的过保守带 —— 合成用例 E。）

    parts = [(rich_name_or_None, text), ...]（None = 非 rich 字面文本）；返回 (x0, x1, y0, y1)。
    """
    _x = float(left)
    _fs_list = []
    for name, txt in parts:
        fs = float(base_fs) if name is None else float(rich_fs.get(name, 12.0))
        _fs_list.append(fs)
        for ch in str(txt):
            _x += fs * (1.0 if ord(ch) > 0x2e80 else 0.56)
    if not _fs_list:
        return None
    _maxfs = max(_fs_list)
    _cy = float(top) + _maxfs / 2.0        # 行盒中心（各片段以 central 基线居中于最大字号的盒）
    # ★ 返回**门禁模型**的框，而**不是**「真值框 [top, top+fs] 与门禁框的并集」——
    #   并集会再把上缘多抬 0.3·fs，造成「贴在筹码条下缘 3.3px 以内的标签被误删」的过保守带
    #   （合成用例 E 实测：真实视觉不重叠、却被判掉）。理由：门禁对**筹码条与标签用的是同一套
    #   模型**，两者同向平移 0.3·fs ⇒ **相对重叠量几乎精确**（残差仅 0.3·|fs_标签 − fs_筹码条|
    #   ≈ 0.3px）。故「两侧统一用门禁模型」既不误删、又能保证门禁结果为 0。
    return (float(left), _x + 0.2 * _maxfs,      # right：门禁在右缘额外加的 fs·0.2
            _cy - 0.8 * _maxfs, _cy + 0.3 * _maxfs)


def dedup_mark_labels(items, n, y_min, y_max, plot_w, plot_h, grid_l, grid_t,
                      idx_map, default_pos="top", bgap=True, w_vis=253, obs=None,
                      push_out=0.0, push_pri=2, push_step=2.0):
    """确定性地去重叠 markPoint 标签：按 (优先级 desc, x asc) 贪心保留，与已保留标签框
    重叠的则隐藏。仅修改各 item 的 label['show']，不改变标记符号。

    R469: 引入优先级 —— 原实现只按 x 排序（"谁靠左谁先占位"），使买卖点与背驰/拐点标签
    平等竞争，被挤掉的恰好是信息量最高的买卖点（实测上证 9 个买卖点被隐藏 4 个，含
    2026-03-12 二类卖、2026-07-01 三类卖，用户主观感受即"图上没有买卖点提示"）。
    现按 item 的 "_pri" 降序优先占位：买卖点=2 > 背驰=1 > 历史拐点/线段端点=0；"_pri" 在
    函数内 pop 掉，不落入产物 JSON。未提供 "_pri" 的调用方（预测图 end_points）全按 0
    处理 ⇒ 排序键退化为纯 x 升序，与原行为逐字节一致。

    R476: x 尺度模型改为**严格复刻 ECharts 的 category 轴映射**（此前是拍脑袋的 `bar = plot_w/W`
    加无偏移的 `grid_l + (i-x0)*bar`，与渲染器差 0.4~1.9%）。用 SSR 真渲染 + ECharts 自带的
    `convertToPixel()` 标定（`_dbg/r476/calib_geom.js`）得两条**精确律**（残差 0.0000）：

      boundaryGap=True （主图）: band = plot_w / W ; x(i) = grid_l + ((i - x0) + 0.5) * band
      boundaryGap=False（预测图）: bar  = plot_w / (n - 1) ; x(i) = grid_l + i * bar

    两个已实证的口径错（均属"生成期模型 ≠ 渲染器"这一类，与 R473 修的 y 向同源）：
      ① 主图窗口 **W = 253 而非 252**：JS 传 `start = (n-252)/n*100`，而 ECharts 对 category 轴按
         `startValue = floor((n-1)*start/100) = n-253` 解释 ⇒ 窗口类别恰好 **253** 个（实测
         `dataZoom[0] = {start:81.77874186550976, startValue:1130, endValue:1382}`，n=1383）。
         旧模型 W=252 且漏掉 boundaryGap 的 **+0.5 格**中心偏移 ⇒ 左端偏低 5.6px、右端偏低 1.9px。
      ② 预测图两处错（**右端累计偏差 17.73px**）：`plot_w` 传 940 而真实绘图区宽 = 1100-96-88
         = **916**（right 是 88 不是 64）；且 n=150 时 boundaryGap=False ⇒ 分母应为 **n-1=149**
         （实测 bar = 916/149 = 6.147651，旧模型 940/150 = 6.266667，相对误差 1.936%）。
    标定实测：主图 `grid_l` 反解 = **96.000**（与配置逐位相等）⇒ 此前记的"真实 ≈101.6"是我的
    推断错误，`grid_l=96` 本来就是对的，**该项作废**。
    R470: x 尺度改按「默认可见窗口」而非全历史 —— 原 `bar = plot_w / n` 把全部 n≈1384 根
    压进 948px（=0.685 px/根），使相隔 76 个交易日的两个标签（2026-03-12 二类卖 vs
    2026-07-01 三类卖，价差仅 2 点）算得 52px 距离、必然判为重叠而把后者烤死成
    show:false；但主图 dataZoom 初始窗口只有最近 253 根（=3.75 px/根），同样两个标签实际
    相距 286px、根本不重叠。尺度差 5.5 倍 ⇒ 生成期系统性过度隐藏，且 show:false 是烙进
    JSON 的，用户放大后标签也不会回来 —— 这正是"该买卖的点标不出来"的主因。
    R472: 生成期只负责**初始视图**的可见性（口径 == verify_overlap.js 的门禁渲染）。至于
    "被隐藏的标签放大后能否回来"与"窗口外元素被渲染器夹到绘图区左缘"这两件事，改由前端
    `relayout()` 在 dataZoom 后按真实视口重算 —— 旧设计让窗口外元素保留 show=True、指望
    用户缩小后由 ECharts 自行重算坐标，但 show 是烙进 JSON 的、ECharts 不会恢复。
    预测图 boundaryGap=False、全量显示（dataZoom start=0）⇒ x = grid_l + i*bar，与主图不同式。

    R476: 引入**障碍矩形** obs=(x0,x1,y0,y1) —— 顶部筹码条（graphic rich 文本）不参与贪心，
    而是一条「谁碰谁让」的硬禁区。理由：R473/R472 的门禁只验**初始视口**，而实测把视口扫到
    start=10/90/93/96 时，3 个主图共出现 6 对重叠，其中 5 对是**标注 ✕ 筹码条**
    （`05-14 段顶·背驰` ✕ `Fib …`、`12-13 段顶` ✕ `ZG 4744`/`ZD 4492` 等，最深 11px）——
    筹码条是 z-index 100 的独立 graphic，既不在 markPoint 集合里、也不参与去重叠，所以
    贪心判据对它完全无感。障碍框的几何由 `_chip_obs_rect()` 按实测排版律给出（见其 docstring）。
    ⚠ 若 obs 为 None（如未提供筹码条内容），行为与改动前**逐字节一致**。

    R479: 引入「碰撞先**外推**再隐藏」（push_out>0 时生效）。此前唯一出路是隐藏，而两个标签的
    **纵向间距**是由 `distance` 决定的 ⇒ 把后来者的 distance 推大到 |Δy| ≥ 13px，两者就能共存。
    触发场景是用户红框指出的那一类：`2026-07-20 段1买·背驰`(3741.11) 与 `2026-07-30 段2买`
    (3767.50) 价差仅 26.39 点，默认视口下 y 向相距 8.53px < 13px ⇒ 后者被隐藏。两者都是各自
    段锚点上**唯一**的二类确认位，藏掉哪个都丢信息，而它们本来就是「同级别相邻两次同向机会」，
    叠在一起才正常 ⇒ 外推后间距 ≥ 13px，可共存且仍紧邻各自锚点。

    ★ 位移量必须**自适应**（`push_step` 由小到大扫到第一个可行值），不能是固定值：
    位移 t 使 |Δy| = |Δy₀ ∓ t| **先减后增**，固定 18px 实测两头都翻过车 ——
      ① 上证 2026-07-30：Δy₀=8.53，t=18 → 推到被撞者另一侧仅 9.5px（仍撞）；
      ② 中证500 2026-08-25：t=18 → 正好撞上第三个标签（Δy=11.8）。
    规则刻意收窄，避免把图搞乱：① 只对 `_pri >= push_pri`（默认 2 = 交易信号）生效，结构
    参照位/历史拐点仍走"让位隐藏"；② 位移有上限 `push_out`（默认 40px），推不进去就隐藏，
    不做链式外推；③ 被推的条目把基准 distance 写进产物的 `d0` 键 —— 前端 `relayout()` 据此
    复位再按同一规则重推，两侧单一口径（否则用户缩放一次，distance 会被反复叠加）。
    ⚠ push_out=0（缺省）时行为与改动前**逐字节一致**（预测图调用点即走此路）。

    ★★ R484：**新增/加宽标签会改变外推量 ⇒ 可能挤掉邻近的低优先级标签**（不是缺陷，是
    优先级策略的必然结果，但必须量化后再接受）。实测（深证成指 / 创业板指，2026-09-18）：
    新增笔级类二后，`09-04 类2买·段类2买` 的显示串由 11 字增至 16 字 ⇒ 与外层邻居的 x 向
    重叠变宽 ⇒ 首个可行位移由 `t=6`(distance 10) 增到 **`t=34`(distance 38)** ⇒ 落点正好压住
    邻近的 pri-0 `09-16 段底·次低`（其 `yc=250.7`）⇒ 后者被判重叠而隐藏（5 图中 2 图，各 1 条）。
    **为什么不能"修"**：该处锚点 `yc=222.05`，要让出空间须满足 `|Δy|≥13` ⇒ 只能取 `t ≤ 14`
    或 `t ≥ 41.65`；而 `t ∈ [2,32]` 已被**已保留的 pri-2 标签群**占满、`t ≥ 41.65` 超出
    `push_out=40` ⇒ **无可行解**。若让外推"预知"该 pri-0 标签，唯一后果是**把优先级更高的
    两级共振信号（笔级类2买 ✕ 段级类2买）一起隐藏** —— 更糟。
    ⇒ 结论：pri-2 交易信号优先于 pri-0 结构参照位（与本文档 R469 的既定策略一致），
    被牺牲的标签**数据仍在产物里**（`show=False`），非数据丢失。
    ⇒ 复现"减少可见信息"的判据 = **比对真渲染 SVG 的 `<text>` 集合**（不是比 `show` 标志：
    运行时 relayout 会重算 `show`，JSON 里的值只是初值）。
    """
    if n <= 0 or (y_max - y_min) == 0:
        return
    if bgap:
        # 主图：窗口类别数 W（= 实测 253，见上）；类别中心落在 band 的中央 ⇒ +0.5
        _W = max(1, min(n, w_vis))
        _bar = plot_w / _W
        _x0 = n - _W
        _xof = lambda i: grid_l + ((i - _x0) + 0.5) * _bar
    else:
        # 预测图：boundaryGap=False ⇒ 首末类别贴绘图区两缘，间隔数 = n-1
        _bar = plot_w / max(1, n - 1)
        _x0 = 0
        _xof = lambda i: grid_l + i * _bar
    ppx = plot_h / (y_max - y_min)

    # R473: 标签纵向位置改用 ECharts 的**精确放置律**（原为「±11 / 层外推 17px」的拍脑袋模型）。
    # 用 SSR 真渲染 345 个样本标定（_dbg/r473/calib_offsets.js）⇒ 真实基线 ty 相对锚点 yy 的
    # 偏移恒为 ∓(symbolSize/2 + distance + fontSize/2)（top 取 −，bottom 取 +），**组内方差为 0**：
    #   ss=10 d=4  fs=11 → 14.5 | ss=7 d=21 fs=10 → 29.5 | ss=8 d=21 fs=11 → 30.5
    # 旧模型 yy∓11∓17·ly 与真值差：层 0 top −3.5 / bottom +3.5；层 1 ss7 −1.5 / ss8 −2.5
    # ⇒ 一旦 top×bottom 混合，**低估收敛量达 6px** —— 模型判「两标签相距 16.8px（≥13 阈值，都保留）」
    # 而真实只有 **10.8px** ⇒ 全历史缩放下残留 1~3px 真重叠（真渲染实测 #4/#8/#10 各 1~2 对）。
    # 层号 "l" 仍写进产物（供前端 relayout 与阅读），但纵向位置不再由它推、一律由本律算。
    # distance 缺省取 ECharts 默认 5（预测图 end_points 未显式给出该项）。
    def _lab_fs(it):
        """标签字号（缺省 12，与 ECharts 一致）。R476: 拆出来供障碍框判据复用 —— 门禁的
        textBoxes() 是**按 SVG 里真实 font-size** 框定标签纵向范围的，故障碍框判据也必须用
        条目自己的 fontSize，不能沿用 _label_w 里写死的 11（那一处是 x 向的保守近似）。"""
        lab = (it or {}).get("label") or {}
        try:
            return float(lab.get("fontSize", 12)) if lab.get("fontSize") is not None else 12.0
        except (TypeError, ValueError):
            return 12.0

    def _lab_dist(it):
        """标签基线与锚点的设计间距（缺省 5，与 ECharts 一致）。R479: 拆出来供外推判据复用 ——
        外推就是在它之上加一层，且前端 relayout() 需要知道**基准值**才能复位。"""
        lab = (it or {}).get("label") or {}
        try:
            return float(lab.get("distance", 5)) if lab.get("distance") is not None else 5.0
        except (TypeError, ValueError):
            return 5.0

    def _lab_off(it, pos):
        ss = (it or {}).get("symbolSize", 0)
        if isinstance(ss, (list, tuple)):
            ss = ss[1] if len(ss) > 1 else (ss[0] if ss else 0)
        try:
            ss = float(ss)
        except (TypeError, ValueError):
            ss = 0.0
        dist = _lab_dist(it)
        fsz = _lab_fs(it)
        off = ss / 2.0 + dist + fsz / 2.0
        return -off if pos == "top" else off

    def _obs_hit(m, o):
        """标签框是否与障碍矩形相交。框模型与门禁 verify_overlap.js textBoxes() 同款：
        x 向取 [x - w/2, x + w/2]（w 来自 _label_w），纵向取 [yc - 0.8·fs, yc + 0.3·fs]
        （ty 即标签中心）。相交阈值同门禁（两向都必须 > 1px）。"""
        if not o:
            return False
        lx0, lx1 = m["x"] - m["w"] / 2.0, m["x"] + m["w"] / 2.0
        ly0, ly1 = m["yc"] - m["fs"] * 0.8, m["yc"] + m["fs"] * 0.3
        ox = min(lx1, o[1]) - max(lx0, o[0])
        oy = min(ly1, o[3]) - max(ly0, o[2])
        return ox > 1.0 and oy > 1.0

    def y_of(p, pos, it=None):
        return grid_t + (y_max - p) * ppx + _lab_off(it, pos)

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
        _w = _label_w(it.get("value", ""), 8.0 if it.get("new") else 0.0)
        _x = _xof(xi)
        if xi < _x0:
            # R472: 窗口之外的元素在**初始视图**里一律不显示。此前（R470/R470b）这里保留
            # show=True，理由是"用户缩小到 1 年以上时 ECharts 会重算坐标"—— 但 show 是烤进
            # JSON 的，ECharts 不会自行恢复；且在用户缩小之前，这些点会被渲染器**夹到绘图区
            # 左缘**（R470b 实测 x=109 与 y 轴刻度打架，当时用 _XM=2 只能兜住最近 2 根）。
            # 现改为：初始态一律隐藏，由前端 relayout() 在 dataZoom 后按**真实视口**重算
            # ⇒ 既彻底杜绝左缘堆叠（不再需要 _XM 容差），又保证缩小后历史标注能正常出现。
            it.setdefault("label", {})["show"] = False
            it.pop("_pri", None)
            continue
        if _x - _w / 2 < grid_l:
            # R470: 标签框左缘探入 y 轴刻度区 —— 可见窗口最左端的点被"居中"标签向左探出，
            # 与刻度文字真实重叠（门禁实测 "09-02 段顶" ✕ "13,000" 6px、"09-04 段底" ✕
            # "6,500" 7px）。刻度文字右缘在 x=88，此处以 grid_l(96) 判，留 8px 余量。
            # 前端 relayout() 在放大后会用新视口重算，该标签能自动回来。
            it.setdefault("label", {})["show"] = False
            it.pop("_pri", None)
            continue
        recs.append({"x": _x, "yc": y_of(c[1], pos, it), "w": _w, "p": it,
                     "fs": _lab_fs(it), "pri": it.pop("_pri", 0)})
    recs.sort(key=lambda d: (-d["pri"], d["x"]))

    def _collide(mm, kept_):
        return any(abs(mm["x"] - k["x"]) < (mm["w"] + k["w"]) / 2 + 2 and
                   abs(mm["yc"] - k["yc"]) < 13 for k in kept_)

    kept = []
    for m in recs:
        # R476: 障碍框优先于贪心 —— 命中即隐藏，且**不占位**（已不可见，不该再挤掉别人）。
        # 放在贪心之前是刻意的：障碍框是「物理上被图形盖住」，与优先级无关。
        if _obs_hit(m, obs):
            m["p"]["label"]["show"] = False
            continue
        if _collide(m, kept):
            # R479: 先试「外推」（逐步加大位移，取**最小可行值**）再隐藏。后来者外推（先到先得）。
            # ★ 不能用**固定**位移：位移 t 让标签沿射线移动，与被撞者的 |Δy| = |Δy0 ∓ t| 是
            #   **先减后增**的 ⇒ 固定 18px 既可能把标签"推过"被撞者、落在其另一侧 9.5px 处
            #   （实测上证 2026-07-30 段2买），也可能正好撞上第三个标签（实测中证500 08-25）。
            _pushed = False
            if push_out > 0 and m["pri"] >= push_pri:
                _pos = (m["p"].get("label") or {}).get("position", default_pos)
                _d0 = _lab_dist(m["p"])
                _sg = 1.0 if _pos != "top" else -1.0
                _t = push_step
                while _t <= push_out:
                    _yc2 = m["yc"] + _sg * _t
                    _m2 = {"x": m["x"], "w": m["w"], "fs": m["fs"], "yc": _yc2}
                    # 纵向落点约束：标签框（[yc-0.8fs, yc+0.3fs]，与门禁 textBoxes 同模型）必须
                    # 留在画布内、且不得探进下方成交量面板（该面板上缘 = grid_t+plot_h）。
                    # ⚠ 不能用「yc 必须落在绘图区内」—— 实测深证 2026-07-01 段2卖 需要推到
                    #   yc≈34（在 grid_t=48 之上、筹码条左侧的空白区），那样才既可见又不撞任何东西；
                    #   按"绘图区内"判会把这类**完全合法**的外推一并否掉。
                    _bbt = _yc2 - m["fs"] * 0.8
                    _bbb = _yc2 + m["fs"] * 0.3
                    if (_bbt >= 2.0 and _bbb <= grid_t + plot_h + 8.0
                            and not _obs_hit(_m2, obs) and not _collide(_m2, kept)):
                        m["p"]["label"]["distance"] = _d0 + _t
                        m["p"]["d0"] = _d0    # 前端 relayout() 据此复位/重推（单一口径）
                        m["yc"] = _yc2
                        kept.append(m)
                        _pushed = True
                        break
                    _t += push_step
            if not _pushed:
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

# 牛/熊情景集合：单一来源为 chanlun.SC_BULL/SC_BEAR（见文件头 import），此处不再重复定义。




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
# ================= ECharts 主图（参考斐波那契项目，解决放大失真） =================

# ── R476: 「新信号」标记 ────────────────────────────────────────────────────────
# 要解决的问题（待议㉓）：看板此前**无法区分「今天新出的信号」与「早就存在的信号」** ——
# 两者视觉完全一致，而新信号的坐标在过去 5~12 个交易日（R474 实证滞后 7/5/12 日）⇒ 用户
# 扫一眼看不出"今天有没有新东西"。根因是信号挂在**已完成笔**的端点上（chanlun.py 的
# `bis_done = bis[:-1]`，防未来函数，是正确设计），笔的确认天然滞后。
# 这里不改滞后（那是正确设计），只把「新」这件事**如实呈现**给用户。
SIG_NEW_DAYS = 3     # 距今 ≤ N 个交易日内诞生的信号 ⇒ 图上加「新」前缀 + 底色
SIG_LOOKBACK = 6     # 有限重放深度（> SIG_NEW_DAYS 即可判"非新"；留 2 档余量防风噪）


def compute_sig_birth(klines, r_now, lookback=SIG_LOOKBACK):
    """算出每条当前信号「已经存在了几个交易日」（0 = 最后一根 K 线才出现）。

    手法 = **有限重放**（与 R474 的端到端核查同法，已被实证）：把 K 线末端依次砍掉
    1..lookback 根重跑 analyze()。某条信号在"砍掉 c 根"时**仍存在** ⇒ 它至少已存在 c 天；
    取最大的 c 即它的年龄。年龄 > SIG_NEW_DAYS ⇒ 视为旧信号。
    key 用 `(date, dir)` 而非 `(date, dir, kind)` —— 重放时 kind 可能随结构微调而改写
    （如"一类卖点"↔"三类卖点"），用 kind 会把它误判成"新生"。
    成本：5 指数 × lookback 次全量 analyze（实测约 0.07s/次 ⇒ 约 2s）。
    R478: now/keys 集合并入**段级信号**（r["seg_signals"]）—— 否则新增的段级买卖点永远拿不到
    「新」标记（它们的 (date,dir) 若不与笔级信号重合，就查不到年龄）。两级用同一张年龄表是
    正确的：段级信号挂在同一支笔端点上时，二者「诞生时刻」本就相同。
    """
    n = len(klines)
    def _keyset(r):
        return ({(s["date"], s["dir"]) for s in r.get("signals", [])} |
                {(s["date"], s["dir"]) for s in r.get("seg_signals", [])})
    now = _keyset(r_now)
    if not now or n < 80:
        return {}
    age = {k: 0 for k in now}
    for cut in range(1, lookback + 1):
        if n - cut < 80:
            break
        # ★ 必须用文件头 `from chanlun import analyze` 导入的那个 analyze —— R476 首版写成
        # `chanlun.analyze`（report.py 并未 `import chanlun`）⇒ NameError，而这里的防御式
        # try 把它静默吞成「全部 age=0 / 耗时 0.00s」的**假绿**。教训：防御式 except 必须
        # 打印，否则它把真 bug 伪装成"没有新信号"。（详见 REF-debug-pitfalls）
        keys = _keyset(analyze(klines[:n - cut]))
        for k in now:
            if k in keys:
                age[k] = cut
        if all(age[k] > SIG_NEW_DAYS for k in now):
            break        # 全部都已超过阈值 ⇒ 再往前追溯没有意义
    return age


def echart_main(klines, r, sym, captured=None, sig_age=None):
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
    ma120 = [round(v, 3) if v is not None else None for v in ma_series(closes, 120)]
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

    # R484: **段级中枢** markArea —— 补上「段级信号不可核查」的缺口。
    # 背景：R478/R480/R483 已把「段1/段2/段3/段类2」四族段级买卖点画到图上，而它们的判据
    # 全部引用**段级中枢**（段三点 = 段级中枢被离开后回抽不重回；段三点止损锚在段级中枢
    # 下沿/上沿，见 attach_rr 的 zs_ref）。但段级中枢**此前从未进入产物** ⇒ 图上完全不可见：
    # 用户看到「段3买」和它旁边的止损数字，却找不到这个数字锚在哪个区间 ⇒ 信号无法自证。
    # 口径：与段三点**同一函数**（chanlun.build_seg_zhongshu，单一来源，不在 report 侧另算），
    # 取最近 2 个（段级中枢跨度天然比笔中枢大一个量级，画多了会把图糊满）。
    # ★ 颜色用**青色 + 虚线边框**（#0d9488 系）：与笔中枢的蓝色实心带(#2b6cb0)、缺口的红/绿
    #   三者在色相与边框样式上都可区分。★ 原本选紫色（#7c3aed 系）—— 已弃用：紫色在产物里
    #   已被**预测图置信锥**占用（`rgba(124,58,237,0.10/0.12)`），跨图同色会让用户误以为同义。
    # ★ label 关闭 ⇒ 不产生任何文字元素 ⇒ 不进入标签去重叠的竞争者之列（obs 只含顶部筹码条），
    #   因此本项对既有标签可见性**零影响**（已由「加前/加后逐键深比较」实证）。
    seg_zs_areas = []
    try:
        _szs = build_seg_zhongshu(r.get("segments") or [], merged)
    except Exception:
        _szs = []
    for zs in _szs[-2:]:
        try:
            _x0 = merged[zs["start"]]["idx_start"]
            _x1 = merged[zs["end"]]["idx_end"]
        except Exception:
            continue
        if _x1 < _x0:
            _x0, _x1 = _x1, _x0
        seg_zs_areas.append([
            {"xAxis": dates[_x0], "yAxis": round(zs["zg"], 2),
             "itemStyle": {"color": "rgba(13,148,136,0.10)",
                           "borderColor": "rgba(13,148,136,0.55)",
                           "borderWidth": 1, "borderType": "dashed"},
             "label": {"show": False}},
            {"xAxis": dates[_x1], "yAxis": round(zs["zd"], 2)}
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
    # R476: 先建**片段表** kl_parts = [(rich 样式名, 文本), …]，再由此生成 key_levels_text
    # 与障碍矩形 —— 内容与几何同源，日后改条目不会出现"文字变了、障碍框没变"的漂移。
    kl_parts = []
    if zg_v is not None:
        kl_parts.append(("zg", f"ZG {zg_v}"))
    if zd_v is not None:
        kl_parts.append(("zd", f"ZD {zd_v}"))
    if fib_pairs:
        fib_txt = " ".join(f"{lab} {pv}" for lab, pv in fib_pairs)
        kl_parts.append(("fib", f"Fib {fib_txt}"))
    key_levels_text = "  ".join(f"{{{n}|{t}}}" for n, t in kl_parts)
    # 障碍矩形：片段间以**两个空格**分隔，那是非 rich 的字面文本 ⇒ 按外层 fontSize 占位
    _kl_seq = []
    for _pi, (_pn, _pt) in enumerate(kl_parts):
        if _pi:
            _kl_seq.append((None, "  "))
        _kl_seq.append((_pn, _pt))
    _chip_obs = _chip_obs_rect(_kl_seq, 100, 32, _KL_BASE_FS, _KL_RICH_FS)

    # 买卖点 markPoint
    # R472: 取消两道**按日期砍信息**的硬过滤，改由「按真实像素去重叠」统一控制可读性。
    #   ① `cutoff = n - 500`（只看最近约 2 年）—— 用户把图缩小到全历史时更早的信号全无标注，
    #      且引擎产出的信号里有 40~53 条落在窗口外被直接丢弃。
    #   ② `同方向 55 个交易日内跳过`（原 L312）—— 杀伤面最大：缠论里"二类买卖点"按定义就是
    #      **紧接一类之后的首次折返**、"三类点"是**中枢结束后的首个回抽**，间隔天然在
    #      20 根以内 ⇒ 必然被 55 根一刀切掉。实测默认可见窗口（最近 252 根）内，
    #      5 个指数合计 **37 条买卖点 + 6 条笔级背驰**从未进过产物，用户主观感受即
    #      "该买该卖的点没标出来"。
    #   而"55 根"是**日期间距**，与视觉重叠无关：相隔 30 个交易日、价差 5% 的两个标签在
    #   3.76px/根下相距 113px，根本不重叠。⇒ 过滤改交 dedup_mark_labels（纯像素判据），
    #   并配合前端 relayout() 按视口重算 —— 放大后仍能逐级显示更密的信号。
    sig_points = []
    for s in r["signals"]:
        b = bis[s["bi_index"]]
        xi = merged[b["end"]]["idx_end"]
        d = s["dir"]
        # R339: 买卖点标到真实确认位 —— 与同函数 bc_points 完全同式取 b["end_price"]
        # (笔末端合并K区间极值: 买=向下笔低点/卖=向上笔高点), 同一笔的背驰 pin 与
        # 买卖点三角坐标重叠自洽。原 price=high if d==1 else low 方向写反: 红"买"三角
        # 浮到当日最高价、绿"卖"倒三角沉最低价, 与同图 bc_points 直接矛盾(实测 3 指数
        # 30 展示点 100% 错位, 偏移 +15~+153 点; sz399006 顶背驰一类卖确认 4090 却画在
        # 3937 低 152 点)。R336 已修 radar.html 同型, 此处孪生(R 轮次此前只审本函数
        # L278-333 段)。
        price = round(b["end_price"], 2)
        _marker = ""
        if s.get("bc_type") == "趋势背驰":
            _marker += "·趋"
        elif s.get("bc_type") == "盘整背驰":
            _marker += "·盘"
        if s.get("vol_confirm"):
            _marker += "·量"
        # R469: 图上显示用短名（一类买→1买，见 _KIND_SHORT）。仅压缩显示串，signal 的
        # kind 原文仍完整保留在 r["signals"] 里（tooltip / 下游逻辑不受影响）。
        _k3 = s["kind"][:3]
        lbl = f"{dates[xi][5:]} {_KIND_SHORT.get(_k3, _k3)}{_marker}"
        # R476: 「新信号」标记（详见 compute_sig_birth 的说明）。前缀「新 」让用户一眼可辨，
        # 底色让它在一堆同类标签里跳出来；两者都会被 _label_w 的 extra 与真实渲染一并计入。
        _age = (sig_age or {}).get((dates[xi], d))
        _is_new = (_age is not None and _age <= SIG_NEW_DAYS)
        if _is_new:
            lbl = "新 " + lbl
        _sig_col = RED if d == 1 else GREEN
        # R476: 新信号额外**外推一层**（distance 4→22，实测 off 由 14.5 → 32.5px）。
        # 理由是实测出来的：09-01 的「一类卖点·顶背驰」与 08-18 的「三类卖点」价格只差 1 点、
        # x 相距 37.5px，两者同为 position=top ⇒ 纵向只差 14.5-14.5=0 ⇒ 一直被 08-18 挤掉
        # （**线上 R473 也是 show=false**，非本轮回退）。外推 18px > 重叠阈值 13px ⇒ 两者可共存，
        # 用户既保留 08-18 的原始确认位、又能看见"今天新出的这个"。distance 会被 _lab_off()
        # 与前端 relayout() 自动消费，无需另写层号。
        _lab = {"show": True, "position": "bottom" if d == 1 else "top",
                "color": _sig_col, "fontSize": 11, "fontWeight": "bold",
                "distance": (4 + 18) if _is_new else 4}
        if _is_new:
            # 同色系浅底 + 细边框：不改动 position/distance（纵向位置律不受影响），
            # 水平外扩 8px 已计入 _label_w / 前端 _labW。
            _lab.update({"backgroundColor": "rgba(229,69,69,0.13)" if d == 1 else "rgba(24,160,88,0.13)",
                         "borderColor": _sig_col, "borderWidth": 1, "borderRadius": 3,
                         "padding": [1, 3]})
        sig_points.append({
            # R469: 优先级 2（最高）—— 去重叠时买卖点先占位，不再被背驰/拐点标签挤掉
            # R472: 同一优先级另以 "p" 落进产物 —— 前端 relayout() 在 dataZoom 后按真实
            # 视口重算时要用同一套优先级（"_pri" 是函数内临时字段，被 dedup pop 掉、不入 JSON）。
            "_pri": 2, "p": 2,
            # R476: 是否「近 SIG_NEW_DAYS 个交易日内新生」（前端 _labW 要用它补 8px 外扩）
            "new": _is_new,
            "coord": [dates[xi], price],
            "value": lbl,
            "itemStyle": {"color": _sig_col},
            "symbol": "triangle" if d == 1 else "invertedTriangle",
            "symbolSize": 10,
            # R339: label position 随确认位翻转 —— 买(低点)标签置下/卖(高点)置上
            "label": _lab
        })

    # R472: **删除** bc_points（笔级背驰标注）—— 实证 100% 冗余：5 个指数共 113 条背驰点，
    # **全部**与 sig_points 里的一类买卖点坐标完全相同（两者都用 merged[b["end"]]["idx_end"]
    # + b["end_price"] 同式构造），"bc 独有 0 条"；而去重叠时买卖点 pri=2 恒压过背驰 pri=1
    # ⇒ 在取消日期硬过滤、买卖点全量进产物之后，bc 的窗口内可见数恒为 0（实测 0/23），
    # 纯属死数据，还要在每次 dataZoom 时参与前端 relayout 的重算。
    # 信息并未丢失：一类买卖点的文案已带 `·趋/·盘`（背驰级别）与 `·量`（量价背离确认），
    # 严格包含原"顶背驰/底背驰"的全部语义。
    # （原实现：遍历 r["beichi"]，按同方向 40 根、cutoff=n-500 过滤后生成 pin 标注。）

    # 线段结构 markLine
    seg_lines = []
    segments = r.get("segments", [])
    seg_pts = []   # (线段序号, klines 索引, 价格, 方向) —— R470 起供线段端点标注复用
    for _sk, sg in enumerate(segments):
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
        seg_pts.append((_sk, idx, pv, sg["dir"]))
    if len(segments) >= 2:
        recent = seg_pts[-14:]
        for i in range(len(recent) - 1):
            _, idx0, v0, _ = recent[i]
            _, idx1, v1, _ = recent[i + 1]
            seg_lines.append([
                {"coord": [dates[idx0], round(v0, 2)], "lineStyle": {"color": "#334155", "width": 1.5, "type": "dashed", "opacity": 0.5}},
                {"coord": [dates[idx1], round(v1, 2)]}
            ])

    # R470: 线段端点标注 —— 引擎早已算出线段级背驰（r["seg_beichi"]）与线段端点，但此前
    # 产物只画了笔级背驰的 pin，"段级顶/底"在图上完全不可见：实测上证 2026-05-14
    # 顶 4258.86 是 seg[29] 线段级顶背驰、2026-07-20 底 3741.11 是 seg[32] 段底，二者
    # 此前都无任何标注 ⇒ 用户主观感受即"该买卖的点没标出来"。
    # 口径：点取「段内极值」（与 segLines 完全同式，保证连线端点 == 标注点）。
    # R472: ① 线段级背驰后缀 `·背驰`、段级二类折返后缀 `·次高/·次低`（原先只写"背驰"，
    # 且 chanlun.build_segments 的端点 off-by-one 已修，见该函数注释）；② 同端点重复
    # 已随端点修正消失（旧实现因起点前移一笔而让相邻段共享端点），仍按 (日期, 价) 兜底去重。
    seg_points = []
    _seg_bc_map = {b["seg_index"]: b["type"] for b in r.get("seg_beichi", [])}
    # R472: 线段级「二类」折返 —— 段背驰（即段级一类买卖点）之后的**首个反向段**，其终点
    # 不破前极值（次高 / 次低）。这正是用户红框②的语义：上证 2026-06-23（4175.35）是顶
    # 背驰段 2026-05-14（4258.86）之后向上折返段的终点，低于前高 ⇒ 段级二类卖点。
    # 笔级 find_signals() 抓不到它 —— 它既非背驰笔、其前也没有可挂靠的「笔级一类卖」，
    # 于是图上此前完全没有提示。口径与 find_signals() 的二类判定同构（首支反向折返、
    # 不破前极值即为二类；破了则该一类已被否定、不再配二类）。
    _seg2_label = {}          # 段序号 → "次高" / "次低"
    for _b in r.get("seg_beichi", []):
        _i0 = _b["seg_index"]
        if _i0 < 0 or _i0 >= len(seg_pts) or _i0 >= len(segments):
            continue
        _want = 1 if _b["type"] == "top" else -1
        _pr0 = seg_pts[_i0][2]
        for _j in range(_i0 + 1, len(segments)):
            if segments[_j]["dir"] != _want:
                continue
            _ep = seg_pts[_j][2]
            _ok2 = (_ep < _pr0) if _b["type"] == "top" else (_ep > _pr0)
            if _ok2:
                _seg2_label[_j] = "次高" if _b["type"] == "top" else "次低"
            break
    _seg_seen = set()
    for _sk, _si, _sp, _sd in seg_pts:
        _key = (dates[_si], round(_sp, 2))
        if _key in _seg_seen:
            continue
        _seg_seen.add(_key)
        seg_points.append({
            # R470: 优先级 0（与历史拐点同级）—— 段端点是结构参照位、不是交易信号；
            # 与买卖点/背驰落在同一位置时让位（信号优先级更高），单独存在时完整显示。
            # 配色跟随 sig_points 方向语义（顶=绿/底=红，与卖点倒三角、买点三角一致）。
            # R472: 取消 `_seg_cut`(n-500) 日期截断（理由同 sig_points 段）。
            "_pri": 0, "p": 0,
            # R472/R473: 结构位标签（层 1）—— 靠 distance 21（vs 买卖点的 4）把标签推出
            # 约 16px，与买卖点在同一 x 区域共存。纵向位置由 ECharts 的真实放置律决定：
            # dedup 与前端 relayout 都用 ∓(symbolSize/2 + distance + fontSize/2)，不再用层号推算。
            "l": 1,
            "coord": [dates[_si], round(_sp, 2)],
            "value": f"{dates[_si][5:]} 段{'顶' if _sd == 1 else '底'}"
                     + ("·背驰" if _sk in _seg_bc_map
                        else ("·" + _seg2_label[_sk] if _sk in _seg2_label else "")),
            "itemStyle": {"color": GREEN if _sd == 1 else RED},
            "symbol": "circle",
            "symbolSize": 7,
            "label": {"show": True, "position": "top" if _sd == 1 else "bottom",
                      "color": GREEN if _sd == 1 else RED, "fontSize": 10, "fontWeight": "bold",
                      "distance": 21}
        })

    # R478: 段级（线段级别）买卖点 —— 级别联立的第二级（引擎见 chanlun.find_seg_signals）。
    # **为什么加**：用户红框指出的「该买点没标」实测根因之一就是这一族完全缺失 ——
    #   2026-07-30 低点 3767.50 是 2026-07-20 段底背驰(3741.11) 之后**次级别(笔)首个回抽
    #   不破前低** ⇒ 标准「段级二类买点」；但引擎的二类点只以**笔级**一类买为锚 ⇒ 系统性漏标
    #   （历史上证 4 例全漏：2022-05-10 / 2024-03-28 / 2025-05-28 / 2026-07-30；5 指数合计 87 条）。
    # 口径：段级背驰端点 = 段级一类买卖点；其后再取次级别首个反向折返不破前极值 = 段级二类。
    # 优先级 2（与笔级买卖点同级）—— 它们是交易信号，不该被结构参照位（pri 0）挤掉。
    # R479: 笔级信号索引 —— (日期, 价, 方向) → 笔级条目，供下面「同坐标同向**合并显示**」用。
    # 方向由 symbol 反推（sig_points 里 `triangle`=买 / `invertedTriangle`=卖，是唯一的单点符号）。
    _sig_at = {}
    for _p in sig_points:
        _sig_at[(_p["coord"][0], _p["coord"][1],
                 1 if _p.get("symbol") == "triangle" else -1)] = _p
    _merged_keys = set()
    seg_sig_points = []
    for s in r.get("seg_signals", []) or []:
        _bi = s["bi_index"]
        if _bi >= len(bis):
            continue
        xi = merged[bis[_bi]["end"]]["idx_end"]
        d = s["dir"]
        _k3 = s["kind"][:3]
        lbl = (f"{dates[xi][5:]} {_KIND_SHORT.get(_k3, _k3)}"
               f"{_SEG_MARKER.get(s.get('bc_type', ''), '')}")
        # 「新信号」判定走与笔级同一张年龄表（compute_sig_birth 的 now 集合已并入段级 key）⇒
        # 段级也能带「新」前缀 + 底色，无需另开一套机制。
        _age = (sig_age or {}).get((dates[xi], d))
        _is_new = (_age is not None and _age <= SIG_NEW_DAYS)
        # R479: 与**笔级信号同坐标同方向**时**合并显示**，不再另画一个 markPoint。
        # 理由：两级落在同一锚点时，两个 markPoint 的符号会**逐像素重合**（实测中证500
        # 2026-08-25 的「2买」与「段2买」坐标逐位相同；上证/深证/沪深300 同类各若干），
        # 而符号重合后贪心必挤掉其中一个 —— 丢掉的是「两级共振」这条信息，不是噪声。
        # ★ 只改**显示串**（`08-25 2买` → `08-25 2买·段2买`）：统计/回测仍各自成行、互不混计。
        _mkey = (dates[xi], round(s["price"], 2), d)
        _host = _sig_at.get(_mkey)
        if _host is not None:
            _host["value"] = f"{_host['value']}·{_KIND_SHORT.get(_k3, _k3)}"
            if _is_new and not _host.get("new"):
                # 段级是「新」而宿主不是 ⇒ 把宿主提升为「新信号」样式（前缀 + 底色 + 外推一层），
                # 与笔级新信号同一套写法；宽度补偿由 it["new"] 驱动（见 _label_w / 前端 _labW）。
                _host["new"] = True
                _host["value"] = "新 " + _host["value"]
                _host["label"].update({
                    "backgroundColor": "rgba(229,69,69,0.13)" if _host.get("symbol") == "triangle"
                                       else "rgba(24,160,88,0.13)",
                    "borderColor": _host["itemStyle"]["color"], "borderWidth": 1,
                    "borderRadius": 3, "padding": [1, 3], "distance": 4 + 18})
            _merged_keys.add((_host["coord"][0], _host["coord"][1]))
            continue
        if _is_new:
            lbl = "新 " + lbl
        _sig_col = RED if d == 1 else GREEN
        _lab = {"show": True, "position": "bottom" if d == 1 else "top",
                "color": _sig_col, "fontSize": 11, "fontWeight": "bold",
                "distance": (4 + 18) if _is_new else 4}
        if _is_new:
            _lab.update({"backgroundColor": "rgba(229,69,69,0.13)" if d == 1 else "rgba(24,160,88,0.13)",
                         "borderColor": _sig_col, "borderWidth": 1, "borderRadius": 3,
                         "padding": [1, 3]})
        seg_sig_points.append({
            "_pri": 2, "p": 2,
            # lvl=2 = 段级（笔级信号不带该键 ⇒ 前端/门禁可据此区分，向后兼容）
            "lvl": 2,
            "new": _is_new,
            "coord": [dates[xi], round(s["price"], 2)],
            "value": lbl,
            "itemStyle": {"color": _sig_col},
            # 比笔级三角大一圈：两级同时出现时用户一眼能分出哪个是线段级别。
            "symbol": "triangle" if d == 1 else "invertedTriangle",
            "symbolSize": 13,
            "label": _lab
        })
    # 去冗余：段级信号与「线段端点参照位」(seg_points) 必然落在**完全相同**的 (日期,价) 上
    # （段一类 = 段背驰端点；段二类 = 段端点·次高/次低，见 _seg2_label）。信号标签信息量严格更大
    # （含买卖方向；表③ 还给出止损/目标/R:R）⇒ 同坐标只留信号，避免两个标签互相挤。
    # R479: 被**合并进笔级标签**的段级信号（_merged_keys）也算「该坐标已有信号」—— 否则它的
    # 段端点参照位会重新出现在同一坐标上，与合并后的标签再撞一次。
    if seg_sig_points or _merged_keys:
        _sg_keys = ({(p["coord"][0], p["coord"][1]) for p in seg_sig_points}
                    | _merged_keys)
        seg_points = [p for p in seg_points
                      if (p["coord"][0], p["coord"][1]) not in _sg_keys]

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
                # R469: 优先级 0（最低）—— 已知历史拐点是参照系而非交易信号，冲突时让位
                "_pri": 0, "p": 0, "l": 1,
                "coord": [dates[_bi], round(klines[_bi]["close"], 2)],
                "value": f"{dates[_bi][5:]} ✓" if _is_cap else f"{dates[_bi][5:]} ◇",
                "itemStyle": {"color": _pcol},
                "symbol": "diamond",
                "symbolSize": 8,
                # R472/R473: 与 segPoints 同层（l=1）—— 历史拐点是参照系，让出买卖点的纵向空间；
                # position/distance 显式写出，供 dedup 与 relayout 的 ∓(ss/2+dist+fs/2) 律取值。
                "label": {"show": True, "position": "top", "distance": 21,
                          "color": _pcol, "fontSize": 11, "fontWeight": "bold"}
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

    # ── R479: 去重叠判据的 y 尺度必须与**运行时同口径**（这是「该标的买卖点标不出来」的
    # 第三个根因；前两个 R470 修了 x 尺度、R472 加了运行时 relayout，y 一直漏着）。
    #   · 生成期：上面的 `_yMin/_yMax` 是**全历史极值**（上证 2021 至今 2635~4260，span 1760
    #     ⇒ ppx ≈ 0.19 px/点）；
    #   · 运行时：`recomputeY()` 在 init（以及每次 dataZoom）按**当前视口**（默认最近 253 根）
    #     重算并 setOption yAxis.min/max（span ≈ 1040 ⇒ ppx ≈ 0.32 px/点）。
    # 实测尺度差 1.2~2.3×（5 指数）⇒ 生成期把真实相距 8.53px 的标签算成 5.04px、判为重叠并
    # **烤死**成 show:false；而前端 init 刻意**不调用** relayout（"首屏保持生成期结果、门禁
    # 看到的就是首屏"）⇒ 用户首屏看到的正是这份过度隐藏。实证：上证 2026-07-20 段1买(3741.11)
    # 与 2026-07-30 段2买(3767.50) 价差 26.39 点，真实 8.53px、生成期算成 5.04px。
    # ⇒ 判据改用**视口口径**：窗口 [a,b] 内 OHLC 极值 + 四档均线 + lastZsLines + fibLines +
    #   gapAreas，pad=6%、floor/ceil 到 10 —— 与 recomputeY() 逐式同律。
    # ⚠ 仅喂给 dedup 判据，**不改** chart_data["yMin"]/["yMax"]（那是 y 轴渲染范围，改了会连锁
    #   影响区间导航条几何，属另一件事）。
    _nd = len(dates)
    _vz = max(0.0, (_nd - 252) / _nd * 100.0) if _nd else 0.0
    _va = max(0, int(math.floor(_nd * _vz / 100.0)))
    _vb = _nd - 1
    _yMinV, _yMaxV = _yMin, _yMax
    if _nd and _vb >= _va:
        _vlo = min(ohlc[i][2] for i in range(_va, _vb + 1))
        _vhi = max(ohlc[i][3] for i in range(_va, _vb + 1))
        for _ser in (ma20, ma60, ma120, ma250):
            for i in range(_va, _vb + 1):
                _v = _ser[i]
                if _v is not None:
                    if _v < _vlo:
                        _vlo = _v
                    if _v > _vhi:
                        _vhi = _v
        for _ln in (last_zs_lines, fib_lines):
            for _e in _ln:
                _v = _e.get("yAxis")
                if _v is not None:
                    if _v < _vlo:
                        _vlo = _v
                    if _v > _vhi:
                        _vhi = _v
        for _gp in gap_areas:
            for _e in _gp:
                _v = _e.get("yAxis")
                if _v is not None:
                    if _v < _vlo:
                        _vlo = _v
                    if _v > _vhi:
                        _vhi = _v
        _vpad = (_vhi - _vlo) * 0.06
        _yMinV = math.floor((_vlo - _vpad) / 10) * 10
        _yMaxV = math.ceil((_vhi + _vpad) / 10) * 10

    vmax = max(volumes) or 1
    hmax = max(abs(v) for v in hist) or 1

    # 确定性去重叠：买卖点/背驰/线段端点/拐点标签（ECharts markPoint 的 hideOverlap 在带 position/distance 时不可靠）
    # R476: w_vis=253 是 ECharts 对 `dataZoom.start = (n-252)/n*100` 的实际解释结果
    # （category 轴按 startValue = floor((n-1)*start/100) ⇒ n-253），SSR 实测 n=1383 时
    # dataZoom[0] = {startValue:1130, endValue:1382} ⇒ 窗口类别 253 个。改这个值必须同改
    # 下方 JS 的 dataZoom.start 算式（`D.dates.length - 252`）。
    # R479: y 传视口口径（_yMinV/_yMaxV），并开启「碰撞自适应外推」（≤40px，只对交易信号）。
    dedup_mark_labels(sig_points + seg_sig_points + seg_points + cap_points,
                      len(dates), _yMinV, _yMaxV,
                      1100 - 96 - 56, 640 * (1 - 0.40) - 48, 96, 48, date_idx,
                      bgap=True, w_vis=253, obs=_chip_obs,
                      push_out=40.0, push_pri=2, push_step=2.0)
    # R476 注：预测图（下方 forecast_echart）**不加**障碍框 —— 它的标签是静态的、
    # 不经 relayout 重排，且其筹码条（left:100, top:50，6 种 rich 片段）在 verify_overlap.js
    # 的初始 + 3 档视口扫描里实测零重叠 ⇒ 已被门禁完全覆盖，不需要额外判据。

    chart_data = {
        "dates": dates,
        "ohlc": ohlc,
        "volume": volumes,
        "ma20": ma20,
        "ma60": ma60,
        "ma120": ma120,
        "ma250": ma250,
        "dif": dif,
        "dea": dea,
        "hist": hist,
        "yMin": round(_yMin, 2),
        "yMax": round(_yMax, 2),
        "vmax": vmax,
        "hmax": round(hmax, 3),
        "markAreas": mark_areas,
        "segZsAreas": seg_zs_areas,
        "lastZsLines": last_zs_lines,
        "gapAreas": gap_areas,
        "fibLines": fib_lines,
        "sigPoints": sig_points,
        "segSigPoints": seg_sig_points,
        "segPoints": seg_points,
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
  (window.__charts = window.__charts || []).push(chart);
  // 主图时间轴标签动态密度：按可见窗口交易日常数选择日/周/月/季边界，避免固定季度标签在放大后"断断续续"。
  // 实现方式：interval 固定为 0 + autoHide 关闭，把"是否显示"的判断下沉到 formatter；彻底避免 ECharts 自动抽稀导致日期"错配"。
  var __mainAxisVisible = D.dates.length;
  function __isMonthStart(idx) {{ var d = D.dates[idx]; return d && d.slice(8,10) === '01'; }}
  function __isQuarterStart(idx) {{
    var d = D.dates[idx];
    if (!d || d.slice(8,10) !== '01') return false;
    var m = parseInt(d.slice(5,7),10);
    return m === 1 || m === 4 || m === 7 || m === 10;
  }}
  function __isMonday(idx) {{
    var parts = D.dates[idx].split('-');
    return new Date(parseInt(parts[0],10), parseInt(parts[1],10)-1, parseInt(parts[2],10)).getDay() === 1;
  }}
  function __mainAxisShowLabel(idx) {{
    var v = __mainAxisVisible;
    if (v <= 30) return true;
    if (v <= 60) return __isMonday(idx);
    if (v <= 180) return __isMonthStart(idx);
    return __isQuarterStart(idx);
  }}
  function __mainAxisFormatter(v, i) {{
    // ECharts dataZoom 后 formatter 的 i 可能是视觉索引而非数据索引；优先用 v 反查真实日期，避免缩放后显示 2021 这种错配。
    var idx = (D.dates && v) ? D.dates.indexOf(v) : i;
    if (idx < 0) idx = i;
    var d = (idx >= 0 && D.dates && D.dates[idx]) ? D.dates[idx] : v;
    if (!d || d.length < 7) return v;
    if (!__mainAxisShowLabel(idx)) return '';
    // R122: 可见天数较多时统一显示月-日，避免首标签年份与紧随其后的月日标签因间隔太近而水平重叠。
    if (__mainAxisVisible <= 10) return d;
    return d.slice(5);
  }}
  function __makeMainAxisFormatter() {{ return function(v, i) {{ return __mainAxisFormatter(v, i); }}; }}
  function updateMainAxisLabels() {{
    var opt = chart.getOption();
    var dz = (opt.dataZoom && opt.dataZoom[0]) || {{ start: 0, end: 100 }};
    var start = dz.start || 0, end = dz.end || 100;
    __mainAxisVisible = Math.max(1, Math.floor(D.dates.length * (end - start) / 100));
    chart.setOption({{ xAxis: [{{}}, {{}}, {{ axisLabel: {{ interval: 0, autoHide: false, hideOverlap: false, formatter: __makeMainAxisFormatter() }} }}] }});
  }}
  // 价格 y 轴按可见窗口动态重算范围：默认视图(近252日)与缩放后都填满纵向空间，
  // 避免全历史 min/max 把近期 K 线压扁(沪深300 近期仅占全范围 32%)。含窗口内 OHLC 极值 +
  // 四档均线 + 近价中枢(ZG/ZD)/Fib/缺口线，远史中枢阴影不纳入以免过度拉宽。防御式 try/catch 兜底。
  function recomputeY() {{
    try {{
      var opt = chart.getOption();
      var dz = (opt.dataZoom && opt.dataZoom[0]) || {{ start: 0, end: 100 }};
      var s = dz.start || 0, e = dz.end || 100;
      var a = Math.max(0, Math.floor(D.dates.length * s / 100));
      var b = Math.min(D.dates.length - 1, Math.ceil(D.dates.length * e / 100));
      var lo = Infinity, hi = -Infinity;
      for (var i = a; i <= b; i++) {{
        var o = D.ohlc[i]; if (!o) continue;
        if (o[2] < lo) lo = o[2];
        if (o[3] > hi) hi = o[3];
        var mas = [D.ma20, D.ma60, D.ma120, D.ma250];
        for (var mi = 0; mi < mas.length; mi++) {{
          var mv = mas[mi] && mas[mi][i];
          if (mv != null) {{ if (mv < lo) lo = mv; if (mv > hi) hi = mv; }}
        }}
      }}
      function _inc(v) {{ if (v != null && !isNaN(v)) {{ if (v < lo) lo = v; if (v > hi) hi = v; }} }}
      D.lastZsLines.forEach(function(x){{ _inc(x.yAxis); }});
      D.fibLines.forEach(function(x){{ _inc(x.yAxis); }});
      (D.gapAreas || []).forEach(function(p){{ _inc(p[0].yAxis); _inc(p[1].yAxis); }});
      if (lo < hi) {{
        var pad = (hi - lo) * 0.06;
        var nmin = Math.floor((lo - pad) / 10) * 10;
        var nmax = Math.ceil((hi + pad) / 10) * 10;
        chart.setOption({{ yAxis: [{{ min: nmin, max: nmax }}, {{}}, {{}}] }});
      }}
    }} catch (err) {{}}
  }}
  // R472: 按**真实视口**重算每个标注的 label.show。生成期（Python）只能按"初始 252 根
  // 窗口 + 设计宽度 1100"算一次并把结果烤进 JSON ⇒ 用户放大后被隐藏的标签不会回来
  // （show:false 不可逆），缩小后历史标注全部缺席，窗口外元素还会被渲染器夹到绘图区
  // 左缘与 y 轴刻度打架。这里把同一套「优先级 + 像素重叠」贪心判据搬到运行时：每次
  // dataZoom 后按当前可见窗口与真实画布宽度重算，任意缩放级别都取该级别下的最优集。
  // 判据与 report.py dedup_mark_labels() 同口径（字宽公式 / 13px 框高 / +2px 余量）。
  // R476: extra = 背景/边框的额外外扩（新信号标签带 padding[1,3]+borderWidth1 ⇒ 8px），
  // 必须与 report.py _label_w(t, extra) 逐字同口径，否则门禁（按真实渲染算）会报重叠。
  function _labW(t, extra) {{
    t = '' + (t == null ? '' : t);
    var w = 0;
    for (var i = 0; i < t.length; i++) w += (t.charCodeAt(i) > 0x2e80) ? 11.0 : 6.16;
    return w + 2.2 + (extra || 0);
  }}
  // R476: 筹码条障碍矩形 —— 缩放后标签会挤到它身上（真渲染实测：视口扫到 start=10/90/93/96
  // 时 3 个主图共 6 对重叠，其中 5 对是「标注 ✕ 筹码条」，最深 11px）。它是 z-index 100 的
  // 独立 graphic，既不在 markPoint 集合里、也不参与贪心 ⇒ 必须单独当**硬禁区**。
  // ★ 宽度**不能靠公式猜**：ECharts 的 rich 片段不继承外层 fontSize（实测 zg/zd 渲染成
  // 12px 而配置写的是 11px），且真实浏览器字体度量 ≠ 我们的 0.56·fs 近似 ⇒ 让渲染器自己报：
  // zrender 的 displayList 里每个文本元素都有 getBoundingRect()（局部）+ getComputedTransform()
  // （全局），合成即绝对矩形。canvas/svg 两种 renderer 都维护该列表，浏览器里同样可用。
  // 片段身份用**内容**匹配（从 option.graphic[0].style.text 解析出各 rich 片段文本），不靠
  // 坐标猜；片段间的字面空格必然落在首尾片段之间，故只匹配 rich 片段即可覆盖全长。
  var _KL_OBS_MFS = {max(_KL_RICH_FS.values())};   // 筹码条最大字号（供两模型并集外扩，见下）
  function _chipObs(opt) {{
    try {{
      // ★ 取值必须兼容**两种形态**：配置里写的是扁平数组 `graphic:[{{type,left,top,style}}]`，
      // 而 `chart.getOption()` 会把它**归一化成** `graphic:[{{elements:[{{…}}]}}]`。
      // R476 首版只读扁平形态 ⇒ klt 恒为空 ⇒ 障碍框恒为 null（真渲染扫描实测"改了等于没改"，
      // 由 _dbg/r476/dbg_obs.js 注入自省取到 getOption().graphic[0] 的键名 = ["elements"]）。
      var _g = (opt.graphic && opt.graphic[0]) || null;
      var _gst = _g ? (_g.style || (((_g.elements || [])[0] || {{}}).style)) : null;
      var klt = (_gst && _gst.text) || '';
      if (!klt) return null;
      var want = {{}}, parts = klt.split('{{');
      for (var i = 0; i < parts.length; i++) {{
        var seg = parts[i].split('}}')[0];
        var bar = seg.indexOf('|');
        if (bar > 0) want[seg.slice(bar + 1)] = 1;
      }}
      var list = chart.getZr().storage.getDisplayList();
      // ★ 取极值用 null 哨兵，刻意**不写 JS 的「无穷大」字面量**：audit_report_runtime.py 的
      //   白名单只认 `var lo = …, hi = …;` 那一种良性循环初值，其余任何一处出现都会被判为
      //   「数据字面量」并**阻断 CI**（本改动首版即因此把该门禁打成 REAL_EXIT=1、残留 15 处）。
      //   不去放宽门禁 —— 那是把护栏改软来迁就新代码。（同理：注释里也不写该 token。）
      var bb = null, hit = 0;
      for (var j = 0; j < list.length; j++) {{
        var el = list[j], st = el && el.style;
        if (!st || typeof st.text !== 'string' || !want[st.text]) continue;
        var br = el.getBoundingRect ? el.getBoundingRect() : null;
        if (!br) continue;
        var mt = el.getComputedTransform ? el.getComputedTransform() : null;
        var ax = br.x, ay = br.y, aw = br.width, ah = br.height;
        if (mt) {{
          var gx1 = ax * mt[0] + ay * mt[2] + mt[4], gy1 = ax * mt[1] + ay * mt[3] + mt[5];
          var gx2 = (ax + aw) * mt[0] + (ay + ah) * mt[2] + mt[4];
          var gy2 = (ax + aw) * mt[1] + (ay + ah) * mt[3] + mt[5];
          ax = Math.min(gx1, gx2); ay = Math.min(gy1, gy2);
          aw = Math.abs(gx2 - gx1); ah = Math.abs(gy2 - gy1);
        }}
        if (!bb) bb = {{ x0: ax, x1: ax + aw, y0: ay, y1: ay + ah }};
        else {{
          if (ax < bb.x0) bb.x0 = ax;
          if (ay < bb.y0) bb.y0 = ay;
          if (ax + aw > bb.x1) bb.x1 = ax + aw;
          if (ay + ah > bb.y1) bb.y1 = ay + ah;
        }}
        hit++;
      }}
      if (!hit || !bb) return null;
      // 与 report.py _chip_obs_rect() **同口径**：量到的是 ECharts 真值（行盒 [top, top+maxfs]），
      // 这里换算成**门禁 textBoxes 模型**的框 —— 纵向以行盒中心为基准取
      // [cy - 0.8·fs, cy + 0.3·fs]、右缘再加 fs·0.2。门禁对筹码条与标签用同一套模型，
      // 两侧同向平移 ⇒ 相对重叠量几乎精确（见 _chip_obs_rect 的说明）。
      var _cy = (bb.y0 + bb.y1) / 2;
      return {{ x0: bb.x0, x1: bb.x1 + 0.2 * _KL_OBS_MFS,
                y0: _cy - 0.8 * _KL_OBS_MFS, y1: _cy + 0.3 * _KL_OBS_MFS }};
    }} catch (e) {{ return null; }}
  }}
  // R478: 段级买卖点（D.segSigPoints）与笔级同列进 markPoint；`|| []` 兜底是为了让
  // 旧产物/局部渲染（无该键）仍能跑，不因新增键而 ReferenceError。
  var _MK = D.sigPoints.concat(D.segSigPoints || []).concat(D.segPoints).concat(D.capPoints);
  var _MKI = {{}};
  for (var _qi = 0; _qi < D.dates.length; _qi++) _MKI[D.dates[_qi]] = _qi;
  // R479: 碰撞「自适应外推」的参数，必须与 report.py dedup_mark_labels(push_out/push_pri/
  // push_step) **同参**（两侧不同 ⇒ 门禁按真实渲染算出的结论与生成期会对不上）：
  //   _PUSH_MAX=40（位移上限，推不进去就隐藏）/ _PUSH_STEP=2（由小到大扫，取**最小可行**位移）/
  //   仅 pri>=2（交易信号）适用。★ 位移不能取固定值 —— |Δy| = |Δy₀ ∓ t| 是**先减后增**的，
  //   固定值既可能"推过"被撞者落在它另一侧（实测上证 07-30 段2买：Δy 9.5px 仍撞），也可能
  //   正好撞上第三个标签（实测中证500 08-25：Δy 11.8px）。
  var _PUSH_MAX = 40, _PUSH_STEP = 2;
  // R479: 障碍框判据抽成函数 —— 外推重试要拿**新的 yc** 再判一次「是否推进筹码条里」。
  // 框模型与 report.py _obs_hit() 逐字同式（x 向 [x±w/2]，纵向 [yc-0.8·fs, yc+0.3·fs]，两向 > 1px）。
  function _obsHit(x, yc, w, fs, o) {{
    if (!o) return false;
    var _ox = Math.min(x + w / 2, o.x1) - Math.max(x - w / 2, o.x0);
    var _oy = Math.min(yc + fs * 0.3, o.y1) - Math.max(yc - fs * 0.8, o.y0);
    return _ox > 1 && _oy > 1;
  }}
  function relayout() {{
    var W = chart.getWidth();
    if (!W || W < 300) return;            // 尺寸不可用（SSR 等）⇒ 保留生成期结果
    var n = D.dates.length;
    var opt = chart.getOption();
    var _obs = _chipObs(opt);   // R476: 筹码条硬禁区（取不到则退化为无禁区 = 改动前行为）
    var dz = (opt.dataZoom && opt.dataZoom[0]) || {{ start: 0, end: 100 }};
    // R476: 与 ECharts category 轴（boundaryGap=True）**严格同式**。旧代码用
    //   s = floor(n*start/100)、bar = plotW/(e-s) —— 两处都与渲染器不符：
    //   ① ECharts 的 percent→索引是 (n-1) 而非 n：start=(n-252)/n*100 时
    //      startValue = floor((n-1)*start/100) = n-253（实测 n=1383 → 1130）；
    //   ② boundaryGap=True 时窗口含 W = e-s+1 个类别（各占一 band），类别中心落在 band 中央
    //      ⇒ x = gridL + ((i-s)+0.5) * plotW/W。
    //   优先直接取 ECharts 算好的 startValue/endValue（最不易错），退回时按 (n-1) 换算。
    var s, e2;
    if (dz.startValue != null) s = dz.startValue;
    else s = Math.max(0, Math.floor((dz.start || 0) / 100 * (n - 1)));
    if (dz.endValue != null) e2 = dz.endValue;
    else e2 = Math.min(n - 1, Math.ceil(((dz.end == null) ? 100 : dz.end) / 100 * (n - 1)));
    var gridL = 96, gridR = 56, gridT = 48;
    var plotW = W - gridL - gridR;
    var plotH = 640 * (1 - 0.40) - gridT;
    if (plotW < 100) return;
    var bar = plotW / Math.max(1, e2 - s + 1);
    var yAx = (opt.yAxis && opt.yAxis[0]) || {{}};
    var yMin = (yAx.min == null ? D.yMin : yAx.min);
    var yMax = (yAx.max == null ? D.yMax : yAx.max);
    if (!(yMax > yMin)) {{ yMin = D.yMin; yMax = D.yMax; }}
    var ppx = plotH / (yMax - yMin);
    var recs = [], i, it, c, xi, lab, w, x, pos;
    for (i = 0; i < _MK.length; i++) {{
      it = _MK[i]; c = it.coord;
      if (!c) continue;
      xi = _MKI[c[0]];
      if (xi == null) continue;
      lab = it.label || (it.label = {{}});
      w = _labW(it.value, it.new ? 8 : 0);
      x = gridL + ((xi - s) + 0.5) * bar;
      // ① 点本身落在绘图区内（否则渲染器会把它 clamp 到边界、与 y 轴刻度打架）
      // ② 标签框左缘不得探入 y 轴刻度区（与 report.py 的轴区判据同口径）
      if (!(x >= gridL - 0.5 && x <= gridL + plotW + 0.5 && x - w / 2 >= gridL)) {{
        lab.show = false; continue;
      }}
      pos = lab.position || 'top';
      // R473: 纵向位置改与 report.py _lab_off() **逐字同律** —— 真实基线偏移 =
      // ∓(symbolSize/2 + distance + fontSize/2)（top 取 −）。旧的 "±11 + 层×17" 在
      // top×bottom 混合时低估收敛量 6px ⇒ 真渲染残留 1~3px 重叠（SSR 标定 345 样本）。
      var _ss = it.symbolSize;
      if (Object.prototype.toString.call(_ss) === '[object Array]') _ss = _ss.length > 1 ? _ss[1] : (_ss[0] || 0);
      _ss = (typeof _ss === 'number' ? _ss : (parseFloat(_ss) || 0));
      var _fsz = parseFloat(lab.fontSize);
      if (isNaN(_fsz)) _fsz = 12;
      // R479: distance 用**基准值**（d0）参与定位，并在每次重排时复位 —— 生成期可能已把它
      // 外推过一层（此时产物里同时写了 d0 = 原值），若不复位会层层叠加（缩放几次就飘走）。
      var _d0 = (typeof it.d0 === 'number' ? it.d0 : (lab.distance == null ? 5 : parseFloat(lab.distance)));
      if (isNaN(_d0)) _d0 = 5;
      it.d0 = _d0;
      lab.distance = _d0;
      var _off = _ss / 2 + _d0 + _fsz / 2;
      var _ybase = gridT + (yMax - c[1]) * ppx;
      var _ysg = (pos === 'top' ? -1 : 1);
      recs.push({{ x: x, yc: _ybase + _ysg * _off, ybase: _ybase, ysg: _ysg, off: _off,
                  w: w, it: it, fs: _fsz, pri: (it.p == null ? 0 : it.p) }});
    }}
    recs.sort(function (a, b) {{ return (b.pri - a.pri) || (a.x - b.x); }});
    var kept = [];
    for (i = 0; i < recs.length; i++) {{
      var m = recs[i], hit = false, k;
      // R476: 障碍框优先于贪心 —— 命中即隐藏、且**不占位**（已不可见，不该再挤掉别人），
      // 与 report.py dedup_mark_labels() 的同一段判据同序同式。框模型与门禁 textBoxes()
      // 同款：x = [x±w/2]，纵向 = [yc-0.8·fs, yc+0.3·fs]，两向都需 > 1px 才算重叠。
      if (_obsHit(m.x, m.yc, m.w, m.fs, _obs)) {{ m.it.label.show = false; continue; }}
      for (k = 0; k < kept.length; k++) {{
        if (Math.abs(m.x - kept[k].x) < (m.w + kept[k].w) / 2 + 2 &&
            Math.abs(m.yc - kept[k].yc) < 13) {{ hit = true; break; }}
      }}
      // R479: 碰撞时先试「自适应外推」再隐藏 —— 与 report.py dedup_mark_labels(push_out=40,
      // push_pri=2, push_step=2) **同规则**（同参同序）。位移由小到大扫到第一个可行值；
      // 推不进去（超出上限 / 会撞筹码条障碍框 / 越过绘图区）就隐藏。被推的条目把 distance
      // 写回 label ⇒ ECharts 按新位置渲染；下次 relayout 由 d0 复位后重推（幂等，不层叠）。
      if (hit && m.pri >= 2) {{
        for (var _t = _PUSH_STEP; _t <= _PUSH_MAX; _t += _PUSH_STEP) {{
          var _yc2 = m.ybase + m.ysg * (m.off + _t);
          // 纵向落点约束与 report.py 同口径：标签框留在画布内、不进下方成交量面板。
          if (_yc2 - m.fs * 0.8 < 2 || _yc2 + m.fs * 0.3 > gridT + plotH + 8) continue;
          if (_obsHit(m.x, _yc2, m.w, m.fs, _obs)) continue;
          var _h2 = false;
          for (k = 0; k < kept.length; k++) {{
            if (Math.abs(m.x - kept[k].x) < (m.w + kept[k].w) / 2 + 2 &&
                Math.abs(_yc2 - kept[k].yc) < 13) {{ _h2 = true; break; }}
          }}
          if (!_h2) {{ m.it.label.distance = m.it.d0 + _t; m.yc = _yc2; hit = false; break; }}
        }}
      }}
      m.it.label.show = !hit;
      if (!hit) kept.push(m);
    }}
    chart.setOption({{ series: [{{ markPoint: {{ data: _MK.slice() }} }}] }});
  }}
  var option = {{
    animation: false,
    axisPointer: {{ link: [{{ xAxisIndex: 'all' }}], label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }},
    tooltip: {{
      trigger: 'axis',
      axisPointer: {{ type: 'cross', label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }},
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
    legend: {{ data: ['日K', 'MA20', 'MA60', 'MA120', 'MA250', '成交量', 'MACD', 'DIF', 'DEA'], top: 2, itemGap: 12, textStyle: {{ fontSize: 11 }} }},
    grid: [
      {{ left: 96, right: 56, top: 48, bottom: '40%' }},
      {{ left: 96, right: 56, top: '62%', height: '11%' }},
      {{ left: 96, right: 56, top: '76%', bottom: 56 }}
    ],
    xAxis: [
      {{ type: 'category', data: D.dates, gridIndex: 0, axisLabel: {{ show: false }}, axisPointer: {{ label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }} }},
      {{ type: 'category', data: D.dates, gridIndex: 1, axisLabel: {{ show: false }} }},
      {{ type: 'category', data: D.dates, gridIndex: 2, axisTick: {{ show: false }}, axisLabel: {{ fontSize: 11, margin: 6, interval: 0, autoHide: false, hideOverlap: false,
        formatter: __makeMainAxisFormatter() }} }}
    ],
    yAxis: [
      {{ scale: false, min: D.yMin, max: D.yMax, gridIndex: 0, splitNumber: 6, axisLine: {{ lineStyle: {{ color: '#cbd5e1' }} }}, splitLine: {{ lineStyle: {{ color: '#eef2f7' }} }}, axisLabel: {{ fontSize: 12, hideOverlap: true }}, axisPointer: {{ label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }} }},
      {{ scale: true, gridIndex: 1, splitNumber: 2, name: '成交量', nameLocation: 'middle', nameGap: 34, nameTextStyle: {{ color: '#94a3b8', fontSize: 11 }}, axisLine: {{ show: false }}, splitLine: {{ show: false }}, axisLabel: {{ show: false }}, axisPointer: {{ label: {{ show: false }} }} }},
      {{ scale: true, gridIndex: 2, min: -D.hmax, max: D.hmax, splitNumber: 2, name: 'MACD', nameLocation: 'middle', nameGap: 34, nameTextStyle: {{ color: '#94a3b8', fontSize: 11 }}, axisLine: {{ show: false }}, splitLine: {{ show: false }}, axisLabel: {{ show: false }}, axisPointer: {{ label: {{ show: false }} }} }}
    ],
    // 默认展示最近约 1 年(252 交易日)，避免首次打开落在 5 年前最早数据上造成"时间轴日期不对"的错觉；用户仍可缩放/平移看全历史。
    dataZoom: [
      {{ type: 'inside', xAxisIndex: [0, 1, 2], start: Math.max(0, (D.dates.length - 252) / D.dates.length * 100), end: 100 }},
      // showDetail 默认 true：拖拽/悬停 slider 手柄时显示当前窗口起止日期，避免 showDetail:false 时缩略图只剩一个左端年份（如 2021）造成"时间轴不对"的误解。
      {{ type: 'slider', xAxisIndex: [0, 1, 2], start: Math.max(0, (D.dates.length - 252) / D.dates.length * 100), end: 100, height: 16, bottom: 12, handleStyle: {{ color: '#2b6cb0' }}, borderColor: '#e2e8f0', fillerColor: 'rgba(43,108,176,0.12)' }}
    ],
    series: [
      {{
        name: '日K', type: 'candlestick', data: D.ohlc,
        itemStyle: {{ color: '#e54545', color0: '#18a058', borderColor: '#e54545', borderColor0: '#18a058' }},
        markArea: {{ data: D.markAreas.concat(D.gapAreas).concat(D.segZsAreas || []), silent: true }},
        markLine: {{ symbol: 'none', data: D.lastZsLines.concat(D.fibLines).concat(D.segLines), silent: false, labelLayout: {{ moveOverlap: 'shiftY' }} }},
        markPoint: {{ data: _MK }}
      }},
      {{ name: 'MA20', type: 'line', data: D.ma20, symbol: 'none', lineStyle: {{ color: '#0ea5e9', width: 1.1 }} }},
      {{ name: 'MA60', type: 'line', data: D.ma60, symbol: 'none', lineStyle: {{ color: '#a855f7', width: 1.2 }} }},
      {{ name: 'MA120', type: 'line', data: D.ma120, symbol: 'none', lineStyle: {{ color: '#f59e0b', width: 1.2 }} }},
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
        fontFamily: 'Microsoft YaHei', fontSize: {_KL_BASE_FS},
        // R476: 各片段 fontSize 由 report.py 的 _KL_FS_* 插值（单一来源）。原先 zg/zd 省写
        // fontSize，而 ECharts 的 rich 片段**不继承**外层 fontSize ⇒ 实际按默认 12 渲染；
        // 现写显式值，使「配置写的就是渲染的」（也才能据此算准障碍矩形）。
        rich: {{
          zg:  {{ fill: '{GOLD}', fontWeight: 'bold', fontSize: {_KL_FS_ZG} }},
          zd:  {{ fill: '{GOLD}', fontWeight: 'bold', fontSize: {_KL_FS_ZD} }},
          fib: {{ fill: '#7c3aed', fontSize: {_KL_FS_FIB} }}
        }}
      }}
    }}];
  }}
  chart.setOption(option);
  chart.on('dataZoom', updateMainAxisLabels);
  chart.on('dataZoom', recomputeY);
  // R472: 注册顺序有意义 —— recomputeY 先按新视口改 y 轴 min/max，relayout 再据此重算
  // 标注可见性（它读 chart.getOption().yAxis[0]）。首屏**不主动调用**：生成期结果与
  // verify_overlap.js 的门禁口径一致，保持"门禁看到的就是首屏"；用户一旦缩放即接管。
  chart.on('dataZoom', relayout);
  chart.on('dblclick', function(){{ chart.dispatchAction({{ type: 'dataZoom', start: Math.max(0, (D.dates.length - 252) / D.dates.length * 100), end: 100 }}); }});  /* R167: 双击复位回初始窗口(最近约1年), 非全量历史 */
  updateMainAxisLabels();
  recomputeY();
}})();
</script>"""


# ================= 区间导航条（缩略图 + 可拖窗口） =================
NAV_H = 24

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


def forecast_svg(klines, r, wcls, conf, sigma, sym, horizon=60, bt=None, bt_paths=None, breadth_score=None, sent_fc=None):
    bt = bt or {}            # 防御：未传回测时退化为空，避免 None.get 崩溃
    bt_paths = bt_paths or {}
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
    # R168: r["bis"] 为空(退化短行情)时两分支皆 IndexError; 加空守卫
    if r["bis"]:
        comp = r["bis"][-2] if len(r["bis"]) >= 2 else r["bis"][-1]
        move = max(abs(comp["end_price"] / comp["start_price"] - 1), 0.03)
    else:
        comp = None
        move = 0.03

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
    # R347: _loglin 在 n<10 退化时第三返回 None(拟合无意义) —— 此处 None 会被乘成 TypeError 崩整份报告;
    # 兜底为 1.0(平线: 数据不足不推趋势方向, 青线与现价齐平, 不崩不误导)。
    trend_end_price = last * (trend_end if trend_end is not None else 1.0)   # R71 修复：_loglin 第三返回为对数线性外推「比值」，须乘 last 还原为绝对价位；
                                           # 此前在 L1585(SVG青线)/L1617(图例)/L1686(fc字段) 直接当价格用 → 青线指向图表底部、图例显示"趋势外推 1"、数值 0.99 错乱
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
    elif sc in SC_BEAR:
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
    trend_agree = bool(_main_mid and abs(trend_end_price - _main_mid) / _main_mid < 0.06
                       and _r2 >= _R2_TH and _agree_dir)
    trend_weak = _r2 < _R2_TH
    # 趋势衰减提示（多窗口上行共识但主窗口加速度衰减）：背驰可能的量化信号，
    # 共振增益不额外授予（不直接改结构路径几何）
    _trend_decaying = bool(_agree_dir and _main_slope > 0 and _decay < -1e-5 and _r2 > 0.3)

    # 结构存续概率 _p_hold（锥模型）改在下方「经验分位扇形置信带」代码块之后计算（R59）：
    # 复用可见带自身参数（中心 _mean + 非对称经验尾部 std），与 P05/P50/P95 标签严格自洽，
    # 不再依赖 forward_vol 的 σ（含 regime_factor、μ=0）独立建模——旧口径与可见带分裂且注释失实。

    # ---- 概率（经验校准 + 结构锚 + 推演置信度 + 结论稳定性微调）----
    # 先按结构分类给基准概率，再叠加置信度偏离与稳定性；避免对“背离/背驰”重复惩罚导致全部贴地板。
    _base_p = {
        "多头延续": 0.58,
        "中枢震荡偏多": 0.50, "高位整理未破前高": 0.50,
        "背驰见底机会": 0.50,   # R159 补全：底背驰是看多信号，基准与"中枢震荡偏多"同级(高于中性0.45)
        "背驰见顶风险": 0.40, "中枢震荡偏空": 0.40,
        "反弹未回中枢": 0.40,   # R159 补全：弱势反弹属看空，基准与"背驰见顶风险"同级
        "弱势反弹": 0.36, "空头延续": 0.34,
    }
    # 经验校准（#1）：优先用本指数"最近且样本足够"的真实信号类型做锚，而非按情景猜一类买/卖。
    # 关键修正：锚点必须与第一/二类买卖方向一致——牛市情景只锚「买点」类信号、熊市只锚「卖点」类，
    # 否则会出现「多头延续的指数却用卖点胜率校准」的方向错配，既削弱准确率又产生自相矛盾的结论文字。
    _bull = sc in SC_BULL
    _bear = sc in SC_BEAR
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
        # R172: 方向命中率(真实方向与主路径方向一致比)用于上限诚实锚定, 避免高 p_main bin 过自信
        _dir_dir = (_dir["dir_main"] / _dir["dir_n"]) if (_dir.get("dir_n", 0.0) > 0.0) else None
        # R172: 经验锚统一用「方向命中率」(_dir_dir), 使 p_main 如实反映方向概率(此前用目标价命中率偏高)
        _anchor = _dir_dir if _dir_dir is not None else _dir_main
        p_main = _base * (1 - _w_dir) + _anchor * _w_dir
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
        # R138 校准修复：p_main 必须紧贴经验真值(方向命中率)对称夹逼, 仅留 ±0.06 的
        # 二阶修正余量（< 自校验告警阈值 0.08），杜绝结构微调把主路径概率系统性抬到经验命中率
        # 之上造成"偏乐观"过拟合。R172 起经验真值由「目标价命中率」升级为「方向命中率」(_anchor),
        # 使 p_main 如实反映方向概率(path_hit_html 自校验同步改比方向命中率, 三者一致无伪告警)。
        _cap = min(0.72, _anchor + 0.06)
        _floor = max(0.30, _anchor - 0.06)
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
    # ---- 经验分位扇形置信带（#预测精度·核心·R57 重写校准口径）----
    # 该函数最终返回 ECharts 交互图，其置信带即由下方 fc_data 的 f95/f75(经 _bandf 生成)绘制；
    # 故本段口径直接决定推演图「可见置信带」的统计准确性。三大修正（均经样本外黄金检验复测）：
    # ① 校准窗口由「全扩张历史」改为「近 3 年(≈732 交易日)」：全历史把 2015 股灾等早期崩溃收益塞进
    #    分位，使中线/离散度系统性偏悲观且 era-shift 失真；近窗口才反映当前波动 regime。
    # ② 中心由「中位」改为「窗口均值(期望)」：A 股 60 日收益含正漂移，中位低估中枢→方向判定仅 36%；
    #    均值中心使方向正确率升至约 54%(超出抛硬币)，偏置显著收敛。
    # ③ 离散度用真实经验分位(上下不对称，尊重右偏/肥尾)，且近窗口已含当前波动，故【不再叠加
    #    regime_factor】——此前近窗口+regime 双重放大使创业板带宽虚胖至 ±60%+；末端乘 κ(覆盖修正)
    #    补偿有限样本估计误差与非平稳，使样本外 P05–P95 覆盖率由约 85% 升至约 90%(名义水平)。
    #    （R56 仅修了「不对称」，但当时中心仍为全历史中位、且未做近窗口/均值中心/去双重放大，
    #     故实测方向率仅 36%、覆盖率约 85%；本次口径重写补齐这三处，使可见置信带真正准确。）
    _WIN = 3 * 244
    _wc = closes[-(_WIN + horizon):] if len(closes) >= _WIN + horizon else closes
    _rets = sorted(math.log(_wc[i + horizon] / _wc[i]) for i in range(len(_wc) - horizon))
    def _q(p):
        if not _rets:
            return 0.0
        k = (len(_rets) - 1) * p
        f0 = int(math.floor(k)); c0 = int(math.ceil(k))
        if f0 == c0:
            return _rets[f0]
        return _rets[f0] * (c0 - k) + _rets[c0] * (k - f0)
    _q50, _q05, _q25, _q75, _q95 = _q(0.5), _q(0.05), _q(0.25), _q(0.75), _q(0.95)
    _mean = (sum(_rets) / len(_rets)) if _rets else 0.0
    # 覆盖修正 κ（R168 重标定 + R171 regime 细化）：
    # R168 walk-forward 实测(同 audit_forecast_calibration, ANCHOR_STEP=15, 5指数): κ=1.8 时
    # 牛/震荡聚合覆盖 96~98%(过宽, 名义90%被高估, 带几乎无信息量); κ=1.4 时聚合精确命中~90%
    # (T+8 89.4%/T+30 91.1%)。但 R171 分 regime 重扫(动态 exec 真实 forecast_svg, 遍历 κ∈[1.4,2.3])
    # 发现: 单一 bull κ 无法同时让 H8/H30 都≈90%——bull H8 在 κ=1.4 仅 79.2%(N=53, 牛市短期急涨急跌
    # 使 30日带对 T+8 偏窄), 而 bull H30 在 κ=1.4 已 90.6%、κ=1.6 即过宽到 96.2%(R168 批评的"无信息量"带)。
    # 故 R171 取折中 bull=1.5: bull H8 79.2%→86.8%(接近名义90%、健康, N=53 足够), bull H30 92.5%(未过宽);
    # range 保持 1.4(聚合 H8/H30 均 93.2%、合理), bear 保持 2.3(下行富尾安全垫, N≈9 保守)。
    # κ 用于补偿近窗口经验分位在 5%/95% 极分位的采样误差+长 horizon 非平稳性; √t 缩放仍成立。
    # —— regime 自适应 κ(R108): 熊市 T+30 原 κ=1.8 漏覆盖 33.3%(LRuc=7.1, 拒绝99%), 故熊市放宽至 2.3
    # 给下行尾部补安全垫(单一来源 classify_regime 与关15/关13 切片口径一致)。熊市 N≈9 过小, 2.3 属
    # 保守改善而非精确标定, 需注明置信局限。
    # R223: 仅对牛市加「近端(f=0)加宽」斜坡修 T+8 漏覆盖(86.8%→≈90+); 其余 regime 维持 R171 原值(牛1.5/震1.4/熊2.3)不动。
    # 依据 walk-forward 实测(vs R171 单值κ): 降 range κ 会把创业板T+30 本就偏低覆盖(86.1%)进一步压到83%(假绿,
    # 跌破名义90%); 且震荡/熊市"过度收窄"属安全侧过宽非缺陷。故只精准修牛市近端这一真缺陷, 零回归。
    # _KAPPA = 远端(f=1) κ(=R171 原值, 牛T+30=92.5%过宽但安全); _KAPPA_NEAR = 近端(f=0)κ(仅牛加宽)。
    _KAPPA = {"bull": 1.50, "range": 1.40, "bear": 2.30}        # 远端(f=1)κ = R171 原值
    _KAPPA_NEAR = {"bull": 1.60, "range": 1.40, "bear": 2.30}   # 仅牛近端加宽(1.5→1.60)修 T+8 漏覆盖(86.8%→≈90+, 精准命中名义90不失控过宽)
    def _kappa_at(f, rg):
        _lo = _KAPPA_NEAR.get(rg, 1.40)
        _hi = _KAPPA.get(rg, 1.40)
        return _lo + (_hi - _lo) * f
    _rg = classify_regime(klines)
    _kappa = _kappa_at(1.0, _rg)   # 远端 κ(供 _sp 基准 / fc_data 透传)
    # R168: _rets 为空(窗口不足/收益恒定)时各分位=0→_sp=0→erf 分母 ZeroDivisionError;
    # 加 1e-9 下限兜底(退化输入下带退化为中线, 不崩)
    # R223: 离散度基础差(不含κ) + 随 f 的 κ 斜坡(_kappa_at) → 近端加宽/远端收窄, 均化覆盖
    _base_up = max(_q95 - _q50, 1e-9)
    _base_dn = max(_q50 - _q05, 1e-9)
    _base_up75 = max(_q75 - _q50, 1e-9)
    _base_dn25 = max(_q50 - _q25, 1e-9)
    _sp_up = _base_up * _kappa_at(1.0, _rg)     # 远端(f=1)上沿离散度(供 _p_hold / band_ext)
    _sp_dn = _base_dn * _kappa_at(1.0, _rg)     # P05 下沿离散度（左尾更宽，如实反映下行风险）
    _sp_up75 = _base_up75 * _kappa_at(1.0, _rg)
    _sp_dn25 = _base_dn25 * _kappa_at(1.0, _rg)
    # 结构存续概率（锥模型·R59 重写为与可见置信带严格自洽）：
    # 旧版用 forward_vol 的 σ（含 regime_factor 放大、μ=0 对数正态）独立建模，与 R57 之后可见带
    # （近窗口经验分位·均值中心·κ=1.8·无 regime）口径彻底分裂，且旧注释谎称"与置信锥同款σ、内部自洽"。
    # 现直接用可见带自身参数反算 P(期末价≥ZD)：中心取带中心 _mean（近窗口均值），尾部离散度取带的非对称
    # 经验分位距离(_sp_dn/_sp_up) 折算为等效正态 std(÷1.645)，使结果严格对齐带标签——
    # ZD=带P05↔95%、=P50↔50%、=P95↔5%（已基准校验）。消除"存续概率"与"置信锥"两张皮的口径矛盾。
    _r_star = math.log(zd / last) if (last > 0 and zd > 0) else 0.0
    if _r_star >= _mean:
        _p_hold = 0.5 * (1 + math.erf(1.645 * (_mean - _r_star) / (_sp_up * math.sqrt(2))))
    else:
        _p_hold = 0.5 * (1 + math.erf(1.645 * (_mean - _r_star) / (_sp_dn * math.sqrt(2))))
    _p_hold = max(0.01, min(0.99, _p_hold))
    def _medf(f):
        return last * math.exp(_mean * f)         # 中心=窗口均值(期望)，非中位
    def _bandf(f, z):
        # z>0 上沿(P95/P75)，z<0 下沿(P05/P25)；分别用对应侧经验分位离散度, 且随 f 施加 κ 斜坡(R223)
        kf = _kappa_at(f, _rg)
        if z > 0:
            _sp = (_base_up75 if abs(z) < 1.0 else _base_up) * kf
        else:
            _sp = (_base_dn25 if abs(z) < 1.0 else _base_dn) * kf
        return last * math.exp(_mean * f + (1.0 if z > 0 else -1.0) * _sp * math.sqrt(f))
    band_ext = []
    for _f in (0.25, 0.5, 0.75, 1.0):
        band_ext.append(_bandf(_f, 1.645))   # 经验上沿(P95)
        band_ext.append(_bandf(_f, -1.645))  # 经验下沿(P05)
    band_ext.append(_medf(1.0))
    all_prices = tail + [v for _, v in main_p + alt_p + risk_p] + [zg, zd] + band_ext + [trend_end_price] \
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

    _hist_k = klines[-len(tail):]
    _hd = [k["date"] for k in _hist_k]
    _last_dt = datetime.strptime(klines[-1]["date"], "%Y-%m-%d")

    def _fut(kk):
        """从最后交易日的下一交易日开始推算 kk 个交易日对应的日历日期（跳过周末与法定节假日，
        保留调休补班日）。kk=0 对应 T+1（明天），避免投影区第一天与历史最后一天重复导致
        x 轴出现两个相同日期，并消除历史线与推演线在同一 x 位置被 vline 误认为"缺口"的视觉问题。"""
        dt = _last_dt
        # 先推进到下一交易日（未来推演不包含当前日）
        dt += timedelta(days=1)
        while not _is_trading_day(dt):
            dt += timedelta(days=1)
        while kk > 0:
            dt += timedelta(days=1)
            if _is_trading_day(dt):
                kk -= 1
        return dt.strftime("%Y-%m-%d")
    # R476: 路径终点来源**透明化**（承 R475 核查）。实测三条路径终点全部 = 中枢点位 × 固定系数
    # （主 mid×0.99 / 次 zd×0.98 / 风险 zd×0.92），属**启发式情景外推，不是缠论推导**；而缠论对
    # "破位后的量度"有自己的标准算法（中枢高度外推：ZD − (ZG − ZD)）。两者不一致时必须让用户
    # 看得见 —— 否则很容易误以为「风险 3466」是缠论算出来的（实测它比缠论量度更悲观 184 点）。
    # 系数按**实测比例**反算展示（而非写死 0.92 等字面量），这样任何分支改动都会自动如实反映。
    _zsh = zd - (zg - zd)                      # 缠论标准破位量度终点
    _src_note = (
        f'<div style="margin-top:6px;font-size:12px;line-height:1.75;color:#94a3b8">'
        f'⚠ <b>路径终点来源</b>：三条终点 = 中枢点位 × 固定系数（主 <b>{main_p[-1][1] / mid:.2f}</b>×中枢中值 '
        f'/ 次 <b>{alt_p[-1][1] / zd:.2f}</b>×ZD / 风险 <b>{risk_p[-1][1] / zd:.2f}</b>×ZD），'
        f'属<b>启发式情景外推、非缠论推导</b>；缠论标准破位量度（中枢高度 ZG−ZD = {zg - zd:.0f} 自 ZD 外推）'
        f'为 <b>{_zsh:.0f}</b>，与「风险」位相差 <b>{abs(risk_p[-1][1] - _zsh):.0f}</b> 点。'
        f'中枢 ZG/ZD 本身由 chanlun.py 的三笔重叠定义给出（纯缠论）；'
        f'置信锥与主/次/风险概率同样来自统计与启发式校准，亦非缠论推导。'
        f'</div>'
    )
    # 图例改为图表下方的 HTML 图例条（不再压住推演路径与时间轴）
    legend_html = (
        f'<div class="fc-legend">'
        f'<span><i class="ln ln-dash" style="background:{RED}"></i>结构演绎主路径 ≈ {p_main * 100:.0f}%（目标 {main_p[-1][1]:.0f}，{((main_p[-1][1]/last-1)*100):+.1f}%）</span>'
        f'<span><i class="ln" style="background:{RED}"></i>统计中位路径（均值期望 {_medf(1.0):.0f}，{((_medf(1.0)/last-1)*100):+.1f}%）</span>'
        f'<span><i class="ln ln-dash" style="background:#94a3b8"></i>次路径：中枢内震荡 ≈ {p_alt * 100:.0f}%</span>'
        f'<span><i class="ln ln-dot" style="background:{GREEN}"></i>风险路径：跌破ZD转空 ≈ {p_risk * 100:.0f}%</span>'
        f'<span><i class="ln ln-band"></i>置信锥 经验分位 P05–P95 / P25–P75（真实分布·非对称）</span>'
        f'<span><i class="ln ln-trend"></i>趋势外推 {trend_end_price:.0f}（R²={_r2:.2f}{"，弱拟合" if trend_weak else ""}）</span>'
        f'</div>'
        f'<div class="fc-targets">结构演绎目标(主路径终点) ≈ <b>{main_p[-1][1]:.0f}</b>（<b style="color:{RED}">{((main_p[-1][1]/last-1)*100):+.1f}%</b>） · '
        f'均值期望终点 ≈ <b>{_medf(1.0):.0f}</b>（{((_medf(1.0)/last-1)*100):+.1f}%） · '
        f'风险止损位(风险路径终点·向下量度) ≈ <b>{risk_p[-1][1]:.0f}</b>（{((risk_p[-1][1]/last-1)*100):+.1f}%） · '
        f'趋势外推位 ≈ <b>{trend_end_price:.0f}</b> · '
        f'主路径失效位(有效跌破ZD) ≈ <b>{zd:.0f}</b> · '
        f'结构存续概率(锥) ≈ <b>{_p_hold*100:.0f}%</b></div>'
        + _src_note
    )
    note = (f"主路径失效位：现价有效跌破 ZD {zd:.0f}（收盘确认）→ 主路径失效、风险路径概率上升；风险路径确认需同时满足「跌破 ZD + 周线笔转向下」。\n"
             f"上方「风险止损位」即该风险路径的<b>向下量度终点</b>（由 ZD 派生的结构参考位）——需要「往下还有多少空间」时读这一栏；确认条件未满足前它只是条件应对的边界，不是对底部的预测。\n"
             f"红色阴影为基于<b>真实历史 {horizon} 日对数收益分布</b>推演的<b>经验分位扇形置信带</b>（P05–P95 外层 / P25–P75 内层）：与对称 ±σ 带不同，它直接由本指数历史兑现统计得出、天然包含 A 股肥尾与涨跌不对称，"
        f"故<b>上下带非对称</b>——按真实历史经验分位分别给上下沿定宽（替代对称 ±1.645σ 等宽假设）：本指数近 3 年 {horizon} 日对数收益呈右偏，上行离散（P95–P50）约为下行的 1.5–2.5 倍，故<b>上行带更宽</b>，如实容纳单边急涨的肥尾。R57+R58 口径：① 校准窗口由全历史改为<b>近 3 年</b>，剔除 2015 股灾等早期崩溃收益导致的 era-shift 偏悲观；② 中心由中位改为<b>窗口均值（期望）</b>，A 股含正漂移、中位低估中枢，使方向判定正确率由约 36% 升至约 54%；③ 近窗口已含当前波动，<b>不再叠加 regime 因子</b>（此前双重放大使创业板带宽虚胖至 ±60%+）。覆盖修正系数 κ（R168 重标定 + R171 regime 细化）：walk-forward 实测(同回测引擎, 5指数 N=180/horizon)表明 κ=1.8 时牛/震荡实测覆盖达 96~98%(过宽、名义90%被高估、带几乎无信息量)，故 R168 将 κ 由 1.8 降至 1.4，聚合精确命中名义 90%(T+8 89.4%、T+30 91.1%)。但 R171 分 regime 重扫(动态 exec 真实 forecast_svg, 遍历 κ∈[1.4,2.3])发现：单一 bull κ 无法同时让 H8/H30 都≈90%——bull H8 在 κ=1.4 仅 79.2%(N=53, 牛市短期急涨急跌使 30日带对 T+8 偏窄)，而 bull H30 在 κ=1.4 已 90.6%、κ=1.6 即过宽到 96.2%(无信息量带)。故 R171 取折中 bull=1.5：bull H8 79.2%→86.8%(接近名义90%、健康)、bull H30 92.5%(未过宽)；range 维持 1.4(聚合 H8/H30 均 93.2%)、bear 维持 2.3(下行富尾安全垫)。早期「κ=1.4→86%/κ=1.8→90.2%」系 R57/R58 改窗口与改中心前的旧口径、已 stale。√t 缩放假设仍成立(覆盖率随 horizon 分桶均匀)。<b>κ 按市场环境自适应(R108)</b>：牛 <b>{_KAPPA_NEAR['bull']:.2f}/{_KAPPA['bull']:.2f}</b>(近端/远端) / 震荡 <b>{_KAPPA['range']:.2f}</b> / 熊市 <b>{_KAPPA['bear']:.2f}</b>(关15 实证熊市 T+30 原 κ=1.8 漏覆盖 33.3%、LRuc=7.1 拒绝 99%，放宽后给下行富尾补安全垫——A 股熊市下跌更急更肥尾，近 3 年经验分位低估了极端下行)。R223：仅牛市加「近端(f=0)加宽」斜坡(1.5→1.60)修 T+8 漏覆盖 86.8%→≈90+(walk-forward 实测复核见 backtest_diff)；震荡/熊市维持 R171 原值不动——过度收窄属安全侧过宽非缺陷，且盲目降 range κ 会把创业板 T+30 本已偏低覆盖(86.1%)进一步压低(假绿)，故仅精准修牛市近端这一真缺陷。中线路径为「实测漂移期望（均值）」而非手工情景路径，置信带中线统计诚实；带宽随时间按 √t 扩张（随机游走特性），近月不确定性即已显著，并非线性外推的针状。<b>代价</b>：创业板等超高波动指数 60 日 P05–P95 带宽达 ~119%，这是其真实波动的诚实反映，而非缺陷。\n"
             f"本图为目的（分类框架）而非点位预测：缠论给出的是「不跌破 ZD 则结构延续、跌破则转弱」的条件应对，不是对具体价位的预测。\n"
             f"趋势外推（青色虚线，对最近 {min(horizon,90)} 日收盘做对数线性回归外推 {horizon} 日）是与结构路径相互独立的验证方法，"
             + (f"但其拟合优度极低（R²={_r2:.2f}），该独立验证参考性很弱、近乎噪声，不宜据此增减仓位；"
                if trend_weak
                else ("其终点与主路径吻合（误差<6%）且拟合较稳（R²={_r2:.2f}），两法指向同一区间，预测可信度更高；"
                      if trend_agree
                      else f"其终点 ≈ {trend_end_price:.0f}，与主路径中点存在偏差（R²={_r2:.2f}），提示两种视角对后市节奏判断不完全一致，宜结合仓位管理；"))
             + f"若趋势外推也跌漏 ZD，则风险路径概率进一步上升。\n"
             f"主图叠加的斐波那契回调位（F38/F50/F62）与本路径上行目标、ZD 支撑相互印证：若回踩至 F61.8 附近获支撑，反弹结构更可靠；若直接跌漏 ZD，则风险路径概率上升。\n"
             f"时间轴：左侧历史区为真实交易日（MM-DD）；右侧投影区从最后交易日的下一交易日（T+1）开始，按「往后推算相应交易日、跳过周末及法定节假日（A股日历）」得到，仅供参照。")
    note += (f"\n结构存续概率（锥模型·R59 与置信带同源）：由可见置信带自身参数（近窗口均值中心 + 非对称经验尾部离散度）直接反算「期末价 ≥ ZD {zd:.0f}」的概率 ≈ {_p_hold*100:.0f}%，与置信带 P05/P50/P95 标签严格自洽（ZD 落在带内对应分位即对应概率）。该概率与「主/次/风险」情景概率互为参照：情景概率衡量方向性演绎（续涨/震荡/跌），存续概率衡量「结构是否守住失效位」；两者现共用同一套置信带波动模型，口径一致、不再分裂。现价远高于 ZD 时存续概率天然偏高，不应与情景概率混为一谈。")
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
    # ---- 结构主路径(目标情景) vs 统计中位(无偏期望) 偏离诚实提示（R62·预测准确性）----
    # main_p 是缠论结构演绎的「方向性目标」，med(_medf) 是近3年对数收益均值推演的「无偏期望」，
    # 二者天然不同。当偏离过大(>8%)时明确提示，避免用户把"目标情景"误读为"概率中点"，
    # 也暴露"结构判断相对纯统计更乐观/悲观"这一真实不确定性，使预测更诚实。
    _dev = (_interp(main_p, 1.0) - _medf(1.0)) / _medf(1.0)
    # R490: 判据补充 —— 旧口径只看「两者相差 >8%」，会漏掉最该提醒的一类：主路径与统计期望
    # 「方向相反」。实测(09-18 上证)主路径 -3.2% vs 均值期望 +0.9%：方向相反，但幅度只差 4.0%
    # ⇒ 提示不显示，用户看到红线朝下、当日却大涨，页面对此零解释。故补「方向冲突」判据：
    # 两侧相对现价的方向必须相反，且各自幅度须超过噪声门限(主 2% / 期望 0.5%)，避免贴平时的伪冲突。
    _r_main = (_interp(main_p, 1.0) - last) / last
    _r_med = (_medf(1.0) - last) / last
    _dir_conflict = bool(_r_main * _r_med < 0
                         and abs(_r_main) > 0.02 and abs(_r_med) > 0.005)
    if _dir_conflict:
        note += (f"\n⚠ <b>路径方向提示</b>：结构主路径(目标情景)终点 {_interp(main_p, 1.0):.0f}"
                 f"（{_r_main*100:+.1f}%）与「统计中位(无偏期望)」终点 {_medf(1.0):.0f}"
                 f"（{_r_med*100:+.1f}%）<b>方向相反</b>。主路径由缠论结构情景"
                 f"（当前为「{sc}」）演绎得出，统计中位由近 3 年真实收益分布外推得出，二者口径独立、"
                 f"本就可能背离 ⇒ 此时主路径应读作「<b>若该结构情景成立会走到哪</b>」的条件推演，"
                 f"而非对后市的概率中点预测；实际落点更可能靠近统计中位(期望)。"
                 f"判读时请结合下方「主路径失效位」与该情景的概率一并看，结论宜保守。")
    elif abs(_dev) > 0.08:
        _d = "偏高" if _dev > 0 else "偏低"
        note += (f"\n⚠ <b>路径偏离提示</b>：结构主路径(目标情景)终点较「统计中位(无偏期望)」{_d} "
                 f"{abs(_dev)*100:.1f}%，反映当前缠论结构判断相对纯历史统计更{'乐观' if _dev > 0 else '悲观'}；"
                 f"实际落点更可能靠近统计中位(期望)，主路径应视为「方向性目标」而非「概率中点」，结论宜保守看待。")
    # ---- R224: p_main 样本外校准诚实标注（仅展示层, 不动 p_main 数值/带/p_hold, 关13 安全）----
    # 依据 audit_probability_calibration (walk-forward, 5指数, 锚点每15交易日) + prob_cal_holdout 留出法验证:
    #   T+8 实际方向命中≈43%(Brier≈0.25 近随机); T+30 低 p_main 箱实际后市多涨>60%(逆向α)。
    # 可靠性 regime/时间依赖不稳定(留出法否决静态重映射), 此处仅做方向性诚实标注, 绝不硬编码精确校准百分比。
    _p_cv = []
    _p_cv.append("主路径概率 p_main 由历史命中率校准，样本外方向准确性有限：walk-forward 回测显示 T+8 短周期方向命中仅≈43%（近随机），较长周期亦非高置信；p_main 宜作概率参考而非方向定论")
    if p_main < 0.40:
        _p_cv.append("当前 p_main 处于低位（模型偏空），历史回测显示该区间实际后市多涨（逆向信号），可结合「别人恐惧我贪婪」的逆向思维看待，而非顺势看空")
    _p_cv.append("置信带覆盖率诚实提示：walk-forward 回测显示，部分高波动指数（如创业板）T+30 置信带覆盖率约 86%，略低于 90% 名义目标——区间端点仅供参考，不构成精确点位预测")
    if _p_cv:
        note += "\n" + "；".join("⚠ " + _s for _s in _p_cv) + "。"
    # ---- 悬浮交互数据：历史区真实收盘价 + 投影区密集采样（供 JS initForecast）----
    hist = [[_hd[i], round(tail[i], 2)] for i in range(len(tail))]
    proj = []
    # R122 + R341: 采样点数与 horizon 交易日数严格一一对应。R341 修正日期 off-by-one——
    # 原实现 fi=0..horizon 共 horizon+1 个采样点(含 f=0 的"今日价"重复点), 日期取 _fut(fi)=
    # 第 fi+1 个未来交易日, 使 horizon=60 的置信带末端(f=1 = 第 60 个未来交易日 T+60)被标成
    # T+61、x 轴右端超前 1 个交易日(实证: T0=2026-09-04 时 _fut(60)=12-08 而真 T+60=12-07)。
    # 现采样 fi=1..horizon(60 点, 值=第 fi 个交易日进度 fi/horizon), 日期=_fut(fi-1)=第 fi 个
    # 未来交易日; f=0 的"今日"点由 forecast_echart 的 bridge(各路径 f=0 恒等值 last/0)承担
    # (与历史末点同 x 位置无缝衔接), 既不产生重复日期、也不向未来多占一天。
    for fi in range(1, horizon + 1):
        f = fi / horizon if horizon > 0 else 0.0
        med = _interp(main_p, f)
        alt = _interp(alt_p, f)
        risk = _interp(risk_p, f)
        dt = _fut(fi - 1)
        # 经验分位扇形（围绕实测漂移中位路径 medf）：P05/P95 外层、P25/P75 内层
        mdf = _medf(f)
        u95 = _bandf(f, 1.645); l95 = _bandf(f, -1.645)
        u75 = _bandf(f, 0.674); l75 = _bandf(f, -0.674)
        trend = round(last * math.exp(_main_slope * fi), 2)
        proj.append({"f": round(f, 3), "tplus": fi, "date": dt,
                     "main": round(med, 2), "alt": round(alt, 2), "risk": round(risk, 2),
                     "trend": trend, "med": round(mdf, 2),
                     "f95l": round(l95, 2), "f95h": round(u95 - l95, 2),
                     "f75l": round(l75, 2), "f75h": round(u75 - l75, 2)})
    fc_data = {"hist": hist, "proj": proj, "p_main": p_main, "p_alt": p_alt, "p_risk": p_risk,
               "closes_all": closes, "n_all": n,  # R119b: 透传全量历史收盘价，供推演图均线基于全量铺垫(左对齐/年线不缺失)
               "regime": _rg, "kappa": round(_kappa, 3),
               "p_hold": round(_p_hold, 3),
               "path_dev": round(_dev, 4),
               "path_dir_conflict": _dir_conflict,   # R490: 与主路径/期望「方向相反」判据同源，供卡片标签复用
               "zd": round(zd, 2), "zg": round(zg, 2), "last": round(last, 2),
               "trend": round(trend_end_price, 2), "trend_agree": trend_agree, "trend_r2": round(_r2, 3),
               "sigma": round(sigma, 4), "horizon": horizon, "lo": round(lo, 4), "span": round(span, 4),
               "med_term": round(_medf(1.0), 2), "q50": round(_q50, 4), "q_sd": round((_sp_up + _sp_dn) / 2, 4),
               "hist_dates": [_hd[i] for i in range(len(tail))],
               "gap_refs": [{"type": g["type"], "top": round(g["top"], 2), "bottom": round(g["bottom"], 2), "date": g["date"]} for g in _gap_refs],
               # R210: 真联动——透传市场情绪预测序列(fear/greed 指数, 0-100), 由 forecast_echart 叠加第二条 Y 轴。
               # 缺失/非 dict 时置 None, 下游优雅降级(仅不画情绪线, 不影响价格推演)。
               "sent_fc": sent_fc if isinstance(sent_fc, dict) else None}
    return forecast_echart(sym, fc_data), note, (p_main, p_alt, p_risk), legend_html, fc_data


def forecast_echart(sym, fc_data):
    """用 ECharts 重绘缠论未来走势推演图（路径+置信锥+标注），对标主图细腻度、放大矢量清晰。"""
    hist = fc_data["hist"]
    proj = [dict(p) for p in fc_data["proj"]]
    zg, zd, last = fc_data["zg"], fc_data["zd"], fc_data["last"]
    p_main, p_alt, p_risk = fc_data["p_main"], fc_data["p_alt"], fc_data["p_risk"]
    gaps = fc_data.get("gap_refs", [])
    x_hist = [h[0] for h in hist]
    x_proj = [p["date"] for p in proj]
    xcats = x_hist + x_proj
    x_full = xcats
    # R210: 市场情绪预测(恐惧贪婪指数 0-100)与价格推演图真联动——按日期对齐到 xcats:
    # 历史段(无情绪历史序列)填 None, 未来段填对应中位/p25/p75。最低点用于标注"情绪已先行见底"。
    sent_med = [None] * len(xcats)
    sent_lo = [None] * len(xcats)
    sent_hi = [None] * len(xcats)
    sent_min = None
    _sf = fc_data.get("sent_fc")
    if _sf and _sf.get("dates") and _sf.get("median"):
        _sd = _sf["dates"]; _sm = _sf["median"]
        # R211: 防御 dates/median 长度不一致(数据源异常) -> 截断/补齐对齐, 避免 _sm[_j] 越界 IndexError
        if len(_sm) > len(_sd):
            _sm = _sm[:len(_sd)]
        elif len(_sm) < len(_sd):
            _sm = list(_sm) + [None] * (len(_sd) - len(_sm))
        _sp25 = _sf.get("p25") or []
        _sp75 = _sf.get("p75") or []
        # R370: R211 只对 median(_sm) 做了 dates 等长截断/补齐, p25/p75 漏网 —— 数据源异常
        # (p25/p75 短于 dates) 时 _sidx 索引可达 len(dates)-1, 下方 _sp25[_j] 越界 IndexError,
        # 该指数 forecast_svg 整段崩 → R173 降级占位(整指数段消失, 浪费而非崩报告)。同族补全:
        # 用不可变重建(切片/list+None), 不 mutate _sf 原列表(浅拷贝只护顶层, 防污染 sent_full)。
        if len(_sp25) > len(_sd):
            _sp25 = _sp25[:len(_sd)]
        elif len(_sp25) < len(_sd):
            _sp25 = list(_sp25) + [None] * (len(_sd) - len(_sp25))
        if len(_sp75) > len(_sd):
            _sp75 = _sp75[:len(_sd)]
        elif len(_sp75) < len(_sd):
            _sp75 = list(_sp75) + [None] * (len(_sd) - len(_sp75))
        _sidx = {_sd[i]: i for i in range(len(_sd))}
        for _si, _sx in enumerate(xcats):
            _j = _sidx.get(_sx)
            if _j is None:
                continue
            if _sm[_j] is not None:
                sent_med[_si] = round(float(_sm[_j]), 1)
            if _sp25[_j] is not None and _sp75[_j] is not None:
                sent_lo[_si] = round(float(_sp25[_j]), 1)
                sent_hi[_si] = round(float(_sp75[_j]) - float(_sp25[_j]), 1)
    n_hist = len(hist)
    # R215: 对未来段中非交易日/forecast未覆盖日期做前向+后向填充，避免情绪曲线断档。
    # 根因: xcats 未来段含非交易日(如 2026-09-20 周日)或 forecast 未覆盖日, 情绪 forecast dates 只含交易日,
    # 对齐后 sentMed/sentLo/sentHi 这些位置为 None; ECharts connectNulls:false 导致断线。
    def _fill_sent(arr):
        last = None
        for i in range(n_hist, len(arr)):
            if arr[i] is not None:
                last = arr[i]
            elif last is not None:
                arr[i] = last
        first = None
        for i in range(len(arr) - 1, n_hist - 1, -1):
            if arr[i] is not None:
                first = arr[i]
            elif first is not None:
                arr[i] = first
    _fill_sent(sent_med); _fill_sent(sent_lo); _fill_sent(sent_hi)
    # R216: 修复"情绪见底"标注点坐标不匹配——旧逻辑从情绪 forecast 自身日期 _sd[_mi] 取最低点,
    # 该日期未必在推演图 xcats 中(情绪模型投影窗口与缠论投影窗口可能不同), 导致 markPoint coord
    # 与 category 轴不匹配而画不出来(与 R215 断档同类边界坑)。改为从已对齐(并经 R215 填充)的
    # sent_med 取最低点, date 直接取自 xcats, 保证坐标一定落在轴上、pin 必渲染。
    _fut = [(i, sent_med[i]) for i in range(n_hist, len(sent_med)) if sent_med[i] is not None]
    if _fut:
        _mi2 = min(_fut, key=lambda t: t[1])[0]
        sent_min = {"date": xcats[_mi2], "val": round(float(sent_med[_mi2]), 1)}
    # R347: 情绪区阈值透传到 JS tooltip——按 fear(≤buy_th)/greed(≥sell_th) 给语境文案,
    # 与情绪板块 zone 判定同源(sent_fc 由 main 浅拷贝携带, forecast 子 blob 自身无此键)。
    _sf_bt = float(_sf.get("buy_th", 20.0)) if isinstance(_sf, dict) else 20.0
    _sf_st = float(_sf.get("sell_th", 85.0)) if isinstance(_sf, dict) else 85.0
    n_proj = len(proj)
    hist_s = [h[1] for h in hist] + [None] * n_proj
    # R123/R125: 历史线末点(idx=n_hist-1=今日)与未来路径衔接——旭总要求"预测的几条线都要
    # 紧贴今日虚线、不要有空档"：故所有预测线(主路径med + 结构演绎/次/风险/趋势外推 + 置信锥)首点统一前移到
    # idx=n_hist-1(今日)，与历史末点同 x 位置无缝衔接。R341: proj 自 T+1 起采样(60 点, 无 f=0 点),
    # bridge 值取各路径在 f=0 的解析恒等值——主/中位/次/风险/趋势/置信带下沿起点均=现价 last、
    # 带高 f95h/f75h 取 0(零宽)；(值与原 proj[0] 相同, 仅不再重复占用一个未来日期槽)。
    # "不往左侧冒头"= bridge 点恰落在今日(idx119=历史末点同位置)，不延伸到历史区(idx<119)；红线自今日向右
    # 展开，既紧贴今日虚线、又不会往左越过今日线伸进历史段。
    main_s = [None] * (n_hist - 1) + [last] + [p["main"] for p in proj]
    med_s = [None] * (n_hist - 1) + [last] + [p["med"] for p in proj]
    alt_s = [None] * (n_hist - 1) + [last] + [p["alt"] for p in proj]
    risk_s = [None] * (n_hist - 1) + [last] + [p["risk"] for p in proj]
    trend_s = [None] * (n_hist - 1) + [last] + [p["trend"] for p in proj]
    f95l = [None] * (n_hist - 1) + [last] + [p["f95l"] for p in proj]
    f95h = [None] * (n_hist - 1) + [0] + [round(p["f95h"], 2) for p in proj]
    f75l = [None] * (n_hist - 1) + [last] + [p["f75l"] for p in proj]
    f75h = [None] * (n_hist - 1) + [0] + [round(p["f75h"], 2) for p in proj]
    lo = fc_data["lo"]
    ymax = round(lo + fc_data["span"], 2)
    tail_prices = [h[1] for h in hist]
    # 推演图均线（R119/R120b）：基于全量历史真实收盘价 + 统计中位路径 拼接算 MA20/60/120/年线(250)，
    # 切片对齐推演图可见窗口——hist 段由全量历史(1363根)铺垫，长周期均线起点左对齐、年线不缺失；
    # 仅历史段(hist)绘制均线，未来预测段置 None 不画（用户要求预测部分不显示均线）。
    _closes_all = fc_data.get("closes_all") or [h[1] for h in hist]
    _n_all = fc_data.get("n_all") or len(hist)
    _offset = max(0, _n_all - len(hist))            # 推演图 hist 段在全量中的位置
    _ma_base = list(_closes_all) + [p["med"] for p in proj]
    _L = len(hist) + len(proj)
    def _sma(vals, m):
        return [None if i < m - 1 else round(sum(vals[i - m + 1:i + 1]) / m, 2) for i in range(len(vals))]
    ma20_s = _sma(_ma_base, 20)[_offset:_offset + _L]
    ma60_s = _sma(_ma_base, 60)[_offset:_offset + _L]
    ma120_s = _sma(_ma_base, 120)[_offset:_offset + _L]
    ma250_s = _sma(_ma_base, 250)[_offset:_offset + _L]
    # 仅历史段(hist, 索引 < n_hist)保留均线值，未来预测段置 None 不绘制（R120b）
    for _arr in (ma20_s, ma60_s, ma120_s, ma250_s):
        for _i in range(n_hist, len(_arr)):
            _arr[_i] = None
    # R135 修复：置信锥(P05–P95/P25–P75)的上下沿必须纳入 y 轴范围，否则锥顶/锥底会被
    # yAxis 的 min/max 裁切成平头。band 上沿 = f95l + f95h（堆叠后的真实顶部），下沿 = f95l。
    # 此前 core_prices 只含结构路径(main/alt/risk/trend/med)与均线，不含置信锥，导致推演图
    # 置信锥被 y 轴上沿截断（如 sh000001 锥顶 4765 远超结构路径高点 4263，被切成平头）。
    band_top = []
    band_bot = []
    for _i in range(len(f95l)):
        _lo = f95l[_i]
        if _lo is None:
            continue
        band_bot.append(_lo)
        _hi = f95h[_i]
        if _hi is not None:
            band_top.append(_lo + _hi)
    core_prices = (tail_prices + [last, zg, zd]
                   + [p["main"] for p in proj] + [p["alt"] for p in proj]
                   + [p["risk"] for p in proj] + [p["trend"] for p in proj] + [p["med"] for p in proj]
                   + band_top + band_bot)
    # R120c: 历史段 MA 极值纳入 yAxis 范围——MA 基于全量窗口(如 MA250 含比 tail 更早的低价)，
    # 否则长周期均线左端会低于 tail 极值被 yAxis 底部裁切（断头）。仅取历史段(索引<n_hist)，未来段已置 None。
    for _a in (ma20_s, ma60_s, ma120_s, ma250_s):
        _hv = [_v for _v in _a[:n_hist] if _v is not None]
        if _hv:
            core_prices.append(min(_hv))
            core_prices.append(max(_hv))
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
    # R133: 强化今日垂直虚线——加粗置顶，让 4 条预测虚线的起点"贴"在今日线上一目了然。
    # markLine 默认绘制在该 series 所有数据之上；参考 series 位于 series 列表末端，故 vline 在所有预测线之上。
    vline = [{"xAxis": x_hist[-1], "lineStyle": {"type": "dashed", "color": "#334155", "width": 2.0},
              "label": {"show": False}}]
    # R135 修复：端点"主/次/风险"须与 tooltip 定义一致——"主"对应结构演绎路径(structural main)，
    # 而非统计中位路径(med)。此前 _em 取 proj[-1]["med"]，使端点"主"标的是统计中位线、与 tooltip
    # "主路径=结构演绎+p_main"自相矛盾。现改为 proj[-1]["main"]，三者即结构主/次/风险路径终点。
    _em, _ea, _er = proj[-1]["main"], proj[-1]["alt"], proj[-1]["risk"]
    # R162: 端点标注加涨跌幅(%)——此前仅纯数字"主 4212", 用户看不出方向幅度、易被统计中位线(平)带偏。
    # 现主路径标注"主目标 4212 (+6%)", 醒目呈现方向与空间; 次/风险同样加涨跌幅保持一致。
    # R354: 端点标签加 align:'right' —— 锚点 xcats[-1] 恰在绘图区右缘(grid right:88), 默认 anchor=start
    # 文本从锚点向右铺 ~100px 侵入右侧情绪轴(0-100, 刻度 x≈1020)刻度带, 被 verify_overlap 门禁报
    # "主目标/风险 ✕ 0/80" 真重叠(SSR 实测 #3/#5/#7/#11, R210 加右轴后回归、R100 hideOverlap 只修 x 轴
    # 日期标签覆盖不到)。align:'right' 令 anchor=end 文本左铺 [912,1012] 全部落绘图区内(实验实证)。
    end_points = [
        {"coord": [xcats[-1], round(_em, 2)], "value": f"主目标 {_em:.0f} ({(_em/last-1)*100:+.0f}%)", "itemStyle": {"color": RED}, "symbol": "pin", "symbolSize": 26,
         "label": {"show": True, "position": "top", "align": "right", "color": RED, "fontSize": 12, "fontWeight": "bold"}},
        {"coord": [xcats[-1], round(_ea, 2)], "value": f"次 {_ea:.0f} ({(_ea/last-1)*100:+.0f}%)", "itemStyle": {"color": "#94a3b8"}, "symbol": "circle", "symbolSize": 6,
         "label": {"show": True, "position": "bottom", "align": "right", "color": "#94a3b8", "fontSize": 11, "fontWeight": "bold"}},
        {"coord": [xcats[-1], round(_er, 2)], "value": f"风险 {_er:.0f} ({(_er/last-1)*100:+.0f}%)", "itemStyle": {"color": GREEN}, "symbol": "circle", "symbolSize": 6,
         "label": {"show": True, "position": "bottom", "align": "right", "color": GREEN, "fontSize": 11, "fontWeight": "bold"}},
    ]
    _year_label = x_hist[0][:4] if x_hist and len(x_hist[0]) >= 4 else ""
    f_kl = [f"{{year|{_year_label}年}}", f"{{zg|ZG {zg_v}}}", f"{{zd|ZD {zd_v}}}", f"{{last|现价 {last_v}}}"]
    for _key, _lab in gap_chips:
        f_kl.append(f"{{{_key}|{_lab}}}")
    key_levels_text = "  ".join(f_kl)

    # 确定性去重叠：推演端点（主/次/风险）标签
    # R476: 三个几何量此前全是错的，用 SSR 真渲染 + convertToPixel() 实测校正（误差 0.0000）：
    #   ① plot_w: 传 940（=1100-96-64）而真实绘图区宽 = 1100-96-**88** = **916**（grid.right=88）
    #   ② plot_h: 传 440-44-74=322 而真实 = 440 - **64** - **80** = **296**（grid top/bottom 实测）
    #   ③ grid_t: 传 44 而真实 = **64**
    #   ④ 且预测图 xAxis boundaryGap=**False** ⇒ 分母是 n-1（不是 n）：实测 bar = 916/149 = 6.147651
    # 叠加后右端标签 x 偏差达 **17.73px** ⇒ 跨价格/跨位置的去重叠判据系统性失准。
    dedup_mark_labels(end_points, len(xcats), core_lo, core_hi, 1100 - 96 - 88, 440 - 64 - 80, 96, 64,
                      {xcats[-1]: len(xcats) - 1}, bgap=False)

    fdata = {
        "keyLevelsText": key_levels_text,
        "xcats": xcats, "xfull": x_full, "n_hist": n_hist, "proj": proj,
        "hist": hist_s, "main": main_s, "alt": alt_s, "risk": risk_s, "trend": trend_s,
        "ma20": ma20_s, "ma60": ma60_s, "ma120": ma120_s, "ma250": ma250_s,
        "lo": round(lo, 2), "ymax": ymax, "ymin_core": round(core_lo, 2), "ymax_core": round(core_hi, 2),
        "hlines": hlines, "vline": vline, "endPoints": end_points,
        "med": med_s, "f95l": f95l, "f95h": f95h, "f75l": f75l, "f75h": f75h,
        "p_main": p_main, "p_alt": p_alt, "p_risk": p_risk, "proj_raw": proj,
        # R210: 真联动情绪数据——按 xcats 日期对齐的中位/分位序列 + 最低点(供 JS 叠加第二条 Y 轴)
        "sentMed": sent_med, "sentLo": sent_lo, "sentHi": sent_hi, "sentMin": sent_min,
        "sent_bt": _sf_bt, "sent_st": _sf_st,
    }
    cid = f"echart-forecast-{sym}"
    return f'''<div class="echart-toolbar">🔍 滚轮/拖拽缩放 · 拖动底部滑块平移 · 悬停看推演路径/置信锥/趋势</div>
<div id="{cid}" class="echart-main" style="width:100%;height:440px;"></div>
<script>
(function(){{
  var D = {json.dumps(fdata, ensure_ascii=False)};
  var chart = echarts.init(document.getElementById('{cid}'));
  (window.__charts = window.__charts || []).push(chart);
  // 推演图时间轴：与主图一致——interval 固定 0 + autoHide 关闭 + formatter 控制显示，杜绝 ECharts 自动抽稀造成的日期错配；并按 dataZoom 可见天数动态切日/周/月/季密度。
  var __fcVisible = D.xcats.length;
  function __fcMonthStart(idx) {{ var d = D.xcats[idx]; return d && d.slice(8,10) === '01'; }}
  function __fcQuarterStart(idx) {{
    var d = D.xcats[idx];
    if (!d || d.slice(8,10) !== '01') return false;
    var m = parseInt(d.slice(5,7),10);
    return m === 1 || m === 4 || m === 7 || m === 10;
  }}
  function __fcMonday(idx) {{
    var parts = D.xcats[idx].split('-');
    return new Date(parseInt(parts[0],10), parseInt(parts[1],10)-1, parseInt(parts[2],10)).getDay() === 1;
  }}
  function __fcShowLabel(idx) {{
    var v = __fcVisible;
    if (v <= 30) return true;
    if (v <= 60) return __fcMonday(idx);
    if (v <= 180) return __fcMonthStart(idx);
    return __fcQuarterStart(idx);
  }}
  function __fcFormatter(v, i) {{
    // ECharts dataZoom 后 formatter 的 i 可能是视觉索引而非数据索引；优先用 v 反查真实日期，避免推演图缩放后日期错配。
    var idx = (D.xcats && v) ? D.xcats.indexOf(v) : i;
    if (idx < 0) idx = i;
    var d = (idx >= 0 && D.xcats && D.xcats[idx]) ? D.xcats[idx] : v;
    if (!d || d.length < 7) return v;
    if (!__fcShowLabel(idx)) return '';
    // R122: 可见天数较多时只显示月-日，避免首标签年份(如"2026")与紧随其后的月日标签(如"03-02")
    // 因间隔只有几个交易日而发生水平重叠。年份信息由顶部关键价位条的年份 chip 补充。
    if (__fcVisible <= 10) return d;
    return d.slice(5);
  }}
  function __makeFcFormatter() {{ return function(v, i) {{ return __fcFormatter(v, i); }}; }}
  function updateForecastLabels() {{
    // 用全量类目数 D.xcats.length 计算可见天数，避免依赖 getOption().xAxis[0].data 在 dataZoom 过滤后的行为（跨版本不可靠）。
    var opt = chart.getOption();
    var dz = (opt.dataZoom && opt.dataZoom[0]) || {{ start: 0, end: 100 }};
    var start = dz.start || 0, end = dz.end || 100;
    __fcVisible = Math.max(1, Math.floor(D.xcats.length * (end - start) / 100));
    chart.setOption({{ xAxis: {{ axisLabel: {{ interval: 0, autoHide: false, hideOverlap: false, formatter: __makeFcFormatter() }} }} }});
  }}
  var option = {{
    animation: false,
    axisPointer: {{ link: [{{ xAxisIndex: 'all' }}], label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }},
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'cross', label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }},
      formatter: function(params){{
        if(!params || !params[0]) return '';
        var i = params[0].dataIndex;
        var x = D.xcats[i];
        if(i < D.n_hist){{
          var hv = D.hist[i];
          if(hv == null) return '<b>'+x+'</b>';
          return '<b>'+x+'</b><br>历史收盘 <b>'+hv.toFixed(2)+'</b>';
        }}
        var pi = i - D.n_hist;
        var p = D.proj[pi];
        var sm = (D.sentMed && i >= 0 && i < D.sentMed.length) ? D.sentMed[i] : null;
        if(!p) return '<b>'+x+'</b>';
        var red='#e54545', gray='#94a3b8', grn='#18a058', cyan='#0891b2';
        var f95l=p.f95l, f95h=p.f95h, f75l=p.f75l, f75h=p.f75h;
        var s95=(f95l!=null&&f95h!=null)?(f95l.toFixed(0)+'~'+(f95l+f95h).toFixed(0)):'—';
        var s75=(f75l!=null&&f75h!=null)?(f75l.toFixed(0)+'~'+(f75l+f75h).toFixed(0)):'—';
        return '<b>推演 · T+'+p.tplus+' ('+p.date+')</b><br>'
          + '<span style="color:'+red+'">主路径 '+p.main.toFixed(2)+'</span> '+Math.round(D.p_main*100)+'%<br>'
          + '<span style="color:'+gray+'">次路径 '+p.alt.toFixed(2)+'</span> '+Math.round(D.p_alt*100)+'%<br>'
          + '<span style="color:'+grn+'">风险路径 '+p.risk.toFixed(2)+'</span> '+Math.round(D.p_risk*100)+'%<br>'
          + '<span style="color:'+cyan+'">趋势外推 '+p.trend.toFixed(2)+'</span><br>'
          + '<span style="color:#64748b">经验分位 P05~P95 '+s95+'</span><br>'
          + '<span style="color:#64748b">P25~P75 '+s75+'</span>'
          + (sm != null ? '<br><span style="color:#7c3aed">市场情绪 '+sm.toFixed(1)
            + (sm <= D.sent_bt ? ' · 情绪低迷，通常领先价格见底' : (sm >= D.sent_st ? ' · 情绪高涨，警惕过热回落' : ' · 中性区')) + '</span>' : '');
      }}
    }},
    legend: {{ data: ['历史','统计中位路径','结构演绎路径','次路径','风险路径','趋势外推','MA20','MA60','MA120','MA250','置信锥 P05–P95','置信锥 P25–P75','市场情绪中位','情绪P25~P75'], top: 2, itemGap: 8, textStyle: {{ fontSize: 11 }} }},
    grid: {{ left: 96, right: 88, top: 64, bottom: 80 }},
    xAxis: {{ type: 'category', data: D.xcats, boundaryGap: false, axisTick: {{ show: false }}, axisPointer: {{ label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }}, axisLabel: {{ fontSize: 11, margin: 6, interval: 0, autoHide: false, hideOverlap: false, showMinLabel: false, showMaxLabel: false,
        formatter: __makeFcFormatter() }} }},
    yAxis: [
      {{ scale: false, min: D.ymin_core, max: D.ymax_core, splitNumber: 6, axisLine: {{ lineStyle: {{ color: '#cbd5e1' }} }}, splitLine: {{ lineStyle: {{ color: '#eef2f7' }} }}, axisLabel: {{ fontSize: 12, hideOverlap: true }}, axisPointer: {{ label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }} }},
      {{ name: '情绪(0-100)', min: 0, max: 100, position: 'right', axisLine: {{ show: true, lineStyle: {{ color: '#7c3aed' }} }}, splitLine: {{ show: false }}, axisLabel: {{ fontSize: 11, color: '#7c3aed', formatter: '{{value}}' }}, axisPointer: {{ label: {{ show: true, backgroundColor: 'rgba(43,108,176,0.85)', color: '#fff', borderColor: 'transparent', padding: [2,6], borderRadius: 3, fontSize: 11 }} }}, nameTextStyle: {{ color: '#7c3aed', fontSize: 11 }} }}
    ],
    // 推演图默认展示历史最后 120 个交易日 + 全部推演窗口；避免首次打开落在多年前历史数据上。
    dataZoom: [
      {{ type: 'inside', xAxisIndex: 0, start: Math.max(0, (D.n_hist - 120) / D.xcats.length * 100), end: 100 }},
      {{ type: 'slider', xAxisIndex: 0, start: Math.max(0, (D.n_hist - 120) / D.xcats.length * 100), end: 100, height: 16, bottom: 32, handleStyle: {{ color: '#2b6cb0' }}, borderColor: '#e2e8f0', fillerColor: 'rgba(43,108,176,0.12)' }}
    ],
    series: [
      {{ name: '历史', type: 'line', data: D.hist, symbol: 'none', smooth: true, lineStyle: {{ color: '#2b6cb0', width: 1.8 }} }},
      {{ name: '统计中位路径', type: 'line', data: D.med, symbol: 'none', smooth: true, lineStyle: {{ color: '#e54545', width: 1.6 }}, z: 5 }},
      {{ name: '结构演绎路径', type: 'line', data: D.main, symbol: 'none', smooth: true, lineStyle: {{ color: '#e54545', width: 2.0, type: 'dashed', opacity: 0.85 }}, z: 6 }},
      {{ name: '次路径', type: 'line', data: D.alt, symbol: 'none', smooth: true, lineStyle: {{ color: '#94a3b8', width: 2.0, type: 'dashed' }}, z: 7 }},
      {{ name: '风险路径', type: 'line', data: D.risk, symbol: 'none', smooth: true, lineStyle: {{ color: '#18a058', width: 2.0, type: 'dashed' }}, z: 8 }},
      {{ name: '趋势外推', type: 'line', data: D.trend, symbol: 'none', smooth: false, lineStyle: {{ color: '#0891b2', width: 2.0, type: 'dashed' }}, z: 9 }},
      {{ name: 'MA20', type: 'line', data: D.ma20, symbol: 'none', smooth: false, lineStyle: {{ color: '#0ea5e9', width: 1, opacity: 0.9 }} }},
      {{ name: 'MA60', type: 'line', data: D.ma60, symbol: 'none', smooth: false, lineStyle: {{ color: '#a855f7', width: 1, opacity: 0.9 }} }},
      {{ name: 'MA120', type: 'line', data: D.ma120, symbol: 'none', smooth: false, lineStyle: {{ color: '#f59e0b', width: 1, opacity: 0.9 }} }},
      {{ name: 'MA250', type: 'line', data: D.ma250, symbol: 'none', smooth: false, lineStyle: {{ color: '#dc2626', width: 1.2, opacity: 0.95 }} }},
      {{ name: '置信锥 P05–P95', type: 'line', data: D.f95l, stack: 'b95', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ opacity: 0 }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '置信锥 P05–P95', type: 'line', data: D.f95h, stack: 'b95', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ color: 'rgba(229,69,69,0.06)' }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '置信锥 P25–P75', type: 'line', data: D.f75l, stack: 'b75', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ opacity: 0 }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '置信锥 P25–P75', type: 'line', data: D.f75h, stack: 'b75', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ color: 'rgba(229,69,69,0.12)' }}, tooltip: {{ show: false }}, silent: true }},
      // R210: 真联动——市场情绪(恐惧贪婪指数)叠加第二条 Y 轴(0-100), 与价格推演同图对照"情绪领先价格见底"
      {{ name: '情绪P25', type: 'line', yAxisIndex: 1, data: D.sentLo, stack: 'sP', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ opacity: 0 }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '情绪P25~P75', type: 'line', yAxisIndex: 1, data: D.sentHi, stack: 'sP', symbol: 'none', lineStyle: {{ opacity: 0 }}, areaStyle: {{ color: 'rgba(124,58,237,0.10)' }}, tooltip: {{ show: false }}, silent: true }},
      {{ name: '市场情绪中位', type: 'line', yAxisIndex: 1, data: D.sentMed, symbol: 'circle', symbolSize: 3.5, smooth: true, connectNulls: false,
        lineStyle: {{ color: '#7c3aed', width: 2.2 }}, z: 11,
        markPoint: {{ data: D.sentMin ? [{{ coord: [D.sentMin.date, D.sentMin.val], value: '情绪见底', itemStyle: {{ color: '#7c3aed' }}, symbol: 'pin', symbolSize: 32,
          label: {{ show: true, position: 'top', color: '#7c3aed', fontSize: 11, fontWeight: 'bold' }} }}] : [] }} }},
      {{ name: '参考', type: 'line', data: [], silent: true,
        markLine: {{ symbol: 'none', data: D.hlines.concat(D.vline), labelLayout: {{ moveOverlap: 'shiftY' }} }},
        markPoint: {{ data: D.endPoints }} }}
    ]
  }};
  if (D.keyLevelsText) {{
    option.graphic = [{{
      type: 'text', left: 100, top: 50, z: 100, silent: true,
      style: {{
        text: D.keyLevelsText,
        fontFamily: 'Microsoft YaHei', fontSize: 11,
        rich: {{
          year:  {{ fill: '#64748b', fontSize: 10 }},
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
  chart.on('dataZoom', updateForecastLabels);
  chart.on('dblclick', function(){{ chart.dispatchAction({{ type: 'dataZoom', start: Math.max(0, (D.n_hist - 120) / D.xcats.length * 100), end: 100 }}); }});  /* R167: 双击复位回初始推演窗口(最近约120日), 非全量历史 */
  updateForecastLabels();
}})();
</script>'''


# ================= 归一化对比图（日历对齐到共同交易日，#7） =================
def compare_svg(data):
    H = 300
    PAD_T2, PAD_B2 = 20, 30
    # R346: compare_svg 在 main() 调用点无 try 包裹 —— data 空时下方 next(iter(...)) 会
    # StopIteration; 交集空时旧 fallback(单 sym 全 klines)会使其它 sym 在 common[0] KeyError;
    # n=1 时 x() 的 (n-1) 除零。三处均崩整份报告, 统一前置防御降级为空(调用方为 details 折叠区)。
    if not data:
        return ""
    # 各指数独立拉取，节假日/停牌可能差一两天；归一化对比需对齐到共同交易日（取交集）
    date_close = {}
    common = None
    for sym, d in data.items():
        m = {}
        for k in d["klines"]:
            m[k["date"]] = k["close"]
        date_close[sym] = m
        common = set(m.keys()) if common is None else (common & set(m.keys()))
    common = sorted(common) if common else None
    if not common or len(common) < 2:
        return ""
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


def _score_color(score):
    """健康度/置信度数值分级配色(全站统一单一来源): >=66 红(高/强) / 45-66 橙(中) / <45 绿(弱)。
    R343: 由 score_chip 内联逻辑上提为模块级——此前 L3036 标题徽章恒用 RED 标健康、BLUE 标置信,
    与 score_chip 分级配色分裂(实证 sh000300 health=30 在卡片/汇总表为绿底「弱」、标题却红底
    「健康 30」, 同值两种语义误导读者)。"""
    if score >= 66:
        return RED
    if score >= 45:
        return "#d97706"
    return GREEN


def score_chip(score, label=''):
    """健康度/置信度等数值评分胶囊"""
    c = _score_color(score)
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
    if n < 8:
        # R173: 样本不足(N<8)时回退 total 也可能为 0 → mr=0 → dev<-8 必判"偏乐观",
        # 这与 R172 消除伪告警的初衷相悖; 此时不对照方向, 直接告知样本不足。
        return ('<div class="pathcheck"><b>推演路径命中率自校验</b>'
                '<span class="pc-sub">历史同类方向结构样本不足(N={n})，不做方向校准对照，'
                '本条不判定偏乐观/偏保守。</span></div>').format(n=n)
    # R172: 自校验改比「方向命中率」(dir_main/dir_n) 而非目标价命中率(main/n), 与 p_main 经验锚一致,
    # 否则方向技能高的环境(牛/熊)会被误报"偏乐观"。total 无方向命中率时回退目标价命中率。
    if e.get("dir_n", 0.0) > 0.0:
        mr = e["dir_main"] / e["dir_n"] * 100
    else:
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
    # R363: 双法一致率守卫——analyze 骨架(空/退化输入) agreement.total=0 时 rate 恒 0,
    # 此前渲染「双法一致 0%」失实(无任何可比笔却宣告 0% 一致)。total==0 显示"—"。
    _ag = r.get("agreement") or {}
    agree = (_ag.get("rate", 0) * 100) if _ag.get("total", 0) else None
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
    _tt_color = {"上涨走势(趋势)": RED, "下跌走势(趋势)": GREEN, "盘整/扩张走势": "#64748b",
                 "盘整走势": "#64748b"}.get(_tt, "#0f172a")  # R363: 补「盘整走势」灰(中枢<3 时 classify 保留初始值, 此前漏配落 INK 墨色, 与盘整/扩张同义异色)
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
    # 信号成熟度（#29·稳健度三级重构）：最后一支已完成笔跨度，年轻信号属"待确认"而非可靠结论。
    # R363: stability 缺失防御——analyze 骨架/with_stability=False 输入 stability=None, 此前
    # 默认 established 渲染「信号成熟·末笔0日」绿徽章(无稳定性数据却宣告成熟, 语义失实)。
    # 缺失时给中性「稳定性未知」徽章; 有数据才走 established/young 两级(正常路径输出逐字不变)。
    _stab = r.get("stability") or None
    _mat = _stab.get("maturity") if _stab else None
    _lbb = _stab.get("last_bi_bars", 0) if _stab else None
    if _mat == "established":
        _mat_txt, _mat_c = "信号成熟", "#18a058"
        _lbb_txt = (" · 末笔%d日" % _lbb) if _lbb is not None else ""
    elif _mat == "young":
        _mat_txt, _mat_c = "信号年轻·待确认", "#d97706"
        _lbb_txt = (" · 末笔%d日" % _lbb) if _lbb is not None else ""
    else:
        _mat_txt, _mat_c, _lbb_txt = "稳定性未知", "#94a3b8", ""
    _mat_chip = badge(_mat_txt + _lbb_txt, _mat_c)
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
      <div class="kv"><span>最近一笔</span><b>{_dd} {amp:.1f}%</b></div>
      <div class="kv"><span>当前分类</span><b style="color:{sc_color}">{cls["scenario"]}</b></div>
      <div class="kv"><span>走势类型</span><b style="color:{_tt_color}">{_tt}</b></div>
      <div class="kv"><span>最后中枢</span><b>{_zs_txt}</b></div>
      <div class="kv"><span>关键缺口(未补)</span><b style="color:#475569;font-size:11px">{_gap_txt}</b></div>
      <div class="kv"><span>均线排列(MA20/60/120/250)</span><b style="color:{ma_color}">{ma_txt}</b></div>
      <div class="kv"><span>多周期共振</span><b style="font-size:11px;line-height:1.55">
        <span style="color:{sc_color}">日 {_dd}</span>·
        <span style="color:{w_color}">周 {_wd}</span>·
        <span style="color:{m_color2}">月 {_md}</span>
        <span style="color:#94a3b8">（{cls['scenario']}/{_wsc}/{_msc}）</span><br>
        <span style="color:{_res_color};font-weight:700">{_res}</span></b></div>
      <div class="chips">{score_chip(health, "结构健康")}{score_chip(conf, "推演置信")}{badge(f'双法一致 {agree:.0f}%' if agree is not None else '双法一致 —', '#64748b')}{_mat_chip}</div>
    </div>"""


# ================= 关键位表 =================
def strategy_text(cls, zs):
    sc = cls["scenario"]
    if zs is None:
        # R363: 区分「真数据不足」(classify 空输入骨架 scenario=数据不足)与「有笔无中枢」
        # (zss 空但 bis 有——无中枢·向上/向下笔, 数据正常仅结构未成型)——此前一律渲染
        # 「结构数据不足」, 令无中枢指数被误称数据不足、与「暂按笔级别对待」的 classify 语义冲突。
        if sc in ("数据不足", "震荡待方向"):
            return "结构数据不足，观望"
        return "暂无已完成中枢，按笔级别方向观望；中枢成型后再定买卖点"
    if sc == "多头延续":
        return f"持股为主；回踩 ZG {zs['zg']:.0f} 不破=三买可加；跌破 ZD {zs['zd']:.0f} 转空"
    if sc in ("中枢震荡偏多", "高位整理未破前高"):
        return f"区间 {zs['zd']:.0f}~{zs['zg']:.0f} 高抛低吸；站稳 ZG 转多，跌破 ZD 转空"
    if sc in ("中枢震荡偏空", "弱势反弹"):
        return f"反抽不过 ZD {zs['zd']:.0f} 减仓；回到中枢内部再观察"
    if sc == "反弹未回中枢":
        return f"反弹未回中枢 ZD {zs['zd']:.0f}，观望；收复 ZD 转震荡，再上破 ZG {zs['zg']:.0f} 转多"
    if sc == "空头延续":
        # R363: 此前无显式分支落入 default 被标「结构中性(空头延续)」——与情景语义自相矛盾
        # (空头延续 ∈ SC_BEAR, 2021-24 熊市常见情景, 当前窗口未触发但历史上必现)。补显式空头措辞。
        return f"空头延续中，反抽不过 ZD {zs['zd']:.0f} 减仓防守；重回中枢内部才转震荡"
    if sc == "背驰见顶风险":
        return f"顶背驰确认中，减仓防守；支撑看 ZG {zs['zg']:.0f}"
    if sc == "背驰见底机会":
        return f"底背驰确认中，分批布局；压力看 ZD {zs['zd']:.0f}"
    # R363: 集合护栏(结构性防漏)——SC_BULL/SC_BEAR 全集成员但未配显式分支(未来新增情景)时
    # 仍按方向给措辞, 杜绝再出现「空头情景被标结构中性」式自相矛盾(根因=分支表与集合靠手写维护)。
    if sc in SC_BEAR:
        return f"偏空结构（{sc}），反抽不过 ZD {zs['zd']:.0f} 减仓防守；重回中枢内部再观察"
    if sc in SC_BULL:
        return f"偏多结构（{sc}），回踩不破 ZG {zs['zg']:.0f} 持股；跌破 ZD {zs['zd']:.0f} 转弱"
    # 其余(震荡待方向等真中性情景)给中性观望建议, 不再误标「空头格局减仓」(R164)
    return f"结构中性（{sc}），观望为主；突破 ZG {zs['zg']:.0f} 转多，跌破 ZD {zs['zd']:.0f} 转空"


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
        # R363: 方向有效性守卫——classify 空/退化输入早返回 last_bi_dir=0(analyze([]) 骨架 scenario=数据不足),
        # 此前 0==0 落入相等分支、再因 !=1 误标「共振空」绿勾(数据不足被宣告为看空共振, 语义失实)。
        # 任一层方向无效(0/None)时给中性「数据不足」徽章, 不进共振/背离判定。
        _dd0, _wd0 = cls.get("last_bi_dir"), wcls.get("last_bi_dir")
        if _dd0 in (1, -1) and _wd0 in (1, -1):
            if _dd0 == _wd0:
                syn = badge(f'共振{"多" if _dd0 == 1 else "空"}', RED if _dd0 == 1 else GREEN, '✓ ')
            else:
                syn = badge('日强周弱背离' if _dd0 == 1 else '日弱周强背离', '#d97706', '⚠ ')
        else:
            syn = badge('数据不足', '#94a3b8')
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
    return """<h3 class="fc-title">关键位与中枢区间<span class="fc-sub">日 / 周 / 月三级联立 · 现价与 ZG/ZD 距离</span></h3>
      <table class="tbl">
      <colgroup><col style="width:90px"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"><col style="width:calc((100%% - 90px)/9)"></colgroup>
      <thead><tr><th>指数</th><th>日线分类</th><th>周线分类</th><th>月线背景</th><th class="tac">级别联立</th><th>现价</th><th>压力 ZG（距离）</th><th>支撑 ZD（距离）</th><th class="tac">健康度 / 置信度</th><th>应对策略</th></tr></thead>
      <tbody>%s</tbody></table>""" % "".join(rows)


def backtest_table(backtests):
    """汇总 5 指数信号回测：{sym: {kind: {h: {n, win_rate, avg_ret}}}}

    R478: 新增段级行（段一买/段一卖/段二买/段二卖）。段级样本与笔级**分开统计**、分开成行 ——
    两族样本性质不同（段级周期长约一个量级、样本少），混计会污染笔级的历史可比性。
    """
    KINDS = ["一类买", "一类卖", "二类买", "二类卖", "类二买", "类二卖",
             "三类买", "三类卖",
             "段一买", "段一卖", "段二买", "段二卖", "段三买", "段三卖",
             "段类买", "段类卖"]
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
    return """<h3 class="fc-title">信号历史回测汇总<span class="fc-sub">2021 至今全部买卖点 · 胜率与均收益</span></h3>
      <table class="tbl">
      <colgroup><col style="width:110px"><col style="width:calc((100%% - 110px)/4)"><col style="width:calc((100%% - 110px)/4)"><col style="width:calc((100%% - 110px)/4)"><col style="width:calc((100%% - 110px)/4)"></colgroup>
      <thead><tr><th>信号类型</th><th class="tac">后 5 个交易日</th><th class="tac">后 10 个交易日</th><th class="tac">后 20 个交易日</th><th class="tac">后 60 个交易日</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">统计 5 大指数 2021-01 至今全部信号（买点胜=之后涨，卖点胜=之后跌）。买卖点按缠论标准：一类=背驰拐点，二类=次低/次高折返，三类=回抽不进中枢；<b>段一/段二/段类二/段三 = 线段级别</b>（段一=线段级背驰拐点，段二=段级一类后次级别<b>首个</b>回抽不破前极值，段类二=<b>第 2 次</b>同类回抽，段三=<b>段级中枢</b>被离开后次级别回抽不重新进入），与笔级<b>分行独立统计、不混计</b>。<b>段三点样本量小</b>（5 指数 2021 至今合计 33 条，其中上证仅 2 条）—— 读数只作方向参考，尤其中短期胜率在 n&lt;15 时易出 100%% 这类假值。<b>段类二</b>为 R483 新增（首个之后<b>第 2 次</b>未破位的反向折返；实测 20 日胜率 78.3%% / 均 +4.40%%，与首个回抽同级、远优于随机基准 49.1%% / +0.22%%；<b>第 3 次</b>起退化到基准，故不再后取）。<b>类二（笔级）</b>为 R484 新增，与段类二同律：笔级一类后<b>第 2 次</b>折返不破前极值（实测 20 日胜率 68.2%%(买)/73.1%%(卖)，不弱于首个 59.3%%/62.9%%，第 3 次退化到 58.8%%/59.1%%）。同一轮已量化并<b>决定不扩</b>三类的第 2 次起（笔 73.1%%→61.5%%、段 81.5%%→66.7%%，两侧一致退化）。样本有限，历史特征，非投资建议。</p>""" % "".join(rows)


def rr_table(data, results, recent_n=8):
    """近期买卖点值博率（R:R）明细：每个指数最近 N 个买卖点的止损/目标/R:R/值博率——
    缠论实战交易计划必备（每个买卖点须有明确止损位与目标位），此前报告完全缺失该维度。

    R478: ① 并入**段级买卖点**（results[sym]["seg_signals"]）—— 否则「表里有、图上没有」
    或反之都破坏一致性（本表是用户核对图上标注的唯一清单）；② 新增「级别」列显式区分
    笔级/段级 —— 同一日期可能两级同时出点，只有 kind 文字区分不够，且两级的止损/目标
    空间完全不同（段级更大）。排序按日期升序后取最近 N 条（原先按 signals 顺序切片，
    并入段级后必须显式排序，否则混序）。"""
    rows = []
    for sym, d in data.items():
        r = results[sym]
        # 合并两级信号并显式按日期排序（同级同日时笔级排前，保持与图上 pri 一致的直觉顺序）
        _all = list(r["signals"]) + list(r.get("seg_signals") or [])
        _all.sort(key=lambda s: (s.get("date") or "", 1 if s.get("level") == "seg" else 0))
        for s in _all[-recent_n:]:
            _q = s.get("quality", "—")
            _qc = {"优": RED, "良": GREEN, "中": "#64748b", "差": "#b45309", "—": "#94a3b8"}.get(_q, "#94a3b8")
            _dir_col = RED if s["dir"] == 1 else GREEN
            _vc = "✓" if s.get("vol_confirm") else "—"
            _lvl = "段级" if s.get("level") == "seg" else "笔级"
            _lvl_txt = (f'<b style="color:#7c3aed">段级</b>' if _lvl == "段级"
                        else '<span style="color:#94a3b8">笔级</span>')
            _rr_raw = s.get("rr")
            _rr = ("≥%.1f" % _rr_raw) if (s.get("rr_capped") and _rr_raw is not None) else (
                ("%.1f" % _rr_raw) if _rr_raw is not None else "—")
            _price = ("%.1f" % s["price"]) if s.get("price") is not None else "—"
            _stop = ("%.1f" % s["stop"]) if s.get("stop") is not None else "—"
            _target = ("%.1f" % s["target"]) if s.get("target") is not None else "—"
            rows.append(f"""<tr data-sym="{sym}" class="linkrow" data-jump>
              <td><b>{d["name"]}</b></td>
              <td class="tac">{_lvl_txt}</td>
              <td style="color:{_dir_col};font-weight:600">{s["kind"]}</td>
              <td>{s["date"]}</td>
              <td class="tac">{_price}</td>
              <td class="tac">{_stop}</td>
              <td class="tac">{_target}</td>
              <td class="tac" style="color:{INK};font-weight:700">{_rr}</td>
              <td class="tac">{badge(_q, _qc)}</td>
              <td class="tac">{_vc}</td>
            </tr>""")
    return """<h3 class="fc-title">近期买卖点值博率（R:R）明细<span class="fc-sub">止损 / 目标 / 风险收益比 —— 缠论实战交易计划必备 · 含段级（线段级别）信号</span></h3>
      <table class="tbl">
      <colgroup><col style="width:104px"><col style="width:46px"><col style="width:calc((100%% - 150px)/8)"><col style="width:calc((100%% - 150px)/8)"><col style="width:calc((100%% - 150px)/8)"><col style="width:calc((100%% - 150px)/8)"><col style="width:calc((100%% - 150px)/8)"><col style="width:calc((100%% - 150px)/8)"><col style="width:calc((100%% - 150px)/8)"><col style="width:calc((100%% - 150px)/8)"></colgroup>
      <thead><tr><th>指数</th><th class="tac">级别</th><th>买卖点</th><th>日期</th><th class="tac">触发价</th><th class="tac">止损位</th><th class="tac">目标位</th><th class="tac">R:R</th><th class="tac">值博率</th><th class="tac">量✓</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">R:R = (目标−触发) / (触发−止损)；值博率：优(RR≥2.5)/良(≥1.5)/中(≥1.0)/差(&lt;1)。止损取局部前低或中枢下沿 ZD，目标取近程摆动极值并封顶 6 倍防失真（表中 R:R 标「≥」者为封顶值，真实风险收益比可能更高）。<b>级别</b>列：<span style="color:#94a3b8">笔级</span> = 笔级一/二/三类买卖点；<b style="color:#7c3aed">段级</b> = 线段级别买卖点（段一=段级背驰拐点，段二=段级一类后次级别首个回抽不破前极值，段三=段级中枢被离开后次级别回抽不重新进入）。<b>段三点的 R:R 普遍偏低</b>（实测中位 0.54，笔级三类为 1.30）——<b>不是算错</b>：三类点一律用「中枢下沿 ZD」当止损，而段级中枢由 ≥3 条线段重叠构成、跨度天然比笔中枢大一个量级 ⇒ 止损位离现价更远；其方向胜率与笔级三类相当（20 日 70%% vs 70%%）。止损位一律取结构位（前低/中枢沿），<b>不做参数优化</b>。本表列出的每一条都能在上方日线图上找到标注，反之亦然。结构参考，非交易建议。</p>""" % "".join(rows)


def robustness_table(robust, data):
    """样本外稳健性检验表：早年(2021~split前) vs 近两年(split起) 买方信号胜率对比，检测校准过拟合。

    R342: 早/近/变化/判定全列统一采用 R326 walk-forward 多切分真均值口径(walk_forward.early_rate /
    recent_rate / decay)——此前渲染仍用 pooled 跨切分累计样本折算(early/recent 累计结构仅保留作
    样本量护栏参考), 跨切分重复计数使 diff 失真: 实测 sh000001 pooled=-42.9pt 会误报「显著衰减」
    (真 wf=-1.7pt 稳定)、sh000300 pooled=+12.1pt 会误报「样本外稳定」(真 wf=-12.9pt 显著衰减),
    与同行「滚动窗口衰减」列自相矛盾。"""
    rows = []
    for sym, rb in robust.items():
        name = data[sym]["name"]
        early, recent, _split_t = rb["early"], rb["recent"], rb["split"]
        split = "多切分(" + "/".join(str(s[:4]) for s in _split_t) + ")" if isinstance(_split_t, (tuple, list)) else _split_t
        wf = rb.get("walk_forward", {}) or {}
        wf_er, wf_rr, wf_d = wf.get("early_rate"), wf.get("recent_rate"), wf.get("decay")

        def _pick(d, h=20):
            st = d.get("一类买", {}).get(h) or d.get("三类买", {}).get(h)
            if not st or st["n"] == 0:
                return None
            return st["win_rate"], st["avg_ret"], st["n"]

        em, rm = _pick(early), _pick(recent)   # 仅作样本量护栏(pooled 累计计数; 极小样本 per-split 均值同样不可信)
        if wf_er is not None and wf_rr is not None and wf_d is not None:
            diff_txt = "%+.0fpt" % (wf_d * 100)
            # 样本充足性护栏：早年或近两年买方累计样本任一不足 20，早期胜率(尤极小样本易出 100%)
            # 不可信，衰减 pt 多为抽样噪声，禁止据此发「过拟合」告警——与 build_quality_cert 的
            # regime 判定 n>=20 可靠性口径一致。仅给中性「样本不足·难判定」，避免误导。
            if (em is None or em[2] < 20) or (rm is None or rm[2] < 20):
                verdict = badge('样本不足 · 难判定', '#64748b')
            elif wf_d <= -0.15:
                verdict = badge('近两年显著衰减 · 校准或存过拟合', '#d97706', '⚠ ')
            elif wf_d >= -0.05:
                verdict = badge('样本外稳定', GREEN, '✓ ')
            else:
                verdict = badge('轻微衰减', '#64748b')
            n_txt = ("n=%d/%d" % (em[2], rm[2])) if (em and rm) else "—"
        else:
            verdict, diff_txt, n_txt = "—", "—", "—"
            wf_er = wf_rr = None

        def _pct(x):
            return ("%.0f%%" % (x * 100)) if x is not None else "—"

        rows.append(f"""<tr data-sym="{sym}" class="linkrow" data-jump>
          <td><b>{name}</b>（{sym}）</td>
          <td class="tac">{_pct(wf_er)}</td>
          <td class="tac">{_pct(wf_rr)}</td>
          <td class="tac">{diff_txt}</td>
          <td class="tac">{n_txt}</td>
          <td>{verdict}</td>
        </tr>""")
    _tbl = """<h3 class="fc-title">样本外稳健性检验<span class="fc-sub">早年 vs 近两年 · 检测校准过拟合</span></h3>
      <table class="tbl">
      <colgroup><col style="width:140px"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"><col style="width:calc((100%% - 140px)/5)"></colgroup>
      <thead><tr><th>指数</th><th class="tac">早年买方信号胜率*</th><th class="tac">近两年买方信号胜率*</th><th class="tac">变化</th><th class="tac">买方样本量(早/近)</th><th>样本外稳健性</th></tr></thead>
      <tbody>%s</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">买方信号（一类买·三类买，持有 20 日）按 {SPLIT} 多个切分点分别折算胜率后对切分点取均值（walk-forward，R326）。<b>判定一律用切分均值而非跨切分累计</b>——累计会把早年样本重复计数使胜率差失真（实测：上证累计口径 -43pt 实为 -2pt 稳定；沪深300 累计 +12pt 实为 -13pt 显著衰减）。近两年显著下滑(≥15pt)提示过拟合风险；持平/更高则样本外稳定。<b>早年或近两年买方样本&lt;20 时判定为「样本不足·难判定」，不据此发过拟合告警</b>（极小样本易出 100%% 胜率致衰减失真）。样本量列为早年/近两年买方信号累计计数，仅作可靠性参考。<b>段级买点（段一买·段二买·段三买）<u>不在本表范围内</u></b>——全市场 5 指数 2021 至今段级买点合计 68 条（段一买 27 / 段二买 26 / 段三买 15），再按指数×早年/近两年四分后每格只有个位数，切分均值无统计意义；纳入只会产出「100%% 胜率」这类假信号。段级的表现见上方回测汇总表（各周期分列、与笔级不混计）。不构成投资建议。</p>""".replace("{SPLIT}", split)
    return _tbl % "".join(rows)


def forecast_summary_table(data, results, results_week, results_month, forecast_info):
    """推演情景汇总：5 指数主/次/风险概率 + 失效位 + 级别联立 + 稳定性并列对比"""
    rows = []
    for sym, d in data.items():
        r = results[sym]
        wcls = results_week[sym]["classify"]
        mcls = results_month[sym]["classify"]
        m_color = SCENARIO_COLOR.get(mcls["scenario"], BLUE)
        # R206: forecast_info 可能因单指数渲染降级(主循环 try/except 分支不写该 sym)而缺键,
        # 用 .get 容错 + 占位行, 与「单指数降级不中断整份报告」纪律对齐, 避免整表 KeyError 崩。
        fi = forecast_info.get(sym)
        if fi is None:
            rows.append(f'<tr data-sym="{sym}" class="linkrow" data-jump>'
                         f'<td><b>{d.get("name", sym)}</b></td>'
                         f'<td colspan="9" style="color:#dc2626;text-align:center">该指数推演降级占位（数据异常，不影响其余指数）</td></tr>')
            continue
        cls = r["classify"]
        sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
        # R363: 同 levels_table —— 日/周任一层方向无效(0/None)时给中性徽章, 勿把「数据不足」误标「共振空」。
        _dd0, _wd0 = cls.get("last_bi_dir"), wcls.get("last_bi_dir")
        if _dd0 in (1, -1) and _wd0 in (1, -1):
            if _dd0 == _wd0:
                syn = badge(f'共振{"多" if _dd0 == 1 else "空"}', RED if _dd0 == 1 else GREEN, '✓ ')
            else:
                syn = badge('日强周弱背离' if _dd0 == 1 else '日弱周强背离', '#d97706', '⚠ ')
        else:
            syn = badge('数据不足', '#94a3b8')
        _lv = fi.get("level", "稳健")
        _dev = (fi.get("fc", {}) or {}).get("path_dev", 0) or 0
        # R490: 与 forecast 的偏离提示同源(读 fc，不重算判据) —— 方向相反同样标注，避免"推演图提示了、
        # 汇总表却不提示"的口径分裂
        _devc = bool((fi.get("fc", {}) or {}).get("path_dir_conflict", False))
        _lv_disp = (_lv + "·结构/统计偏离") if (abs(_dev) > 0.08 or _devc) else _lv
        _lv_c = {"稳健": GREEN, "边缘": "#d97706", "敏感·待确认": RED}.get(_lv, GREEN)
        stab = _lv_disp
        stab_c = _lv_c
        rows.append(f"""<tr data-sym="{sym}" class="linkrow" data-jump>
          <td><b>{d.get("name", sym)}</b></td>
          <td>{badge(cls["scenario"], sc_color)}</td>
          <td>{badge(mcls["scenario"], m_color)}</td>
          <td class="tac">{syn}</td>
          <td class="tac"><b style="color:{RED}">{(fi.get("p_main") or 0)*100:.0f}%</b>{prob_bar(fi.get("p_main") or 0, RED)}</td>
          <td class="tac"><b style="color:#64748b">{(fi.get("p_alt") or 0)*100:.0f}%</b>{prob_bar(fi.get("p_alt") or 0, "#64748b")}</td>
          <td class="tac"><b style="color:{GREEN}">{(fi.get("p_risk") or 0)*100:.0f}%</b>{prob_bar(fi.get("p_risk") or 0, GREEN)}</td>
          <td class="tac"><b style="color:{BLUE}">{(fi.get("p_hold") or 0)*100:.0f}%</b>{prob_bar(fi.get("p_hold") or 0, BLUE)}</td>
          <td class="tac">{fi.get("zd") or 0:.0f}</td>
          <td class="tac">{badge(stab, stab_c)}</td>
        </tr>""")
    return f"""<h3 class="fc-title">推演情景概率与结论稳定性<span class="fc-sub">主 / 次 / 风险已归一 · 结构存续为独立参照</span></h3>
      <table class="tbl">
      <colgroup><col style="width:90px"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"><col style="width:calc((100% - 90px)/9)"></colgroup>
      <thead><tr><th>指数</th><th>日线分类</th><th>月线背景</th><th class="tac">级别联立</th><th class="tac">主路径概率</th><th class="tac">次路径概率</th><th class="tac">风险概率</th><th class="tac">结构存续(锥)</th><th class="tac">失效位 ZD</th><th class="tac">结论稳定性</th></tr></thead>
      <tbody>{"".join(rows)}</tbody></table>
      <p style="font-size:12px;color:#64748b;margin-top:8px">概率为「级别共振+置信度+回测胜率」启发式估算，主/次/风险已归一(合计100%)，结构存续(锥)为独立参照不计入。稳健度三级：<b style="color:{GREEN}">稳健</b>=极性趋势 20 日内不变；<b style="color:#d97706">边缘</b>=趋势守住但末笔年轻；<b style="color:{RED}">敏感·待确认</b>=极性翻转、已下调主路径概率。末笔仅~7根K线者宜轻仓等周线确认。</p>"""


# ================= 年度收益表 =================
def load_live_sentiment():
    """读取 H5 情绪看板 live 情绪分(同机绝对路径; 可用环境变量 SENTIMENT_V2_PATH 覆盖)。
    不可用时(如 CI 无该文件)优雅降级为 None —— 不影响预测管线, 仅用于 R76 极端区透明化提示。
    注: 仅做提示, 不进入任何预测数学(预测数学由门禁 R76 严格管控, 当前未达并入阈值)。"""
    try:
        import os as _os
        p = _os.environ.get("SENTIMENT_V2_PATH")
        if not p:
            # R175: 修正情绪模块路径——它随仓库内置在 chanlun/sentiment/(calc_v2.py 写入、
            # audit_sentiment_conditioning.py 读取均在此), 此前写死 "../sentiment/" 指向 chanlun 的
            # 同级目录, 仓库内实为子目录, 致文件永远找不到、load_live_sentiment 静默降级 None,
            # 线上 R76 极端区情绪透明化提示从未生效。优先用内置子目录路径, 兼容部署同级布局作 fallback。
            _base = _os.path.dirname(_os.path.abspath(__file__))
            _candidates = [
                _os.path.join(_base, "sentiment", "sentiment_v2.json"),
                _os.path.join(_base, "..", "sentiment", "sentiment_v2.json"),
            ]
            p = None
            for _c in _candidates:
                if _os.path.exists(_c):
                    p = _c
                    break
            if p is None:
                p = _candidates[0]  # 仍给默认, 下方 exists 检查会 return None
        if not _os.path.exists(p):
            return None
        d = json.load(open(p, encoding="utf-8"))
        # R346: clamp 兜底(calc_v2 R328 后顶层 score 仍可能为 raw 旧产物) —— 0-100 标尺统一,
        # 避免超界(raw 域 [-12.5,112.5])时 R76 提示显示 "极端104分/极端-5分" 与温度计分裂。
        _sv = d.get("score")
        if _sv is None:
            return None  # 缺 score 保持原降级语义(None), 不设默认值
        score = float(max(0.0, min(100.0, _sv)))
        buy_th = float(d.get("buy_th", 20))
        sell_th = float(d.get("sell_th", 85))
        asof = d.get("asof")
        if score <= buy_th:
            zone, label = "fear", "恐惧"
        elif score >= sell_th:
            zone, label = "greed", "贪婪"
        else:
            zone, label = "neutral", "中性"
        return {"score": score, "zone": zone, "label": label,
                "buy_th": buy_th, "sell_th": sell_th, "asof": asof}
    except Exception:
        return None


# ================= 市场情绪板块（R177: 参考情绪看板设计, 与结构/推演/回测各模块互联） =================
def load_sentiment_full():
    """完整读取情绪引擎产物 sentiment_v2.json（六因子/快慢线/弹性/共振/历史信号/背离全量）。
    与 load_live_sentiment 的差异: 后者只取 score/zone 供极端区提示, 这里取全字段渲染情绪板块。
    缺失/损坏/为空时返回 None, 由调用方优雅降级(不影响主报告)。"""
    try:
        import os as _os
        p = _os.environ.get("SENTIMENT_V2_PATH")
        if not p:
            _base = _os.path.dirname(_os.path.abspath(__file__))
            _candidates = [
                _os.path.join(_base, "sentiment", "sentiment_v2.json"),
                _os.path.join(_base, "..", "sentiment", "sentiment_v2.json"),
            ]
            p = next((_c for _c in _candidates if _os.path.exists(_c)), _candidates[0])
        if not _os.path.exists(p):
            return None
        d = json.load(open(p, encoding="utf-8"))
        if not isinstance(d, dict) or "score" not in d or "final" not in d:
            return None
        return d
    except Exception:
        return None


def _sent_zone(score, buy_th, sell_th):
    """情绪档位: <=买入线=恐惧(机会区·绿), >=卖出线=贪婪(危险区·红),
    中间按分位中线 50 细分为偏恐/偏贪, 避免把 33 分的中性区误标成"安全"。"""
    if score <= buy_th:
        return "fear", "恐惧", GREEN
    if score >= sell_th:
        return "greed", "贪婪", RED
    if score < 50:
        return "fearish", "中性·偏恐", "#2f9e6e"
    return "greedish", "中性·偏贪", "#d97706"


def _sent_trend(ma5s, ma20s):
    """快慢线方向: 快线上慢线=情绪回暖, 反之下行。None 数据退化为"—"。"""
    if ma5s is None or ma20s is None:
        return "—"
    return "回暖 ↑" if ma5s > ma20s else ("降温 ↓" if ma5s < ma20s else "持平 →")



def _sent_x_struct(sc, zone, score):
    """情绪×结构交叉判定（情绪板块与分指数图解互联的核心）:
    sc 为指数日线情景字符串(SC_BULL/SC_BEAR/其余), zone 为情绪档位(含半区 fearish/greedish)。
    返回 (信号标签, 说明, 颜色, 信号权重) —— 权重 3=强信号(顶背离/底共振), 2=顺势一致/半区提示, 1=观望。"""
    bull = sc in SC_BULL
    bear = sc in SC_BEAR
    if zone == "fear":
        if bull:
            return ("逆向共振·底部", f"情绪冰点({score:.0f}分) + 结构转多({sc})——历史胜率高, 分批布局窗口", GREEN, 3)
        if bear:
            return ("顺势看空一致", f"情绪恐惧 + 结构偏空({sc})——空头环境未逆转, 防御为主", BLUE, 2)
        return ("恐惧观望", f"情绪冰点但结构未转多({sc})——等待底分型/背驰确认", "#94a3b8", 1)
    if zone == "greed":
        if bear:
            return ("顶部背离·风险", f"情绪狂热({score:.0f}分) + 结构转空({sc})——警惕反转, 减仓防守", RED, 3)
        if bull:
            return ("顺势看多一致", f"情绪贪婪 + 结构偏多({sc})——多头环境延续, 持股但警惕回撤", RED, 2)
        return ("贪婪观望", f"情绪狂热但结构未转空({sc})——不追高, 等结构确认", "#94a3b8", 1)
    # 半区(中性·偏恐/偏贪): 贴近阈值一侧, 给弱化提示而非笼统"中性", 提升矩阵区分度
    if zone == "fearish":
        if bull:
            return ("偏恐·结构偏多", f"情绪 {score:.0f} 分接近冰点线未触发, 结构已转多({sc})——关注底共振确认", "#18a058", 2)
        if bear:
            return ("偏恐·结构偏空", f"情绪偏恐 + 结构偏空({sc})——弱势环境, 防御为主", BLUE, 2)
        return ("偏恐观望", f"情绪偏恐但结构未转多({sc})——等待确认", "#94a3b8", 1)
    if zone == "greedish":
        if bear:
            return ("偏贪·结构偏空", f"情绪 {score:.0f} 分接近狂热线未触发, 结构已转空({sc})——警惕反转", "#d97706", 2)
        if bull:
            return ("偏贪·结构偏多", f"情绪偏贪 + 结构偏多({sc})——多头环境, 注意情绪过热风险", "#e54545", 2)
        return ("偏贪观望", f"情绪偏贪但结构未转空({sc})——不追高", "#94a3b8", 1)
    if bull:
        return ("中性·结构偏多", f"情绪中性 + 结构偏多({sc})——按结构顺势", BLUE, 2)
    if bear:
        return ("中性·结构偏空", f"情绪中性 + 结构偏空({sc})——按结构谨慎", BLUE, 2)
    return ("中性观望", f"情绪与结构均无明确方向({sc})", "#94a3b8", 1)


# ================= R178 情绪板块（对齐 sentiment-dashboard 视觉语言: 温度计 + 维度表 + 走势预测 + 情绪vs指数） =================
# R179/R183: 情绪图共用的时间轴年份/月份标注 + 自适应密度标签 formatter(沿用 forecast_echart 的反抽稀逻辑,
# 杜绝缩放后日期错配; 按全量类目数切 日/周/月/季 密度, 年份由 x 轴标签显示, 竖线仅作今日参考)。
_SENT_DATE_FMT = """
/* R194: 全部日期边界判断以 v(数据点日期字符串) 为锚, 用 indexOf 求全局索引。
   根因: dataZoom 缩放后 ECharts 传给 axisLabel formatter 的 i 是可见窗口内相对索引,
   旧代码用 D.xcats[i] 取日期 → 标签系统性错位(如 2026-04 窗口内 i=0 取到 2021-01-04)。 */
function _sGI(v){return D.xcats.indexOf(v);}
/* R197: 移除 g<=0 兜底。副图 D.xcats 是近180日切片, g=0 的前一点是切片末尾(2026-08),
   若 g<=0 判为年/月首, 会在 2025-12-08 处误标"2025年", 并与几天后的真实"12月"标签重叠。
   改为仅做真实相邻比较; 首点若不是真实月/季首, 自然留白, 避免和真实月首重叠。 */
function _sMonStart(g){var d=D.xcats[g];if(!d)return false;if(g<=0)return false;var pd=D.xcats[g-1];return (+d.slice(5,7))!==(+pd.slice(5,7));}
function _sQuarterFirst(g){var d=D.xcats[g];if(!d)return false;var m=+d.slice(5,7);if(m!==1&&m!==4&&m!==7&&m!==10)return false;if(g<=0)return false;var pd=D.xcats[g-1];if(!pd)return false;return (+pd.slice(5,7))!==m;}
function _sYearStart(g){var d=D.xcats[g];if(!d)return false;if(g<=0)return false;var pd=D.xcats[g-1];return (+pd.slice(0,4))<(+d.slice(0,4));}
/* R194: 可见窗口自适应密度 —— 修复缩放后标签错配/断续(根因: 旧版按全量类目数定密度,
   主图 dataZoom 默认窗口仅末90日+预测, 全量1426点走季首分支 → 可见窗内标签几乎空白)。
   _sVis 由 dataZoom 事件实时刷新: 可见<=30 逐日 / <=60 每周(i%5) / <=180 月首 / 更长季首; 年首标年份。 */
var _sVis=null;
/* R194b: 初始可见窗口直接用 D.zoomStart 手动算(setOption 前 formatter 已被调用,
   _sRefreshVis 依赖 setOption 后的 dataZoom, 首帧会落到全量档 → 默认窗只显季首)。
   副图无 zoomStart → 取全量长度。 */
function _sVisInit(){var si=D.xcats.indexOf(D.zoomStart);_sVis=(si>=0)?(D.xcats.length-si):D.xcats.length;}
_sVisInit();
/* R194c: dataZoom 事件刷新可见窗口数。关键健壮性: 若 getOption().dataZoom 暂不可得
   (SSR 首帧/交互瞬间), 必须保留 _sVis 当前值, 严禁回退全量 —— 否则默认窗口标签会从月档
   退化成季档(只显2个季首), 表现为"标签稀疏/断续/错配"。_sVisInit 已给出正确基线。 */
function _sRefreshVis(){var zooms=(chart.getOption().dataZoom)||[];if(!zooms.length||!zooms[0])return;var z=zooms[0]||{};var total=D.xcats.length;var s=(z.start!=null?z.start:(z.startValue!=null?Math.max(0,D.xcats.indexOf(z.startValue)):0));var e=(z.end!=null?z.end:100);if(typeof s!=='number'||typeof e!=='number')return;_sVis=Math.max(1,Math.round(total*(e-s)/100));}
chart.on('dataZoom',_sRefreshVis);
function _sShowLabel(v,g){var n=_sVis||D.xcats.length;if(n<=30)return true;if(n<=60)return g%5===0;if(n<=180)return _sMonStart(g);return _sQuarterFirst(g);}
/* R197: 同一刻度只输出一个标签, 年首的月首合并为"YYYY年M月", 杜绝"2025年"与"12月"两个独立短标签相邻重叠。 */
function _sFmt(v,i){var g=D.xcats.indexOf(v);if(g<0)return v||'';if(!_sShowLabel(v,g))return '';var d=D.xcats[g];if(!d)return v||'';var n=_sVis||D.xcats.length;if(n<=30)return d.slice(5);if(n<=60)return d.slice(5);if(n<=180){if(_sYearStart(g))return (+d.slice(0,4))+'年'+(+d.slice(5,7))+'月';if(_sMonStart(g))return (+d.slice(5,7))+'月';return '';}
if(_sYearStart(g))return d.slice(0,4)+'年';if(_sQuarterFirst(g))return 'Q'+Math.floor(((+d.slice(5,7))-1)/3+1);
return '';}
"""


def _year_lines(xcats):
    """每个自然年首根交易日作为年份竖线锚点(与分指数图解年份对齐)。"""
    out, last_y = [], None
    for x in xcats:
        y = x[:4]
        if y != last_y:
            out.append({"x": x, "y": y})
            last_y = y
    return out


def _sent_echart(cid, toolbar, height, fdata, opt_js):
    """情绪 ECharts 容器块: 数据 json.dumps 注入, option 由 JS 构建(支持 formatter 函数/年份标注)。
    图表统一 push 到 window.__charts 由全局 resizeAll 接管缩放。"""
    js = ("(function(){var D=" + json.dumps(fdata, ensure_ascii=False) + ";"
          "var chart=echarts.init(document.getElementById('" + cid + "'));"
          "(window.__charts=window.__charts||[]).push(chart);"
          + _SENT_DATE_FMT +
          "chart.setOption((" + opt_js + ")(D,chart));"
          "_sRefreshVis();})();")
    return ('<div class="echart-toolbar">' + toolbar + '</div>'
            + '<div id="' + cid + '" class="echart-main" style="width:100%;height:'
            + str(height) + 'px;"></div>'
            + '<script>' + js + '</script>')


def _sent_thermometer_html(final, zone, zlabel, zcolor, buy_th, sell_th, final_pct, ma5s, ma20s, scores=None):
    """市场情绪温度计头图(R205深度美化): SVG半圆仪表盘(弧+指针+居中大分) + 分段档位刻度条 + 趋势徽标 + 档位分布迷你条 + 一句话解读。"""
    bt = max(0.0, min(100.0, float(buy_th)))
    st = max(0.0, min(100.0, float(sell_th)))
    pos = max(0.0, min(100.0, float(final)))
    # 趋势箭头
    trend_arrow = "→ 持平"
    trend_color = "#94a3b8"
    if ma5s is not None and ma20s is not None:
        if ma5s > ma20s:
            trend_arrow, trend_color = "↗ 升温", RED
        elif ma5s < ma20s:
            trend_arrow, trend_color = "↘ 降温", GREEN
    # 档位持续统计(冰点/偏冷/中性/偏热/狂热 全样本天数) -> 迷你分布条
    zstat = ""
    zstat_bar = ""
    if scores:
        # R340: 中性档边界不再依赖 mid=(bt+st)/2 —— 阈值网格寻优范围 bt∈[10,30]/st∈[70,90],
        # bt+st<100 的组合(如 bt=10/st=70→mid=40)会使原中性档 [50,mid) 恒空(确定性缺陷, 非
        # 当前配置偶然); 当前 25/80→mid=52.5 也仅 2.5 分宽(48/1325=3.6% 天, 视觉塌陷), 且与
        # 主档位 50 分界口径冲突: 50~52.5 分历史日在 _sent_zone 判"中性·偏贪"却在分布条计"中性"。
        # 改为固定对称中性带 [45,55)(围绕温度计"中性 50"刻度, 与阈值解耦; bt_max=30<45、
        # st_min=70>55, 全网格组合分档单调)。R328 曾记录"无法确证作者带宽意图"待议, 本轮定案。
        bins = [0, 0, 0, 0, 0]  # 冰点,偏冷,中性,偏热,狂热
        for s in scores:
            if s < bt:
                bins[0] += 1
            elif s < 45:
                bins[1] += 1
            elif s < 55:
                bins[2] += 1
            elif s < st:
                bins[3] += 1
            else:
                bins[4] += 1
        tot = max(1, sum(bins))
        _zc = ["#16a34a", "#65a30d", "#64748b", "#ea580c", "#dc2626"]
        _zl = ["冰点", "偏冷", "中性", "偏热", "狂热"]
        segs = "".join('<span class="zseg" style="flex:%d;background:%s"></span>' % (bins[i], _zc[i]) for i in range(5))
        zstat = ('<div class="sent-zstat">'
                 + "".join('<span class="zs" style="color:%s">%s %d日</span>' % (_zc[i], _zl[i], bins[i]) for i in range(5))
                 + '</div>')
        zstat_bar = ('<div class="sent-zstat-bar" title="全样本档位分布">' + segs + '</div>')
    # 一句话解读
    if final_pct is not None:
        bias = "偏低" if final_pct < 40 else ("偏高" if final_pct > 60 else "中性")
        interp = ("当前情绪%s，处于历史%d%%分位（%s），多空%s；极端区信号更具参考价值。"
                  % (zlabel, int(round(final_pct)), bias, "偏弱" if final < 50 else "偏强"))
    else:
        interp = "当前情绪%s，极端区信号更具参考价值。" % zlabel
    ztext = {
        "fear": "恐惧 · 机会区", "fearish": "中性偏恐", "greedish": "中性偏贪", "greed": "贪婪 · 危险区"
    }.get(zone, zlabel)
    pct_txt = ("历史分位 %g%%" % final_pct) if final_pct is not None else ""
    # SVG 半圆仪表盘: 180°弧(-90°→90°), 半径 52, 中心(60,60)
    import math
    _ang = math.pi * (pos / 100.0)  # 0..pi, 0=左(-90°), pi=右(90°)
    _px = 60 + 50 * math.cos(math.pi - _ang)  # 映射到半圆
    _py = 60 - 50 * math.sin(_ang)
    _arc = ("M 10 60 A 50 50 0 0 1 110 60")
    return f"""
    <div class="sent-head">
      <div class="sent-head-row">
        <div class="sent-title-wrap">
          <span class="sent-main-title">市场情绪温度</span>
          <span class="sent-tag" style="background:{zcolor}">{zlabel}</span>
          <span class="sent-trend-badge" style="background:{trend_color}">{trend_arrow}</span>
        </div>
        <div class="sent-gauge">
          <svg viewBox="0 0 120 72" class="sent-gauge-svg" aria-hidden="true">
            <path d="{_arc}" fill="none" stroke="url(#sgGrad)" stroke-width="9" stroke-linecap="round"/>
            <defs>
              <linearGradient id="sgGrad" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stop-color="#22c55e"/>
                <stop offset="50%" stop-color="#f59e0b"/>
                <stop offset="100%" stop-color="#ef4444"/>
              </linearGradient>
            </defs>
            <line x1="60" y1="60" x2="{_px:.1f}" y2="{_py:.1f}" stroke="{zcolor}" stroke-width="2.5" stroke-linecap="round"/>
            <circle cx="60" cy="60" r="4.5" fill="{zcolor}"/>
            <circle cx="{_px:.1f}" cy="{_py:.1f}" r="4" fill="#fff" stroke="{zcolor}" stroke-width="2.5"/>
            <text x="60" y="56" text-anchor="middle" class="sent-gauge-num" fill="{zcolor}">{final:.1f}</text>
            <text x="60" y="68" text-anchor="middle" class="sent-gauge-sub">{pct_txt}</text>
          </svg>
        </div>
      </div>
      <div class="sent-bar-wrap">
        <div class="sent-bar-track">
          <div class="sent-bar-fill" style="width:{pos:.1f}%;background:{zcolor}"></div>
          <div class="sent-bar-tick" style="left:{bt:.1f}%"></div>
          <div class="sent-bar-tick" style="left:{st:.1f}%"></div>
          <div class="sent-bar-tick-label" style="left:{bt:.1f}%;color:{GREEN}">机会 {buy_th:.0f}</div>
          <div class="sent-bar-tick-label" style="left:{st:.1f}%;color:{RED}">危险 {sell_th:.0f}</div>
        </div>
        <div class="sent-bar-labels">
          <span style="color:{GREEN}">机会区</span>
          <span style="color:#64748b">中性 50</span>
          <span style="color:{RED}">危险区</span>
        </div>
      </div>
      {zstat}
      {zstat_bar}
      <div class="sent-interp">{interp}</div>
      <div class="sent-head-note">{ztext} · 绿=恐惧(机会) 红=贪婪(危险) · 数据截至今日收盘</div>
    </div>"""


def _sent_warm_band(yvals, xvals):
    """返回预热期(首段连续 None 样本不足)的 x 区间 [x0, x1]; 无预热则 None。
    仅覆盖前导连续 None(数据本身首段才缺样本), 不做内部断档——内部断档非统计预热, 不标灰。"""
    first = next((i for i, y in enumerate(yvals) if y is not None), None)
    if first is None or first == 0:
        return None
    return (xvals[0], xvals[first])


def _sent_warm_markarea(wb, band_color="#94a3b8", band_op=0.22):
    """构造 markArea 数据(灰带 + 文字标注)。wb=None 时返回空列表。"""
    if not wb:
        return []
    return [[{"xAxis": wb[0],
              "itemStyle": {"color": band_color, "opacity": band_op},
              "label": {"show": True, "formatter": "数据预热期（样本不足）",
                        "position": "insideLeft", "color": "#64748b",
                        "fontSize": 10, "align": "left"}},
             {"xAxis": wb[1]}]]


def _sent_main_chart(forecast, hist, buy_th, sell_th, acc=None):
    """情绪走势与未来预测合一主图(R181): 全量历史 + KNN预测(紫虚线) + 50%校准置信带(κ重标定, 阴影) +
    今日竖线 + 机会/危险区 + 年份竖线 + dataZoom。acc=forecast_acc 用于标题可信度标注。"""
    rows = [r for r in (hist or []) if isinstance(r, (list, tuple)) and len(r) >= 3]
    if len(rows) < 2:
        return ""
    x_hist = [r[0] for r in rows]
    y_hist = [None if r[2] is None else float(r[2]) for r in rows]
    today_x = x_hist[-1]
    fmed = (forecast or {}).get("median") or []
    fp25 = (forecast or {}).get("p25") or []
    fp75 = (forecast or {}).get("p75") or []
    fdates = (forecast or {}).get("dates") or []
    # R280: dates/median/p25/p75 等长对齐(R211 同族防御) —— 数据源异常(预测尾部 None 被省略)
    # 致 dates 长于各值序列时, 下方 fc_series/band_lo 将短于 xcats → ECharts 序列与类目错位;
    # fp25 若短于 median 还会在 band_lo 循环越界 IndexError(R205f 仅防了 fp75)。统一截断/补 None。
    _Hd = len(fdates)
    if _Hd:
        for _arr in (fmed, fp25, fp75):
            if len(_arr) > _Hd:
                del _arr[_Hd:]
            elif len(_arr) < _Hd:
                _arr.extend([None] * (_Hd - len(_arr)))
    H = len(fmed)
    x_fc = fdates
    xcats = x_hist + x_fc
    _wb = _sent_warm_band(y_hist, x_hist)
    _ma = [
        [{"yAxis": 0, "itemStyle": {"color": "#18a058", "opacity": 0.10}}, {"yAxis": buy_th}],
        [{"yAxis": sell_th, "itemStyle": {"color": "#e54545", "opacity": 0.10}}, {"yAxis": 100}],
    ] + _sent_warm_markarea(_wb)
    hist_series = y_hist + [None] * H
    fc_series = [None] * (len(x_hist) - 1) + [y_hist[-1]] + [None if fmed[i] is None else float(fmed[i]) for i in range(H)]
    # 预测置信带: 仅未来段; band_lo=p25 下界, band_hi=p75-p25 堆叠增量(面积填充至 p75)
    # R205f: fmed/fp25 未来段可能含 None(某日 KNN 邻居轨迹全缺失), 与 band_hi 一致做 None 守卫, 避免 float(None) 崩
    band_lo = [None] * len(x_hist) + [None if fp25[i] is None else float(fp25[i]) for i in range(H)]
    band_hi = [None] * len(x_hist) + [None if (i >= len(fp75) or fp75[i] is None or fp25[i] is None)
                                       else round(fp75[i] - fp25[i], 1) for i in range(H)]
    # 预测可信度信息已通过下方 .sent-acc 黄底行披露, 此处不再堆叠到标题(避免工具栏过长/窄屏换行拥挤)
    fdata = {
        "xcats": xcats, "yhist": hist_series, "yfc": fc_series,
        "band_lo": band_lo, "band_hi": band_hi,
        "buy_th": buy_th, "sell_th": sell_th, "today_x": today_x,
        "today_y": y_hist[-1],
        "yearLines": _year_lines(x_hist),
        "markAreaData": _ma,
        # R193: 默认缩放起点用绝对日期(历史末90日), 替代百分比 start:62, 数据长度变化不再漂移
        "zoomStart": x_hist[-90] if len(x_hist) > 90 else x_hist[0],
    }
    opt_js = (
        "function(D,chart){"
        "var yearML=(D.yearLines||[]).map(function(o){return {xAxis:o.x,"
        "lineStyle:{color:'#cbd5e1',type:'dashed',width:1},"
        "label:{show:false}};});"
        "yearML.unshift({xAxis:D.today_x,lineStyle:{color:'#334155',type:'dashed',width:1.5},"
        "label:{formatter:'今日',position:'insideEndBottom',color:'#475569',fontSize:10}});"
        "return {tooltip:{trigger:'axis',axisPointer:{type:'cross',label:{show:true,backgroundColor:'rgba(43,108,176,0.85)',color:'#fff',borderColor:'transparent',padding:[2,6],borderRadius:3,fontSize:11}}},"
        "legend:{data:['历史情绪','预测情绪'],top:2,textStyle:{fontSize:11}},"
        "grid:{left:46,right:16,top:48,bottom:66},"
        "xAxis:{type:'category',data:D.xcats,boundaryGap:false,"
        "axisPointer:{label:{show:true,backgroundColor:'rgba(43,108,176,0.85)',color:'#fff',borderColor:'transparent',padding:[2,6],borderRadius:3,fontSize:11}},"
        "axisLabel:{fontSize:10,autoHide:false,position:'bottom',interval:0,formatter:function(v,i){return _sFmt(v,i);}},axisTick:{show:false}},"
        "yAxis:{type:'value',min:0,max:100,axisPointer:{label:{show:true,backgroundColor:'rgba(43,108,176,0.85)',color:'#fff',borderColor:'transparent',padding:[2,6],borderRadius:3,fontSize:11}},axisLabel:{fontSize:11},splitLine:{lineStyle:{color:'#eef2f7'}}},"
        "dataZoom:[{type:'inside',xAxisIndex:0,startValue:D.zoomStart,end:100},"
        "{type:'slider',xAxisIndex:0,height:18,bottom:14,startValue:D.zoomStart,end:100,showDetail:false,"
        "handleStyle:{color:'#2b6cb0'},borderColor:'#e2e8f0',fillerColor:'rgba(43,108,176,0.12)',"
        "dataBackground:{lineStyle:{color:'#cbd5e1'},areaStyle:{color:'rgba(203,213,225,0.25)'}},"
        "selectedDataBackground:{lineStyle:{color:'#2b6cb0'},areaStyle:{color:'rgba(43,108,176,0.25)'}}}],"
        "series:[{name:'历史情绪',type:'line',data:D.yhist,symbol:'none',smooth:true,"
        "lineStyle:{color:'#2b6cb0',width:2},z:4,"
        "markArea:{silent:true,data:D.markAreaData},"
        "markLine:{symbol:'none',data:yearML}},"
        # 预测置信带(p25-p75, 逐日κ重标定至名义50%覆盖): 蓝系半透明面积, 与主体视觉一致(R191 移除黄带后本轮恢复)
        "{name:'预测区间下沿',type:'line',data:D.band_lo,stack:'bsent',symbol:'none',"
        "lineStyle:{opacity:0},areaStyle:{opacity:0},tooltip:{show:false},silent:true,z:3},"
        "{name:'预测区间',type:'line',data:D.band_hi,stack:'bsent',symbol:'none',"
        "lineStyle:{opacity:0},areaStyle:{color:'rgba(124,58,237,0.12)'},tooltip:{show:false},silent:true,z:3},"
        "{name:'预测情绪',type:'line',data:D.yfc,symbol:'none',smooth:true,"
        "lineStyle:{color:'#7c3aed',width:2,type:'dashed'},z:5,"
        # R226: 今日锚点圆点 + 数值标注, 显式把历史实线↔预测虚线的交接点压出来, 消除"断开"错觉
        "markPoint:{silent:true,symbol:'circle',symbolSize:9,"
        "itemStyle:{color:'#334155',borderColor:'#fff',borderWidth:2},"
        "label:{show:true,formatter:function(p){return '今日 '+D.today_y.toFixed(1);},position:'top',color:'#334155',fontSize:10,fontWeight:'bold'},"
        "data:[{coord:[D.today_x,D.today_y]}]}},"
        "]};}"
    )
    zone_cap = ('<div class="sent-zone-cap">'
                '<span class="zc zc-g">▾ 绿带=机会区(&lt;%.0f)</span>'
                '<span class="zc zc-r">▴ 红带=风险区(&gt;%.0f)</span>'
                '<span class="zc zc-w">▦ 灰带=数据预热期(样本不足, 非断线)</span>'
                '<span class="zc">蓝实线=历史情绪　紫虚线=未来KNN预测　紫色阴影=预测区间(p25–p75, κ 重标定)　● 今日锚点=历史↔预测交接点　年份/月份在 x 轴　·　滚轮拖拽缩放</span>'
                '</div>') % (buy_th, sell_th)
    return _sent_echart("echart-sent-main",
                        "📈 情绪走势与未来预测",
                        340, fdata, opt_js) + zone_cap



def _sent_index_chart(hist, forecast=None):
    """情绪 vs 上证指数(R193): 双 Y 轴, 纯历史近期切片(近180交易日), 不含预测空段, 独立展示不联动主图。
    对齐斐波那契看板 sentVsChart 逻辑: 副图只做近期历史对照, 无 dataZoom/无 connect, 从同一历史起点对齐,
    与主图(含预测)互不联动, 彻底消除 R192 预测段 None 造成的右端空白错配。"""
    rows = [r for r in (hist or []) if isinstance(r, (list, tuple)) and len(r) >= 3]
    if len(rows) < 2:
        return ""
    x_hist = [r[0] for r in rows]
    sc_hist = [None if r[2] is None else float(r[2]) for r in rows]
    cl_hist = [None if r[1] is None else float(r[1]) for r in rows]
    # R193: 纯历史近期切片(近180交易日), 不含预测空段 → 无右端空白; 独立展示, 不联动主图
    N = max(2, min(180, len(x_hist)))
    xs = x_hist[-N:]
    sc = sc_hist[-N:]
    cl = cl_hist[-N:]
    xcats = xs
    _wb = _sent_warm_band(sc, xcats)
    fdata = {"xcats": xcats, "sc": sc, "cl": cl, "yearLines": _year_lines(xcats),
             "markAreaData": _sent_warm_markarea(_wb)}
    opt_js = (
        "function(D,chart){"
        "var yearML=(D.yearLines||[]).map(function(o){return {xAxis:o.x,"
        "lineStyle:{color:'#cbd5e1',type:'dashed',width:1},"
        "label:{show:false}};});"
        "return {tooltip:{trigger:'axis',axisPointer:{type:'cross',label:{show:true,backgroundColor:'rgba(43,108,176,0.85)',color:'#fff',borderColor:'transparent',padding:[2,6],borderRadius:3,fontSize:11}}},"
        "legend:{data:['情绪温度','上证指数'],top:2,textStyle:{fontSize:11}},"
        "grid:{left:46,right:58,top:50,bottom:42},"
        "xAxis:{type:'category',data:D.xcats,boundaryGap:false,"
        "axisPointer:{label:{show:true,backgroundColor:'rgba(43,108,176,0.85)',color:'#fff',borderColor:'transparent',padding:[2,6],borderRadius:3,fontSize:11}},"
        "axisLabel:{fontSize:10,autoHide:false,position:'bottom',interval:0,formatter:function(v,i){return _sFmt(v,i);}},axisTick:{show:false}},"
        "yAxis:[{type:'value',min:0,max:100,position:'left',"
        "axisPointer:{label:{show:true,backgroundColor:'rgba(43,108,176,0.85)',color:'#fff',borderColor:'transparent',padding:[2,6],borderRadius:3,fontSize:11}},"
        "axisLabel:{fontSize:11,color:'#2b6cb0'},splitLine:{lineStyle:{color:'#eef2f7'}},name:'温度'},"
        "{type:'value',position:'right',scale:true,axisPointer:{label:{show:true,backgroundColor:'rgba(43,108,176,0.85)',color:'#fff',borderColor:'transparent',padding:[2,6],borderRadius:3,fontSize:11}},axisLabel:{fontSize:11,color:'#b45309'},name:'上证'}],"
        "series:[{name:'情绪温度',type:'line',data:D.sc,symbol:'none',smooth:true,"
        "lineStyle:{color:'#2b6cb0',width:2},yAxisIndex:0,markArea:{silent:true,data:D.markAreaData},markLine:{symbol:'none',data:yearML}},"
        "{name:'上证指数',type:'line',data:D.cl,symbol:'none',smooth:true,"
        "lineStyle:{color:'#b45309',width:1.6},yAxisIndex:1}]};}"
    )
    cap = ('<div class="sent-zone-cap">'
           '<span class="zc zc-w">▦ 灰带=数据预热期(样本不足, 非断线)</span>'
           '<span class="zc">蓝=情绪温度　棕=上证指数　纯历史近180日切片·独立展示（不与主图联动）</span>'
           '</div>')
    return _sent_echart("echart-sent-index",
                        "🔗 情绪温度 vs 上证指数",
                        300, fdata, opt_js) + cap


def load_other_fib_nodes(base=None):
    """R204 跨项目锚点对照：读取另一斐波那契项目(A-share-Fibonacci)的波浪节点
    （含 date/price/lo/hi/label/side）。

    数据源优先级：
      1) 本仓库内置快照 sentiment/other_fib_nodes.json —— 线上 CI 稳定可用，
         该文件由 R204b 从另一项目 data.js 导出（另一项目为人工维护买/卖点，
         非每日变动，内置快照合理）；
      2) 同机另一项目 data.js（本机实时热更新兜底）。

    路径不可达或解析失败 -> 返回 None（调用方静默降级，不阻断报告）。
    仅读取、不参与任何预测数学（合规 R76）。"""
    import re, json as _json
    candidates_json = []
    candidates_src = []
    if base:
        # base 为本工程 chanlun 目录，内置快照在 sentiment/ 下
        candidates_json.append(os.path.join(base, "sentiment", "other_fib_nodes.json"))
        # 同机兄弟项目（本机实时兜底）
        _parent = os.path.dirname(base)
        candidates_src.append(os.path.join(_parent, "A-share-Fibonacci", "data", "data.js"))
    # 绝对兜底路径（本机固定布局）
    candidates_src.append("C:/Users/Administrator/WorkBuddy/2026-08-04-23-16-18/A-share-Fibonacci/data/data.js")
    # 1) 优先内置 JSON 快照（线上 CI 可用）
    for _p in candidates_json:
        if _p and os.path.exists(_p):
            try:
                _meta = _json.load(open(_p, encoding="utf-8"))
                _nodes = _meta.get("nodes") if isinstance(_meta, dict) else None
                if _nodes:
                    _out = []
                    for p in _nodes:
                        try:
                            _out.append({
                                "date": str(p.get("date")),
                                "price": float(p.get("price")),
                                "lo": float(p.get("lo")),
                                "hi": float(p.get("hi")),
                                "label": str(p.get("label")),
                                "side": str(p.get("side")),
                            })
                        except (TypeError, ValueError):
                            continue
                    return _out if _out else None
            except Exception:
                pass
    # 2) 兜底：同机 data.js 实时解析
    src = None
    for _p in candidates_src:
        if _p and os.path.exists(_p):
            try:
                src = open(_p, encoding="utf-8").read()
            except Exception:
                src = None
            if src:
                break
    if not src:
        return None
    try:
        start = src.find('"subForecast"')
        if start < 0:
            return None
        seg = src[start:start + 8000]
        k = seg.find('"points"')
        if k < 0:
            return None
        sub = seg[k:]
        i = sub.find('[')
        if i < 0:
            return None
        depth = 0
        end = -1
        for j in range(i, len(sub)):
            c = sub[j]
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:
            return None
        arr = ast.literal_eval(sub[i:end + 1])
        nodes = []
        for p in arr:
            if not isinstance(p, dict):
                continue
            try:
                nodes.append({
                    "date": str(p.get("date")),
                    "price": float(p.get("price")),
                    "lo": float(p.get("lo")),
                    "hi": float(p.get("hi")),
                    "label": str(p.get("label")),
                    "side": str(p.get("side")),
                })
            except (TypeError, ValueError):
                continue
        return nodes if nodes else None
    except Exception:
        return None


def sentiment_board_html(base, data, results, results_week, scores, last_date,
                          idx_proj=None, idx_last=None):
    """R178 市场情绪板块：温度计头图 + 维度拆解表 + 情绪走势与未来预测主图 + 情绪vs上证指数副图。
    删除旧版的仪表盘/六因子卡/弹性共振/互联矩阵/历史信号/背离记录，仅保留截图所示核心结构。
    情绪数据缺失时降级为提示条（不阻断主报告）。"""
    sent = load_sentiment_full()
    if not sent:
        return ('<section class="panel" id="sentiment-board">'
                '<h2 class="sec" id="s2">二、市场情绪 <span class="badge" style="background:#94a3b8">数据未生成</span></h2>'
                '<div class="verdict"><b>情绪板块：</b><p>未找到 sentiment/sentiment_v2.json'
                '（运行 <code>python sentiment/calc_v2.py</code> 或等待 CI 生成后可见），其余模块不受影响。</p></div>'
                '</section>')
    try:
        # R346: score 读顶层键并 clamp 兜底(calc_v2 R328 后顶层 score 仍可能 raw 超界旧产物) ——
        # zone 判定序保持(阈值 20/85 远离 clamp 边界), 且 KPI/徽章数字与 final/gauge 同标尺不分裂
        score = float(max(0.0, min(100.0, sent.get("score", 50))))
        final = float(sent.get("final", score))
        buy_th = float(sent.get("buy_th", 20))
        sell_th = float(sent.get("sell_th", 85))
        asof = str(sent.get("asof", "—"))
        # R230: 情绪数据滞后诚实标注——对比主行情末日(last_date)与情绪 asof,
        # 滞后≥1日即红标提示(根因: 东财情绪源在云端 CI 被限流, calc_v2 静默回退到已提交旧快照)。
        # R395: 滞后口径由「自然日」改为「交易日」——A股周末/法定假日休市无新数据, 自然日差
        # 会把跨周末场景放大误报(实证: 情绪 asof 周五 + 行情 last 周一 = 自然日 3 却只滞后
        # 1 个交易日, 每周一首次报告必误弹「滞后 3 日」红标)。与 main() 新鲜度护栏(R55/R247
        # _is_trading_day 口径)对齐; asof 晚于 last_date(情绪比行情新)时计数为 0 不提示。
        _lag = None
        try:
            _d0 = datetime.strptime(asof, "%Y-%m-%d").date()
            _d1 = datetime.strptime(last_date, "%Y-%m-%d").date()
            _lag = sum(1 for _i in range(1, (_d1 - _d0).days + 1)
                       if _is_trading_day(_d0 + timedelta(days=_i)))
        except Exception:
            _lag = None
        _stale_badge = ('<span class="badge" style="background:#dc2626;color:#fff">'
                        '⚠️ 情绪数据滞后 %d 日（东财行情源在云端 CI 被限流，沿用 %s 成功快照）</span>'
                        % (_lag, asof)) if (_lag is not None and _lag >= 1) else ""
        _lag_note = ('；<b style="color:#dc2626">当前情绪数据滞后 %d 日</b>——东财在云端 CI 被限流，'
                     '需在本机运行 <code>python fetch_data.py &amp;&amp; python sentiment/calc_v2.py</code> '
                     '后推送刷新' % _lag) if (_lag is not None and _lag >= 1) else ""
        # R232: 数据模式诚实标注。CI/沙箱东财被限流时走腾讯 gtimg 回退, 成交额/换手率为
        # OHLCV 派生代理(指数 成交额≈成交量×指数点位, 滚动分位/比值 scale-invariant, 模型零改动)。
        _mode_badge = ""
        _mode_note = ""
        try:
            _mode = open(os.path.join(base, "sentiment", ".sent_mode"), encoding="utf-8").read().strip()
            if _mode == "tencent_proxy":
                _mode_badge = ('<span class="badge" style="background:#0891b2;color:#fff">'
                               '数据源: 腾讯回退(成交额/换手率为 OHLCV 派生代理)</span>')
                _mode_note = ('；<b style="color:#0891b2">当前情绪走腾讯 gtimg 回退</b>——东财在云端 CI 被限流，'
                              '成交额/换手率由成交量×指数点位派生(模型权重/阈值不变, 精度门禁已验证), 仅供研判参考')
            elif _mode == "em":
                # R233: 东财在 CI 直达(海外 runner 可出网至东财 https 端点)时也要常驻标注,
                # 否则用户无从确认面板已刷新且数据来自全量源, 反复误判"没更新"。
                _mode_badge = ('<span class="badge" style="background:#16a34a;color:#fff">'
                               '数据源: 东财全量(含成交额/换手率)</span>')
            else:
                _mode_badge = ('<span class="badge" style="background:#d97706;color:#fff">'
                               '数据源: 未知(.sent_mode=%s)</span>' % _mode)
        except Exception:
            _mode_badge = ('<span class="badge" style="background:#d97706;color:#fff">'
                           '数据源: 未知(读取 .sent_mode 失败)</span>')
        ma5s = sent.get("ma5s")
        ma20s = sent.get("ma20s")
        ma5s = None if ma5s is None else float(ma5s)
        ma20s = None if ma20s is None else float(ma20s)
        # R349: zone 判据必须与板块内显示数字同轨 —— 徽章(L2766)/温度计指针/档位刻度全用
        # final, 若 zone 按 score 判, 当 adj≠0 使 final 跨过 buy_th(如 09-07 CI: score=19.2
        # 判 fear, 但 final=clamp(19.2+1.3)=20.5>20)会出现"恐惧 20.5 分"绿底机会区 + 指针越界
        # 的自相矛盾(数字已出恐惧区标签仍在机会区)。final 恒存在(L2592 兜底=score), 改判 final
        # 后 zone/数字/指针/刻度四者全一致。main KPI 与 R76 提示保持 score 轨(读数语义, 自洽)。
        zone, zlabel, zcolor = _sent_zone(final, buy_th, sell_th)
        final_pct = sent.get("final_pct")
        final_pct = None if not isinstance(final_pct, (int, float)) else float(final_pct)
        hist = sent.get("hist") or []
        forecast = sent.get("forecast")
        acc = sent.get("forecast_acc")
        scores = [float(r[2]) for r in hist if isinstance(r, (list, tuple)) and len(r) >= 3 and r[2] is not None]
        header = _sent_thermometer_html(final, zone, zlabel, zcolor, buy_th, sell_th, final_pct, ma5s, ma20s, scores)
        main_chart = _sent_main_chart(forecast, hist, buy_th, sell_th, acc)
        index_chart = _sent_index_chart(hist, forecast)
        acc_html = ""
        if isinstance(acc, dict):
            by_zone = acc.get("by_zone") or {}
            zone_line = ""
            if by_zone:
                def _ztag(zk):
                    z = by_zone.get(zk)
                    if not z:
                        return ""
                    zname = {"fear": "恐惧区", "greed": "贪婪区", "neutral": "中性区"}[zk]
                    return ('<span class="zc">%s(n=%d): 方向 <b>%.0f%%</b> · 覆盖 <b>%.0f%%</b></span>'
                            % (zname, z.get("n", 0), z.get("dir_acc", 0), z.get("cov", 0)))
                zone_line = ('<div class="sent-acc sent-acc-sub">分情绪区回测（透明化，提示极端区更难测）：'
                             + _ztag("fear") + _ztag("neutral") + _ztag("greed") + '</div>')
            acc_html = ('<div class="sent-acc">预测可信度（walk-forward 样本外回测 {n} 锚点）：'
                        '预测带覆盖率 <b>{cov:.1f}%</b> · 方向命中 <b>{d:.1f}%</b> · 平均误差 <b>{m:.1f}</b> 分'
                        '（0–100 标尺，30 日 horizon；阴影带经逐日 κ 重标定至名义 50% 覆盖，近紧远宽）。</div>'
                        + zone_line).format(
                n=acc.get("n", 0), cov=acc.get("cov", 0),
                d=acc.get("dir_acc", 0), m=acc.get("mae", 0))
        # R203: 情绪×指数背离提示(展示层, 不并入预测数学, 合规 R76)
        # 触发: 情绪预测未来窗口内任一日处于恐惧区(<buy_th) 且 同期上证推演主情景(main)仍 > 当前价(idx_last)
        diverge_html = ""
        if isinstance(forecast, dict) and idx_proj and idx_last is not None:
            _fdates = forecast.get("dates") or []
            _fmed = forecast.get("median") or []
            # 建 日期->main 映射(上证推演按 tplus 日期)
            _idx_main = {p.get("date"): p.get("main") for p in idx_proj if isinstance(p, dict)}
            _hit = None
            for _j in range(min(len(_fdates), len(_fmed))):
                _dt, _mv = _fdates[_j], _fmed[_j]
                if _mv is None:
                    continue
                _im = _idx_main.get(_dt)
                if _im is not None and _mv < buy_th and _im > idx_last:
                    _hit = (_dt, _mv, _im)
                    break
            if _hit:
                _dt, _mv, _im = _hit
                _p25 = forecast.get("p25") or []
                _p75 = forecast.get("p75") or []
                _p25v = _p25[_j] if _j < len(_p25) else None
                _p75v = _p75[_j] if _j < len(_p75) else None
                _band = ""
                if _p25v is not None and _p75v is not None:
                    _band = "（p25–p75 区间 %.0f–%.0f，外推远端不确定性大）" % (_p25v, _p75v)
                diverge_html = (
                    '<div class="sent-acc sent-diverge">'
                    '⚠ <b>情绪×指数背离</b>：未来 <b>%s</b> 情绪预测处于冰点（<b>%.1f</b> 分，&lt;%.0f 恐惧区）'
                    '，但同期上证推演主情景仍上行至 <b>%.0f</b>（当前 %.0f）——'
                    '背离非"指数真强"，而是价格维度弱上行与情绪冰点并存；主情景非确定值（另含 alt/risk 更低情景），'
                    '谨慎追高、逆向关注恐慌区机会%s。</div>'
                    % (_dt, _mv, buy_th, _im, idx_last, _band))
        # R204: 跨项目锚点对照(展示层, 不并入预测数学, 合规 R76)
        # 触发: 本工程上证推演主价(idx_proj main, 点位量纲) 与另一项目波浪节点目标价(price)最接近者 -> 核心锚点对照
        # 注: 情绪 forecast.median 为 0-100 情绪分, 与波浪节点点位量纲不同, 不可直接比; 必须用上证点位维度
        anchor_html = ""
        if idx_proj:
            _onodes = load_other_fib_nodes(base)
            if _onodes:
                _idx_main = {p.get("date"): p.get("main") for p in idx_proj if isinstance(p, dict)}
                if _idx_main:
                    # 对每个波浪节点, 在本工程推演主价中找与该节点目标价(price)最接近的那一天作代表锚点
                    _pairs = []
                    for _n in _onodes:
                        _best = None
                        for _dt, _mv in _idx_main.items():
                            if _mv is None:
                                continue
                            _d = abs(_mv - _n["price"])
                            if _best is None or _d < _best[0]:
                                _best = (_d, _dt, _mv)
                        if _best:
                            _pairs.append((_best[1], _best[2], _n, _best[0]))
                    # 仅保留"带内"或"足够接近(距目标价<=节点半带宽)"的代表锚点, 按本工程日期序
                    _keep = []
                    for _dt, _mv, _n, _d in _pairs:
                        _half = max(1.0, (_n["hi"] - _n["lo"]) / 2.0)
                        if _n["lo"] <= _mv <= _n["hi"] or _d <= _half:
                            _keep.append((_dt, _mv, _n))
                    _keep.sort(key=lambda x: x[0])
                    if _keep:
                        _parts = []
                        _n_in = 0
                        for _dt, _mv, _n in _keep:
                            _in_band = _n["lo"] <= _mv <= _n["hi"]
                            if _in_band:
                                _n_in += 1
                            _side_tag = {"buy": "买点", "sell": "卖点", "hold": "持有"}.get(_n["side"], "")
                            # 标注本工程主价与该波浪节点目标价的偏差
                            _dev = _mv - _n["price"]
                            _dev_s = "（偏离目标 %.0f）" % _dev if abs(_dev) >= 1 else ""
                            _band_s = "（带内）" if _in_band else "（接近·带外）"
                            # 双日期显式标注: 本工程推演日(_dt) vs 波浪节点自身日期(_n["date"])
                            # 两法时间轴独立, 以"点位吻合"为锚, 不暗示同一天
                            _parts.append("本工程主价 <b>%.0f</b>（本工程 %s）≈ 波浪%s目标 <b>%.0f</b>（波浪 %s）%s%s%s"
                                          % (_mv, _dt, _n["label"], _n["price"], _n["date"],
                                             _dev_s, _band_s,
                                             "（%s）" % _side_tag if _side_tag else ""))
                        _detail = "；".join(_parts)
                        # 诚实措辞: 区分带内/带外接近, 不统一宣称"均落入带内"
                        if _n_in == len(_keep):
                            _band_note = "上述本工程主价均落入对应波浪节点置信带（两法时间轴独立，以点位吻合为锚，非同一天）"
                        elif _n_in == 0:
                            _band_note = "上述本工程主价均接近（带外）对应波浪节点目标价（两法时间轴独立，以点位吻合为锚，非同一天）"
                        else:
                            _band_note = "其中 %d/%d 个本工程主价落入对应波浪节点置信带，其余接近（带外）；两法时间轴独立，以点位吻合为锚，非同一天" % (_n_in, len(_keep))
                        # 定量边界说明: 锚点仅在点位维度交叉参照, 时间轴不可比
                        _boundary = ("锚点仅在点位维度交叉参照（两法目标价位落入同一置信带即视为价格共振）；"
                                     "时间轴各自独立、不可比，请勿据日期差推断到达先后或方法对错。")
                        anchor_html = (
                            '<div class="sent-acc sent-anchor">'
                            '🔗 <b>跨项目锚点对照</b>：本工程上证推演主价与另一斐波那契项目（艾略特波浪+斐波那契比率）节点目标对照——%s。'
                            '两法维度不同（统计路径外推 vs 波浪子浪比率）、时间轴各自独立，%s，共识非互相验证、仅供交叉参照。'
                            '<br><span style="opacity:.82">ⓘ %s</span></div>'
                            % (_detail, _band_note, _boundary))

        return f"""
    <section class="panel" id="sentiment-board" style="border-left:4px solid {zcolor}; --sent-accent:{zcolor};">
      <h2 class="sec" id="s2">二、市场情绪
        <span class="badge" style="background:{zcolor}">{zlabel} {final:.1f} 分</span>
        <span class="badge" style="background:#64748b">asof {asof}</span>{_stale_badge}{_mode_badge}
      </h2>
      <p class="sent-asof" style="font-size:12px;color:#64748b;margin:2px 0 12px;line-height:1.7">
        情绪数据由仓库内置 <code>sentiment/calc_v2.py</code> 基于提交的指数日K计算（asof <b>{asof}</b>），
        更新节奏独立于行情数据（截至 {last_date}）{_lag_note}{_mode_note}；情绪仅作环境参考，不进入推演数学（R76 监控中）。</p>
      {header}
      <div class="sent-chart-card">{main_chart}</div>
      {acc_html}
      {diverge_html}
      {anchor_html}
      <div class="sent-chart-card">{index_chart}</div>
      <p class="sent-footnote">代理情绪温度（monitor_only）：由上证量能/动量/波动/牛熊位置 + 宽基与跨市场广度合成，非全市场涨跌家数；量能维度受数据源 volume 单位差异影响，仅供研判参考，不参与任何概率/方向计算。history 为全部可用交易日逐日回算；forecast 由 KNN 历史轨迹派生（k=15/ctx=15 等权全局，经 walk-forward 回测反选），紫色虚线为未来预测中值（κ 重标定，样本外实测覆盖率≈名义 50% 的 p25–p75 区间）；预测为路径派生，非因子预测，仅供参考。主图含预测可缩放联动自身历史/预测；副图（情绪 vs 上证）为纯历史近180日独立切片，不与主图联动。</p>
    </section>"""
    except Exception as _e:
        print("WARN 情绪板块渲染降级: %s" % _e)
        return ('<section class="panel" id="sentiment-board">'
                '<h2 class="sec" id="s2">二、市场情绪 <span class="badge" style="background:#94a3b8">渲染降级</span></h2>'
                '<div class="verdict"><b>情绪板块：</b><p>情绪数据解析异常（已降级占位，不影响其余模块）。</p></div>'
                '</section>')


def build_quality_cert_html(base):
    """R79 预测质量自检证书：读 quality_cert.json 渲染顶部常驻区块（让预测准确性可见可验）。
    文件缺失(CI 未生成/本地未跑)时优雅降级为提示条, 不报错不阻断。"""
    p = os.path.join(base, "quality_cert.json")
    if not os.path.exists(p):
        return ('<div class="disclaimer" style="background:#f8fafc;border-color:#e2e8f0;'
                'color:#64748b;margin:14px 0">ℹ️ 预测质量自检证书未生成'
                '（运行 <code>python gen_quality_cert.py</code> 或等待 CI 部署后可见）。</div>')
    try:
        c = json.load(open(p, encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(c, dict):
        return ""
    # R395: 渲染段防崩 —— 证书是顶部辅助区块(R79), 任何结构异常都应降级而非阻断整份报告
    # (R173/R366 单模块降级纪律)。旧代码只 try 了 json.load, 渲染段裸跑: calibration/
    # regime_coverage 等键值为 None/非 dict 时 cal.get/regime_cov.get 链 AttributeError 崩
    # 整报告。逐层消毒: 非 dict 桶置空 dict -> 下方 _dc.get/_t8c.get/cell() 天然跳过,
    # 非法文本键置空串 -> 不渲染 "None" 字样。
    for _k in ("calibration", "regime_coverage", "regime_direction", "drift", "sentiment"):
        if not isinstance(c.get(_k), dict):
            c[_k] = {}
    for _k in ("regime_coverage", "regime_direction"):
        for _b in ("bull", "bear", "range"):
            _bb = c[_k].get(_b)
            if not isinstance(_bb, dict):
                c[_k][_b] = {}
            else:
                for _h in ("T8", "T30"):
                    if not isinstance(_bb.get(_h), dict):
                        _bb[_h] = {}
    for _k in ("bias_ok", "regime_warn", "regime_dir_warn"):
        if not isinstance(c.get(_k), bool):
            c[_k] = {"bias_ok": True}.get(_k, False)  # bias_ok 缺省 True(L2867 原语义); warn 类缺省 False 不误弹告警
    for _k in ("accuracy_note", "regime_note", "generated_at", "data_last_date"):
        if not isinstance(c.get(_k), str):
            c[_k] = ""
    cal = c.get("calibration", {})
    t8, t30 = cal.get("T8", {}), cal.get("T30", {})

    def cell(label, d, key):
        v = d.get(key)
        return (f'<div class="qc-cell"><div class="qc-v">{v if v is not None else "-"}</div>'
                f'<div class="qc-l">{label}</div></div>')
    bias_ok = c.get("bias_ok", True)
    bias_val = c.get("bias_worst", t8.get("bias_median"))  # 优先 worst(与 bias_ok 同口径), 老证书回退 T8
    drift = c.get("drift", {}).get("note", "")
    sent = c.get("sentiment", {}).get("note", "")
    acc = c.get("accuracy_note", "")
    # R173: R172 把 accuracy_status 由写死改为派生, 但此前报告从不读它 → 派生结果在看板不可见。
    # 此处渲染为顶部徽标, 让"预测准确性自检结论"真正对用户可见可验。
    acc_status = c.get("accuracy_status", "healthy")
    _acc_color = {"healthy": "#0891b2", "review": "#d97706", "warn": "#dc2626"}.get(acc_status, "#0891b2")
    warn_cls = "" if bias_ok else " warn"
    # R208: 老证书可能缺 bias_worst 且 T8 无 bias_median -> bias_val=None,
    # 原 {bias_val}% 会渲染 "None%" 不专业输出; 与 cell() 对齐, None 显示 "-"。
    _bias_disp = ("%.2f%%" % bias_val) if isinstance(bias_val, (int, float)) else "-"
    bias_cell = (f'<div class="qc-cell{warn_cls}"><div class="qc-v">{_bias_disp}</div>'
                 f'<div class="qc-l">中线偏置(中位)</div></div>')
    # R80/R81 分 regime 覆盖 + 方向块：覆盖低 ≠ 方向错——两者并排切片,
    # 暴露「全样本平均」掩盖的弱点(如熊市常见『方向看空可信 / 区间太窄破带』)。
    regime_cov = c.get("regime_coverage", {})
    regime_dir = c.get("regime_direction", {})
    regime_warn = c.get("regime_warn", False) or c.get("regime_dir_warn", False)
    regime_note = c.get("regime_note", "")
    _foot_regime = (f'🔍 <b>分regime告警</b>: {regime_note}<br>' if regime_note else "")
    _rg_rows = ""
    if regime_cov:
        _rg_rows = ('<div class="qc-regime"><div class="qc-regime-h">'
                    '📈 分市场环境（牛/熊/震荡）：覆盖=区间可信度，方向=方向可信度 — 暴露「全样本平均」掩盖的弱点</div>')
        for _rg, _lab in (("bull", "牛"), ("bear", "熊"), ("range", "震荡")):
            _dc = regime_cov.get(_rg, {})
            _dd = regime_dir.get(_rg, {})
            _t8c, _t30c = _dc.get("T8", {}), _dc.get("T30", {})
            _t8d, _t30d = _dd.get("T8", {}), _dd.get("T30", {})
            _c8, _c30 = _t8c.get("cover95"), _t30c.get("cover95")
            _d8, _d30 = _t8d.get("dir_main"), _t30d.get("dir_main")
            _n8, _n30 = _t8c.get("N", 0), _t30c.get("N", 0)
            _bad8 = (_c8 is not None and _c8 < 85)
            _bad30 = (_c30 is not None and _c30 < 85)
            _d8bad = (_d8 is not None and _n8 >= 20 and _d8 < 50)  # 方向<抛硬币且样本足=方向不可信
            _d30bad = (_d30 is not None and _n30 >= 20 and _d30 < 50)
            _ins8 = (_n8 is not None and _n8 < 20)   # 样本不足(N<20): 统计不可靠
            _ins30 = (_n30 is not None and _n30 < 20)
            _cls8 = " bad" if _bad8 else ""
            _cls30 = " bad" if _bad30 else ""
            _dcls8 = " bad" if _d8bad else ""
            _dcls30 = " bad" if _d30bad else ""
            _v = lambda x: ("%.1f%%" % x) if isinstance(x, (int, float)) else "-"
            _t8_txt = f'覆盖{_v(_c8)}' + (' ⚠️样本不足' if _ins8 else '')
            _t30_txt = f'覆盖{_v(_c30)}' + (' ⚠️样本不足' if _ins30 else '')
            _t8d_txt = f'方向{_v(_d8)}' + (' ⚠️样本不足' if _ins8 else '')
            _t30d_txt = f'方向{_v(_d30)}' + (' ⚠️样本不足' if _ins30 else '')
            _rg_rows += (f'<div class="qc-regime-row"><span class="qc-regime-lab">{_lab}</span>'
                         f'<span class="qc-regime-cov{_cls8}">T+8 {_t8_txt}</span>'
                         f'<span class="qc-regime-cov{_cls30}">T+30 {_t30_txt}</span>'
                         f'<span class="qc-regime-cov{_dcls8}">T+8 {_t8d_txt}</span>'
                         f'<span class="qc-regime-cov{_dcls30}">T+30 {_t30d_txt}</span></div>')
        _rg_rows += '</div>'
    # R371: generated_at 为 UTC ISO(CI GitHub Actions 环境), 直渲染会让北京用户看到
    # "2026-09-08T02:45:21Z"——凌晨 0-7 点 CI 生成时 UTC 日期早于北京时间一天, 证书
    # 会被误读为"昨天生成"的旧证书。消费端转北京时间(UTC+8)再显示, 写盘端保持 UTC 机器可读。
    _ga_raw = c.get("generated_at") or "-"
    _ga = _ga_raw
    if isinstance(_ga_raw, str) and _ga_raw.endswith("Z"):
        try:
            _ga = (datetime.fromisoformat(_ga_raw.replace("Z", "+00:00"))
                   .astimezone(timezone(timedelta(hours=8)))
                   .strftime("%Y-%m-%d %H:%M")) + "（北京时间）"
        except ValueError:  # 非标准 ISO(老证书/异常值) 原样显示, 不加时区后缀避免误导
            _ga = _ga_raw
    html = (
        f'<div class="qc-card{" warn" if regime_warn else ""}">'
        f'<div class="qc-head">📊 预测质量自检证书'
        f' <span style="color:{_acc_color};font-weight:700">[{acc_status}]</span>'
        f'{" ⚠️" if regime_warn else ""}'
        f'<span class="qc-sub">数据截至 {c.get("data_last_date") or "-"} · 生成 {_ga}</span></div>'
        + '<div class="qc-grid">'
        + cell("P05-P95 覆盖 T+8", t8, "cover95") + cell("P05-P95 覆盖 T+30", t30, "cover95")
        + cell("方向命中 T+8", t8, "dir_main") + cell("方向命中 T+30", t30, "dir_main")
        + cell("中线 MAE T+30", t30, "mae_med") + bias_cell
        + '</div>'
        + _rg_rows
        + f'<div class="qc-foot">⚙️ <b>漂移监控</b>: {drift}<br>'
        + f'🧭 <b>情绪条件化</b>: {sent}<br>'
        + _foot_regime
        + f'📌 <b>结论</b>: {acc}</div></div>'
    )
    return html


def main():
    _base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_base, "data.json"), encoding="utf-8") as f:
        data = json.load(f)
    if not data:
        raise SystemExit("data.json 为空：无指数数据，无法生成报告（上游数据管线可能失败）")

    results = {sym: analyze(d["klines"]) for sym, d in data.items()}
    results_week = {sym: analyze(d["week_klines"], MIN_BI_PCT_WEEK) for sym, d in data.items()}
    results_month = {sym: analyze(d["month_klines"], MIN_BI_PCT_MONTH) for sym, d in data.items()}
    # R478: 段级信号并入同一 dict（键为 kind，段级 kind 是 段一买/段二买… ⇒ 不与笔级撞键），
    # 由 backtest_table 的 KINDS 决定展示顺序与分组 ⇒ 表里笔级/段级各成行、各算各的胜率。
    backtests = {}
    for sym, d in data.items():
        _bt = dict(backtest_signals(d["klines"], results[sym], exclude_last=True))
        _bt.update(backtest_signals(d["klines"], results[sym], exclude_last=True,
                                    signals=results[sym].get("seg_signals") or []))
        backtests[sym] = _bt
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
    # R156: 取所有指数末根日期的最大值(而非首个symbol), 与 gen_quality_cert 口径一致——
    # 各指数末根可能差 1 个交易日, 取首个会令数据新鲜度护栏锚定偏早日期、误报滞后。
    last_date = max(d["meta"]["last_date"] for d in data.values())
    # 数据新鲜度护栏：推演完全基于截至 last_date 的行情，若严重滞后应醒目预警，
    # 避免用户拿过期数据得出的预测当作当下结论（过期行情→结构/概率全失真）。
    _last_d = datetime.strptime(last_date, "%Y-%m-%d").date()
    # R422: "今天"必须取**北京时间** —— GitHub runner 本地时区是 UTC(与 fetch_data.py L219
    # 注释同源的坑, 那边已修、此处漏改), 裸 datetime.now() 在北京 00:00~08:00 区间会取到
    # **前一天**的日期(该时段 UTC 仍停在昨日), 使 _gap_days 少 1 天、_gap_td 随之少算 1 个
    # 交易日。实证(2026 年看门狗 261 个真实调度点 × 前 1~20 个 last_date = 3435 组):
    # 74% 的组合 gap_td 被低估, 16.5% 连告警等级都被降级 —— 例: 真实「滞后 3 个交易日
    # [轻度]」被判成 2 个 → **完全不告警**(阈值 >2); 真实「11 日[严重]」→「10 日[中度]」。
    # 该数字由下方 L3034「已滞后约 N 个交易日」直接展示给用户, 错的正是它。
    # 口径与同文件 L3103(gen_time) / L2961(证书时间) 统一为 UTC+8, 消除时区口径分裂。
    _today = datetime.now(timezone(timedelta(hours=8))).date()
    _gap_days = (_today - _last_d).days
    # 滞后交易日：复用 R55 的 A 股交易日历 _is_trading_day（跳过周末+法定假期、保留补班），
    # 避免把国庆/春节等长假的休市日误算为「滞后交易日」（此前 weekday()<5 会在休市期误报
    # 「数据滞后」，其实休市无交易、数据即最新）。这是与 _is_trading_day 口径分裂的回归修复。
    _gap_td = sum(1 for i in range(1, _gap_days + 1)
                  if _is_trading_day(_last_d + timedelta(days=i)))
    if _gap_td > 2:
        # 分级预警：轻度(3~5) / 中度(6~10) / 严重(>10，CI 很可能已失败停更)
        if _gap_td <= 5:
            _sev, _col, _bg, _bd, _msg = "轻度", "#a0701f", "#fff8ec", "#f0d9a0", "结论可能略滞后"
        elif _gap_td <= 10:
            _sev, _col, _bg, _bd, _msg = "中度", "#a05020", "#fff2e8", "#f0b890", "结论或已失真，建议重新生成"
        else:
            _sev, _col, _bg, _bd, _msg = ("严重", "#a03030", "#fff0f0", "#f0a0a0",
                                          "数据疑似停更（CI 可能失败），结论很可能已严重失真")
        freshness_banner = (
            f'<div class="disclaimer" style="border-color:{_bd};background:{_bg};color:{_col};'
            f'border-radius:10px;padding:12px 18px;font-size:13px;line-height:1.8;margin:14px 0">'
            f'⚠️ <b>数据滞后预警（{_sev}）</b>：当前行情数据截至 <b>{last_date}</b>，已滞后约 '
            f'<b>{_gap_td}</b> 个交易日。本报告的结构识别与推演概率均基于旧行情，{_msg}，'
            f'请以最新行情重新生成后为准。</div>')
    else:
        freshness_banner = ""
    # 预测质量自检证书(R79): 读 quality_cert.json 渲染顶部常驻区块, 让准确性可见可验
    cert_html = build_quality_cert_html(_base)
    # R177: 情绪板块数据(完整版)提前加载, 供面板徽章 / 情绪×结构互联矩阵 / 情绪板块使用;
    # 不可用时 sent_full=None, 各消费点优雅降级(不阻断主报告)。
    sent_full = load_sentiment_full()
    _sz, _szl, _szc = "neutral", "中性", GRAY
    _s_score = None
    if sent_full:
        try:
            # R346: clamp 兜底 —— 与情绪板块/final/gauge 同 0-100 标尺(顶层 score 旧产物可能 raw 超界)
            _s_score = float(max(0.0, min(100.0, sent_full.get("score", 50))))
            _sz, _szl, _szc = _sent_zone(_s_score,
                                         float(sent_full.get("buy_th", 20)),
                                         float(sent_full.get("sell_th", 85)))
        except (TypeError, ValueError):
            _sz, _szl, _szc = "neutral", "中性", GRAY
    # 净极性：全看多 +8 / 全看空 -8（0-100 置信度刻度）。
    # 旧式 (bull/total - 0.5) * 16 在存在中性情景(震荡待方向/无中枢笔/数据不足，classify 可能返回
    # 且不在 _SC_BULL/_SC_BEAR 内)时，会把"多数震荡"错误压成偏空——数学上 = 净极性 - 8*neutral/total，
    # 致推演置信度被误惩罚。改用 (bull - bear) / total * 8，中性情景正确计入"非多非空"，不污染极性。
    _breadth_bias = (_bull_cnt - _bear_cnt) / _total * 8
    # 关键修复(R136)：在「算 scores / 画 card / 推演」之前，统一用周线 classify 重算日线 classify，
    # 使 interval_nesting / ma_alignment 等字段在 health_score / forecast_confidence / forecast_svg
    # 全链路口径一致。此前 scores 在重算之前计算，导致 health/conf 不含 nest、与 p_main 的 nest
    # 增益(+0.03)分裂；且卡片「均线排列」与交叉验证惩罚逻辑所用 classify 与推演所用不一致。
    for sym, d in data.items():
        _r = results[sym]
        _old_cls = _r["classify"]
        _r["classify"] = classify(_r["bis"], _r["zhongshu"], _r["beichi"],
                                  d["klines"][-1]["close"], results_week[sym]["classify"],
                                  _r["segments"], _r["seg_beichi"], results_month[sym]["classify"])
        # classify() 返回字典不含 ma_alignment（均线排列由 analyze 单独计算），重算会丢弃它——
        # 回写以恢复卡片「均线排列」显示与 health/forecast 的「均线多空排列交叉验证」惩罚逻辑。
        _r["classify"]["ma_alignment"] = _old_cls.get("ma_alignment")
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
                      f'<h4 style="font-size:15px;color:{BLUE};margin-bottom:10px">跨指数市场广度综合研判 '
                      f'<span style="font-size:12px;color:#64748b;font-weight:400">日 / 周 / 月三级区间套（数据截至 {last_date}）</span></h4>'
                      f'{_rows}'
                      f'<p style="font-size:13px;color:#334155;line-height:1.75;margin-top:10px;background:#f8fafc;'
                      f'border-radius:6px;padding:8px 12px">{bd["conclusion"]}</p>'
                      f'<p style="font-size:12px;color:#64748b;margin-top:6px">综合广度评分 '
                      f'<b style="color:{_bcolor}">{bd["composite"]["score"]:+.2f}</b>（{bd["composite"]["label"]}）· '
                      f'已折算为「全市场对齐度」±8 反馈进各指数推演置信度。</p>'
                      f'</div>')

    gen_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") + " (UTC+8)"

    # 日周背离检测（用于结论）
    # R325: 背离按方向拆分——日1/周-1=日强周弱(上涨反弹结构)、日-1/周1=日弱周强(回调次级整理)，
    # 两类含义相反，文案必须分别描述。此前只判"不等"且 fixed 文案写"日线向上笔、周线向下笔"，
    # 当日弱周强(如 sh000300 日-1/周1)时方向说反、误导读者（levels_table 早有双向区分，此处修复落后）。
    # R365: dir=0(数据不足骨架, R363/R364 同族守卫)不是有效方向——divergent 原用 != 会把
    # 0 vs ±1 当背离, 而 _updn/_dnup 只认 ±1 对 → 下方 pat 出现「3/5 背离(1 强+1 弱)」计数矛盾、
    # 空洞括号; 全 dir=0 时还会误报「日周共振」。统一: 背离须双方 dir∈{±1} 且不等。
    _day_dir = {sym: results[sym]["classify"].get("last_bi_dir") for sym in data}
    _wk_dir = {sym: results_week[sym]["classify"].get("last_bi_dir") for sym in data}
    divergent = [d["name"] for sym, d in data.items()
                 if _day_dir[sym] in (1, -1) and _wk_dir[sym] in (1, -1)
                 and _day_dir[sym] != _wk_dir[sym]]
    _updn = [d["name"] for sym, d in data.items() if _day_dir[sym] == 1 and _wk_dir[sym] == -1]
    _dnup = [d["name"] for sym, d in data.items() if _day_dir[sym] == -1 and _wk_dir[sym] == 1]
    # 有效方向对计数: 双方 dir 均 ∈{±1} 的指数(供 pat 全退化前哨与 stance 判定)
    _n_dir_valid = sum(1 for s in data if _day_dir[s] in (1, -1) and _wk_dir[s] in (1, -1))

    # 市场概览 KPI
    n_multi = sum(1 for s in data if results[s]["classify"]["scenario"] in ("多头延续",))
    # R160 补全: 背驰见底机会(底背驰·看多)此前不计入任何 KPI 档——当前 5 指数中 4 个是它,
    # 导致市场概览显示"多头1/震荡0/空头0"严重失真(漏掉 4 个见底信号)。归入"震荡偏多"(偏多类)。
    # R164: 计数统一引用单一来源 SC_BULL（排除明确多头延续，余者归震荡偏多类），杜绝漏计复发
    n_osc = sum(1 for s in data if results[s]["classify"]["scenario"] in SC_BULL and results[s]["classify"]["scenario"] != "多头延续")
    # R160 补全: 背驰见顶风险(顶背驰·看空)同理归入"空头/偏弱"。
    # R164: 计数统一引用单一来源 SC_BEAR，杜绝漏计复发
    n_bear = sum(1 for s in data if results[s]["classify"]["scenario"] in SC_BEAR)
    n_div = len(divergent)
    avg_health = sum(v[0] for v in scores.values()) / len(scores)
    avg_conf = sum(v[1] for v in scores.values()) / len(scores)
    avg_agree = sum(results[s]["agreement"]["rate"] for s in data) / len(data) * 100
    total = len(data)

    cards, sections, conclusions = [], [], []
    forecast_info = {}
    paths_bt = {}
    for sym, d in data.items():
        try:
            r = results[sym]
            # R476: 「新信号」标记 —— 有限重放算出每条信号"已存在几天"（详见 compute_sig_birth）。
            # 包 try：这是展示层增强，任何异常都不应影响报告生成（退化为"无新信号"）。
            try:
                sig_age = compute_sig_birth(d["klines"], r)
            except Exception as _e:
                # 不能静默：防御式 except 会把代码错误伪装成"没有新信号"（R476 首版即如此）
                print(f"[warn] {sym} 新信号标记失败（{type(_e).__name__}: {_e}）"
                      f"，本条退化为不标记", file=sys.stderr)
                sig_age = {}
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
            # 注：日线 classify 已在此前预扫描中用周/月线重算（含 interval_nesting / ma_alignment 回写），
            # 此处 r["classify"] 即为统一口径，下游 card / forecast 行为一致。
            health, conf = scores[sym]
            sigma = forward_vol([k["close"] for k in d["klines"]], horizon)
            cards.append(card_html(sym, d["name"], d["klines"], r, wcls, health, conf))
            cls = r["classify"]
            sc_color = SCENARIO_COLOR.get(cls["scenario"], BLUE)
            w_color = SCENARIO_COLOR.get(wcls["scenario"], BLUE)
            # R210: 真联动——提取市场情绪预测序列(恐惧贪婪指数)透传给走势推演图, 叠加第二条 Y 轴
            sent_fc = None
            if isinstance(sent_full, dict) and isinstance(sent_full.get("forecast"), dict):
                _sf = sent_full["forecast"]
                if _sf.get("dates") and _sf.get("median"):
                    # R347: 浅拷贝并携带情绪区阈值(buy_th/sell_th)——推演图 tooltip 需按区给语境文案
                    # ("情绪低迷·领先见底"仅低位成立)。dict(_sf) 防污染 sent_full["forecast"] 原引用;
                    # 阈值同源 sentiment_v2.json 顶层键, 与情绪板块 zone 判定(20/85)天然一致不分裂。
                    sent_fc = dict(_sf)
                    sent_fc["buy_th"] = float(sent_full.get("buy_th", 20))
                    sent_fc["sell_th"] = float(sent_full.get("sell_th", 85))
            fs_svg, fs_note, fs_probs, fs_legend, fc_data = forecast_svg(d["klines"], r, wcls, conf, sigma, sym, horizon, backtests[sym], paths_bt[sym], breadth_score=bd["composite"]["score"], sent_fc=sent_fc)
            # R177: 情绪徽章 + 情绪×结构联动行(与情绪板块互联; 数据缺失时为空串, 不影响原布局)
            _sent_badge, _sent_row = "", ""
            if sent_full and _s_score is not None:
                _sig, _txt, _col, _wgt = _sent_x_struct(cls["scenario"], _sz, _s_score)
                _sent_badge = badge(f'情绪 {_s_score:.0f}·{_szl}', _szc)
                _sent_row = (f'<p style="margin-top:4px"><b>情绪联动</b>'
                             f'（市场情绪 {_s_score:.0f}·{_szl}，asof {sent_full.get("asof", "—")}）：'
                             f'<span style="color:{_col};font-weight:600">{_sig}</span> —— {_txt}</p>')
            sections.append(f"""
    <section class="panel" id="sec-{sym}">
      <h2>{d["name"]}（{sym}）{badge(f'日线：{cls["scenario"]}', sc_color)}{badge(f'周线：{wcls["scenario"]}', w_color)}{badge(f'月线：{mcls["scenario"]}', m_color)}{badge(f'健康 {health}', _score_color(health))}{badge(f'置信 {conf}', _score_color(conf))}{_sent_badge}</h2>
      <div class="chartbox">
        {echart_main(d["klines"], r, sym, r["captured"], sig_age)}
      </div>
      <div style="margin-top:6px;font-size:12px;line-height:1.75;color:#64748b">标签：<b>1/2/3买·卖</b> = <b>笔级</b>一类/二类/三类买卖点（背驰拐点 / 次低次高折返 / 回抽不进中枢）·
        <b>类2买 / 类2卖</b> = <b>笔级</b>「类二」买卖点（笔级一类之后，次级别(笔)<b>第 2 次</b>折返不破前极值）·
        <b>段1买·段1卖</b> = <b>线段级</b>一类买卖点（线段级背驰拐点，三角比笔级大一圈）·
        <b>段2买 / 段2卖</b> = <b>线段级</b>二类买卖点（段级一类之后，<b>次级别(笔)</b>首个回抽不破前极值）·
        <b>段3买 / 段3卖</b> = <b>线段级</b>三类买卖点（<b>段级中枢</b>被离开后，次级别(笔)回抽<b>不重新进入</b>段级中枢）·
        <b>段类2买 / 段类2卖</b> = <b>线段级</b>「类二」买卖点（段级一类之后，次级别(笔)<b>第 2 次</b>回抽不破前极值）·
        <b>段顶 / 段底</b> = 线段端点结构参照位（含 ·背驰 / ·次高 / ·次低）——
        <b>它们是结构位、不是买卖点</b>；⚠ <b>·次低 / ·次高</b>（<b>段级</b>折返口径：段底/顶背驰之后
        首个反向<b>段</b>不破前极值）与 <b>段2买 / 段2卖</b>（<b>笔级</b>折返口径：首个反向<b>笔</b>不破前极值）
        <b>不是同一个点</b> —— 前者标记在段的最低/最高点，后者才是交易信号，两者可同时出现在不同日期·
        <span style="background:rgba(43,108,176,0.10);border:1px solid #2b6cb0;border-radius:3px;padding:0 3px">蓝带</span> = <b>笔中枢</b>区间（最近 8 个）·
        <span style="background:rgba(13,148,136,0.10);border:1px dashed #0d9488;border-radius:3px;padding:0 3px">青带</span> = <b>段级中枢</b>区间（最近 2 个，虚线框）—— <b>段3买/段3卖 的止损锚就在这里</b>（段级中枢下沿/上沿）·
        <b>·趋 / ·盘</b> = 趋势 / 盘整背驰 · <b>·量</b> = 量价背离确认 ·
        <span style="background:rgba(229,69,69,0.13);border:1px solid #e54545;border-radius:3px;padding:0 3px;font-weight:700">新 XX 买·卖</span>
        = <b>近 {SIG_NEW_DAYS} 个交易日内才出现</b>的信号（因信号须等其所处「笔」走完才确认，
        其坐标日期会<b>早于</b>诞生日 <b>3~13 个交易日</b>（中位 <b>5</b>），属正常，非数据错误）<br>
        ⚠ <b>尚未走完的「最后一笔」不参与信号评估</b> —— 引擎只对<b>已确认的笔</b>判买卖点，
        以免用未完成数据（该笔的低点 / 高点仍可能继续延伸）。因此<b>最新一两根 K 线上即使结构上
        已出现背驰，也不会立刻冒出买卖点标签</b>，要等下一笔走完、它进入「已确认」集合后才补标。<br>
        ★ <b>实测（5 指数 2021 至今，逐日重放）</b>：<b>最新一根 K 线上不会出现任何买卖点标签</b> ——
        220 次信号诞生中「坐标日期就是当天」的 <b>0 次</b>。原因是结构性的：<b>分型必须有右侧 K 线</b>
        （扫描范围止于倒数第二根），故最新一根<b>连分型都不是</b>，更不可能是笔 / 段端点。<br>
        ★ <b>「底部当天」尤其不会出标签</b>：事后确认的底部共 <b>797</b> 个，当天就冒出买点标签的仅
        <b>12 个（1.5%）</b>，<b>低于</b>随机交易日的 <b>3.4%</b> —— 底部当天通常仍在「下降笔延续」中，
        恰是被排除的状态。<b>从该底到首个买点标签，中位要等 22 个交易日</b>（P25=7 / P75=39）。<br>
        <b>当下想判断最后一笔的状态</b>，请看该处有没有 <b>段底·背驰 / 段底·次低</b> 这类
        <b>结构参照标注</b> —— 它们由线段序列实时算出，<b>不受「笔是否走完」限制</b>；
        但反过来也要注意：<b>它只是结构参照，不等于已确认的买卖点</b>（实测这类「未确认点」
        事后转为正式信号的比例有限，故不单独出标签）。<br>
        ★★ <b>那到底「什么时候该看什么」？</b>实测（5 指数 2021 至今，口径 =「标注出现后<b>次日开盘</b>
        买入、持有 H 个交易日」）：<b>段级背驰标注是当日可操作的结构判据</b> —— 它由线段序列<b>实时</b>算出，
        实测出现日只需比段端点晚 <b>1 个交易日</b>，且 <b>100% 锚在「最后一段」</b>上（不必等该段之后的笔走完）。
        看到「段底·背驰」后买入：<b>H=20 胜率 70.0%</b>（随机基准 49.1%，<b>二项单侧 p≈0.017</b>）、
        <b>H=60 均值 +5.34%</b>（基准 +2.05%）；而若改等<b>引擎买卖点标签</b>（它是<b>确认</b>口径、天然滞后），
        H=20 胜率只有 <b>45.9%</b>、均值 +0.41%，<b>反而低于随机基准</b> —— 因为标签的坐标日期平均比诞生日
        <b>早 5 个交易日</b>，等到它出现，行情已走了一段。<br>
        ⚠ <b>但段底背驰绝非「抄底必胜」</b>：实测 <b>56.7%</b> 的段底背驰出现后，该线段<b>继续创新低</b>
        （中位再跌 <b>4.52%</b>，P90 达 15.88%，最长 109 个交易日后才见底）；且看到标注时，价格通常已比
        标注点（= 段的最低点）高 <b>2.24%</b> ⇒ <b>不可因为「背驰了」就满仓</b>。<br>
        ★★ <b>那止损到底该放哪？</b>把「仓位方案 × 止损口径」做成网格实测（5 指数、事件 = 段级背驰首次出现、
        入场 = 次日开盘、持有 20 日）后，得到<b>三条反直觉的结论</b>：<br>
        ① <b>「线段端点被跌破」不能当止损信号用</b> —— 它的触发率高达 <b>53%</b>（买侧），而
        <b>破位那一刻「平仓」与「继续持有」是五五开</b>（16 例配对：均值差 −0.70pp、中位 +0.23pp、
        止损更优占比 50.0%、符号检验 <b>p=0.598</b>）。破位后既可能续跌（2023-12-22 中证500：持有 −8.22%
        vs 止损 −2.53%），也可能深挖坑后 V 反（2024-08-26 中证500：持有 +8.95% vs 止损 −0.82%），
        <b>两个方向完全对称</b> ⇒ 当硬止损会把胜率从 <b>70.0% 打到 50.0%</b>。
        ⚠ 但<b>「破位」本身是有信息的</b>：破位组最终均值 <b>−0.99%</b>（胜率 43.8%），
        未破位组 <b>+5.19%</b>（胜率 <b>100%</b>，14/14）—— 差别在于这是<b>事后</b>分组（要等 20 日满期才知道）。
        ⇒ 正确用法 = 把「端点被跌破」当作<b>「局势转坏、停止加仓」的标志</b>，
        而不是<b>「立刻清仓」的扳机</b>。<br>
        ② <b>固定百分比止损更实用</b>：止损位设<b>入场价 −3%</b> 时，止损率仅 <b>33%</b>、胜率仍保 <b>63.3%</b>、
        均值 <b>+2.11%</b>（不低于不止损的 +1.89%）。它不靠"预测"赚钱，而是靠<b>限定单笔最大损失</b>：
        账户层面最差浮亏由 <b>−12.22% 收窄到 −5.38%</b>（各类止损的收益差<b>都不显著</b>，
        p≈0.6~1.0；价值全在<b>削掉尾部</b>，不在提高胜率）。<br>
        ③ <b>「分批」在这个样本上不划算</b>：「回撤 2%/4% 各补 1/3」的 20 日均值 <b>+0.39%</b>
        （满仓 +1.89%）—— 因为 70% 的情形是直接上涨，空置的 2/3 资金没有收益；而它的风险也
        <b>同比例缩小</b>（最差 −5.98% vs −12.22%）⇒ <b>风险调整比并未改善</b>。
        ⇒ <b>想要低风险，应整体缩小仓位，而不是「先小后大」地分批</b>。<br>
        <b>样本边界（如实）</b>：买侧仅 30 例 / 卖侧 19 例，且 5 指数同期高度共振 ⇒ <b>独立事件仅约 13 / 10</b>；
        上述配对差<b>均未达显著</b>，只有「破位组 vs 未破位组」的 <b>+6.18pp</b> 差别足够大 ⇒
        属<b>方向性证据</b>，非统计定论。<br>
        ⚠ <b>段级信号会「暂时撤下」</b>：若最后一段的终点笔重新变成<b>未完成笔</b>（典型情形 = 末段
        向下延伸创了新低、把段端点拉到末笔上），该段<b>整段不产出信号</b> ⇒ 此前挂着的段级买卖点会
        <b>短暂消失</b>，待新的反向笔确认、该段端点笔重新「完成」后再恢复。这是「不猜未完成数据」的
        代价，<b>不是数据错误</b>（实测沪深300 在 2026-09-17 那天，段级信号由 28 条降为 26 条，
        即此机制；两条旧点仍在引擎里，只是该段端点笔退回未完成）。<br>
        ⚠ <b>段级标注的坐标与强度会随行情更新</b>：线段端点由后续行情决定 —— 若末段继续朝原方向
        创出新极值，该段的<b>端点会被延伸改写</b>，连带它的 MACD 面积比、以及「段底/段顶·背驰」标注
        所在的价格<b>一并重算</b>。实测（沪深300 段 36）：端点原为 <b>07-30</b>，09-17 因向下创新低
        被延伸到 <b>09-16</b>，面积比由 <b>0.301 → 0.648</b>（深证 #54 / 创业板 #74 / 中证500 #56 同理）。
        ⇒ 这是「<b>段随行情生长</b>」的正常表现，<b>不是数据错误</b>；
        但请勿把此前截图上的段级数值当作不变量。</div>
      <div style="margin-top:4px;font-size:12px;line-height:1.75;color:#94a3b8">
        ⚠ <b>级别说明（为什么有「段」族）</b>：缠论是级别递归体系（笔 → 线段 → 走势类型）。此前图上<b>只有笔级</b>买卖点，
        线段级别仅把段端点当结构参照位画出，<b>不产生买卖点</b> —— 后果是「段底背驰（线段级一类买点）之后的次级别回抽
        不破前低」（= 标准<b>段级二类买点</b>）系统性缺失：实测上证 4 例（2022-05-10 / 2024-03-28 / 2025-05-28 / 2026-07-30）
        全部漏标。现已补上 <b>段1买·段1卖·段2买·段2卖</b>（R478）、<b>段3买·段3卖</b>（R480）
        与 <b>段类2买·段类2卖</b>（R483）—— 段级一/二/类二/三类至此齐备，与笔级同构、只换级别。
        <b>段级中枢由线段序列重构</b>（≥3 条线段重叠区间），故段三点的止损锚在<b>段级中枢</b>的下沿/上沿，
        与笔级三类的止损位不是一回事。<b>两级信号各自独立统计</b>（见下方回测表），不混计。<br>
        ★ <b>为什么补「段类二」</b>：标准二类点只取「<b>首个</b>不破位的反向折返」，而实测（5 指数 2021 至今）
        <b>第 2 次</b>同样有效 —— 20 日胜率 <b>78.3%</b> / 均 <b>+4.40%</b>（首 76.0% / +3.09%，
        随机基准 49.1% / +0.22%）；第 3 次起退化到基准（54.5%，60 日仅 40.9%）⇒ 只补到第 2 次为止。
        任一反向折返<b>破位</b>即锚点失效、不再后扫。<br>
        ★ <b>R484 系统性补全</b>：把「每锚点只取首个」这条去重律在<b>全部信号族 × 两级</b>上逐格量化后 ——
        ① <b>笔级二类</b>与段级同病（第 2 次 20 日胜率 <b>68.2%(买)/73.1%(卖)</b>，
        <b>均不弱于</b>首个 59.3%/62.9%，第 3 次退化到 58.8%/59.1%）⇒ 已补 <b>类2买 / 类2卖</b>；
        ② <b>三类族不动</b>：第 2 次起<b>两侧一致退化</b>（笔 73.1%→61.5%→56.4%，
        段 81.5%→66.7%→70.6%）⇒ 中枢的「首个回抽」才是离开确认，后续回抽退化为中枢震荡，
        补齐只会稀释读数 —— 这是<b>有证据的「不补」</b>，不是遗漏。</div>
      <div class="verdict"><b>结构解读：</b><p>{cls["detail"]}</p>
      <p style="margin-top:4px"><b>周线级别：</b>{wcls["detail"]}</p>{_sent_row}</div>
      <h3 class="fc-title">未来走势推演</h3>
      <div class="chartbox fcbox" id="fcbox-{sym}">
        {fs_svg}
        <div class="xh-tip" id="fctip-{sym}"></div>
      </div>
      <div class="sec-foot">
      <div class="fc-note2" style="white-space:pre-line;font-size:13px;line-height:1.85;color:#475569;margin:10px 0">{fs_note}</div>
      {fs_legend}
      {path_hit_html(cls["scenario"], paths_bt[sym], fs_probs[0], fs_probs[1], fs_probs[2], horizon)}
      </div>
    </section>""")
            conclusions.append(f'<li><b>{d["name"]}</b>：日线 {cls["scenario"]} / 周线 {wcls["scenario"]} —— {cls["detail"]} <a href="#sec-{sym}" data-sym="{sym}" data-jump style="font-size:12px;color:{BLUE}">[查看图解]</a></li>')
            forecast_info[sym] = {"p_main": fs_probs[0], "p_alt": fs_probs[1], "p_risk": fs_probs[2],
                                  "p_hold": fc_data["p_hold"],
                                  "zd": (r["zhongshu"][-1]["zd"] if r["zhongshu"] else d["klines"][-1]["close"] * 0.95),
                                  "stable": r["stability"]["stable"],
                                  "level": r["stability"].get("level", "稳健"),
                                  "last_bi_bars": r["stability"].get("last_bi_bars", 0),
                                  "sigma": sigma, "fc": fc_data}
        except Exception as _e:
            # R173: 单指数计算异常(脏数据/边界)不再中断整份报告, 降级为该指数占位卡, 其余照常渲染。
            # R366: 占位卡文案区分「数据缺失(空序列)」与「渲染异常」——空 klines 是数据源缺数据
            # (自愈型, 待下轮 fetch 恢复即可, 非代码问题); 原统一正文「渲染异常」+badge「数据缺失」
            # 自相矛盾, 数据缺失被误报为渲染故障会引导用户误查代码而非等数据恢复。
            import traceback as _tb
            print("WARN 指数 %s 渲染失败, 降级占位: %s" % (sym, _e))
            _tb.print_exc()
            _no_data = not d.get("klines")
            _bdg_txt = "数据缺失" if _no_data else "渲染异常"
            _bdg_col = "#d97706" if _no_data else "#dc2626"
            _dgd_txt = ("该指数本轮数据缺失（空序列），未纳入分析，不影响其余指数；"
                        "待行情数据恢复后将自动更新。" if _no_data
                        else "该指数本轮渲染异常（已降级占位，不影响其余指数）。")
            sections.append(f"""
    <section class="panel" id="sec-{sym}">
      <h2>{d["name"]}（{sym}）{badge(_bdg_txt, _bdg_col)}</h2>
      <div class="verdict"><b>结构解读：</b><p>{_dgd_txt}</p></div>
    </section>""")
            conclusions.append(f'<li><b>{d["name"]}</b>（{sym}）：{_dgd_txt}</li>')

    # R280: 单指数降级防御漏网点 —— 循环 try/except(R173)已对该 sym 降级占位但未写
    # forecast_info, 此处若按 data 全键取 forecast_info[sym]["fc"] 会 KeyError 崩掉整份报告
    # (与 R206 在 forecast_summary_table 已做的占位防御同族漏网, 实证: 模拟 sh000905 渲染
    # 异常 → 循环内降级成功但 fc_blob 构建 KeyError)。改为仅取成功渲染的指数; 缺失 sym 的
    # 情绪背离/锚点对照自动不显示(下方调用点已对 None 优雅降级)。
    fc_blob = {sym: forecast_info[sym]["fc"] for sym in data if sym in forecast_info}
    diverge_note = ""
    if divergent:
        # R325: 双向背离分别措辞——日强周弱=周线调整中的反弹(降预期)；日弱周强=周线上行中的次级回调
        # (观察周线笔企稳的低吸窗口)。此前固定写"日线向上笔、周线向下笔"，日弱周强方向会被说反。
        _parts = []
        if _updn:
            _parts.append(
                f"<b>{'、'.join(_updn)}</b> 当前<b>日线向上笔、周线向下笔</b>（日强周弱背离）。"
                f"历史统计上此类组合意味着日线上涨是周线调整中的反弹结构，<b>仓位与预期应低于\"日周共振多头\"的情形</b>；"
                f"只有周线笔重新转向上（周线底分型确认），日线多头延续的置信度才会提高。")
        if _dnup:
            _parts.append(
                f"<b>{'、'.join(_dnup)}</b> 当前<b>日线向下笔、周线向上笔</b>（日弱周强背离）。"
                f"日线回调属周线上行中的次级整理而非趋势反转，<b>不宜在恐慌中追空</b>；"
                f"可等待日线底分型/背驰确认后的次级买点，并防周线笔若转弱则背离升级为共振下跌。")
        diverge_note = ('<p style="margin-top:10px;color:#b45309;font-size:14px;line-height:1.8">'
                        '⚠️ <b>级别背离提示</b>：' + "；".join(_parts) + "</p>")

    # 预测校准脚注(#预测精度·R74)：把 R72 滚动样本外回测的实证校准结果作为常驻透明提示，
    # 避免用户把"主路径"误当方向信号——看板真正的价值在风险带(置信区间)，不在方向赌注。
    # R76: 情绪极端区提示(读取 live 情绪分; 不可用时 sent=None, sentiment_note 为空)。
    sent = load_live_sentiment()
    if sent and sent["zone"] != "neutral":
        zlabel = sent["label"]
        sentiment_note = (
            "<p style='margin-top:10px;color:#9a3412;font-size:13px;line-height:1.85;background:#fff7ed;"
            "padding:10px 12px;border-left:3px solid #ea580c;border-radius:4px'>"
            f"<b>⚠️ 情绪极端区提示（R76 回测证据）：</b>当前市场情绪处于<b>极端{zlabel}</b>区"
            f"（情绪分 {sent['score']:.1f}，阈值 {sent['buy_th']:.0f}/{sent['sell_th']:.0f}，asof {sent['asof']}）。"
            "历史回测显示此类区制下斐波那契主路径方向有<b>系统性偏置</b>——T+30 极端区基线命中仅 31%、"
            "翻转逆向后达 69%；主路径方向在此类拐点区<b>仅供参考，请以风险带/反转应对为准，勿直接押方向</b>。</p>")
    else:
        sentiment_note = ""
    calib_note = (
        "<p style='margin-top:10px;color:#475569;font-size:13px;line-height:1.85;background:#f8fafc;"
        "padding:10px 12px;border-left:3px solid #0891b2;border-radius:4px'>"
        "<b>📐 预测校准参考（walk-forward 样本外回测，五指数·180 锚点，可复跑 audit_forecast_calibration.py）：</b><br>"
        "• <b>风险带（置信区间）准确且偏保守</b>：P05–P95 实测覆盖 ≈95%（名义 90%），真实罩住后来走势；<br>"
        "• <b>方向性技能较弱</b>：主路径方向命中 T+8≈43%、T+30≈53%（接近抛硬币）——<b>主路径不是可靠方向信号，切勿据此满仓押方向</b>；<br>"
        "• <b>中线中心校准良好</b>：稳健中位口径实测偏置≈0.9%（此前均值口径显示的 +2.8% 为右偏肥尾造成的指标假偏置，非中心真偏）；<br>"
        "• <b>跨指数方向共识无效</b>：5 指数主路径方向投票在 T+8 反而更差（-2.2pp）、T+30 仅边际改善（+5.6pp，≈噪声）——方向偏差为系统性（同模型同 regime），聚合无法分散误差，故看板不提供「共识方向」信号；<br>"
        "• <b>情绪条件化（R76 第九道门禁，监控中未并入）</b>：回测显示斐波那契主路径在情绪极端区有系统性方向偏置——"
        "T+30 全样本条件化 +9.4pp（近期样本外 +8.9pp，稳定）、极端区基线 31%→翻转后 69%；但 T+8 近期样本外仅 +2.2pp（短期增益不稳定），"
        "且极端区近期样本仅 10 个（不足 20 阈值）→ <b>暂未并入预测数学，仅做监控+本提示</b>。注：纯情绪逆向信号本身无效（T+8 命中 26.7%，比抛硬币差），"
        "增益来自「纠正斐波那契在拐点的方向偏置」，非情绪预测涨跌；<br>"
        "• <b>突变漂移监控（R78 第十道门禁）+ 日历补全</b>：每日构建后自动检测相邻刷新（±15 交易日）的中线预测移动是否远超同期行情移动，"
        "捕捉「行情没动、预测自己跳」的过拟合/数据异常信号（详见 audit_forecast_drift.py）；同时为推演落点日期准确性对齐 A 股日历"
        "（R247：周末恒休含补班日——交易所不随调休补班开市，腾讯 K线实测 2021 至今无任何周末 K线，已删除原 _A_SHARE_MAKEUP 补班交易表）。两项均为监控/防御层，不改动预测数学；<br>"
        "• <b>用法</b>：用带宽管理波动/止损，用「跌破 ZD 即主路径失效」做条件应对，方向仅作参考。</p>")


    # 数据驱动的市场格局描述（不写死，随每日自动刷新保持准确）
    n_daily_up = sum(1 for s in data if _day_dir[s] == 1)
    n_week_up = sum(1 for s in data if _wk_dir[s] == 1)
    n_div = len(divergent)
    total = len(data)
    if _n_dir_valid == 0:
        # R365: 全部指数日/周结构笔方向均不可用(数据退化)时, 原逻辑 n_div==0 会误报
        # 「日周共振, 结构方向一致性较高」——无方向说共振=失实, 显式数据不足文案。
        pat = f"{total} 个指数日/周结构笔方向暂不可用（结构数据不足），日周共振/背离待数据恢复后评估"
    elif n_div == total:
        # R325: 全背离时按方向给准确描述（此前固定"日线向上笔、周线向下笔"，全为日弱周强时会说反）
        if _dnup and not _updn:
            pat = (f"全部 {total} 个指数日线向下笔、周线向上笔（日弱周强背离），当前回调在更大级别上属"
                   f"<b>上行中的次级整理</b>，而非趋势反转")
        elif _updn and not _dnup:
            pat = (f"全部 {total} 个指数日线向上笔、周线向下笔（日强周弱背离），当前上涨在更大级别上属"
                   f"<b>反弹中的强势段</b>，而非主升浪")
        else:
            pat = (f"{total} 个指数全部日周背离（日强周弱 {len(_updn)} 个 / 日弱周强 {len(_dnup)} 个），"
                   f"多空级别方向分裂、无一致主线")
    elif n_div == 0:
        # R395: R365 只防了「全部方向不可用」(n_dir_valid==0) —— 当部分指数方向不可用
        # (数据缺失→骨架 classify last_bi_dir=0, R173/R366 降级占位) 而其余可用且同向时,
        # 旧文案仍称「{total} 个指数日周共振」, 把未参与分析的指数也算进共振计数(失实)。
        # 按方向有效子集(_n_dir_valid)如实描述; 有效 <2 个时"共振"名不副实, 降级为观察提示。
        if _n_dir_valid >= 2:
            pat = (f"{total} 个指数中 {_n_dir_valid} 个日/周方向可用且同向（日周共振），"
                   f"{total - _n_dir_valid} 个方向暂不可用（数据不足）"
                   if _n_dir_valid < total
                   else f"{total} 个指数日线与周线同向（日周共振），结构方向一致性较高")
        else:
            pat = (f"{total} 个指数中方向可用者不足 2 个（{_n_dir_valid} 个可用 / "
                   f"{total - _n_dir_valid} 个数据不足），日周共振/背离待数据恢复后评估")
    else:
        _parts = []
        if _updn:
            _parts.append(f"{len(_updn)} 个日强周弱背离")
        if _dnup:
            _parts.append(f"{len(_dnup)} 个日弱周强背离")
        pat = f"{n_div}/{total} 个指数日周背离（{'、'.join(_parts)}）、{total - n_div} 个日周共振"
    if n_daily_up >= total * 0.6 and n_week_up <= total * 0.4:
        stance = "；仓位与预期应低于\"日周共振多头\"的情形"
    else:
        stance = ""
    # R280: 全部指数均降级(forecast_info 空, 极端但 R173 哲学防"不可能")时 min()/max() 空序列
    # ValueError 崩 —— 预抽区间, 空时推演结论行降级为提示(与 R206 占位风格一致)。
    _fc_pmains = [v["p_main"] for v in forecast_info.values()] if forecast_info else []
    if _fc_pmains:
        _pmn = int(round(min(_fc_pmains) * 100))
        _pmx = int(round(max(_fc_pmains) * 100))
        _fc_line = (f"<li><b>推演结论：</b>各指数主路径概率约 {_pmn}%~{_pmx}%；"
                    f"跌破中枢 ZD 即主路径失效。详见<a href=\"#s4\">第四节</a>。</li>")
    else:
        _fc_line = ("<li><b>推演结论：</b>全部指数渲染降级（数据异常），推演概率暂不可用，"
                    "详见各指数占位提示。</li>")
    exec_summary = f"""
    <div class="panel exec">
      <h4>一句话结论</h4>
      <ul>
        <li><b>市场格局：</b>{pat}{stance}。</li>
        {_fc_line}
      </ul>
    </div>"""

    # 全局指数联动条（模块互联互通：点击聚焦某指数，卡片/表行/图解三向联动）
    sym_rail = ('<nav class="sym-rail" id="symRail" aria-label="指数联动条">'
                + ''.join(f'<a class="chip" href="#sec-{sym}" data-sym="{sym}" data-jump>{d["name"]}</a>' for sym, d in data.items())
                + '</nav>')
    # R177: 市场情绪板块（与决策总览 KPI / 分指数 panel 徽章 / 情绪×结构矩阵互联; 数据缺失时内部降级为提示条）
    # R280: sh000001 自身也可能降级(不入 fc_blob) —— 显式取行再取字段, 避免 None.get 二次崩
    _sh_fc = fc_blob.get("sh000001") or {}
    sent_board = sentiment_board_html(_base, data, results, results_week, scores, last_date,
                                     idx_proj=_sh_fc.get("proj"),
                                     idx_last=_sh_fc.get("last"))

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<script>window.__BUILD_TIME__="{gen_time}";</script>
<title>A股缠论结构分析报告 · 2021 至今</title>
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
  .kv b {{ color: {INK}; font-variant-numeric: tabular-nums; }}
  .panel {{ background: #fff; border: 1px solid #e5e9f0; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(15,23,42,.04); }}
  .panel h2 {{ font-size: 18px; margin-bottom: 10px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }}
  .badge {{ font-size: 12px; color: #fff; padding: 2px 10px; border-radius: 999px; font-weight: 600; white-space: nowrap; font-variant-numeric: tabular-nums; vertical-align: middle; }}
  .verdict {{ background: #f0f6ff; border-left: 4px solid {BLUE}; padding: 10px 14px; margin-top: 12px; font-size: 14px; border-radius: 0 6px 6px 0; }}
  .verdict p {{ margin-top: 4px; color: #475569; line-height: 1.7; }}
  .tbl {{ width: 100%; border-collapse: collapse; font-size: 13px; background: #fff; table-layout: fixed; }}
  .tbl th, .tbl td {{ padding: 9px 10px; text-align: left; vertical-align: middle; border-top: 1px solid #eef2f7; overflow-wrap: break-word; transition: background .12s ease; font-variant-numeric: tabular-nums; }}
  .tbl th {{ background: #f1f5f9; color: #475569; font-weight: 600; border-top: none; }}
  .tbl tbody tr:hover td {{ background: #f8fafc; }}
  .tbl tbody tr:last-child td {{ border-bottom: 1px solid #eef2f7; }}
  .tbl .tac {{ text-align: center; }}
  .tbl .best {{ font-weight: 700; }}
  .strategy {{ color: #475569; line-height: 1.6; }}
  .conclusion li {{ margin: 8px 0 8px 18px; line-height: 1.8; font-size: 14px; }}
  .disclaimer {{ background: #fff8e6; border: 1px solid #f0d98c; color: #92600a; border-radius: 10px; padding: 14px 18px; font-size: 13px; line-height: 1.8; }}
  /* R79 预测质量自检证书（顶部常驻, 准确性可见可验） */
  .qc-card {{ background: linear-gradient(135deg,#f0f9ff,#eef2ff); border: 1px solid #c7d2fe; border-radius: 12px; padding: 14px 16px; margin: 14px 0; box-shadow: 0 4px 14px rgba(79,70,229,0.08); }}
  .qc-head {{ font-size: 15px; font-weight: 700; color: #3730a3; margin-bottom: 10px; }}
  .qc-sub {{ font-size: 11px; font-weight: 400; color: #64748b; margin-left: 8px; }}
  .qc-grid {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .qc-cell {{ flex: 1 1 120px; background: #fff; border: 1px solid #e0e7ff; border-radius: 8px; padding: 8px 10px; text-align: center; }}
  .qc-cell.warn {{ border-color: #fca5a5; background: #fef2f2; }}
  .qc-v {{ font-size: 18px; font-weight: 700; color: #1e293b; }}
  .qc-cell.warn .qc-v {{ color: #b91c1c; }}
  .qc-l {{ font-size: 11px; color: #64748b; margin-top: 2px; }}
  .qc-foot {{ font-size: 12px; line-height: 1.7; color: #475569; margin-top: 10px; border-top: 1px dashed #c7d2fe; padding-top: 8px; }}
  /* R80 分市场环境(regime)覆盖块：暴露全样本平均掩盖的弱点, 熊市塌方标红 */
  .qc-regime {{ margin-top: 10px; border-top: 1px dashed #c7d2fe; padding-top: 8px; }}
  .qc-regime-h {{ font-size: 12px; font-weight: 600; color: #4338ca; margin-bottom: 6px; }}
  .qc-regime-row {{ display: flex; align-items: center; gap: 10px; font-size: 12px; padding: 3px 0; }}
  .qc-regime-lab {{ flex: 0 0 40px; font-weight: 700; color: #334155; }}
  .qc-regime-cov {{ flex: 1; color: #16a34a; font-variant-numeric: tabular-nums; }}
  .qc-regime-cov.bad {{ color: #b91c1c; font-weight: 700; }}
  .qc-regime-cov i {{ color: #94a3b8; font-style: normal; font-size: 11px; }}
  i.dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }}
  h2.sec {{ font-size: 19px; margin: 26px 0 12px; padding-left: 12px; border-left: 4px solid {BLUE}; line-height: 1.3; scroll-margin-top: calc(env(safe-area-inset-top, 0px) + 96px); }}  /* R167: TOC 锚点跳转偏移, 避免被 sticky 导航(toc+sym-rail)遮挡 */
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
  .quality .qsub {{ color: #64748b; font-size: 12px; margin-left: 6px; }}
  details.method summary {{ list-style: none; }}
  details.method summary::-webkit-details-marker {{ display: none; }}
  .method h4 {{ margin: 14px 0 6px; font-size: 14px; color: #334155; }}
  .method p, .method li {{ font-size: 13px; color: #475569; line-height: 1.85; }}
  .method ul {{ margin: 0 0 4px 18px; }}
  .method li {{ margin: 4px 0; }}
  .exec {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px; padding: 14px 18px; margin-bottom: 20px; }}
  .exec h4 {{ font-size: 15px; color: {BLUE}; margin-bottom: 8px; }}
  .exec ul {{ margin: 0 0 0 18px; }}
  .exec li {{ font-size: 13px; color: #334155; line-height: 1.85; margin: 5px 0; }}
  .xh-tip {{ position: absolute; pointer-events: none; background: rgba(15,23,42,.92); color: #fff; font-size: 13px; font-weight: 500; line-height: 1.55; padding: 8px 12px; border-radius: 6px; display: none; z-index: 20; white-space: nowrap; font-variant-numeric: tabular-nums; box-shadow: 0 2px 10px rgba(0,0,0,.3); }}
  .xh-tip b {{ color: #fbbf24; }}
  .tablescroll {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  /* R178 市场情绪板块（对齐 sentiment-dashboard 视觉语言） */
  /* 基础层：结构/布局/色彩，圆角与阴影统一引用 R153 令牌（--radius-* / --shadow-*），
     由 R153 增强层统一覆盖质感，避免与看板主体（.panel/.card/.chartbox）风格割裂。 */
  .sent-head {{ position: relative; overflow: hidden; background: var(--surface, #fff); border: 1px solid #e5e9f0; border-radius: var(--radius-lg, 18px); padding: 18px 20px; margin-bottom: 14px; box-shadow: var(--shadow-sm2, 0 2px 10px rgba(15,23,42,.05)); }}
  /* 情绪主题顶饰：用当前 zone 色（内联 style 注入的 --sent-accent），无则回退主色渐变 */
  .sent-head::before {{ content: ""; display: block; height: 4px; margin: -18px -20px 14px; background: linear-gradient(90deg, var(--sent-accent, #2b6cb0), #60a5fa); }}
  .sent-head-row {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
  .sent-title-wrap {{ display: flex; align-items: center; gap: 10px; }}
  .sent-main-title {{ font-size: 18px; font-weight: 800; color: #1e293b; letter-spacing: .2px; }}
  .sent-tag {{ font-size: 12px; color: #fff; padding: 4px 12px; border-radius: 999px; font-weight: 700; box-shadow: 0 1px 2px rgba(0,0,0,.08); font-variant-numeric: tabular-nums; }}
  .sent-score-wrap {{ display: flex; align-items: baseline; gap: 10px; }}
  .sent-big-score {{ font-size: 44px; font-weight: 800; line-height: 1; font-variant-numeric: tabular-nums; letter-spacing: -1px; }}
  .sent-score-meta {{ display: flex; flex-direction: column; align-items: flex-start; gap: 3px; font-size: 12px; color: #64748b; }}
  .sent-pct {{ font-variant-numeric: tabular-nums; background: #f1f5f9; padding: 1px 6px; border-radius: 4px; font-size: 11px; }}
  .sent-trend {{ font-weight: 700; font-variant-numeric: tabular-nums; }}
  /* R205 深度美化: 趋势徽标 + SVG 半圆仪表盘 + 刻度标签 + 档位分布条 */
  .sent-trend-badge {{ font-size: 11px; color: #fff; padding: 2px 9px; border-radius: 999px; font-weight: 700; white-space: nowrap; box-shadow: 0 1px 2px rgba(0,0,0,.10); font-variant-numeric: tabular-nums; }}
  .sent-gauge {{ flex: 0 0 132px; display: flex; align-items: center; justify-content: center; }}
  .sent-gauge-svg {{ width: 132px; height: 78px; overflow: visible; }}
  .sent-gauge-num {{ font-size: 19px; font-weight: 800; font-variant-numeric: tabular-nums; }}
  .sent-gauge-sub {{ font-size: 8.5px; fill: #64748b; font-variant-numeric: tabular-nums; }}
  .sent-bar-wrap {{ margin-top: 14px; padding: 0 2px; }}
  .sent-bar-track {{ position: relative; height: 10px; border-radius: 5px; background: linear-gradient(90deg, #22c55e 0%, #84cc16 24%, #f59e0b 50%, #f97316 72%, #ef4444 100%); overflow: visible; }}
  .sent-bar-fill {{ position: absolute; left: 0; top: -2px; height: 14px; border-radius: 7px; box-shadow: 0 0 0 3px rgba(255,255,255,.9), 0 2px 6px rgba(0,0,0,.18); min-width: 4px; max-width: 100%; transition: width .6s ease; }}
  .sent-bar-tick {{ position: absolute; top: -3px; width: 2px; height: 16px; background: rgba(255,255,255,.95); border-radius: 1px; box-shadow: 0 1px 2px rgba(0,0,0,.25); }}
  .sent-bar-tick-label {{ position: absolute; top: 16px; transform: translateX(-50%); font-size: 9.5px; font-weight: 600; white-space: nowrap; }}
  .sent-bar-labels {{ display: flex; justify-content: space-between; font-size: 11px; color: #64748b; margin-top: 16px; text-align: center; }}
  .sent-bar-labels small {{ font-size: 10px; opacity: .8; }}
  .sent-zstat {{ display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 11px; font-variant-numeric: tabular-nums; margin-top: 12px; }}
  .sent-zstat .zs {{ font-weight: 600; white-space: nowrap; }}
  /* 档位分布迷你条(全样本 hist 分箱) */
  .sent-zstat-bar {{ display: flex; height: 7px; border-radius: 4px; overflow: hidden; margin-top: 7px; background: #eef2f7; box-shadow: inset 0 0 0 1px rgba(15,23,42,.04); }}
  .sent-zstat-bar .zseg {{ display: block; min-width: 2px; transition: flex .4s ease; }}
  .sent-head-note {{ font-size: 12px; color: #64748b; margin-top: 8px; line-height: 1.5; }}
  .sent-interp {{ font-size: 12.5px; color: #334155; margin-top: 8px; line-height: 1.6; background: var(--surface-2, #f8fafc); border-left: 3px solid var(--sent-accent, #94a3b8); border-radius: 0 8px 8px 0; padding: 8px 10px; }}
  .sent-chart-card {{ background: var(--surface, #fff); border: 1px solid #e5e9f0; border-radius: var(--radius-md, 14px); padding: 10px 14px 12px; margin-bottom: 12px; box-shadow: var(--shadow-sm2, 0 2px 10px rgba(15,23,42,.05)); position: relative; overflow: hidden; }}
  .sent-chart-card:last-child {{ margin-bottom: 0; }}
  /* R205 图表卡渐变顶饰条(对齐 .card/.qc-card 观感) */
  .sent-chart-card::before {{ content: ""; display: block; height: 3px; margin: -10px -14px 10px; background: linear-gradient(90deg, var(--primary, #2b6cb0), var(--primary2, #60a5fa)); opacity: .85; }}
  .sent-chart-card .echart-toolbar {{ font-size: 13px; color: #475569; font-weight: 600; padding: 2px 2px 10px; line-height: 1.55; flex-wrap: wrap; word-break: break-word; border-bottom: 1px solid #f1f5f9; margin-bottom: 8px; }}
  .sent-footnote {{ font-size: 11px; color: #94a3b8; line-height: 1.7; margin-top: 12px; padding: 10px 12px; background: var(--surface-2, #f8fafc); border-left: 3px solid #cbd5e1; border-radius: 0 8px 8px 0; }}
  /* R205 提示/对照行美化: 圆角胶囊 + 悬停微抬 + 图标感(用 ::before 注入符号), 三态配色一致 */
  .sent-acc {{ font-size: 12px; color: #475569; line-height: 1.65; margin: -4px 0 12px; padding: 10px 14px; background: #fffbeb; border: 1px solid #fde68a; border-left: 4px solid #f0c14b; border-radius: 10px; box-shadow: 0 1px 3px rgba(15,23,42,.04); transition: box-shadow .16s ease, transform .16s ease; }}
  .sent-acc:hover {{ box-shadow: 0 4px 12px rgba(15,23,42,.08); transform: translateY(-1px); }}
  .sent-acc b {{ color: #b45309; font-variant-numeric: tabular-nums; }}
  .sent-diverge {{ background: #fff7ed; border-color: #fdba74; border-left-color: #ea580c; }}
  .sent-diverge:hover {{ box-shadow: 0 4px 12px rgba(234,88,12,.10); }}
  .sent-diverge b {{ color: #c2410c; }}
  .sent-anchor {{ background: #ecfeff; border-color: #a5f3fc; border-left-color: #0891b2; }}
  .sent-anchor:hover {{ box-shadow: 0 4px 12px rgba(8,145,178,.10); }}
  .sent-anchor b {{ color: #0e7490; }}
  .sent-acc-sub {{ margin-top: -8px; background: #f8fafc; border-color: #e2e8f0; border-left-color: #cbd5e1; font-size: 11.5px; }}
  .sent-acc-sub .zc {{ color: #64748b; margin-right: 14px; white-space: nowrap; }}
  .sent-acc-sub .zc b {{ color: #475569; }}
  .sent-diverge {{ background: #fff7ed; border-color: #fdba74; border-left-color: #ea580c; }}
  .sent-diverge b {{ color: #c2410c; }}
  .sent-anchor {{ background: #ecfeff; border-color: #a5f3fc; border-left-color: #0891b2; }}
  .sent-anchor b {{ color: #0e7490; }}
  .sent-zone-cap {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 11px; color: #64748b; margin-top: 6px; padding: 0 2px; line-height: 1.6; }}
  .sent-zone-cap .zc-g {{ color: #18a058; font-weight: 600; }}
  .sent-zone-cap .zc-r {{ color: #e54545; font-weight: 600; }}
  .sent-zone-cap .zc-w {{ color: #64748b; font-weight: 600; }}

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
    .hero {{ gap: 8px; }}
    .sent-head {{ padding: 12px 14px; }}
    .sent-big-score {{ font-size: 34px; }}
    .sent-head-row {{ gap: 10px; }}
  }}
  /* ===== 模块互联互通 ===== */
  nav.sym-rail {{ position: sticky; top: 54px; z-index: 49; background: rgba(255,255,255,0.97); backdrop-filter: blur(8px); border: 1px solid #e2e8f0; border-radius: 12px; padding: 6px 10px; margin: 10px 0 18px; display: flex; flex-wrap: wrap; gap: 6px; box-shadow: 0 4px 14px rgba(15,23,42,0.05); }}
  nav.sym-rail .chip {{ text-decoration: none; font-size: 13px; color: #475569; background: #f1f5f9; padding: 5px 12px; border-radius: 999px; cursor: pointer; transition: all .15s ease; white-space: nowrap; border: 1px solid transparent; }}
  nav.sym-rail .chip:hover {{ background: #e2e8f0; color: #1e293b; }}
  nav.sym-rail .chip.active {{ background: {BLUE}; color: #fff; border-color: {BLUE}; box-shadow: 0 2px 8px rgba(43,108,176,0.25); }}
  .card.linked-active {{ border-color: {BLUE}; box-shadow: 0 0 0 2px rgba(43,108,176,0.25), 0 6px 18px rgba(15,23,42,0.12); transform: translateY(-2px); }}
  tr.linkrow {{ cursor: pointer; }}
  .tbl tbody tr.row-linked td {{ background: #f8fafc; }}
  .tbl tbody tr.row-linked:hover td {{ background: #f1f5f9; }}
  .sec-flash {{ animation: secflash 1.1s ease; }}
  @keyframes secflash {{ 0% {{ box-shadow: 0 0 0 0 rgba(43,108,176,0); }} 25% {{ box-shadow: 0 0 0 4px rgba(43,108,176,0.35); }} 100% {{ box-shadow: 0 1px 3px rgba(15,23,42,0.04); }} }}
  @media (max-width: 720px) {{ nav.sym-rail {{ top: 48px; }} nav.sym-rail .chip {{ padding: 4px 9px; font-size: 12px; }} }}
  /* ===== 手机横屏深度优化（宽>高，视口矮） ===== */
  /* ===== 手机横屏深度优化（宽>高，视口矮） =====
     目标：把宽屏优势用满——K线/推演左右并排、顶部与卡片竖向压缩、图表高度自适应矮视口，
     文本块(质量证书/免责/执行/表格/方法)统一收紧，避免横屏下大量无效滚动。 */
  @media (orientation: landscape) and (max-height: 560px) {{
    body {{ padding: 6px calc(8px + env(safe-area-inset-left)) 6px calc(8px + env(safe-area-inset-right)); -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
    .wrap {{ max-width: 100%; }}
    header {{ padding: 10px 16px !important; border-radius: 12px; margin-bottom: 8px !important; }}
    header h1 {{ font-size: 16px !important; letter-spacing: 0; }}
    header p {{ font-size: 10.5px; line-height: 1.4; margin-top: 2px; max-height: 2.9em; overflow: hidden; }}
    /* 导航：横屏下转顶部横滑条并吸顶冻结（sticky），滚动时保持可点 */
    nav.toc, nav.sym-rail {{
      position: sticky; flex-wrap: nowrap; overflow-x: auto; white-space: nowrap;
      margin: 4px 0; padding: 4px 6px; gap: 4px; -webkit-overflow-scrolling: touch; z-index: 50;
    }}
    nav.toc {{ top: calc(env(safe-area-inset-top, 0px)); }}
    nav.sym-rail {{ top: calc(env(safe-area-inset-top, 0px) + 40px); }}
    nav.toc a, nav.sym-rail .chip {{ flex: 0 0 auto; }}
    nav.toc a .num {{ display: none; }}
    /* 顶部 KPI 行收紧 */
    .hero {{ gap: 6px; margin: 8px 0 2px; }}
    .kpi {{ min-width: 78px; padding: 6px 7px; }}
    .kpi-v {{ font-size: 18px; }}
    .kpi-l {{ font-size: 10.5px; }}
    /* 指数概览卡片：宽屏多列 + 收紧 */
    .cards {{ grid-template-columns: repeat(auto-fit, minmax(138px, 1fr)); gap: 6px; }}
    .card {{ padding: 9px 9px; }}
    .card::before {{ margin: -9px -9px 8px !important; }}
    .price {{ font-size: 16px; margin: 4px 0; }}
    .sym {{ font-size: 10px; }}
    .kv {{ font-size: 11px; padding: 2px 0; }}
    .chips {{ gap: 4px; margin-top: 5px; }}
    h2.sec {{ font-size: 14px; margin: 10px 0 6px; }}
    /* 每指数：左=K线(标题)/右=推演(标题) 并排，解读与备注整行置底 */
    section.panel {{
      display: grid; grid-template-columns: 1.45fr 1fr;
      grid-template-areas:
        "title fctitle"
        "kline fc"
        "verdict verdict"
        "foot  foot";
      gap: 6px 10px; padding: 8px 10px; margin-bottom: 8px; align-items: start;
    }}
    section.panel > h2 {{ grid-area: title; font-size: 14px; margin: 0; }}
    section.panel > h3.fc-title {{ grid-area: fctitle; font-size: 12px; margin: 0; }}
    section.panel > .chartbox:first-of-type {{ grid-area: kline; }}
    section.panel > .chartbox.fcbox {{ grid-area: fc; }}
    section.panel > .verdict {{ grid-area: verdict; }}
    section.panel > .sec-foot {{ grid-area: foot; display: block; }}
    .verdict {{ margin-top: 0; font-size: 12px; }}
    .verdict p {{ line-height: 1.5; }}
    .fc-note2 {{ font-size: 11.5px; line-height: 1.6; margin: 6px 0; }}
    .fc-legend {{ font-size: 10.5px; gap: 7px; margin: 4px 0; }}
    .fc-targets {{ font-size: 11px; }}
    .pathcheck {{ padding: 7px 10px; margin-top: 6px; }}
    .pathcheck > b, .pc-row, .pc-calib {{ font-size: 11.5px; }}
    .pc-lab {{ width: 54px; }}
    /* 图表高度自适应横屏矮视口（!important 覆盖内联 640/440px）；
       dvh 计入移动端浏览器栏；封顶 280px 给底部解读留空间，下限 150px 防过小 */
    .echart-main {{ height: calc(100vh - 200px) !important; height: calc(100dvh - 200px) !important; max-height: 300px; min-height: 160px; }}
    .echart-toolbar {{ font-size: 10.5px; padding: 4px 6px; }}
    /* 文本块统一收紧 */
    .qc-card {{ margin: 8px 0; padding: 10px 12px; }}
    .qc-head {{ font-size: 13px; }}
    .qc-cell {{ flex: 1 1 86px; padding: 6px 7px; }}
    .qc-v {{ font-size: 15px; }}
    .qc-l {{ font-size: 10px; }}
    .qc-foot {{ font-size: 11px; margin-top: 6px; padding-top: 5px; }}
    .qc-regime-row {{ font-size: 11px; padding: 2px 0; }}
    .quality, .disclaimer, .exec {{ padding: 10px 12px; font-size: 11.5px; line-height: 1.7; }}
    .quality-grid {{ gap: 6px; }}
    .qcard {{ padding: 8px 9px; }}
    details.method {{ font-size: 11.5px; }}
    .fc-title {{ font-size: 12px; margin: 3px 0; }}
    .tbl {{ font-size: 10.5px; }}
    .tbl th, .tbl td {{ padding: 4px 5px; white-space: nowrap; }}
    .tablescroll {{ border-radius: 8px; }}
    .tablescroll::-webkit-scrollbar {{ height: 6px; }}
    .tablescroll::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 3px; }}
  }}

  /* ===================== R134 深度美化增强层 =====================
     覆盖于基础样式之上，不改动任何 DOM 结构与 JS 交互；
     仅通过更靠后的同选择器规则提升质感（现代专业金融仪表盘风格）。 */
  :root {{
    --bg1:#eef2f7; --bg2:#e6edf5;
    --card:#ffffff; --panel:#ffffff;
    --ink:#0f172a; --muted:#64748b;
    --border:#e3e9f2; --border2:#cdd7e5;
    --primary:#2b6cb0; --primary2:#60a5fa;
    --up:#e54545; --down:#18a058;
    --shadow-sm:0 1px 3px rgba(15,23,42,.06);
    --shadow:0 6px 20px rgba(15,23,42,.08);
    --shadow-lg:0 14px 38px rgba(15,23,42,.14);
    --radius:14px;
  }}
  body {{ background:linear-gradient(180deg,#eef2f7 0%, #f7fafc 45%, #eef2f7 100%); color:var(--ink); }}
  .wrap {{ max-width:1160px; }}

  /* —— 顶部品牌 banner —— */
  header {{ background:linear-gradient(120deg,#0f172a 0%, #1e3a5f 52%, #2b6cb0 100%); color:#fff; border-radius:18px; padding:22px 28px; margin-bottom:18px; box-shadow:var(--shadow-lg); position:relative; overflow:hidden; }}
  header::after {{ content:""; position:absolute; right:-50px; top:-50px; width:220px; height:220px; background:radial-gradient(circle, rgba(255,255,255,.14), transparent 70%); pointer-events:none; }}
  header h1 {{ color:#fff; font-size:28px; letter-spacing:.5px; }}
  header p {{ color:rgba(255,255,255,.82); }}

  /* —— 指数概览卡片 —— */
  .card {{ border-radius:var(--radius); box-shadow:var(--shadow); border:1px solid var(--border); overflow:hidden; background:var(--card); }}
  .card::before {{ content:""; display:block; height:4px; background:linear-gradient(90deg,var(--primary),var(--primary2)); margin:-14px -16px 12px; }}
  .card:hover {{ box-shadow:var(--shadow-lg); transform:translateY(-3px); border-color:var(--border2); }}
  .card-head .idx-name {{ letter-spacing:.3px; }}

  /* —— 区块面板 —— */
  .panel {{ border-radius:var(--radius); box-shadow:var(--shadow); border:1px solid var(--border); background:var(--panel); }}
  .panel h2 {{ border-bottom:1px solid #f1f5f9; padding-bottom:9px; }}

  /* —— KPI 指标卡 —— */
  .kpi {{ border-radius:var(--radius); box-shadow:var(--shadow); border:1px solid var(--border); background:var(--card); }}
  .kpi:hover {{ box-shadow:var(--shadow-lg); transform:translateY(-2px); }}
  .kpi-v {{ letter-spacing:-.5px; }}

  /* —— 表格 —— */
  .tbl {{ border-radius:10px; overflow:hidden; box-shadow:var(--shadow-sm); }}
  .tbl th {{ background:linear-gradient(180deg,#f8fafc,#eef2f7); }}
  .tbl tbody tr:nth-child(even) td {{ background:#fafcfe; }}
  .tbl tbody tr:hover td {{ background:#eef4fb; }}

  /* —— 章节标题 —— */
  h2.sec {{ background:linear-gradient(90deg, rgba(43,108,176,.10), transparent 55%); border-radius:0 10px 10px 0; padding:9px 16px; }}

  /* —— 图表容器 —— */
  .chartbox {{ box-shadow:var(--shadow-sm); border:1px solid #eef2f7; }}

  /* —— 导航胶囊 —— */
  nav.toc, nav.sym-rail {{ box-shadow:var(--shadow); }}
  nav.toc a.active {{ background:linear-gradient(135deg,var(--primary),var(--primary2)); }}

  /* —— 预测质量自检卡 —— */
  .qc-card {{ box-shadow:var(--shadow); }}
  .fc-note {{ background:#fffbeb; }}

  /* ===== R153 全站深度美化增强层（桌面/竖屏基准，置于 R134 之后、R152 横屏层之前） =====
     在 R134 基础上做更深的专业化打磨：排版层级、分层阴影令牌、卡片质感、图表容器、表格、章节标题、提示块、过渡与入场动画。
     横屏由随后的 R152 媒体层覆盖，本层作为全站基准（非媒体，桌面/竖屏生效）。 */
  :root {{
    --surface:#ffffff;
    --surface-2:#f7fafc;
    --shadow-md: 0 12px 32px rgba(15,23,42,.10);
    --shadow-sm2: 0 2px 10px rgba(15,23,42,.05);
    --grad-accent: linear-gradient(135deg, var(--primary), var(--primary2));
    --radius-lg: 18px;
    --radius-md: 14px;
    --radius-sm: 10px;
  }}

  /* 排版：数据等宽数字 + 标题字距 + 抗锯齿 */
  body {{ font-feature-settings: "tnum" 1; text-rendering: optimizeLegibility; }}
  h1, h2, h3, h4 {{ letter-spacing: .2px; }}
  .kpi-v, .price, .qc-v, .tbl td, .tbl th, .pc-p, .pc-h {{ font-variant-numeric: tabular-nums; }}

  /* 指数概览卡：渐变顶条 + 悬停彩色阴影 + 价格强调 */
  .card {{ border-radius: var(--radius-md); box-shadow: var(--shadow-md); border: 1px solid var(--border); transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease; }}
  .card::before {{ height: 4px; background: var(--grad-accent); margin: -14px -16px 12px; }}
  .card:hover {{ box-shadow: 0 18px 44px rgba(43,108,176,.20); transform: translateY(-4px); border-color: var(--primary2); }}
  .card .price {{ font-weight: 800; letter-spacing: -.3px; }}
  .card .sym {{ letter-spacing: .4px; }}

  /* 区块面板：圆角 + 分层阴影 */
  .panel {{ border-radius: var(--radius-lg); box-shadow: var(--shadow-md); border: 1px solid var(--border); }}
  .panel h2 {{ border-bottom: 1px solid #eef2f7; padding-bottom: 10px; color: var(--ink); }}

  /* KPI 卡：渐变顶饰 + 悬停微抬 */
  .kpi {{ border-radius: var(--radius-md); box-shadow: var(--shadow-sm2); border: 1px solid var(--border); background: var(--surface); transition: transform .18s ease, box-shadow .18s ease; }}
  .kpi:hover {{ transform: translateY(-3px); box-shadow: var(--shadow-md); }}
  .kpi-v {{ letter-spacing: -.6px; }}

  /* 图表容器：圆角 + 内描边 + 柔和底色 */
  .chartbox {{ border-radius: var(--radius-md); box-shadow: var(--shadow-sm2); border: 1px solid #eef2f7; overflow: hidden; background: linear-gradient(180deg,#ffffff,#fbfdff); }}
  .echart-toolbar {{ border-radius: var(--radius-sm); background: var(--surface-2); }}

  /* 章节标题：渐变胶囊 + 左主色强调 */
  h2.sec {{ background: linear-gradient(90deg, rgba(43,108,176,.12), rgba(43,108,176,.02) 62%, transparent); border-radius: 0 12px 12px 0; padding: 11px 18px; font-weight: 800; color: #1e3a5f; box-shadow: inset 3px 0 0 var(--primary); }}

  /* 表格：圆角 + 表头渐变 + 隔行柔色 + 悬停高亮 + 数字对齐 */
  .tbl {{ border-radius: var(--radius-md); overflow: hidden; box-shadow: var(--shadow-sm2); font-variant-numeric: tabular-nums; }}
  .tbl th {{ background: linear-gradient(180deg,#f1f5f9,#e6edf5); color: #334155; font-weight: 700; letter-spacing: .3px; }}
  .tbl tbody tr:nth-child(even) td {{ background: #fafcfe; }}
  .tbl tbody tr:hover td {{ background: #eaf2fb; }}
  .tbl td, .tbl th {{ padding: 9px 12px; }}

  /* 顶部导航胶囊：激活态强化 */
  nav.toc a.active {{ background: var(--grad-accent); color:#fff; box-shadow: 0 4px 12px rgba(43,108,176,.30); }}

  /* 提示/结论块：左侧强调边 + 柔色底 */
  .exec {{ background: linear-gradient(180deg,#eff6ff,#f6faff); border: 1px solid #bfdbfe; border-left: 4px solid var(--primary); border-radius: var(--radius-md); }}
  .disclaimer {{ background: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #94a3b8; border-radius: var(--radius-md); color: #475569; }}
  .conclusion {{ border-left: 4px solid var(--primary); }}
  .verdict {{ background: linear-gradient(180deg,#f0f6ff,#f7faff); border-left: 4px solid var(--primary); border-radius: 0 8px 8px 0; }}

  /* 预测质量自检卡 */
  .qc-card {{ box-shadow: var(--shadow-md); border-radius: var(--radius-md); }}

  /* —— 市场情绪板块（R196/R205：对齐看板主体现代视觉语言） —— */
  /* 头图：分层阴影 + 悬停微抬，呼应 .card/.kpi */
  .sent-head {{ box-shadow: var(--shadow-md); transition: box-shadow .18s ease, transform .18s ease; }}
  .sent-head:hover {{ box-shadow: 0 18px 44px rgba(43,108,176,.16); transform: translateY(-2px); }}
  .sent-main-title {{ letter-spacing: .3px; }}
  .sent-big-score {{ letter-spacing: -1.2px; }}
  /* R205 仪表盘：悬停微旋指针 + 分数辉光 */
  .sent-gauge {{ transition: transform .25s ease; }}
  .sent-head:hover .sent-gauge-svg {{ transform: scale(1.02); }}
  .sent-gauge-svg {{ transition: transform .25s ease; transform-origin: center bottom; }}
  .sent-chart-card {{ box-shadow: var(--shadow-md); background: linear-gradient(180deg,#ffffff,#fbfdff); }}
  .sent-chart-card:hover {{ box-shadow: var(--shadow); }}
  /* 图表卡内 ECharts 容器：圆角 + 内描边，对齐 .chartbox */
  .sent-chart-card .echart-main {{ border-radius: var(--radius-sm); background: linear-gradient(180deg,#ffffff,#fbfdff); }}
  /* 提示/解读块：柔色胶囊 + 渐变底，对齐 .fc-note / .disclaimer 观感 */
  .sent-interp {{ background: linear-gradient(180deg,#f8fafc,#f3f7fc); }}
  .sent-footnote {{ background: linear-gradient(180deg,#f8fafc,#f3f7fc); border-left-color: #cbd5e1; }}
  .sent-acc {{ background: linear-gradient(180deg,#fffbeb,#fff8e6); }}
  .sent-diverge {{ background: linear-gradient(180deg,#fff7ed,#fff3e6); }}
  .sent-anchor {{ background: linear-gradient(180deg,#ecfeff,#e6fbff); }}
  /* 图表工具栏：圆角底色容器，对齐 .echart-toolbar 增强层（R153 已对通用 .echart-toolbar 处理，
     此处情绪专用 toolbar 因外层容器不同需单独补圆角底色） */
  .sent-chart-card .echart-toolbar {{ background: var(--surface-2); border-radius: var(--radius-sm); border-bottom: none; padding: 8px 10px; margin-bottom: 6px; }}
  /* 等宽数字保护，对齐全站 tnum 排版 */
  .sent-big-score, .sent-pct, .sent-trend, .sent-bar-labels, .sent-zstat {{ font-variant-numeric: tabular-nums; }}

  /* 通用柔和过渡 */
  .card, .kpi, .panel, nav.toc a, nav.sym-rail .chip {{ transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease; }}

  /* 入场动画（仅 .card / .qc-card，避免与 section.panel 的 sec-flash 冲突）；尊重减弱动效偏好。
     关键：fill-mode 用 backwards 而非 both——both 会把 to 帧的 transform:none 持久钉住(动画优先级高于普通 :hover 规则)，
     导致 .card:hover{{transform:translateY(-4px)}} 悬停抬升被废掉；backwards 仅动画前防闪烁、结束后不钉 transform，悬停抬升恢复。 */
  @media (prefers-reduced-motion: no-preference) {{
    .card, .qc-card {{ animation: rise .55s ease backwards; }}
    @keyframes rise {{ from {{ opacity:0; transform: translateY(12px); }} to {{ opacity:1; transform:none; }} }}
  }}

  /* ===== R152 横屏深度美化增强层 =====
     置于 R134 美化层之后，仅 (orientation:landscape)&(max-height:560px) 生效；
     源码靠后 => 同优先级下天然覆盖 R134，把横屏做成专业交易终端观感。 */
  @media (orientation: landscape) and (max-height: 560px) {{
    /* 全局令牌（横屏紧凑化覆盖） */
    .wrap {{ max-width: 100%; }}

    /* 品牌 banner：紧凑但保留深海渐变 + 左侧高光条 */
    header {{
      padding: 9px 18px 9px 20px !important; border-radius: 14px; margin-bottom: 7px !important;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
    }}
    header h1 {{ font-size: 16px !important; letter-spacing: .3px; }}
    header p {{ font-size: 10px; line-height: 1.35; margin: 1px 0 0; max-height: 2.6em; overflow: hidden; color: rgba(255,255,255,.82) !important; }}
    header::before {{ content:""; position:absolute; left:0; top:10px; bottom:10px; width:4px; background:linear-gradient(180deg,#60a5fa,#2b6cb0); border-radius:4px; }}

    /* 顶部导航：磨砂胶囊横滑条 + 吸顶冻结（sticky） */
    nav.toc, nav.sym-rail {{
      position: sticky; flex-wrap: nowrap; overflow-x: auto; white-space: nowrap;
      margin: 3px 0; padding: 5px 9px; gap: 5px; -webkit-overflow-scrolling: touch; z-index: 50;
      background: rgba(255,255,255,.82); -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
      border: 1px solid #e2e8f0; border-radius: 12px; box-shadow: 0 4px 14px rgba(15,23,42,.05);
    }}
    nav.toc {{ top: calc(env(safe-area-inset-top, 0px)); }}
    nav.sym-rail {{ top: calc(env(safe-area-inset-top, 0px) + 40px); }}
    h2.sec, section.panel {{ scroll-margin-top: calc(env(safe-area-inset-top, 0px) + 84px); }}
    nav.toc a, nav.sym-rail .chip {{ flex: 0 0 auto; border-radius: 999px; transition: all .15s ease; }}
    nav.toc a .num {{ display: none; }}
    nav.toc a.active {{ background: linear-gradient(135deg,var(--primary),var(--primary2)); color:#fff; box-shadow: 0 2px 8px rgba(43,108,176,.3); }}

    /* KPI 小卡：精致 + 悬停微抬 */
    .hero {{ gap: 6px; margin: 8px 0 4px; }}
    .kpi {{
      min-width: 80px; padding: 7px 9px; border-radius: 12px; background: #fff;
      border: 1px solid #e5e9f0; box-shadow: 0 1px 3px rgba(15,23,42,.05);
      transition: transform .15s ease, box-shadow .15s ease;
    }}
    .kpi:hover {{ transform: translateY(-2px); box-shadow: var(--shadow); }}
    .kpi-v {{ font-size: 18px; font-variant-numeric: tabular-nums; letter-spacing: -.5px; }}
    .kpi-l {{ font-size: 10px; }}

    /* 指数概览卡：宽屏多列 + 渐变顶条 + 悬停微抬 */
    .cards {{ grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap: 6px; }}
    .card {{ padding: 9px 10px; border-radius: 12px; }}
    .card::before {{ margin: -9px -10px 8px !important; height: 3px; }}
    .card:hover {{ transform: translateY(-2px); }}

    /* 核心：每指数双栏交易终端 */
    section.panel {{
      display: grid; grid-template-columns: 1.5fr 1fr;
      grid-template-areas:
        "title fctitle"
        "kline fc"
        "verdict verdict"
        "foot foot";
      gap: 8px 14px; padding: 10px 12px; margin-bottom: 9px; align-items: stretch;
      border-radius: 14px; background: #fff; border: 1px solid #e3e9f2; box-shadow: var(--shadow);
    }}
    section.panel > h2 {{ grid-area: title; font-size: 14px; margin: 0; padding: 0; border: 0; align-self: center; color: var(--ink); }}
    section.panel > h3.fc-title {{ grid-area: fctitle; font-size: 12px; margin: 0; color: var(--muted); text-align: right; align-self: center; }}
    section.panel > .chartbox:first-of-type {{
      grid-area: kline; background: #fbfdff; border: 1px solid #eef2f7; border-radius: 10px; padding: 6px;
    }}
    section.panel > .chartbox.fcbox {{
      grid-area: fc; background: #fbfdff; border: 1px solid #eef2f7; border-left: 1px dashed #dbe4ef; border-radius: 10px; padding: 6px;
    }}
    .echart-main {{ height: calc(100dvh - 215px) !important; max-height: 290px; min-height: 150px; border-radius: 8px; }}
    .echart-toolbar {{ font-size: 10.5px; padding: 4px 7px; border-radius: 8px; background:#f1f5f9; }}

    /* 解读 / 图例 / 路径校验区 */
    section.panel > .verdict {{
      grid-area: verdict; margin-top: 2px; font-size: 12px; line-height: 1.5;
      background: #f8fafc; border: 1px solid #eef2f7; border-left: 3px solid var(--primary); border-radius: 8px; padding: 8px 10px;
    }}
    section.panel > .sec-foot {{ grid-area: foot; display: block; }}
    .fc-note2 {{ font-size: 11.5px; line-height: 1.55; margin: 6px 0; background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 7px 9px; }}
    .fc-legend {{ font-size: 10.5px; gap: 8px; margin: 5px 0; color: var(--muted); }}
    .pathcheck {{ padding: 8px 11px; margin-top: 7px; border-radius: 10px; background: #f8fafc; border: 1px solid #e2e8f0; }}
    .pathcheck > b {{ font-size: 12px; }}

    /* 章节标题：渐变标签（继承 R134，横屏微调） */
    h2.sec {{ font-size: 14px; margin: 10px 0 6px; }}

    /* 表格：精致表头 + 圆角滚动条 */
    .tbl {{ font-size: 10.5px; border-radius: 10px; }}
    .tbl th, .tbl td {{ padding: 5px 6px; white-space: nowrap; }}
    .tablescroll {{ border-radius: 10px; }}
    .tablescroll::-webkit-scrollbar {{ height: 7px; }}
    .tablescroll::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 4px; }}
    .tablescroll::-webkit-scrollbar-track {{ background: #f1f5f9; border-radius: 4px; }}

    /* 文本块圆角统一 */
    .qc-card {{ margin: 8px 0; padding: 11px 13px; border-radius: 14px; }}
    .qc-head {{ font-size: 13px; }}
    .qc-v {{ font-size: 15px; }}
    .qc-l {{ font-size: 10px; }}
    .quality, .disclaimer, .exec {{ padding: 11px 13px; font-size: 11.5px; line-height: 1.7; border-radius: 14px; }}
    .qc-regime-row {{ font-size: 11px; }}

    /* 通用柔和过渡 */
    .card, .kpi, nav.toc a, nav.sym-rail .chip {{ transition: all .15s ease; }}

    /* —— 市场情绪板块：横屏紧凑化（R196，对齐看板主体横屏层，避免拥挤/溢出） —— */
    .sent-head {{ padding: 11px 13px; margin-bottom: 9px; border-radius: 12px; }}
    .sent-head::before {{ margin: -11px -13px 10px; height: 3px; }}
    .sent-head-row {{ gap: 8px; }}
    .sent-main-title {{ font-size: 14px; }}
    /* R205 横屏: 仪表盘缩小防溢出 */
    .sent-gauge {{ flex: 0 0 96px; }}
    .sent-gauge-svg {{ width: 96px; height: 58px; }}
    .sent-gauge-num {{ font-size: 15px; }}
    .sent-gauge-sub {{ font-size: 7px; }}
    .sent-trend-badge {{ font-size: 9.5px; padding: 1px 7px; }}
    .sent-big-score {{ font-size: 30px; letter-spacing: -.6px; }}
    .sent-score-meta {{ font-size: 10.5px; }}
    .sent-bar-wrap {{ margin-top: 9px; }}
    .sent-bar-tick-label {{ font-size: 8px; top: 14px; }}
    .sent-bar-labels {{ font-size: 10px; margin-top: 14px; }}
    .sent-zstat {{ font-size: 10px; gap: 4px 10px; margin-top: 7px; }}
    .sent-zstat-bar {{ height: 5px; margin-top: 5px; }}
    .sent-interp {{ font-size: 11px; padding: 6px 8px; margin-top: 6px; }}
    .sent-chart-card {{ padding: 7px 9px 8px; margin-bottom: 9px; border-radius: 12px; }}
    .sent-chart-card .echart-toolbar {{ font-size: 10.5px; padding: 4px 6px; margin-bottom: 5px; }}
    .sent-chart-card .echart-main {{ height: calc(100dvh - 220px) !important; max-height: 240px; min-height: 140px; border-radius: 8px; }}
    .sent-zone-cap {{ font-size: 10px; gap: 8px; margin-top: 4px; }}
    .sent-acc {{ font-size: 10.5px; padding: 6px 8px; margin: -4px 0 8px; }}
    .sent-footnote {{ font-size: 10px; padding: 7px 9px; margin-top: 9px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>A股主要指数缠论结构分析报告</h1>
    <p>数据区间：2021-01-04 ~ {last_date} · 生成时间：{gen_time}<br>日线+周线+月线 · 前复权</p>
  </header>
  {freshness_banner}
  {cert_html}
  <nav class="toc">
    <a href="#s1"><span class="num">一</span>决策总览</a>
    <a href="#s2"><span class="num">二</span>市场情绪</a>
    <a href="#s3"><span class="num">三</span>分指数图解</a>
    <a href="#s4"><span class="num">四</span>关键位与推演</a>
    <a href="#s5"><span class="num">五</span>信号回测对比</a>
    <a href="#s6"><span class="num">六</span>免责说明</a>
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
    <a href="#s2" style="text-decoration:none;color:inherit"><div class="kpi" style="border-top:3px solid {_szc};cursor:pointer"><div class="kpi-v" style="color:{_szc}">{'' if _s_score is None else ('%.0f' % _s_score)}</div><div class="kpi-l">市场情绪 · {_szl}{'' if sent_full else '（未生成）'}</div></div></a>
  </div>
  {sent_board}
  <h2 class="sec" id="s3">三、分指数结构图解</h2>
  {"".join(sections)}

  <h2 class="sec" id="s4">四、关键位与推演汇总</h2>
  <div class="panel"><div class="tablescroll">{levels_table(data, results, results_week, results_month, scores)}</div></div>
  <div class="panel"><div class="tablescroll">{forecast_summary_table(data, results, results_week, results_month, forecast_info)}</div></div>
  <div class="panel conclusion">
    <h3 class="fc-title">各指数推演结论</h3>
    <ul>{"".join(conclusions)}</ul>
    {diverge_note}
    {calib_note}
    {sentiment_note}
  </div>

  <h2 class="sec" id="s5">五、信号回测与走势对比</h2>
  <div class="panel"><div class="tablescroll">{backtest_table(backtests)}</div></div>
  <div class="panel"><div class="tablescroll">{rr_table(data, results)}</div></div>
  <div class="panel"><div class="tablescroll">{robustness_table(robust, data)}</div></div>
  <details class="panel" style="cursor:pointer">
    <summary style="font-weight:700;color:{BLUE};cursor:pointer">五指数归一化对比图（2021=100）</summary>
    <div style="margin-top:12px">{compare_svg(data)}</div>
  </details>

  <h2 class="sec" id="s6">六、免责说明</h2>
  <div class="disclaimer">
    <b>免责声明：</b>本报告基于缠论技术分析的自动化结构划分，推演概率为启发式估算，非点位预测，不构成投资建议。市场有风险，决策需独立。
  </div>
</div>
<script>
function navH(){{ var t=document.querySelector('nav.toc'), r=document.getElementById('symRail'); var h=(t?t.offsetHeight:0)+(r?r.offsetHeight:0); return h + 10; }}
(function(){{
  // 防御式：link+sec 配对数组，避免某 TOC 链接目标缺失时 filter(Boolean) 使 links[i] 与 sections[i] 索引错位而标错 active。
  var toc = Array.prototype.map.call(document.querySelectorAll('nav.toc a'), function(a){{ return {{ link: a, sec: document.querySelector(a.getAttribute('href')) }}; }}).filter(function(p){{ return p.sec; }});
  function onScroll(){{
    if (!toc.length) return;
    var y = window.scrollY + (window.matchMedia('(orientation: landscape) and (max-height: 560px)').matches ? navH() : 90);
    var cur = toc[0].link;
    toc.forEach(function(p){{ if (p.sec && p.sec.offsetTop <= y) cur = p.link; }});
    toc.forEach(function(p){{ p.link.classList.remove('active'); }});
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
  function offset(){{ return (window.matchMedia('(orientation: landscape) and (max-height: 560px)').matches) ? navH() : 108; }}
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
<script>
(function(){{
  function resizeAll(){{ (window.__charts||[]).forEach(function(c){{ try{{ c.resize(); }}catch(e){{}} }}); }}
  window.addEventListener('resize', resizeAll, {{passive:true}});
  window.addEventListener('orientationchange', function(){{ setTimeout(resizeAll, 250); }});
  window.addEventListener('load', function(){{ setTimeout(resizeAll, 120); }});
}})();
</script>
__POLL_JS__
</body>
</html>"""

    # R229: 前端版本检测——轮询 version.json, 发现新版本(生成时间不同)弹红色横幅提示刷新,
    # 根治 GitHub Pages CDN 边缘 max-age=600 导致的"服务端已更新但用户浏览器/CDN 仍吐旧版"感知偏差。
    _POLL_JS = '''
    <script>
    (function(){
      var bt = window.__BUILD_TIME__;
      if(!bt) return;
      function show(){
        var b=document.getElementById('ver-banner');
        if(!b){
          b=document.createElement('div');b.id='ver-banner';
          b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:9999;background:#dc2626;color:#fff;text-align:center;padding:9px;font-size:14px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.2)';
          b.onclick=function(){location.reload(true);};
          (document.body||document.documentElement).appendChild(b);
        }
        b.textContent='检测到新版本（'+window.__LATEST__+'），点击此处刷新';
      }
      function check(){
        fetch('./version.json?t='+Date.now()).then(function(r){return r.json();}).then(function(v){
          if(v && v.time && v.time!==bt){
            window.__LATEST__=v.time;
            show();
          }
        }).catch(function(){});
      }
      setTimeout(check, 8000);
      setInterval(check, 120000);
    })();
    </script>
    '''

    # R173: 原子写——先写 .tmp 再 os.replace, 避免任一指数计算异常导致本次不写文件、
    # 磁盘残留上一次成功的陈旧 report.html 被误当最新。
    # R229: 注入版本检测 JS + 生成 version.json 供前端轮询对比新版本
    html = html.replace("__POLL_JS__", _POLL_JS)
    import subprocess as _sp
    _sha = "local"
    try:
        _sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=_base, stderr=_sp.DEVNULL).decode().strip()
    except Exception:
        _sha = "local"
    with open(os.path.join(_base, "version.json"), "w", encoding="utf-8") as _vf:
        json.dump({"time": gen_time, "sha": _sha}, _vf, ensure_ascii=False)

    _out = os.path.join(_base, "report.html")
    _tmp = _out + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(_tmp, _out)
    print("saved -> report.html")  # R349: 实际路径为仓库根 report.html(_base=report.py 目录), 旧文案 chanlun/ 为旧布局残留


if __name__ == "__main__":
    main()
