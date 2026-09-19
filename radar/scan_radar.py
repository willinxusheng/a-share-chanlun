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
import math
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
# R445: 行业信号计数改「规模可比」口径 —— n_sig_top/bot 是**绝对数**, 但 32 个板块的成分数
# 从 16(综合) 到 386(机械设备) 差 **24 倍** ⇒ 绝对数排序被大板块恒定霸榜。实测(09-11):
#   煤炭 29 只成分 / 6 个顶信号 → 绝对数仅第 15, 密度却是全市场第一 20.7%;
#   电子 368 只 / 15 个 → 绝对数第 4, 摊薄后只剩 4.1%。
# 判据改 **95% Wilson 置信下界**(Wilson score interval lower bound):
#   小样本自动降权(22 只里 1 只 = 原始密度 4.5%, 下界仅 0.8% → 不误判为"集中");
#   大样本不因规模被抬(386 只 20 个信号 → 下界 3.4%, 相对 29 只 6 个的 9.9% 落于其后);
#   单调、无需人工设"最小样本量"门槛。**展示值仍是原始密度**(可自行按 k/n 复核),
#   判据/排序/色条/regime 四处一律用下界 —— 前端 tooltip 与说明栏同步披露。
SIG_LB_Z = 1.96                # Wilson 下界置信水平(95% 双侧)
DENS_LB_STRONG = 2.5           # 「显著集中」门槛(百分点): 色条 + regime 共用同一判据
SIG_ABS_MIN = 2                # 集中度绝对下限(1 只不算"集中", 与旧口径兼容)
IND_POS_WIN = 250              # 行业位置窗口(根, ≈近一年): 回撤/价格分位统一用此窗口
# R447: 个股/ETF 层补「低吸/趋势地基」—— 与行业同源实现(_mom/_rs_pctile 直接复用),
# 但**口径必须换**: 行业(32 个板块)用「同批板块横截面分位」是因其天然可比; 个股若照搬
# 「全市场 5016 只横截面」会被 size/风格主导(微盘 20 日涨 30% 是常态, 大盘涨 10% 就顶尖)
# —— 正是 R443 踩过的坑(沪深300 基准把 size 算进超额 +2.25pp vs 横截面 +0.58pp)。
# 故个股/北交一律用 **同行业内分位**, ETF 单独用 **ETF 池**(ETF 的 ind 恒为 '-', 无申万归属)。
# 只落**描述性**字段(动量 + 分位), **不加**「按动量排序」的 tab —— 个股按 mom60 降序 =
# 追涨视角, 与 revpool 的逆向哲学方向相反。四象限也不做: 单票 5 日波动绝大部分是噪声
# (行业是成分平均, 天然平滑), 且标签会暗示预测力, 与 R443「底背驰无可检测超额」自相矛盾。
MOM_WINS = (5, 20, 60)         # 多周期动量窗口(根)
IND_RS_MIN = 5                 # 可比组内相对强度的最小成员数(不足则不落分位, 防 2~4 只的假分位)
# R447: ETF **同指数去重** —— ETF 板块 11 只顶信号里 6 只是「自由现金流」系列;
# revpool 候选 15 只里 10 只是「科创人工智能」同一指数 ⇒ 一屏刷屏、真标的被淹没。
# 判定用 **60 日日收益相关系数** —— 名称不可靠: "科创AIETF银华" 与 "科创人工智能ETF"
# 名称不同, 实测相关 0.995~0.998 = 同一指数; 涨跌方向指纹法实测 17 只**无一相同**
# (对跟踪误差过于敏感) ⇒ 不可用。
# 阈值实测标定(17 只真实 ETF, 数据截至 2026-09-11):
#   同指数组内(科创人工智能 ×10) 0.994~0.999; 跨赛道(对宽基/券商/黄金/纳指/货币) -0.397~0.819
#   ⇒ **0.99 余量极大, 零误合并风险**。
# 宁漏不误并: 实测两只「云计算ETF」(0.980) **不合并** —— 无法确证同指数, 宁可留着。
ETF_TWIN_WIN = 60              # 同指数判定窗口(日收益根数)
ETF_TWIN_CORR = 0.99           # 同指数判定阈值(正向相关下限; 反向ETF天然负相关, 不得并组)
ETF_TWIN_PREF = 1.5            # 预筛: 窗口累计收益差上限(百分点) —— 免 O(n²) 全量两两算相关
ETF_TWIN_MINBARS = 61          # 参与判定的最少 K 线根数
# R448: ETF **官方「跟踪标的」** —— 上面相关法的根治手段(相关法作补漏保留)。
# 为什么相关法必须让位(实证, 非推测): 相关法对「同指数异基金」有**假阴性下限** ——
#   sz159527(云计算ETF广发) 与 sz159739(云计算ETF鹏华) 官方跟踪标的**字面完全相同**
#   「中证云计算与大数据主题指数」, 但 60 日相关仅 **0.9800** ⇒ 未并组。而 0.98 档同时混着
#   **不同指数**的近似产品(6 只「自由现金流/全指现金流/现金流」两两 0.9175~0.9886 却属不同指数)
#   ⇒ **阈值无处可调**(降必误并, 升则漏并更多) —— 下面是**实测反例**, 把"无处可调"从判断变成证明:
#     · 同指数最松的一对: 159527~159890(同跟踪「中证云计算与大数据主题指数」, 官方确认)相关 **0.9818**;
#       且 159527 与组内**每一只**都在 0.9818~0.9854(其余两两 0.9935~0.9977) —— 不是"某两只之间
#       的问题", 而是**该基金跟踪偏松**(规模 3.09 亿, 组内最小) ⇒ 只测一对会误判成偶发。
#     · 不同指数最紧的一组: 「现金流」系列最高 **0.9886** —— 这族 31 只名字几乎一样的产品经官方
#       跟踪标的核实横跨 **6 个不同指数**(国证/中证800/中证全指/沪深300/中证500 自由现金流 +
#       富时中国A股自由现金流聚焦) ⇒ R447「两两 0.9175~0.9886 却属不同指数」**已由官方数据证实**。
#     · **0.9818(同指数) < 0.9886(不同指数) ⇒ 两区间重叠 ⇒ 任何阈值都分不开**(降必误并/升必漏并)。
#       这不是"阈值没调好", 是**判据本身缺信息** ⇒ 只能换判据(R442 同源: 先怀疑口径, 再怀疑数据)。
#     · 名称同样不可靠且会**系统性误导**: 「云计算ETF」一族名字像一回事, 官方是 **3 个不同指数**
#       (中证云计算与大数据主题 / 中证沪港深云计算产业人民币 / 中证云计算50); 反向地
#       「科创AIETF」与「科创人工智能ETF」名字不同却是同一指数。
#     · 官方判据还解锁了相关法**永远无法判定**的一类: K线不足 62 根的新 ETF(实测 159099 仅 32 根)
#       没有可比收益序列 ⇒ `_rets` 返回 None, 相关法无从下手; 官方 track 直接按指数名并组。
#       (单测已证: 159099/560660 同跟踪「中证云计算50指数」⇒ 零收益序列也能并组)
# 数据源: 天天基金 fundf10 基本信息页(公开静态页, 无需鉴权), 一页同给
#   跟踪标的 / 管理费率 / 托管费率 / 净资产规模 / 成立日期 ⇒ 一次抓取同时解决三件事。
#   实测(2026-09-13) 全量 1282 只 ETF: **1282 成功 0 失败, 跟踪标的/费率/规模三项 100% 覆盖**,
#   用时 10 分 12 秒(串行 0.2s 间隔; 并发会触发 WAF) ⇒ 缓存入库可长期复用, 仅需低频刷新。
# 双判据分工(刻意**并存**而不是替换):
#   官方 track = **主判据**(精确, 零漏并零误并; 194 个同指数组 / 覆盖 1103 只)
#   相关法 corr = **补漏**(本轮新上市、缓存尚无该代码的 ETF 兜底; 旧行为不回退)
# ⚠️ 缓存缺失 / 代码不在缓存 / track 字段为空 → **一律回落相关法** —— 绝不因元数据缺失而漏并。
ETF_META_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_meta.json")
# 同指数「选优」的判据(实证支撑, 见 2026-09-13 全量统计):
#   ① 组内费率极差 **96/194 组 ≥ 0.20pp**, 最大 0.20%~1.10%(0.90pp/年) —— 这是**确定**的成本差。
#   ② 「费率最低」≠「规模最大」的组 **132/194(68%)**, 即"挑最大的买"在三分之二的指数上是错的。
#   ③ 但唯费率也会踩坑: 中证A50 组费率最低者 512240(0.20%) 规模仅 **0.2 亿**、60日均额 287 万
#      ⇒ 低于清盘线、流动性也差。故选优必须**双约束**: 费率优先 + 规模/流动性硬否决。
ETF_CLEAR_YI = 0.5             # 规模硬否决线(亿元): 低于此记「清盘风险」(行业惯例 5000 万清盘线)
ETF_THIN_AMT = 1000.0          # 60日均额硬否决线(万元): 低于此记「流动性偏弱」(一次进出会打滑)
# 官方 track 组内的**反向/杠杆否决线** —— 这是官方判据唯一的已知失效模式: 反向产品声明了与
# 正向产品**相同的指数名**(A 股现存极少, 分级基金 2020 年底已清理)。反向产品的日收益
# ≈ −1×(指数收益) ⇒ 两者相关**≈ −0.99**, 特征极强。
# ⚠️ 阈值取 −0.6 而非 0 —— **绝不能取 0**: 同指数异基金的正常相关是 0.98~0.99, 但
#   ①QDII/港股通 ETF 交易日历与 A 股不同, 序列按各自末 61 根对齐会**错位** ⇒ 相关塌向 0 甚至
#     小幅为负; ②个别基金申赎冲击/现金替代会让短期相关破位。以 0 为门槛 ⇒ 系统性**误拆官方组**,
#     破坏面比它想防的还大(单元自测: 同指数随机收益对 → 867 对被否决、194 组被拆成 163 组)。
#   取 −0.6 后, 只有真实反向关系才会命中; 宁漏不误并(误拆 = 退回 R447 行为, 误并 = 藏掉真标的)。
ETF_TRACK_VETO = -0.6
# ================= R457: ETF **折溢价率**(场内价 vs 官方单位净值) =================
# 为什么加: ETF 的实操风险有一半不在"涨跌", 而在**你买入时多付/少付了多少**。跨境 QDII 因
#   **限购无法申赎套利**, 折溢价可长期挂在两位数 —— 实测(2026-09-14) 513100 纳指ETF
#   **+12.875%**、513500 标普500 **+9.942%**, 且连续 4 日稳定在 +8.7~+12.9%(非瞬时错价)。
#   此时"底背驰买入信号"再好也是陷阱(买贵 13%)。而境内股票 ETF 套利有效, 实测
#   1281/1319 只落在 ±0.5% 内 ⇒ **折溢价这一维度只在跨境/商品类上有区分度**, 必须能看见。
#
# 数据源: 天天基金 F10 历史净值 API `api.fund.eastmoney.com/f10/lsjz`(免鉴权;
#   **Referer 必须为 fundf10.eastmoney.com**, 否则 403)。实测全量 1326 只并发 6:
#   **1326 成功 / 3 失败(全是非基金的 81xxxx 代码) / 19.5s**, 无限流。
#   ⚠️ 排队排除过的批量源(**没有**第二次尝试的必要, 已实测):
#     · 腾讯 fqkline / 新浪 CN_MarketData —— 只有价格, 无净值;
#     · 东财 push2 clist/getStock 宽字段 —— 无 IOPV/无净值(且 300 字段单请求被源端断连);
#     · `fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx` 一次可取全量 24058 只(**仅 3.6MB, 1 请求**)
#       —— 但**只含场外份额**: 实测 510300/513100/511990/159915 在该全量列表中**一个都查不到**;
#     · `fund.eastmoney.com/data/rankhandler.aspx?ft=zs|gp|zq|qdii` —— 同样只列场外份额
#       (实测 5 个 ft 类型、3000 行内目标代码命中 **0**)。
#     ⇒ 场内 ETF 的单位净值**只此一处可得**, 只能逐只取(但 1326 只仅 19.5s, 成本可忽略)。
#
# ★★ 必须按**净值日期严格配对**当日收盘价, **绝不可**「最新收盘 ÷ 最新净值」:
#   反例(实测): 511990 华宝添益 / 511660 建信添益 的净值日是 **2026-09-13(周日, 货币基金按
#     自然日发净值)**, 该日**无 K 线** ⇒ 硬配 100/0.5456 = **+18231%** 的伪溢价。
#   反例: QDII 净值天然滞后 —— 513100/513500 净值停在 09-10 而境内已到 09-11;
#     硬配会得到 +12.875%(假) 与真值 +0%(更假) 之间的任意垃圾。
# ★ 日期配对**同时免疫除权/份额折算**: 同一日期上"价格"与"净值"处于同一份额基准, 比值与
#   折算比例无关 —— 这点与收益率链不同(后者必须先复权)。
EF_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=1&pageSize=1"
EF_NAV_REF = "https://fundf10.eastmoney.com/"
EF_NAV_WORKERS = 6             # 并发上限(实测 6 并发稳定; 源端未声明限流, 保守取值)
EF_NAV_DELAY = 0.10            # 每请求后 sleep(秒), 压低瞬时速率
EF_NAV_TIMEOUT = 12.0
EF_NAV_BUDGET = 300.0          # 整段硬预算(秒): 超出后余票直接放弃(缺字段, 前端降级, 不拖全量)
# ★ 合理性带: |折溢价| 超此值判「口径不可解释」并**剔除**(落 meta.etf_prem.implausible 计数+样例)。
#   依据(2026-09-14 实测, 双侧取证):
#     真实上界 —— 跨境 QDII 极端 +12.9%(513100); 境内股票 ETF 实测 1281/1319 落在 ±0.5% 内;
#     口径错误 —— 货币 ETF 的单位净值是「每万份收益」口径, 与场内每份价格不同基准,
#                实测 sz159001(易方达保证金) 收盘 100.0000 vs 单位净值 0.2673 = **+37311%**,
#                且连续 4 日稳定(非瞬时噪声), 同类还有 511990(+35899%)。
#   带宽取 50%: 放过真实极端(历史限购狂热期 QDII 溢价可达数十%), 挡住 10^2~10^4 量级的
#   口径错误 —— 两者量级相差 3 个数量级, 取值落在中间的任意位置等价, 50% 只是保守取低端。
EF_PREM_MAX = 50.0
# 净值日距 asof 超此天数 ⇒ 计为「净值滞后」(仍展示: 其日期已在 UI 明示; 只用于 meta 自证)
EF_NAV_STALE = 7
# R270: 新浪 datalen 实测支持 1500(2020-07 起), 扩至与腾讯qfq窗口(2021至今)同量级,
# 避免新浪兜底时缠论结构起点(原800根≈3.2年自2023-05)与腾讯不一致导致的笔/中枢划分差异。
SINA_LEN = 1500                # 新浪兜底K线根数(~6年, 2021至今全覆盖)
EM_KLINE_LEN = 1600            # 东财前复权K线根数(2021起含裕量)
SPARK_N = 150                  # 信号票内嵌迷你K线根数(前端实操卡用)
IND_KLINE_N = 320              # 行业K线入库根数(画行业走势; 合成全量更长仅用于行业缠论)
# R496b: 前端**个股/ETF**详情页取的 K 线根数 —— 契约常量，不是"大概值"。
#   来源：radar.html getStkData() 的 `?param=<sym>,day,,,660,qfq`。
#   ★ 为什么后端必须知道它：个股 mark 的偏移 `off` 是「距末根」的绝对量，后端缠论跑的是
#     全部历史（~1380 根），而前端只画 660 根 ⇒ **off > 659 的标注前端 xi() 返回 null
#     被静默剔除**（图上"比后端少几条"，无任何报错）。故 analyze_one 用它做 draw_n 上界：
#     不变量 = **后端永不发出前端画不出的标注**。
FE_KLINE_N = 660
# R496 详情页 K 线标注 —— **唯一判据 = 「前端画得出的 K 线根数」**（= analyze_one 的 draw_n）。
#
# 历史教训（为什么最终收敛成"只有一个上界"）：
#   ① R495 及之前：mark 六类全按**条数**截断（`bis[-10:]` / `signals[-14:]` / `_ssig[-20:]`
#      / `zss[-3:]` / `bc[-6:]` / `_segzs[-2:]`）。笔的密度因票而异 ⇒ 实测上证「笔折线只
#      覆盖末 11% 的 K 线、笔级买卖点只覆盖末 18%」，而 dataZoom 默认就显示末 70%
#      ⇒ **可视窗口内大面积"有信号却没标"**（用户主观感受 = "买卖点没标全"）。
#      这与主界面 report.py 当年 R472 删掉的 `cutoff = n-500` 是同一类错的**第二个副本**。
#   ② R496 初版：改成"按末 460 根窗口"（= 覆盖默认 dataZoom 可视区 462 根）。**但仍然错**：
#      · 后端缠论跑全历史（个股 ~1380 根 / 行业合成 ~1386 根），前端只画 FE_KLINE_N=660
#        （行业 320）⇒ `off` 越界的标注**后端照发、前端 `xi()` 返回 null 静默丢掉**
#        （实测上证行业 sig 23 条丢 5 条；个股最近 3 个笔中枢里有 off=1083 的）。
#      · 段级信号**稀疏**，旧代码 `_ssig[-20:]`（按条数）覆盖的历史比 460 根窗口**更长**
#        ⇒ 460 窗口反而把旧版**画得出**的段级点丢了（无头渲染实测 sz000001 段级买点 14→10）。
#      ⇒ 「稠密族够覆盖 / 稀疏族不缩水」用单一根数窗口**无法同时满足**。
#   ③ R496b（最终）：**去掉额外窗口，只留 draw_n 一个上界** —— 即"前端能画的全部"。
#      可证性质（这也是选它的核心理由）：新集合 = {元素 | 全部锚 off ≤ draw_n-1}，
#      旧集合 = {按条数取末 K 个}，其**可绘制子集**必然 ⊆ 新集合
#      ⇒ **对每个族都严格不缩水**（数学上 ⊇，不依赖样本）。
#
# 体积（实测，非估算）：样本均值 460→2335 B/只、660→2983 B/只 ⇒ 全市场外推 15.4MB → 19.7MB。
#   · 因为是"末 660 根内的元素"，元素数只取决于**信号密度、不随历史增长** ⇒ 体积**有封顶**。
#   · git 侧代价很小：14 版 marks.json 未压缩合计 138.4MB，而整个 pack 仅 16.76MiB
#     （git 对每日快照的 delta 压缩很有效，≈1.2MB/版）。
#   · 线上 Pages 走 gzip（实测 9.99MB → 1.65MB 传输）⇒ 19.7MB ≈ 3.2MB 传输。
#     故不保留额外的"省体积窗口"：为省 ~1MB 传输而牺牲"齐全"不划算（旭总诉求 = 标注齐全）。
#   ★ 若日后确要压体积：正解不是加窗口，而是把 marks.json **按代码前缀分片**、点开个股时
#     再按需取（静态站也能做，fetch 同目录分片）。此项已登记为待议，本轮不做。
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
# R444: 复探周期 300 → 80。09-11 实况: meta.src_fail 显示腾讯侧 tx_empty 38 + err 36 + tx_stale 2
# = 74 次失败, 即整轮内发生了 ~5 轮「停用→复探→恢复→再停用」循环 —— 说明腾讯 WAF 限流是
# **间歇性**的(本机实测同类限流约 10 分钟自恢复), 而不是整轮不可用。但每 300 次调用才给一次
# 复探机会时, 全市场 6758 票仅 22 次机会, 短恢复窗口极易被整段错过 ⇒ 单轮 87% 票被迫走裸价。
# 降到 80 后机会 22→85 次(代价: 最多多打 ~63 次单票轻量请求, 占主请求量 <1%, 且复探走 _tx_th
# 节流不抢带宽)。阈值 TX_FAIL_MAX=15 **不动** —— 它防的是"成段故障时空耗 timeout", 与恢复无关。
TX_REPROBE_EVERY = 80
EM_REPROBE_EVERY = 80
EM_TIMEOUT = 10                # 东财单请求超时(不可达时快速失败, 不拖全量)
EM_HOSTS = ["https://push2delay.eastmoney.com", "https://push2.eastmoney.com",
            "http://82.push2.eastmoney.com", "http://push2delay.eastmoney.com"]
EM_FS_STOCK = "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80"      # 沪深A股(含主板/中小/创业/科创)
EM_FS_BJ = "m:0+t:81+s:2048"                            # 北交所
# R456: 场内基金 —— 原 `b:MK0021` 单板块**枚举不全**, 只覆盖境内股票 ETF/LOF。
#   2026-09-14 实测东财基金板块空间(逐板取全量, 各板**互不重叠**, 合计 1752 只):
#     b:MK0021 1326 境内股票ETF/LOF     b:MK0022   27 货币ETF      b:MK0023  241 跨境ETF
#     b:MK0024   10 商品ETF             b:MK0025  111 科创/混合LOF b:MK0026   26 债券LOF
#     b:MK0027    4 黄金LOF             b:MK0028    7 原油/商品LOF
#   ⇒ 原池只覆盖 **1326/1752 = 75.7%**, 缺 426 只。且缺口**不是随机的**, 恰是实操风险
#   最高的一类(实测缺 `b:MK0023` 后 513100/513500/518880/513050/511990 全部不可见):
#     · 全部**跨境 ETF**(纳指/标普/恒生科技/中概互联/日经…), 241 只
#     · 全部**货币 ETF**(华宝添益/银华日利…) 与**商品 ETF**(黄金/豆粕…), 共 37 只
#   ★ 为什么这是**真缺口**而非有意排除(两条独立证据):
#     ① 本行自 P1 首提交 00a97fb 起**从未改动**(`git log -S MK0021` 只命中该次), 注释原文
#        即写「场内基金(ETF/LOF)」—— 原意就是「全部场内基金」, 是枚举不全;
#     ② 同文件 L142 的 ETF 判据注释**预设了 QDII 在池内**(「QDII/港股通 ETF 交易日历与
#        A 股不同, 序列错位会让相关塌向 0」)—— 若 QDII 从不在池内, 这条注释无从谈起。
#   ★ 缺口代价(可量化): 跨境 QDII 因**限购无法申赎套利**, 折溢价可长期挂在两位数
#     (实测 513100 纳指ETF 折溢价 **+12.875%**、513500 标普500 **+9.942%**, 且连续多日;
#      而池内境内股票 ETF 实测 1281/1319 只落在 ±0.5% 内 —— 套利有效)。雷达看不见它们
#     = 看不见**最该预警的一类**。
#   ⚠️ 刻意**不含** b:MK0025~0028 的 148 只 LOF: 它们是「场内交易的场外型基金」, 纳入会改变
#      「ETF板块」的语义与信噪比(多小规模/低流动), 属待议(旭总 2026-09-14 拍板仅补 ETF 四板)。
EM_FS_FUND = "b:MK0021,b:MK0022,b:MK0023,b:MK0024"      # 场内 ETF: 股票+货币+跨境+商品(1604)
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
# R444: stops=本轮内被停用次数 | resumes=复探成功次数 —— 两者之比直接量化"限流是间歇还是持续":
# stops=1 且 resumes=0 = 整轮持续不可用(复探无意义); stops 与 resumes 都在涨 = 反复抖动
# (正是加密复探能救的场景)。09-11 只有 src_fail 计数可查, 分不清这两种形态, 故补落痕。
_tx_down = {"flag": False, "count": 0, "reasons": {}, "n": 0,
            "max": TX_FAIL_MAX, "every": TX_REPROBE_EVERY, "probe": None,
            "stops": 0, "resumes": 0}
_em_down = {"flag": False, "count": 0, "reasons": {}, "n": 0,
            "max": EM_FAIL_MAX, "every": EM_REPROBE_EVERY, "probe": None,
            "stops": 0, "resumes": 0}
# R272: 市场末交易日锚(probe_mkt_last 在 main 串行段探测后写入)。
# 用于 _last_fresh/_should_weekend_skip 的新鲜度判定: 数据末根 >= 该锚 即"不比市场旧",
# 市场无更新的日子(周末/长假/当日行情未出)旧数据即最新 —— 取代硬编码长假窗口的
# 大部分职责(窗口表只作探测失败时的 fallback), 2027+ 节假日无需维护。
_mkt_last = None

def _src_down(d, lock):
    """读停用状态并累计调用; 停用中周期执行一次轻量复探, 成功则复位切回。
    (复探为网络请求, 在锁外执行避免阻塞其他取数线程; 状态写回再取锁。)
    R444: 复探节拍改 `n % every == 1` —— 停用后**第一次调用**即复探(原 `== 0` 必须等到第
    every 次), 之后每 every 次一次。停用多由瞬时抖动/短时限流触发(09-11 实测整轮内发生
    ~5 轮停用-恢复循环), 把首次机会从"第 300 票"提前到"第 1 票", 短恢复窗口一出现就能
    被抓住, 不必白等一整段。"""
    with lock:
        d["n"] = d.get("n", 0) + 1
        if not d["flag"]:
            return False
        do_probe = (d["n"] % d["every"] == 1 and d["probe"])
    if do_probe and d["probe"]():
        with lock:
            d["flag"] = False
            d["count"] = 0
            d["n"] = 0
            d["resumes"] = d.get("resumes", 0) + 1     # R444: 恢复轮次落痕
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
        if d["count"] >= d["max"] and not d["flag"]:
            d["flag"] = True
            d["n"] = 0                     # R275: 复探节拍原点 = 停用时刻
            d["stops"] = d.get("stops", 0) + 1   # R444: 停用轮次落痕(与 resumes 共同刻画间歇性)
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
# R458b: 腾讯 qfq **深度守卫**。09-14 实测腾讯把个股/ETF 的 qfq 历史**硬截断在 641 根**
#   (首根恒 2024-01-22; count 给 1700 或 1999 一样、加日期段也一样、两个主机同款 ——
#    见 fetch_data.TX_KLINE_HOSTS 注释。指数不受影响: 它们返回的是 `day`(不复权)满 2000 根)。
#   而契约是"2021 至今"≈1380 根, 新浪兜底 1500 根, 东财 1600 根。若让 tx 正常胜出,
#   全市场窗口会从 5.7 年**静默砍到 2.6 年**, 且各源窗口**参差**(09-11 线上即
#   876 票 641 根 + 5821 票 1500 根混跑, 跨票比结构时起点不一致)。
#   `_KS_MIN_BARS=30` 拦不住这种截断 —— R348 那条只防"1 根假成功"。⇒ 深度不足判 tx_shallow
#   并**继续切下一源**; 与 tx_short/em_short 同族 = 票面/接口深度问题, **不计入源故障连败**
#   (R374 的血泪: 把票面短史混进连败窗口会冤停复权主源)。
#   ★★ R460(2026-09-14) 更正 —— 上面 R458b 的核心前提**是错的, 守卫已拆除**。
#     R458b 记「腾讯把 qfq 历史硬截断在 641 根(count 给 1700/1999 一样、加日期段也一样)」
#     ⇒ 加了深度守卫 + 一次性 latch。但**只试了 count 槽**: 该接口 param 的语义是
#     `code, period, start_date, end_date, count, fq`, 生产把 start/end 留空 ⇒ 永远只能
#     取到"最新 640 根"⇒ **641 是"单次请求上限", 不是"腾讯没有历史"**。实测填 end 游标:
#       end=2021-01-01~2023-12-31 → 640 根(首根 2021-05-18); end=2019-01-01~2021-12-31
#       → 640 根(首根 2019-05-21); 两主机同款。⇒ 沿 end 往回翻页即得**全量前复权**
#       (sh600000 两页拼出 1281 根, 首根 2021-06-07; 拼接安全性已实证: 6 个不同 end 窗口
#        取同一日期共 **2797 对全部同价, 最大差 0.000000** ⇒ 基准锚定固定、与窗口无关)。
#     ⚠ 代价实测(必须与结论一起读): 保守估计落空 —— R458b 原记"每票多打一次纯属浪费
#       (6630 票 × 0.35s ≈ 39 分钟)", 但**日线路径根本没有 tx 节流**(`_tx_th.wait()` 只用于
#       _probe_tx 与月线路径; _try_em/_fetch_sina_checked 各自有节流)。日线走腾讯的边际成本
#       是"+1 次**无节流**请求", 换掉的却是**被 0.18s 节流的新浪请求** ⇒ 净效果**更快**。
#     而 R458b 守卫的**净效果是净退化**(实测线上源占比): 09-11 tx=876/sina=5821(守卫尚不存在)
#       → 09-14 tx=**1**/sina=6979(守卫上线当日)。把 641 根的**前复权**换成 1380 根的**裸价**,
#       是拿"窗口深度"换掉了"复权正确性", 且该守卫的 latch 会因**任意 5 只次新股**(它们本就
#       根数少、是完整历史而非截断)而把腾讯整源掐掉 —— 与"源健康"无关。
#   ⇒ 现改为: 分页取数(见 fetch_data.fetch_tx_qfq_paged), 深度由**请求次数**而非"拒绝整源"来换;
#     失败仍按原状态机计连败, 但**不再有"根数短即判源坏"的推断**。
#   ⚠ 修正(R458c): 原注释写"截断是全市场**恒定**的" —— 不准确。实测存在**另一类**票
#   腾讯**压根不返回 qfqday**(只给完整 `day` 裸价), 它们**穿过本守卫**(根数够长),
#   却是不复权序列 —— 已由 fetch_data.fetch_tx_qfq 单独拦下(见下方 _tx_noqfq),
#   与本守卫是**两回事**, 两者都判失败切源。
#   ★ 全市场规模(2026-09-14 用线上满量产物 run7/asof 09-11 反向度量, 非抽样):
#     src=='tx' 876 只里 —— n_bars>641 者 **44 只**(硬论证: qfqday 上限 641,
#     根数 >700 者**不可能**是 qfqday ⇒ 必是裸价 day); n_bars<640 者 46 只中实测
#     **16 只**无 qfqday、30 只确有 ⇒ 暴露合计 **60 只 = tx 的 6.8% / 全市场的 0.90%**,
#     **全部是 sh688*(科创板个股)**。⇒ 正确表述是"**集中在科创板**", **不能**说
#     "科创板普遍"(同批 sh688 里 30 只有 qfqday)。且该产物 ETF/北交**全走 sina**,
#     故 60 只只覆盖**个股端**, 不含 ETF 端暴露。
#   ⚠ 为什么不能靠 n_bars 离线判: qfqday 对**上市晚于 2024-01-22** 的票是**从上市日**
#     开始给的(根数 = 上市以来交易日数), 与裸价 day **同形** ⇒ 46 只那档只能实测。
#   ⚠ `--src tx` 显式指定时**不套用**守卫: 那是本地取证开关, 要看到源的真实原貌(641 根)。
_TX_MIN_BARS = 1200          # "2021 至今"≈1380 根, 取 1200 留约 1 年裕量
# R460: 分页取的**目标深度 / 请求上限**(取代原 `_TX_SHALLOW_MAX` + `_tx_shallow` 一次性 latch)。
#   上限 3 页 = 640×3 ≈ 1920 根(实际取到 `first <= MIN_DATE` 即停) ⇒ sh600000 实测 **1382 根,
#   首根 2021-01-04** —— 与新浪兜底**同深度**, 故本变更只含"复权口径"一个变量、**深度零变化**。
#   ★ "为什么必须是 3 页而不是 1~2 页" —— 本轮实测(证据 `_dbg/r460/depth_ab.py`, **纯本地**:
#     同一批票/同一引擎/同一 asof/同一种源, 唯一差异 = 只留尾部 N 根):
#       | 尾部深度 | scen | 顶/底背驰 | zs_last | **gate(低自洽)** | dd  |
#       |   641(1页)| 0.0% |   0.0%    |  0.0%   |     **19.0%**     | 50% |
#       |  1281(2页)| 0.0% |   0.0%    |  0.0%   |     ** 6.2%**     | 23% |
#       |  1200     | 0.0% |   0.0%    |  0.0%   |       9.3%        | 35% |
#     ⇒ ① **发布标签(场景/背驰/中枢价位)对深度不敏感**(0.0%) —— R458b"砍到 2.6 年会改变
#          结构"对**信号层**不成立; 但 ② **`gate` 对深度敏感**: 浅窗口让 `低自洽` 判定翻转
#          6~19%(样本越少, 自洽率越噪) ⇒ **不取满深度, 等于"用一种不一致换另一种"**(R459 层C
#          实测除权假跳空本身造成 scen 3.6%/gate 8.2% 差异; 与 641 根的 gate 19% 同量级) ⇒ 净效果
#          大致抵平甚至更差。**只有 3 页(深度一致)才让本次变更是一次纯粹的正确性修正。**
#   ⚠ 代价与**下行风险有界**(实测/推演): 3 页 = 3 次请求/票; 但日线路径**无 tx 节流**是缺陷
#     (见 fetch_data.TX_INTERVAL 注释: 无节流突发会把记忆主机打到 501, 进而让源状态机整段停用,
#      600 只实测 tx 仅 79/599、degraded_pct=87 —— 与 09-11 线上 87% 裸价同因)。现已在 tx_get
#      内加**按主机**节流(各 2.9 rps; R460 两主机轮页 ⇒ 聚合 ~5.7 rps,
#      **R463 接入第 3 台同源镜像 `proxy.finance.qq.com/ifzqgtimg` ⇒ 聚合 ~8.6 rps**)。
#      最坏情形不是"更差的数据"而是"更慢": 源被拦后 `_src_down` 会**短路**(不再尝试),
#      暴露量仅"整段停用前的 15 连败 ≈ 21s"(+ R428 每 300 票复探一次 ≈ 23×1.4s), 之后照常回落
#      新浪 ⇒ 总量 ≈ 今日时长 + 1~2 分钟; 数据侧要么全站前复权、要么回到今日的裸价, **不会更坏**。
#   ✅ 已落地(R463, 见 main 的抓K线段): **两阶段取数** —— 阶段①全市场只取 1 页(641 根),
#     阶段②只对 `gate_prefix4` 四项全过的票补第 2/3 页。请求数 −13.6%(20940 → 18094)。
#     ★ 更正本段原先的口径错误: 原文写"只对通过门禁的 ~2500 票回补" —— 2546 是**被门禁
#       剔除**的票数(09-14 产物 meta.n_gate), 通过的是 4360(`gate_cnt[""]`)。两阶段真正
#       省的是"被**前 4 项**剔除的那部分" = **1423 只**(次新233 + 停牌17 + 低流动性1172 +
#       一字板1, 顺序判定互斥)。为什么低自洽**不能**前置、前 4 项为何安全: 见
#       `gate_prefix4` 与 main 抓K线段(证据 _dbg/r463/stage2_ab.py)。
#   ⚠ 仍未知: "腾讯单次 run 的**请求预算**上限" —— 本机测它会被自己的探针打到 501(自伤);
#     而线上扫描跑在 **GitHub runner 的出口 IP** 上(与**本机不同**), 故本机测出的阈值
#     **不可外推**到线上。真正的答案只能由线上首轮 run 的 meta 给出:
#     `src_fail.tx` / `src_cycle.tx` / `degraded_pct` / `tx_stage2` / `tx_depth`。
_TX_MAX_PAGES = 3
# R460: 深度**观测**计数(不参与任何判定, 只落 meta.tx_depth) —— 替代原 latch 的可观测性:
#   要能看见"这一轮的 tx 序列到底多长", 而不是只能靠降级率反推(那正是 R458b 想消灭的黑盒)。
_tx_depth = {"n": 0, "min": 0, "max": 0, "sum": 0, "thin": 0}
# R463: 两阶段取数的**阶段一 → 阶段二续取游标**: sym → 下一页的 `end`(字符串)。
#   只记"上一页的边界日期"这一个字符串, **不存 K 线序列** —— 若存 {date: bar}(641 条/票)
#   则会占 ~690 MB(6906 票), 而游标只有 ~30 B/票。阶段二的 K 线由调用方从 got 里带过来
#   (见 _fetch_deep_one 的 got0 传参), 与本表配合即可无损续取。
#   全程在 _tx_lock 下读写(_fetch_tx 内已持锁)。
_SHALLOW_RESUME = {}
# R458c: 「腾讯没给前复权序列(只给裸价 day)」的独立计数 —— 与 641 截断**分开记**(原因不同),
#   但同属**票面/接口**问题: 判失败切源, **不进源故障连败**(R374 血泪), 单独落 meta.src_fail.
_tx_noqfq = {"n": 0, "samples": []}


def _record_tx_depth(ks):
    """R463: `meta.tx_depth` 的唯一写入点 —— 只记**最终采纳**的腾讯序列深度。

    ★ 为什么必须抽成一个函数(而不是继续内联在 _fetch_tx 里): 两阶段取数之后, "拿到腾讯序列"
      有**两条**路径(阶段①的浅取 / 阶段②的补深度), 若各写一份, `tx_depth` 的语义就会漂 ——
      实测踩过: 只在 _fetch_tx 里记 ⇒ 阶段①的 641 根把分布钉死在 {min=641,max=641,thin=+n},
      而"这只票最终到底多深"完全看不见(阶段②绕过 _fetch_tx 直调 fetch_tx_qfq_paged)。
      现在的规则是**只记最终态**: 浅取(max_pages==1)不记(它只是中间态, 不是给上层的序列),
      阶段②补深后记一次, 非两阶段的常规路径照旧记。
    ★ `thin` 的语义随之保住: 它统计"腾讯给得比 _TX_MIN_BARS 少"的票 —— 浅取的 641 根
      **不是**"腾讯给得少"(是我们只取了一页), 不该计入, 否则每次全量都会凭空多出几千个 thin。"""
    with _tx_lock:
        _tx_depth["n"] += 1
        _tx_depth["sum"] += len(ks)
        _tx_depth["min"] = len(ks) if not _tx_depth["min"] else min(_tx_depth["min"], len(ks))
        _tx_depth["max"] = max(_tx_depth["max"], len(ks))
        if len(ks) < _TX_MIN_BARS:
            _tx_depth["thin"] += 1


def _fetch_tx(sym, max_pages=None, page0=None):
    """腾讯 qfq(前复权)主源: 纯 count 形态(R248), 2021-01-01 起裁剪。
    R463: `max_pages=1` = **两阶段取数的阶段一浅取**(只取最新 641 根), 见本文件 R463 段;
    缺省 None = 原来的 `_TX_MAX_PAGES`(3 页/1382 根), 既有调用方行为逐字节不变。
    R467: `page0`(页序起点)缺省 = `fd.tx_prefer_seed(sym)` —— **按票分散 prefer**, 见
      `fetch_data.tx_prefer_seed` 注释(原为全市场固定 0 ⇒ 三台主机首尾接力 ⇒ 吞吐锁死在
      单台上限 2.86 rps)。两阶段的续接关系不变: 阶段②传 seed+1。
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
    # R458c: 走 fetch_tx_qfq —— **要求腾讯真的给出前复权序列**。原 `fd.fetch_tx` 的
    #   `qfqday or ... or day` or 链会把"只有 day(不复权)"的票静默当成功(见该函数 docstring)。
    # R460: 改用**分页**入口 fetch_tx_qfq_paged —— 见文件内 _TX_MIN_BARS 上方 R460 段:
    #   641 根是"单次请求上限"而非"腾讯没有历史", 沿 end 游标翻页即得**全量前复权**
    #   (3 页 = 1382 根, 首根 2021-01-04, 与新浪兜底同深度)。拼接安全性已实证(2797 对同价)。
    #   min_bars=None ⇒ 不在"够深"处早停, 只按 `first<=MIN_DATE`/窗口耗尽/页数上限停 ——
    #   目的是让本变更**只含复权口径一个变量**, 不引入深度变化(见 _TX_MAX_PAGES 注释)。
    ks, _dirty, _hasqfq, _np = fd.fetch_tx_qfq_paged(
        sym, "day", min_bars=None, max_pages=max_pages or _TX_MAX_PAGES,
        page0=(fd.tx_prefer_seed(sym) if page0 is None else page0))
    if not _hasqfq:
        with _tx_lock:
            _tx_noqfq["n"] += 1
            if len(_tx_noqfq["samples"]) < 8:
                _tx_noqfq["samples"].append(sym)
        return [], "tx_noqfq"
    if not ks:
        return [], "tx_empty"
    if not _last_fresh(ks[-1]["date"]):
        return [], "tx_stale"              # CDN 陈旧缓存(R248: 停 12h+), 重试无意义
    if len(ks) < _KS_MIN_BARS:
        return [], "tx_short:%d" % len(ks) # R348: 短序列(接口截断/异常)显式判失败切源, 不假成功吞票
    # ★ R460: 原 R458b 的"深度守卫 + 一次性 latch"**已拆除**(前提被证伪, 见上方 R460 段)。
    #   改为**纯观测**: 记录深度分布落 meta.tx_depth, 序列照常采用 ——
    #   分页后短序列只可能来自"上市晚"(完整历史)或"腾讯侧确实没有更早数据", 两者都不是故障;
    #   拒收它们只会把**前复权**换成**裸价**, 是净退化(09-14 实测 tx 876→1)。
    # R463: 深度**不在这里记** —— 两阶段取数下"最终深度"要等阶段②跑完才知道,
    #   统一由 main 在取数全部结束后对 src=="tx" 的票扫一遍(见 main 的"落深度"段)。
    with _tx_lock:
        # R463: 浅取成功后记下**续取游标**供阶段二。
        #   只在"这一页被填满"时有第 2 页可翻 —— 根数更少说明腾讯侧没有更早数据
        #   (次新股/窗口耗尽), 记了也只会在阶段二空翻一次。游标 = 本页首根 − 1 天,
        #   与 fetch_tx_qfq_paged 内部翻页用的**同一算式**(直接引用 fd._TX_PAGE_CAP,
        #   不再另写一个 640 字面量, 避免两处阈值日后漂移)。
        if max_pages == 1:
            if len(ks) >= fd._TX_PAGE_CAP:
                _SHALLOW_RESUME[sym] = (
                    datetime.datetime.strptime(ks[0]["date"], "%Y-%m-%d")
                    - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                _SHALLOW_RESUME.pop(sym, None)
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
    """腾讯源轻量复探(整段停用后周期调用): 单票纯count请求 + 新鲜度判定, 成功即复位。
    R458: 经 fd.tx_get 走多主机回退 —— 原实现直连 fd._tx_url(硬编码 ifzq.gtimg.cn), 主机被
    WAF 拦时复探**永远失败**, 源状态机再也恢复不了(09-11 那轮 87% 裸价即卡死在此)。"""
    _tx_th.wait()
    try:
        raw = fd.tx_get("sh600000", "day", timeout=8)
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
# R400: 月线补拉「腾讯源停用短路」计数(与网络失败分开归类) —— 09-09 r11 首扫实况:
# 腾讯日K源 16:43 连败停用后, qfqmonth 补拉 143 票被 _src_down 逐个短路成 None,
# main 原统计把停用短路误归「数据不足 143」, 且 WARN 前置 _mb_net and 永不触发,
# 月线排序键整键失效线上无痕。此计数使补拉块能正确分类 down 并落 meta。
_mb_down_fail = {"n": 0}
# R428: 月线新浪聚合兜底成功计数 —— 腾讯月K不可得时用新浪日线聚自然月顶上。
_mb_sina_ok = {"n": 0}
# R438: 「不足判定门槛(<40 根)」时的实际月K根数 —— 供前端灰徽章区分两类空值:
#   有键且有根数 = 上市不足 40 个月(数据在、只是太短) → 徽章「月K不足」+ tooltip 根数
#   无键         = 腾讯/新浪两路均失败或完全无月K      → 徽章「月线—」(真取不到)
# 背景: 用户 09-11 第四次反馈「月线不齐全」, 深查证实取数链路零缺口 —— 26 只空值票
# 两源独立重拉月K **全部 <40 根**(39×2/38/37/36/35/34/33/27/26/22/21/20×2/19/18/17/16×2/
# 15/14/13×2/11/9/3), 且 sigrad 顶背驰组默认折叠态**第 2 行**即命中(563020) ⇒
# 默认视野出现空白必然被读作"数据缺失"。本次**不动判定门槛** —— 收敛实验(16 只 69 根
# 样本做截断对照)显示 40 根时 h2 相对全史中位偏差 0.062/最大 0.477、state 一致率 94%,
# 边界票 君逸数码 h2=0.047 / 石油ETF国泰 0.033 属掷硬币, 必须继续排除。
_mb_bars = {}


def _macd_month_state(pairs):
    """月K [(date, close)] -> MACD(12,26,9) 末两柱状态 dict; 不足 40 根返 None。

    R428: 从 _month_macd 抽出, 腾讯 qfq 月K 与 新浪日线聚合 两条路径共用同一实现 ——
    双实现必然漂移(本月线口径已因双路径/双守卫修过 R390/R400 两轮), 口径唯一化。
    状态语义(前端双周期排序键, R383):
      red          = hist>=0 月线红柱(多头背景)
      green_shrink = hist<0 且较上月缩短(下跌动能衰减)
      green_grow   = hist<0 且较上月加长(月线下跌中继, 沉同档尾)
    """
    if len(pairs) < 40:      # MACD 26+9 EMA 收敛需近 35 根月K(约3年); 不足=结构不可靠
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


def _month_closes_sina(sym):
    """(R428) 新浪日线 -> 自然月末收盘价序列 [(月末交易日, 月收盘)]; 失败返 None。

    背景(09-10 CI 实况): 腾讯 fqkline 对 GitHub 境外出口被 WAF 持续限流(连续两日
    仅 861/6695=13% 成功), 日K阶段即触发源停用; 月线补拉排在扫描末尾, 见 flag 即整块
    跳过 -> 153 票底背驰股月线 100% 落空, 前端全员「月线—」中性。而新浪同轮 87% 可用,
    且月线 MACD 只需月收盘序列, 可由日线聚合得到 -> 用它兜底。
    实测(09-10, 24 只大票): 与腾讯 qfq 月K 的 MACD 状态一致率 79.2%(19/24), 5 处差异
    全在 h2≈0 的临界态(如 sh600887 腾讯 +0.067 / 新浪 -0.020) —— 新浪为不复权裸价,
    除权月有跳空扰动, 故临界票可能翻档。用途是"从完全不可得提升到约八成正确"。
    口径诚实标注: 返回的 state dict 由调用方补 src="sina", 前端 tip 与 meta 落痕区分。
    裁剪到 fd.MIN_DATE 与主链一致(67 根月K > 40 门槛)。
    """
    _sina_th.wait()
    u = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
         "?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, SINA_LEN))
    try:
        arr = json.loads(_get(u).decode("utf-8", "ignore")) or []
    except Exception:   # noqa: BLE001
        return None
    by = {}
    for row in arr:
        d = row.get("day")
        try:
            c = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if not d or d < fd.MIN_DATE or c <= 0:
            continue
        by[d[:7]] = (d, c)     # 同月后写覆盖前写 => 天然取该自然月最后一个交易日
    return sorted(by.values(), key=lambda x: x[0]) or None


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
    pairs = None
    if _src_down(_tx_down, _tx_lock):
        # R428: 腾讯整段停用 —— 不再直接弃票, 转下方新浪聚合兜底(down 计数保留供落痕区分)。
        # R400 原此处是 `_mb_down_fail += 1; return None`, 09-09/09-10 实况 143/153 票
        # 被短路成全中性; 兜底上线后同场景仍能给出约八成正确的月线档。
        _mb_down_fail["n"] += 1
    else:
        _tx_th.wait()
        for _attempt in range(2):      # 初试 + 1 次轻量重试(吸收腾讯偶发 501/超时)
            try:
                raw = fd.tx_get(sym, "month", timeout=15)
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
                break          # 拉取+解析成功; 不足 40 根走下方兜底, 不属网络失败不重试
            except Exception:   # noqa: BLE001
                if _attempt == 1:      # 末次仍失败 → 计数 + 转兜底(补拉块汇总 WARN)
                    _mb_net_fail["n"] += 1
                    pairs = None
                    break
                _tx_th.wait()          # 重试前让出节流时隙(0.35s 自然间隔吸收瞬时 501)
    res = _macd_month_state(pairs) if pairs else None
    if res is not None:
        res["src"] = "tx"
        return res
    # R428: 腾讯路径不可得(源停用 / 网络失败 / 月K不足) -> 新浪日线聚合兜底。
    # 收益实测: 09-10 CI 腾讯月K 0/153 全落空 -> 兜底上线后按新浪 87% 可用率可救回约八成。
    sp = _month_closes_sina(sym)
    res = _macd_month_state(sp) if sp else None
    if res is not None:
        res["src"] = "sina"
        _mb_sina_ok["n"] += 1
        return res
    # R438: 两路都判不出状态 —— 若其中一路确实拿到了月K, 记根数供前端区分
    # 「上市不足 40 个月」与「真取不到」; 两路皆空则不留键 ⇒ 前端仍显「月线—」。
    # ⚠️ 根数取「判定所依据的那个源」: 北交 920 段腾讯恒回 **1 根假数据**(日线主链早有
    # `tx_skip_bj` 短路, 此处自解析路径无守卫), 故 tx 根数 <=1 时改用新浪根数, 否则
    # 北交票会显示成「月K不足 1」(实际 sina 聚合有 3~14 根)。
    _tx_n = len(pairs) if pairs else 0
    _sp_n = len(sp) if sp else 0
    _nb = _tx_n if _tx_n > 1 else _sp_n
    if _nb:
        _mb_bars[sym] = _nb
    return None


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


def _try_tx(sym, max_pages=None):
    """腾讯qfq一次尝试: 成功 (ks,"tx"); 失败(异常/空/陈旧) 计入停用统计后返回 ([],tag)。
    R463: `max_pages=1` 供两阶段取数的**阶段一浅取** —— 状态机/豁免/计数**全部照旧**
    (浅取与全取必须走同一条计数路径: 否则同一次尝试会被记成两次源故障, 或反过来漏计)。
    R337: 成功即复位连续失败计数 —— 原实现只在停用后 probe 成功才清零, 健康期的
    `count` 只增不清, "连续失败"退化成"自上次停用以来累计失败": 分散在多轮抖动中的
    失败会被错误加总(如 5 次历史失败 + 恢复后 1 次偶发失败即达阈值停用主源, 全市场
    误转东财/新浪降级)。成功请求本身就是源健康的最强证据, 应清零。"""
    try:
        ks, tag = _fetch_tx(sym, max_pages=max_pages)
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
        # R458c: `tx_shallow`(深度不足) 与 `tx_noqfq`(腾讯没给复权序列) **同属票面/接口问题**,
        #   与 tx_short 同族 ⇒ 不计连败。
        #   ★ 这是 R458b 的自造缺陷: 新增的 tag 前缀不匹配上面两条豁免, 直接落到 `_src_fail`。
        #   影响**实测为两层**(注意量级, 别夸大):
        #     ① 每轮往 `_tx_down["count"]` 掺入 **≤5 次**伪失败(auto 分支的 _TX_SHALLOW_MAX latch
        #        在第 5 次后就不再尝试 tx, 故单轮封顶 5) ⇒ 与**真实**网络失败(err/empty/stale)
        #        同池累加, 可把原本不到 15 的计数顶过阈值 → 冤停主源(R374 同款血泪, 只是单轮
        #        贡献更小); latch 被绕过时更甚 —— 本地逐票取证(_tx_shallow 逐票清零)实测
        #        20 票即打「源连续失败 >=15 次, 整段停用 (末因: tx_shallow)」, 而腾讯此刻 100% 健康
        #        (两主机 40/40 = 200)。
        #     ② **诊断被污染**: meta.src_fail.tx 与 src_cycle 把"接口只给 641 根"记成"源故障",
        #        于是"腾讯到底挂没挂"再次变成只能靠降级率反推的黑盒 —— 恰是 R458b 自己声明
        #        要消灭的黑盒(R458b 原注释: "不判故障, 仅跳过; 落 meta.src_fail.tx" 自相矛盾)。
        #   修法与 tx_short 对齐: 只切下级源, 不动状态机。
        if tag and tag.startswith(("tx_shallow", "tx_noqfq")):
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


def fetch_kline(sym, skip_tx=False):
    """三级源(按 SRC_ONLY):
      auto: 腾讯qfq(复权) → 东财qfq(复权) → 新浪裸价(最后兜底, 除权假跳空风险由黄条提示)
      tx / em / sina: 仅指定源(本地调试/降速场景)。
    R270: 每级失败计入源统计, 连续 >=MAX 次整段停用 + 周期复探自动恢复 —— 单次抖动不再
    废掉整 run 主源, 也避免境外不可达源逐票空耗 timeout。
    R463: `skip_tx=True` **只**给两阶段取数的阶段一兜底用 —— 它已经单独试过腾讯(浅取)且失败了,
      这里**不该再打一次腾讯**。这不是"省一次请求"那么小: ETF(约 1557 只)+北交+`tx_noqfq`
      的票**本来就拿不到腾讯 qfq**(票面/接口问题, 与源健康无关), 浅取对它们**必然**失败;
      若兜底再试一次腾讯, 这类票的额外请求(≈1600)会把两阶段省下的额度(≈2846)吃掉一大半。"""
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
    # R460: 原此处的 `_tx_shallow["n"] < _TX_SHALLOW_MAX` 闸门已拆 —— 它的 latch 会因**任意 5 只
    #   次新股**(根数天然少, 是完整历史而非截断)把腾讯整源掐掉, 与源健康与否无关; 而分页取数
    #   已把"深度"从"拒收整源"变成"多打 1 次请求"(见文件内 R460 段)。
    if not skip_tx and not _src_down(_tx_down, _tx_lock):
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


def _fetch_one_shallow(sym):
    """R463 **阶段一**取数(worker): 腾讯**只取 1 页**(641 根)。
    为什么 1 页就够: 阶段一判的是 `gate_prefix4` 那 4 项, 实测 100% 深度无关(见该函数)。
    返回与 `_fetch_one` 同形 `(sym, (ks, src))`, 故 `ex.map` 直接复用。

    两条路径:
      · 腾讯可用 ⇒ 浅取 1 页。若这票之后被判"需要深度", 续取游标已由 `_fetch_tx`
        写进 `_SHALLOW_RESUME`(只在"这一页被填满"时才写, 次新票不写)。
      · 腾讯不可用(整源停用/北交跳过/请求失败) ⇒ 落到 `_fetch_one` 的**完整三级源**
        —— 东财(1600 根)/新浪(1500 根)给的本来就是完整序列, 无需阶段二。
    ⚠ 只在 `SRC_ONLY == "auto"` 启用: `--src tx/em/sina` 是本地取证开关, 必须保持
      "取满深度"的原貌(它们的设计意图就是看源的真实全貌)。
    ⚠ 浅取失败**不重试腾讯** —— 直接落到 `fetch_kline(skip_tx=True)`: 腾讯既然刚试过,
      再打一次既浪费时间又**重复计一次源故障**(状态机按次数判"整段停用")。
    ⚠ 浅取走 `_try_tx` 而不是直接 `_fetch_tx`: 源状态机(连败计数 / 票面豁免 tx_short·
      tx_noqfq / 成功复位)必须与全量路径是**同一条**, 否则计数口径就分叉了。"""
    if SRC_ONLY == "auto" and not _src_down(_tx_down, _tx_lock):
        ks, _tag = _try_tx(sym, max_pages=1)      # 走**状态机同一条路径**(计数/豁免/复位照旧)
        if ks:
            return sym, (ks, "tx")
        # 腾讯刚试过且失败(源级故障 or 票面问题) ⇒ 兜底**不再打腾讯**(见 fetch_kline.skip_tx)。
        ks, src = fetch_kline(sym, skip_tx=True)
        if not ks and _need_retry(src):
            time.sleep(0.4)
            ks, src = fetch_kline(sym, skip_tx=True)
        return sym, (ks, src) if ks else (None, src)
    return _fetch_one(sym)


def _fetch_deep_one(job):
    """R463 **阶段二**取数(worker): 对阶段一判"需要深度"的票, 从续取游标再往回翻
    (_TX_MAX_PAGES − 1) 页, 与阶段那 1 页**合并**(走 fetch_tx_qfq_paged 的 got0)。
    入参 `job = (sym, ks_shallow)`。
    返回 `(sym, (ks, "tx"))`, 或 `(sym, None)` = 无可续取 / 续取没多拿到东西
    (两者都表示"**保留阶段一的数据**", 调用方据此不动原值)。
    ★ 本函数失败**不影响正确性**: 拿不到更深就用浅的, 绝不把票弄丢 —— 这是与"降页数"
      那种做法的本质区别(后者是主动放弃深度)。
    ★ `page0 = tx_prefer_seed(sym) + 1`: **页序接着阶段一编** —— 阶段一那 1 页用的是
      `TX_KLINE_HOSTS[seed]`(R467 起按票分散, 见 `fetch_data.tx_prefer_seed`; R467 之前恒为
      `[0]`), 故这里从 seed+1 起(仍保证**同一票内各主页不重复**, 每台只被 prefer 一次)。
      若不续接, 同一台会被 prefer 两轮, 在"该台不可用"时白打一次请求(实测 110 票里 51 票的
      tx 请求从 5 涨到 6 —— 见 fetch_tx_qfq_paged 的 `page0` 注释)。这是**纯负载顺序**修正,
      不改变任何取到的数据(三台同源已实测逐字节一致), 故零正确性风险。"""
    sym, ks1 = job
    end = _SHALLOW_RESUME.get(sym)
    if not end or not ks1:
        return sym, None
    try:
        ks, _dirty, _hasqfq, _np = fd.fetch_tx_qfq_paged(
            sym, "day", min_bars=None, max_pages=max(1, _TX_MAX_PAGES - 1),
            got0={k["date"]: k for k in ks1}, start_end=end,
            page0=fd.tx_prefer_seed(sym) + 1)
    except Exception:                     # noqa: BLE001
        return sym, None
    if not ks or len(ks) <= len(ks1):
        return sym, None                  # 一页都没多取到 ⇒ 保留阶段一数据
    return sym, (ks, "tx")


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
# R467: 新浪与腾讯**两路都用这组样本**(见 probe_mkt_last / _probe_last_tx)。
_MKT_PROBE_SYMS = ("sh600000", "sz000001")


def _probe_last_tx():
    """R467: 用**腾讯**(雷达主取数源)探末交易日, 返回各样本的末根日期(失败则少/空)。

    只取 1 页 ⇒ 2 个请求; 与新浪那路合计 ≤4 次, 符合"探针低速率(≤5 次/主机)"纪律。
    ★ 为什么值得多探一路: **新浪日K发布晚于腾讯**。09-15 15:11 实测新浪仍停在 09-14,
      而腾讯三台镜像(ifzq/web.ifzq/proxy)均已返回 2026-09-15 且**逐字节一致**
      ⇒ 只探新浪会让北京 15 点档的 dispatch 误判"无新行情"而秒退(09-15 15:03
      run 34939802418: 全部 step success、仅跑 11 秒), 当日首轮真扫被迫等到 16:37。"""
    out = []
    for sym in _MKT_PROBE_SYMS:
        try:
            ks, _d, _q, _n = fd.fetch_tx_qfq_paged(
                sym, "day", min_bars=None, max_pages=1,
                page0=fd.tx_prefer_seed(sym))
            if ks:
                out.append(ks[-1]["date"])
        except Exception:                     # noqa: BLE001
            continue
    return out


def probe_mkt_last():
    """探测市场最新交易日(轻量) —— **多源取最大日期**(R467)。
    新浪沪深龙头日K末根 + **腾讯同源样本**取 max; 成功写入模块全局 _mkt_last 供
    _last_fresh/_should_weekend_skip 作新鲜度锚; 失败返回 None 且保留旧锚(网络
    闪断不倒退锚值)。仅在 main 串行段调用一次, 不进 worker 并发。

    ★ R467 改动的安全性论证(为什么"锚前进"不会误杀票):
      · 该锚只出现在两处 —— ① `_should_weekend_skip`: `asof >= 锚` 才跳过本轮;
        ② `_last_fresh` 的 **gap>=4 分支**(源末根落后于市场末交易日 = 真陈旧才拒)。
      · 票面 gap = 北京今日 − 末根日; 常规行情下 gap 恒为 0~3 ⇒ 走 `gap <= 3 → True`
        **根本不会读锚** ⇒ 锚前进**不可能**让任何票被判陈旧。
      · 周末/长假同理安全: 锚 = 各源**末根日期**的 max(不是"今天"), 周六探到的仍是周五
        ⇒ asof(周五) >= 锚(周五) ⇒ 仍跳过, 不产生假日空扫。
      · 另加"锚 ≤ 北京今日"夹取(与 _last_fresh 的未来日期防御同口径), 防单源脏日期。
    ★ R467 合成用例抓到的真缺陷(已修): 原实现只在 `dates` **为空**时保留旧锚, 于是"某一源
      可达但回了个更旧的日期"会把锚**倒退**(如旧锚 09-01、本次两源给出 08-30 ⇒ 锚变 08-30),
      与 docstring 承诺的"网络闪断不倒退锚值"相矛盾。现改为把**旧锚一并纳入 max** ⇒ 单调不减。"""
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
    dates.extend(_probe_last_tx())            # R467: 腾讯也探(其发布早于新浪)
    if _mkt_last:
        dates.append(_mkt_last)               # R467: 旧锚参与 max ⇒ 锚单调不减(不倒退)
    if dates:
        _cap = _bj_today().isoformat()
        dates = [d for d in dates if d and d <= _cap]
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


def _bc_tail(bc, bis, btype, n_last=10, ks=None):
    """近 n_last 笔内的 type 背驰(正序最后一条), 返回 {bi_date_end, end_price, area_ratio,
    bc_type, vol_confirm, fresh_days} 或 None。
    R439: 传 ks 时额外判「背驰端点已被击穿」—— 取背驰笔结束日**之后**的 K 线极值对比
    end_price(bottom 比 low / top 比 high); 破则附 broken/broken_price/broken_date。
    ⚠️ 此判据用**极值**而非收盘价, 与 revpool 的剔票判据(收盘价, 有意避开日内插针)
    刻意不同: 前者回答「结构还成不成立」, 后者回答「今天要不要把它移出池子」——
    两个口径必然会在「盘中刺破但收盘收回」时给出相反答案(sh605020 09-11 实证)。"""
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
    out = {"bi_date_end": bi["date_end"], "end_price": round(bi["end_price"], 3),
           "area_ratio": round(x.get("area_ratio", -1), 3),
           "bc_type": x.get("bc_type", ""), "vol_confirm": bool(x.get("vol_confirm")),
           "fresh_days": fresh}
    if ks:
        d = bi["date_end"]
        after = [k for k in ks if k["date"] > d]
        if after:
            if btype == "bottom":
                k0 = min(after, key=lambda k: k["low"])
                if k0["low"] < bi["end_price"]:
                    out["broken"] = True
                    out["broken_price"] = round(k0["low"], 3)
                    out["broken_date"] = k0["date"]
            else:
                k0 = max(after, key=lambda k: k["high"])
                if k0["high"] > bi["end_price"]:
                    out["broken"] = True
                    out["broken_price"] = round(k0["high"], 3)
                    out["broken_date"] = k0["date"]
    return out


def _seg_extrema(seg, ks, merged):
    """段内极值点 —— 与 report.py `seg_pts` **逐字同式**：段顶 = 段内 high 最大，
    段底 = 段内 low 最小。返回 (klines 索引, 价)；索引不可用时返回 (None, None)。

    ★ 为什么不直接用 `seg["end_price"]`：线段端点价是**笔端点**价，而段内极值才是
    "这段走势真正打到哪"。多数时候二者相同（段末笔就是极值笔），但段内存在更深回踩 /
    更高冲高时不同 —— 主界面线段端点标注用的是极值口径，两处必须一致，否则同一根线段
    在雷达与主界面会标在**两个不同价位**上（规则 11：对照类论断两侧口径都要有一手出处）。"""
    s0 = merged[seg["start"]]["idx_start"]
    e0 = merged[seg["end"]]["idx_end"]
    if e0 < s0:
        s0, e0 = e0, s0
    s0 = max(0, s0)
    e0 = min(len(ks) - 1, e0)
    if e0 < s0:
        return None, None
    if seg["dir"] == 1:
        i = max(range(s0, e0 + 1), key=lambda j: ks[j]["high"])
        return i, ks[i]["high"]
    i = min(range(s0, e0 + 1), key=lambda j: ks[j]["low"])
    return i, ks[i]["low"]


def _bc_state_of(ks, bis, bc, segs, seg_bc, merged):
    """R495: 「最新 K 线是否背驰」状态 —— 雷达详情页头部提示的**唯一判据来源**。

    **段级为主口径**：R487 实证当天可操作的只有段级背驰（H20 70.0%, p≈0.017），
    笔级标签 45.9% 不如随机 ⇒ 结论由段级给，笔级只作对照（不并列成"两个平级结论"）。

    ⚠️ 两个「活跃」不是一回事，必须**同时**报出（最容易被误读的一处）：
       seg.gap  = 自末段往回数的**段数**（结构活跃度，判据 cl.SEG_BC_ACTIVE_GAP）
       seg.days = 端点距末根的**交易日根数**（时间新鲜度，判据 cl.SEG_BC_FRESH_DAYS）
       实测上证 2026-09-18 那条活跃段级底背驰：gap=1（"就在最近一轮"）而 days=44
       （44 个交易日前）—— 只报 gap 会被读成"昨天刚出的"。

    ⚠️ 顶/底并存时取**更新的那一段/那一笔**（max seg_index / max bi_index），与
    `cl.pick_recent_bc` 同律（R492 修的就是"顶无条件优先 ⇒ 更新的底被忽略"）；
    并存这一事实本身如实报出（both），不静默丢弃另一方向。

    ⚠️ 段级与笔级的 days **统一为交易日根数** —— 下方笔级刻意不复用 `_bc_tail` 的
    `fresh_days`（那是**自然日**口径，既有的 st["bottom_bc"] 字段沿用不改），否则同一
    提示条里两个天数含义相反。笔级仍用 `_bc_tail` 判定"是否近端背驰"（保留既有门槛）。
    """
    out = {"seg": None, "bi": None,
           # ★ 阈值随数据下发，前端**不写第二份副本**（规则 20：别名/阈值表禁第二份副本，
           #   否则引擎调参后前端文案仍写着旧数字，两处静默不一致）。
           #   gap_th  = 段级背驰「仍活跃」的段数窗口（cl.SEG_BC_ACTIVE_GAP）
           #   fresh_th= 「新鲜」的交易日窗口（cl.SEG_BC_FRESH_DAYS）
           #   area_th = 背驰成立的 MACD 面积比上限（cl.BC_AREA_RATIO_TH）
           "gap_th": cl.SEG_BC_ACTIVE_GAP, "fresh_th": cl.SEG_BC_FRESH_DAYS,
           "area_th": cl.BC_AREA_RATIO_TH}
    n = len(ks)
    # ---- 段级（主口径）----
    if segs and seg_bc:
        _act = [b for b in seg_bc
                if (len(segs) - 1 - int(b.get("seg_index", -1))) <= cl.SEG_BC_ACTIVE_GAP]
        if _act:
            _pk = max(_act, key=lambda b: int(b["seg_index"]))
            _si = int(_pk["seg_index"])
            _i, _p = _seg_extrema(segs[_si], ks, merged)
            _td = (n - 1 - _i) if _i is not None else None
            out["seg"] = {
                "dir": _pk["type"],              # bottom=底背驰(机会) / top=顶背驰(风险)
                "seg_index": _si,
                "gap": len(segs) - 1 - _si,
                "date": (ks[_i]["date"] if _i is not None else ""),
                "price": (round(_p, 2) if _p is not None else None),
                "days": _td,
                "fresh": bool(_td is not None and _td <= cl.SEG_BC_FRESH_DAYS),
                "area": round(float(_pk.get("area_ratio", 0) or 0), 3),
                "both": len({b["type"] for b in _act}) > 1,
                "n_active": len(_act),
            }
    # ---- 笔级（对照）----
    _pair = []
    for _d, _x in (("bottom", _bc_tail(bc, bis, "bottom", ks=ks)),
                   ("top", _bc_tail(bc, bis, "top", ks=ks))):
        if not _x:
            continue
        _bi = next((int(z["bi_index"]) for z in bc
                    if z.get("type") == _d
                    and 0 <= int(z.get("bi_index", -1)) < len(bis)
                    and bis[int(z["bi_index"])]["date_end"] == _x["bi_date_end"]), None)
        _i = next((i for i, k in enumerate(ks) if k["date"] == _x["bi_date_end"]), None)
        _pair.append((_d, _x, -1 if _bi is None else _bi,
                      None if _i is None else n - 1 - _i))
    if _pair:
        _d, _x, _bi_i, _td = max(_pair, key=lambda t: t[2])
        out["bi"] = {"dir": _d, "date": _x["bi_date_end"], "price": _x["end_price"],
                     "days": _td,                 # ★ 交易日根数（与 seg.days 同口径）
                     "fresh": bool(_td is not None and _td <= cl.SEG_BC_FRESH_DAYS),
                     "area": _x.get("area_ratio"), "bc_type": _x.get("bc_type", ""),
                     "broken": bool(_x.get("broken")), "bi_index": _bi_i,
                     "both": len({t[0] for t in _pair}) > 1}
    return out


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


def shallow_stats(sym, ks, sanitized=False):
    """R463: 门禁**前 4 项**所需的那些字段 —— `analyze_one` 与两阶段取数的**阶段一**共用同一份
    计算(不是复制代码: analyze_one 内部**调本函数**) ⇒ 浅深度判定与全量判定口径必然一致。

    刻意**不触碰 chanlun 引擎**(笔/中枢/背驰) —— R461 实测单票 92.7% 墙钟在 `bi_agreement`,
    而本函数只做 O(n) 的近端统计 ⇒ 这就是"阶段一判定几乎免费"的来源(否则阶段一要再跑一遍
    全量 analyze_one, 6906 票 ≈ 2.8 分钟, 会把两阶段省下的时间吃掉一大半)。

    `sanitized=True` 表示调用方已经 `_sanitize_ks` 过(analyze_one 就是), 跳过重复净化。
    返回 dict(键名与 st 一致, 可直接取用) 或 None(净化后为空 ⇒ 调用方按 ks_bad 处理)。"""
    if not sanitized:
        ks = _sanitize_ks(ks)
        if not ks:
            return None
    last = ks[-1]
    d0, d1 = ks[0]["date"], last["date"]
    _a0 = _days_ago(d0)                       # 首根距今(自然日); 复用避免双算(7000票级)
    tail60 = ks[-60:]
    avg_amt = sum(k["volume"] * _vol_share_per_unit(sym) * k["close"] for k in tail60) / max(1, len(tail60)) / 1e4  # R300: 科创板 volume 单位=股, 勿再 ×100
    return {
        "n_bars": len(ks), "first": d0, "last": d1,
        "span_days": _a0 - _days_ago(d1) if _a0 < 30000 else -1,
        "avg_amt60": round(avg_amt, 1),
        "one_word": _one_word_count(ks),   # R277: 近端120根窗口(原全历史累计误杀上市初期连板的正常票)
        "stop_days": _days_ago(d1),        # 距今天数(停牌判定: 明显大于3)
    }


def analyze_one(sym, ks, draw_n=None):
    """chanlun.analyze -> 精简摘要(雷达schema) + 轻量绘图标注 mark。
    返回 (st, err, mark)。mark 仅供前端详情页叠画, 门禁剔除票也尽量给(可点看结构)。
    R271: 入口统一过 _sanitize_ks 净化(日期有序去重/OHLC自洽/契约窗), 坏数据不进引擎。

    `draw_n` = 该端前端**实际画得出的 K 线根数**（个股/ETF FE_KLINE_N=660 / 行业 IND_KLINE_N=320）。
      · mark 的偏移 `off` 是「距末根」的绝对量，后端缠论跑的是**全历史**（个股 ~1380 根、
        行业合成 ~1386 根）而前端只画 draw_n 根 ⇒ `off > draw_n-1` 的标注前端
        `xi()`/`kAt()` 返回 null 会被**静默剔除**：图上只表现为"比后端少几条"，**不报任何错**。
      · 故 mark 的**唯一上界**就是 draw_n：**不变量 = 后端产出 ⊆ 前端可绘制**（一条都不多发）。
      · ★ 为什么不再加一层"末 N 根窗口"（R496 初版曾用 460）：单一根数窗口**无法同时**满足
        「稠密族(笔/笔级点)够覆盖」与「稀疏族(段级/背驰)不缩水」—— 段级信号稀疏，旧代码按
        **条数**取 `_ssig[-20:]` 覆盖的历史比 460 根**更长**，加窗口反而丢了旧版画得出的点
        （无头渲染实测 sz000001 段级买点 14→10）。只留 draw_n 则对每个族都**数学上 ⊇ 旧版**
        （新集合 = {off ≤ draw_n-1 的全部元素} ⊇ {按条数取末 K 个的可绘制子集}）。
        体积论证与"若要压体积的正解"见 FE_KLINE_N 常量处注释。
      · 默认 None ⇒ **在调用时**解析成模块常量（而非 `= FE_KLINE_N` 默认值）。★ 这不是风格
        问题：默认值在 `def` 时求值 = 把常量**抄了第二份**，改常量（含测试里 monkeypatch 的
        正控）对已绑定默认值**静默无效** —— R496b 踩过：`verify_enc.py` 的正控把窗口调到
        200/60 读数**一模一样**（假绿）。"""
    # ★ 在**调用时**解析默认值（单一来源 = 模块常量）—— 见 docstring 末段。
    draw_n = FE_KLINE_N if draw_n is None else int(draw_n)
    ks = _sanitize_ks(ks)
    if not ks:
        return None, "ks_bad", {}
    # R463: 6 个"门禁前缀字段"改走 shallow_stats —— 与两阶段取数的**阶段一**是同一个函数,
    #   故"浅深度判定"与"全量判定"口径**必然一致**(原先是靠两处代码写一样来维持, 会漂)。
    #   传 sanitized=True: 上面的 _sanitize_ks 刚做过, 不必再 O(n) 重做一遍(6906 票级)。
    _sh = shallow_stats(sym, ks, sanitized=True)
    if _sh is None:
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
        # R463: n_bars/first/last/span_days/avg_amt60/one_word/stop_days 这 7 项已由
        #   shallow_stats 算好(单一事实来源) —— 此处不再重算, 只留引擎侧真正要用的量。
        amp = [h / l - 1 for h, l in zip(highs, lows) if l > 0]
        med_amp = (sorted(amp)[len(amp) // 2] if amp else 0.0) * 100
        zs_last = ({"zd": round(zss[-1]["zd"], 2), "zg": round(zss[-1]["zg"], 2),
                    "date_end": zss[-1]["date_end"]} if zss else None)
        scenario = cls.get("scenario", "")
        bottom = _bc_tail(bc, bis, "bottom", ks=ks)
        top = _bc_tail(bc, bis, "top", ks=ks)
        # R495: 段级（线段级别）三份引擎产物 —— 在此统一取出，`st` 与 `mark` 共用一份
        #   （下方 mark 段级块不再重取，避免同一语义出现第二份副本）。
        #   ⚠️ 必须取在**最早**的消费点之前：本块放在 `st = {` 之前是因为 st["bc_state"]
        #      要用它——这正是 R492 踩过的坑（新增变量放到消费点之后 ⇒ 运行时 NameError 或
        #      静默取到旧值）。自查口令：「这个变量在构造它的那一刻是否已被填好？」
        _segs = r.get("segments") or []
        _sbc = r.get("seg_beichi") or []
        _ssig = r.get("seg_signals") or []
        # R496: 段级中枢**在此一次算好**（原来在 mark 段现算）。理由有二：
        #   ① st 需要 `segzs_n`（引擎侧段级中枢个数）—— CI 断言用它而不是 mark 条数：
        #      mark 已是"只发前端画得出的"（受 draw_n 约束），若断言仍读 mark 计数，就等于
        #      "用守卫去迁就窗口"，且跨轮不可比（改了前端根数断言也跟着变）。
        #   ② 避免同一语义出现第二份副本（规则 20）。★ 位置纪律：必须放在**最早**
        #      消费点（`st = {`）之前 —— 这正是 R492 踩过的坑。
        try:
            _segzs = cl.build_seg_zhongshu(_segs, merged)
        except Exception:   # noqa: BLE001
            _segzs = []
        st = {
            "n_bars": _sh["n_bars"], "first": _sh["first"], "last": _sh["last"],
            "span_days": _sh["span_days"],
            "close": round(last["close"], 3), "chg1d": round(last["close"] / closes[-2] - 1, 4) if n >= 2 else 0,
            "bi_n": len(bis), "zs_n": len(zss), "bc_n": len(bc), "sig_n": len(signals),
            "agree": round(agree["rate"], 3), "agree_n": agree["total"],
            "scenario": scenario, "trend": cls.get("trend_type", ""),
            "last_bi_dir": bis[-1]["dir"] if bis else 0,
            "avg_amt60": _sh["avg_amt60"], "med_amp": round(med_amp, 2),
            "one_word": _sh["one_word"], "dd": round(last["close"] / max(highs) - 1, 3) if highs else 0,
            "zs_last": zs_last, "stop_days": _sh["stop_days"],
            "bottom_bc": bottom, "top_bc": top,
            "seg_bot": bool(cls.get("seg_bc_bottom")), "seg_top": bool(cls.get("seg_bc_top")),
            # R495: 段级信号只数（供详情页"段级 N 条"展示；笔级只数沿用既有 sig_n）。
            # R496: 以上四项均为**引擎侧全量**计数（与 mark 的 draw_n 上界无关）——
            #   产物里的 mark 只含"前端画得出的近端"，两者不要互相替代（见 meta.seg_mark 说明）。
            "segsig_n": len(_ssig), "seg_n": len(_segs), "segbc_n": len(_sbc),
            "segzs_n": len(_segzs),
            # R495: 「最新 K 线是否背驰」的显式状态 —— 前端详情页提示条的**唯一判据来源**。
            #   ⚠️ 与上面 seg_bot/seg_top 的区别：那两项是**方向布尔**（供信号强度与情景判定，
            #   下游 96% 的信号在用），本项是给**人看**的状态包（方向/确认日/距今交易日/新鲜度/
            #   面积比/顶底并存/是否已破），回答的是"当下还能不能按背驰读"。
            #   判据与它们**同源**（都走 cl.SEG_BC_ACTIVE_GAP 活跃窗口）⇒ 不会给出相反结论。
            "bc_state": _bc_state_of(ks, bis, bc, _segs, _sbc, merged),
        }
        # R496: mark 六类改为**只受 draw_n 约束**（= 前端画得出的全部），不再按条数/根数截断。
        #   修的是"可视窗口内大面积有信号却没标"（R495 前六类全按**条数**截断：`bis[-10:]`
        #   /`signals[-14:]`/`_ssig[-20:]`/`bc[-6:]`/`zss[-3:]`/`_segzs[-2:]`）：
        #   实测上证笔折线只覆盖末 11% K 线、笔级买卖点只覆盖末 18%，而 dataZoom 默认显示末 70%。
        #   ★ 为什么不设"末 N 根窗口"（R496 初版曾用 460）——见 FE_KLINE_N 常量处与 docstring：
        #     单一根数窗口无法同时满足稠密族与稀疏族；只留 draw_n 则对每族都数学上 ⊇ 旧版。
        #
        #   ★ 编码（为压 marks.json 体积 —— 该文件被 CI 每次扫描 git add+commit，体积=仓库增量）：
        #     ① **日期 → 距末根偏移** `off = n-1-idx`：`"2026-06-08"`(12B) → `1312`(4B)。
        #        选偏移而非绝对索引：后端源（tx/sina）与前端的腾讯源**末端必然对齐**
        #        （同一市场同一交易日），中段停牌差异只影响中段；绝对索引会被中段差异**整体平移**。
        #     ② **笔折线 → 端点序列**（扁平一维）：笔端点实测 100% 首尾相连
        #        （`bis[i]['end']==bis[i+1]['start']` 且价同：上证 166/166、深证 198/198），
        #        故 (n笔 × 2 端点) 可去重成 (n笔+1) 端点 —— 体积 -60%。
        #     ③ `mk` = 自检包：前端据此选用新解码路径，并把 `dn` 与本端末根日期比对，
        #        不一致时 console.warn（平移风险），**不静默**。
        #   ★ 中枢（zs/segzs）仍各取"最近 3 / 2 个"：与主界面同密度（主界面 8 个/1386 根），
        #     且它们只有 3/2 个、体积可忽略；取全部会把图糊满。**不是**按 off 截断，故不涉及
        #     "发了画不出"（每个都还要过 `_ok` 两端判据）。
        #   enc=2 元素格式：
        #     zs/segzs   [zg, zd, off_start, off_end]
        #     bc         [type, off, price]
        #     line       [off, px, off, px, ...]        ← 扁平端点序列，首尾相连
        #     sig/segsig [dir, kind, off, price]
        _n1 = n - 1
        # 唯一上界 = 前端画得出（draw_n-1）。off 越小越新，0 = 末根。
        _draw_max = draw_n - 1

        def _off(mi, side="e"):
            """merged 索引 → 距末根偏移（0=末根）；越界/缺值返回 None。"""
            try:
                mi = int(mi)
            except (TypeError, ValueError):
                return None
            if not (0 <= mi < len(merged)):
                return None
            ix = merged[mi]["idx_start" if side == "s" else "idx_end"]
            return None if ix is None else (_n1 - int(ix))

        def _ok(*offs):
            """**全部**给定偏移都必须在前端画得出的范围内。
            · 整条才画得出的族（笔折线 / 中枢）：前端是 `if(!kAt(a)||!kAt(b)) continue`
              —— 只要一端画不出就整条丢，故后端必须在**发之前**就按同一判据过滤。
            · 单点族传一个偏移，语义相同（不必再分两套判据：R496 初版就是因为
              "点族上界 / 整条上界"分开两套而漏掉了笔折线首端点与中枢）。"""
            return all(o is not None and 0 <= o <= _draw_max for o in offs)

        mark = {"zs": [], "bc": [], "line": [], "sig": [], "segsig": [], "segzs": [],
                "mk": {"enc": 2, "n": n, "draw_n": draw_n,
                       "d0": ks[max(0, n - draw_n)]["date"], "dn": last["date"]}}
        # 笔中枢：最近 3 个（与主界面"最近 8 个/1386 根"同密度；R496 未改语义）
        # R496b: 判据是 `_ok`（**两端**都要在前端画得出范围内）而非只判"取到了值"。
        #   原因：前端对中枢是 `if(kAt(z[2])&&kAt(z[3])) zsOut.push(z)` —— 只要一端越界
        #   整条就被丢。实测 上证 的最近 3 笔中枢里就有 off=1083 的（前端 320 根显然画不出）
        #   ⇒ 后端原先照发、前端静默丢弃。
        for z in zss[-3:]:
            _zs0, _zs1 = _off(z["start"], "s"), _off(z["end"], "e")
            if not _ok(_zs0, _zs1):
                continue
            mark["zs"].append([round(z["zg"], 2), round(z["zd"], 2), _zs0, _zs1])
        # 笔级背驰点：前端可画范围内全部（原 bc[-6:] ⇒ 按条数，覆盖不足）
        for b in bc:
            i = int(b.get("bi_index", -1))
            if not (0 <= i < len(bis)):
                continue
            _x = _off(bis[i]["end"], "e")
            if not _ok(_x):
                continue
            mark["bc"].append([b["type"], _x, round(bis[i]["end_price"], 2)])
        # 笔折线：前端可画范围内全部，端点序列（首端点单独补，其后每笔只追加终点）
        # R496b: 首端点也必须过 `_ok`（原先只判 `is None`）—— 实测 R496 初版首端点会越界到
        #   off=322~339（>319），前端 `kAt()` 返回 null ⇒ 该笔被剔、折线少一截。
        #   端点序列的性质：整笔要求**两端**都可画，故越界的那一笔直接不入列（前端同样会丢它，
        #   故**视觉零变化**），且序列仍保持首尾相连（不变量：bis[i].end == bis[i+1].start）。
        _ln = []
        for b in bis:
            _x = _off(b["end"], "e")
            if not _ok(_x):
                continue
            if not _ln:
                _s0 = _off(b["start"], "s")
                if not _ok(_s0, _x):
                    continue
                _ln += [_s0, round(b["start_price"], 2)]
            _ln += [_x, round(b["end_price"], 2)]
        mark["line"] = _ln
        # 分类买卖点（笔级）：前端可画范围内全部。原来 signals[-14:] ⇒ 只覆盖末 10~20% K 线。
        for s in signals:
            i = int(s.get("bi_index", -1))
            if not (0 <= i < len(bis)):
                continue
            _x = _off(bis[i]["end"], "e")
            if not _ok(_x):
                continue
            mark["sig"].append([s["dir"], s["kind"], _x, round(s["price"], 2)])
        # 段级（线段级别）买卖点：前端可画范围内全部。R495 加、原来 _ssig[-20:]（同样按条数）。
        for s in _ssig:
            i = int(s.get("bi_index", -1))
            if not (0 <= i < len(bis)):
                continue
            _x = _off(bis[i]["end"], "e")
            if not _ok(_x):
                continue
            mark["segsig"].append([s["dir"], s["kind"], _x, round(s["price"], 2)])
        # 段级中枢：最近 2 个（与主界面同取法 —— 段级中枢跨度天然比笔中枢大一个量级）。
        # `_segzs` 已在 `st` 构造前算好并复用（单一来源，不在两处各算一遍）。
        for z in _segzs[-2:]:
            _sz0, _sz1 = _off(z["start"], "s"), _off(z["end"], "e")
            if not _ok(_sz0, _sz1):     # 同 zs —— 两端都要在前端可画范围内
                continue
            mark["segzs"].append([round(z["zg"], 2), round(z["zd"], 2), _sz0, _sz1])
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


def gate_prefix4(st):
    """R463: `gate_of` 的**前 4 项**(次新/停牌/低流动性/一字板) —— 专门给两阶段取数的**阶段一**
    用: 判定"这票需不需要补深度"。从 gate_of 抽出来是为了**两处共用同一份判定**(复制一份必漂移)。

    ★ 为什么只有这 4 项能用浅数据判 —— 实测(_dbg/r463/stage2_ab.py, 401 票 × 三个深度
      641/1282/1500, 纯本地重放生产引擎):
        · 这 4 项**100% 深度无关**(逐票判定 0 差异): 判定的**结果**不随深度变, 虽然 `n_bars`
          这类字段值本身会变 —— 机理是三者都落在浅取(641)窗口内:
            `MIN_BARS=120` **远小于 641** ⇒ 浅取一律满足第 1 项; `stop_days` 只看最后一根;
            `avg_amt60` 只用近 60 根; `one_word` 只用近 120 根窗口。加长历史只在**远端**追加。
        · 第 5 项「低自洽」**不满足**该性质: 它统计的是**全局**笔序列一致率(`agree`/`agree_n`),
          641 根 vs 1500 根有 **57/401 票翻转**(浅判低自洽而全量判通过), 反向 19 票 ——
          **双向、非单调**, 且 `agree` 大量票卡在阈值 0.6 附近。
      ⇒ 阶段一**必须**止步于这 4 项。把低自洽放进阶段一 = 用噪声把本该进信号的票永久剔出
        (那 57 票会拿不到深度、最终被标成"低自洽"而不进信号), 是**净损失**而非省事。

    ★ 第 6 项 `bi_n < 4`(笔数不足, 见 gate_of 末行)**同样不能**前置 —— 它是**笔数**, 会随
      深度单调变化, 不像前 4 项那样有近端窗口保护。它的实际暴露量**实测为 0**, 但依据
      **不是数值下界**(那种推理是错的, 见下):
        · 实测(_dbg/r463/gate_stage2_risk.py —— 401 票真实 K 线 × 641/1500 两深度本地重放):
          "进阶段二"的 **334 票**里, 641 根下 `bi_n<4` 的 1 只、1500 根下 1 只, 且是**同一只**
          (`sh511990`, bi=2 vs 1 —— 两深度**同侧**都在阈下) ⇒ **真翻转 0 票**。
        · ⚠ 反面教训(**别再用这个推理**): 曾想用"线上产物通过桶的 bi_n 下界 68(全量口径)"
          推"腰斩也不到 4" —— **无效**: 实测进阶段二的票在 641 根下 `bi_n` 可低至 2, 且
          "243→114"这种腰斩是常态。结论要站在**逐票实测**上, 不能站在汇总量的下界上。
        · 机理: `bi_n<4` 只在"几乎无结构"时命中, 这类票笔数在**两个深度下都极少**;
          且它们 n_bars 通常同时偏小, 多被第 1 项(MIN_BARS=120)先拦下。
      ⇒ 结论: 第 5 项(低自洽)才是浅深度的**唯一**真实风险源(76/401 翻转), 第 6 项实测无暴露。
        ⇒ 两者都**不必**进阶段一 —— 这就是「只前置前 4 项」这个设计的完整论证。
      ⚠ 但第 5 项的 76/401 翻转会在**另一处**以另一种方式暴露: 若阶段二对某只有游标的票失败,
        该票停在 641 根 ⇒ 第 5 项须在浅数据上判 ⇒ 门禁码可能与单阶段不同(实测该格子
        76/334 = **22.8%**)。这是两阶段取数**等价性的适用边界**(不是前置决策的问题) —— 见
        main 抓K线段, 监测字段 `meta.tx_stage2.deep_ok/need`。
    返回 gate_code(与 gate_of 同名同义), "" = 这 4 项全过(还需 gate_of 判后两项)。"""
    if st is None:
        return "fail"
    if st["n_bars"] < MIN_BARS:
        return "次新"
    if st["stop_days"] > 20:
        return "停牌"
    if st["avg_amt60"] < LOW_AMT60:
        return "低流动性"
    if st["one_word"] >= ONE_WORD_MAX:
        return "一字板"
    return ""


def gate_of(st):
    """门禁: 返回 (gate_code, desc)。gate="" 表示可通过。
    R463: 前 4 项改调 `gate_prefix4`(单一事实来源, 两阶段取数阶段一也用它) ——
    本函数**判定顺序与输出与 R463 之前逐字节相同**, 已用同进程逐票比较验证。"""
    if st is None:
        return "fail", "分析失败"
    _g4 = gate_prefix4(st)
    if _g4 == "次新":
        return "次新", "数据不足(%d根<%d)" % (st["n_bars"], MIN_BARS)
    if _g4 == "停牌":
        return "停牌", "最后K线距今%d天" % st["stop_days"]
    if _g4 == "低流动性":
        return "低流动性", "近60日均额%.0f万" % st["avg_amt60"]
    if _g4 == "一字板":
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


def _wilson_lb(k, n, z=SIG_LB_Z):
    """比例 k/n 的 Wilson 置信下界(百分点)。n<=0 -> 0.0。

    R445: 行业"信号集中度"的判据。相比原始密度 k/n, 它在小样本上自动降权
    (k=1,n=22 时原始密度 4.5% 但下界只有 0.8%), 在大样本上不因规模被抬高;
    相比"设最小成分数门槛", 它不丢样本(煤炭 29 只仍能凭 6 个信号夺冠)。
    公式: (p + z²/2n ∓ z·√(p(1-p)/n + z²/4n²)) / (1 + z²/n), 取下界。"""
    if n <= 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h) * 100


def _mom(ks, win):
    """近 win 根累计涨幅(%), 不足 win+1 根 -> None。"""
    if len(ks) < win + 1:
        return None
    c1, c0 = ks[-1]["close"], ks[-1 - win]["close"]
    if not c0:
        return None
    return round((c1 / c0 - 1) * 100, 2)


def _dd_pxq(ks, win=IND_POS_WIN):
    """区间位置: (距窗口最高价回撤%, 收盘价在窗口内的分位 0~100, 窗口高, 窗口低)。

    R445: 行业此前只有 amp20(振幅=波动), 没有"跌透度"。低吸的核心是"跌得够不够透",
    故补 距高回撤 + 价格分位。窗口固定取最近 win 根(≈近一年)且 win <= IND_KLINE_N(320),
    使该指标可由入库的 kline 完整复算(CI 与本地/前端三处同口径, 不受 iks 实际长度影响)。
    用窗口内 high/low 极值而非收盘, 与个股 dd(距历史高回撤)同口径。"""
    seg = ks[-win:] if len(ks) > win else ks
    if len(seg) < 20:
        return None, None, None, None
    hi = max(x["high"] for x in seg)
    lo = min(x["low"] for x in seg)
    c = ks[-1]["close"]
    dd = round((c / hi - 1) * 100, 2) if hi > 0 else None
    pxq = round((c - lo) / (hi - lo) * 100) if hi > lo else None
    return dd, pxq, hi, lo


def _rs_pctile(vals):
    """横截面相对强度分位(0~100, 值越大分位越高); None 原样保留。并列取平均秩。

    R443 教训: 相对强度**必须用横截面等权基准**, 不能用指数基准 —— 沪深300 会把
    size/风格算进超额(实测 +2.25pp vs 横截面 +0.58pp)。此处直接对同批板块做分位,
    天然不含基准污染。"""
    ok = sorted(v for v in vals if v is not None)
    n = len(ok)
    if n < 2:
        return [None] * len(vals)
    out = []
    for v in vals:
        if v is None:
            out.append(None)
            continue
        below = sum(1 for x in ok if x < v)
        same = sum(1 for x in ok if x == v)
        out.append(round((below + same / 2.0) / n * 100))
    return out


def _rot_quadrant(rs20, accel):
    """行业轮动四象限标签: 相对强度(横截面分位) × 动量变化(5日 vs 20日 日均差)。

    R445: 只用**描述性**口径回答"钱在往哪流"(强势是否在加速), 不含任何"该买"的
    预测含义 —— 与 R443 回测结论(底背驰机会无统计显著性)保持一致。"""
    if rs20 is None or accel is None:
        return None
    strong = rs20 >= 50
    up = accel > 0
    if strong and up:
        return "领先加速"
    if strong and not up:
        return "领先减速"
    if not strong and up:
        return "落后转强"
    return "落后加速"


def _rets(ks, win=ETF_TWIN_WIN):
    """最近 win 日简单收益率序列(小数); 不足 win+1 根 -> None。

    R447: 供 ETF 同指数判定。用**日收益**而非价格: 价格序列非平稳(共同趋势会虚高相关),
    收益序列才能反映"同一篮子"的同步性。"""
    if len(ks) < win + 1:
        return None
    seg = ks[-(win + 1):]
    out = []
    for i in range(1, len(seg)):
        a = seg[i - 1]["close"]
        b = seg[i]["close"]
        if not a:
            return None
        out.append(b / a - 1.0)
    return out


def _corr(x, y):
    """皮尔逊相关系数; 长度不符或任一序列无波动 -> None。"""
    n = len(x)
    if n < 5 or n != len(y):
        return None
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sxx = syy = 0.0
    for a, b in zip(x, y):
        da = a - mx
        db = b - my
        sxy += da * db
        sxx += da * da
        syy += db * db
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _load_etf_meta(path=ETF_META_JSON):
    """R448: 读官方 ETF 元数据缓存 -> {6位代码: {"t":跟踪标的, "f":合计费率%, "v":规模亿, "n":简称}}。

    **任何异常一律返回 {}** —— 文件缺失/JSON 损坏/结构变更都不应影响扫描主流程;
    调用方据"空字典"自然回落 R447 相关法(即今天的行为), 不产生新的失败模式。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        data = d.get("data") if isinstance(d, dict) else None
        return data if isinstance(data, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def _ef_nav_one(code):
    """R457: 取单只基金**最新单位净值** -> (nav: float, nav_d: 'YYYY-MM-DD') 或 None。

    nav_d(净值日期) 是配对的**唯一依据**, 不是装饰: 用"最新净值"配"最新收盘"会产生
    伪溢价(见 EF_NAV_URL 处反例 511990 +18231%)。失败/字段缺失一律返回 None
    (由调用方计入 fail, 该票不落折溢价字段 ⇒ 前端降级, 绝不编数)。"""
    c = str(code or "")
    if len(c) != 6 or not c.isdigit():
        return None
    try:
        d = json.loads(_get(EF_NAV_URL % c, timeout=EF_NAV_TIMEOUT,
                            referer=EF_NAV_REF).decode("utf-8", "ignore"))
        L = (d.get("Data") or {}).get("LSJZList") or []
        if not L:
            return None
        r0 = L[0]
        v = float(r0.get("DWJZ"))
        dt = str(r0.get("FSRQ") or "")[:10]
        return (v, dt) if (v > 0 and len(dt) == 10) else None
    except Exception:
        return None


def _close_on(ks, d0):
    """R457: 从(升序)K线里取**指定日期**的收盘; 该日无 K 线返回 None。

    ★ 绝不"取最近一根代替" —— 那正是伪溢价的来源。倒序扫描: 命中的通常是末根或倒数第二根,
      故实际 O(1)(无需为 1600 只 ETF 各建一份 date->close 全表, 省 ~100MB 峰值内存)。"""
    if not ks or not isinstance(ks, (list, tuple)):
        return None
    for k in reversed(ks):
        if not isinstance(k, dict):        # 形状异常宁可放弃该票, 也不让整段抛出去
            return None
        dt = k.get("date")
        if dt == d0:
            return k.get("close")
        if dt and dt < d0:          # 已越过净值日(升序) ⇒ 该日无K线
            return None
    return None


def _days_between(d1, d2):
    """纯日期差(天); 任一不可解析返回 None。绝不碰 now()。"""
    try:
        a = datetime.date(*[int(x) for x in str(d1)[:10].split("-")])
        b = datetime.date(*[int(x) for x in str(d2)[:10].split("-")])
        return (b - a).days
    except Exception:
        return None


def _etf_prem_scan(universe, got, csym, asof):
    """R457: ETF 折溢价率 —— 逐只取官方单位净值, 与**同净值日期**的收盘配对。

    就地写 row.etf_uav(单位净值) / etf_uavd(净值日期) / etf_prem(折溢价%) / etf_premd。
    返回统计 dict 供落 meta.etf_prem(把"覆盖多少/剔除了什么"变成每日刷新数字, 而非口头声称)。
    无 ETF 或整段失败 -> 返回 None(不落 meta 键, 前端按缺失降级)。"""
    rows = [r for r in universe.values()
            if r.get("type") == "ETF" and str(r.get("code") or "").isdigit()]
    if not rows:
        return None
    codes = sorted({str(r["code"]) for r in rows})
    t0 = time.time()

    def _w(c):
        if time.time() - t0 > EF_NAV_BUDGET:      # 硬预算: 超时余票直接放弃(缺字段, 不拖全量)
            return (c, None)
        r = _ef_nav_one(c)
        time.sleep(EF_NAV_DELAY)
        return (c, r)

    nav = {}
    with ThreadPoolExecutor(max_workers=EF_NAV_WORKERS) as ex:
        for c, r in ex.map(_w, codes):
            if r:
                nav[c] = r

    st = {"src": "api.fund.eastmoney.com/f10/lsjz", "asof": asof,
          "n_etf": len(rows), "n_codes": len(codes), "n_nav": 0, "n_prem": 0,
          "n_nav_no_px": 0, "n_implausible": 0, "n_stale": 0,
          "fail": len(codes) - len(nav), "hi1": 0, "hi3": 0, "max_abs": 0.0,
          "max_prem": EF_PREM_MAX, "stale_days": EF_NAV_STALE,
          "workers": EF_NAV_WORKERS, "budget_s": EF_NAV_BUDGET,
          "days": {}, "implausible": [], "secs": 0.0}
    for r in rows:
        c = str(r["code"])
        nv = nav.get(c)
        if not nv:
            continue
        v, d0 = nv
        st["n_nav"] += 1
        st["days"][d0] = st["days"].get(d0, 0) + 1
        px = _close_on((got.get(csym.get(c)) or (None,))[0], d0)
        if px is None:
            # 净值日无当日K线: 货币 ETF 净值按**自然日**发布(实测 511990/511660 = 周日),
            # 与交易日K线无法配对; 个别停牌亦然。**此票不产出折溢价**(降级, 绝不硬配)。
            st["n_nav_no_px"] += 1
            continue
        p = (px / v - 1.0) * 100.0
        if abs(p) > EF_PREM_MAX:
            # 口径不可解释(货币ETF「每万份」净值口径; 见 EF_PREM_MAX 实证) -> 剔除并留痕
            st["n_implausible"] += 1
            if len(st["implausible"]) < 8:
                st["implausible"].append([c, round(p, 1)])
            continue
        r["etf_uav"] = round(v, 4)
        r["etf_uavd"] = d0
        r["etf_prem"] = round(p, 3)
        r["etf_premd"] = d0
        st["n_prem"] += 1
        if abs(p) > st["max_abs"]:
            st["max_abs"] = round(abs(p), 3)
        if abs(p) >= 1.0:
            st["hi1"] += 1
        if abs(p) >= 3.0:
            st["hi3"] += 1
        _gap = _days_between(d0, asof) if asof else None
        if _gap is not None and _gap > EF_NAV_STALE:
            st["n_stale"] += 1
    st["secs"] = round(time.time() - t0, 1)
    print("  ETF 折溢价: 取净值 %d/%d(失败%d) %.0fs | 产出折溢价 %d(净值日无K线 %d / 口径剔除 %d"
          " / 滞后 %d) | |溢价|>=1%% %d 只, >=3%% %d 只, 最大 %.3f%%"
          % (st["n_nav"], st["n_codes"], st["fail"], st["secs"], st["n_prem"],
             st["n_nav_no_px"], st["n_implausible"], st["n_stale"],
             st["hi1"], st["hi3"], st["max_abs"]), flush=True)
    return st


def _etf_track_groups(track_of, rets_map, mcap_map):
    """R448: 按官方「跟踪标的」**精确**分组(与 _etf_twin_groups 同输出契约)。

    返回 (out, stat):
      out  = {sym: (代表sym, 组内只数)} —— 仅多成员组落键
      stat = {"groups": 同指数组数, "members": 落键只数, "veto_neg": 被反向相关否决的对数}

    ⚠️ 唯一的**已知失效模式**: 反向/杠杆产品声明了与正向产品**相同的指数名**(A 股现存极少,
       且清盘/转型多年)。故组内再加一道**正向相关否决** —— 双方都有收益序列且 corr<0 时
       不并入。track 相同但**缺收益序列**者照并(新上市 ETF 与老产品同指数, 不能因缺序列而漏)。
    ⚠️ 与相关法**刻意不同**: 本函数**不做**「窗口累计收益差 ≤1.5pp」预筛 —— 该预筛的前提是
       "跟踪误差不会累积到 1.5pp", 而实测同指数异基金的相关可以低到 0.98(见常量块),
       说明跟踪偏差比预想的大 ⇒ 预筛对同指数对存在**误杀**。官方 track 已是精确判据,
       再加收益预筛只会引入假阴性。"""
    by = {}
    for s, t in (track_of or {}).items():
        if t and str(t).strip():
            by.setdefault(str(t).strip(), []).append(s)
    out, n_grp, vneg = {}, 0, 0
    for _t, mem in by.items():
        if len(mem) < 2:
            continue
        mem = sorted(mem)
        par = {s: s for s in mem}

        def find(a, par=par):
            while par[a] != a:
                par[a] = par[par[a]]
                a = par[a]
            return a

        for i in range(len(mem)):
            for j in range(i + 1, len(mem)):
                a, b = mem[i], mem[j]
                ra, rb = find(a), find(b)
                if ra == rb:
                    continue
                x, y = rets_map.get(a), rets_map.get(b)
                if x and y:
                    c = _corr(x, y)
                    if c is not None and c < ETF_TRACK_VETO:     # 反向/杠杆同指数名 -> 否决
                        vneg += 1
                        continue
                par[rb] = ra
        grp = {}
        for s in mem:
            grp.setdefault(find(s), []).append(s)
        for _r, ms in grp.items():
            if len(ms) < 2:
                continue
            n_grp += 1
            rep = max(ms, key=lambda x: (mcap_map.get(x) or 0, x))
            for s in ms:
                out[s] = (rep, len(ms))
    return out, {"groups": n_grp, "members": len(out), "veto_neg": vneg}


def _etf_twin_groups(rets_map, mcap_map, track_of=None):
    """R448: ETF 同指数分组 = **官方跟踪标的(主判据)** ∪ **60 日日收益相关(补漏)**。

    返回 (out, stat):
      out  = {sym: (代表sym, 组内只数)}, 仅多成员组落键
      stat = {"track_groups","track_members","veto_neg","corr_only_groups",
              "missed_by_corr","judged","groups","members","folded"}

    ★ 为什么要双判据(R448 定案, 实证):
      R447 只用相关法。相关法对**同指数异基金**存在假阴性下限 —— sz159527/sz159739 官方
      跟踪标的**字面完全相同**(中证云计算与大数据主题指数), 60 日相关仅 **0.9800** ⇒ 未并组。
      而 0.98 档同时混着**不同指数**的近似产品(6 只「现金流」系列 0.9175~0.9886) ⇒ 阈值
      **无处可调**: 降必误并、升则漏并更多。「表现像阈值不准」实为「判据本身有信息上限」。
      ⇒ 判据换成官方 track(精确), 相关法**保留作补漏**(本轮新上市、缓存尚无代码的 ETF 兜底),
        缓存缺失时相关法自动成为唯一判据 = 精确等于今天的行为(**零行为回退风险**)。

    ★ missed_by_corr 是**自证字段**: 官方 track 找出的同指数组里, 相关法**没找全**的组数。
      它把"相关法漏了多少"从口口相传变成一个每日随扫描刷新的数字 —— 无此字段, 改进幅度
      只能靠一次性人工比对(不可持续)。实测口径见 meta.etf_twin。

    ⚠️ 性能: 官方 track 先并 parent 后, 相关法**仍跑全量两两**(不做"已并则跳过"的优化)——
       因为 skipped 的对会同时从 corr-only 视图里消失, 使 missed_by_corr **虚高**(把"官方
       找全了、相关法也找全了"的对误记成"相关法漏了")。宁可多花 0.7s, 不制造假证据。
       预筛(累计收益差 >ETF_TWIN_PREF)与相关阈值的标定见常量块。"""
    track_of = track_of or {}
    syms = [s for s in rets_map if rets_map.get(s) and len(rets_map[s]) >= 5]
    cum = {}
    for s in syms:
        c = 1.0
        for r in rets_map[s]:
            c *= (1.0 + r)
        cum[s] = (c - 1.0) * 100.0
    syms.sort(key=lambda s: (cum[s], s))       # 升序: 内层可提前 break

    # ---- 1) 官方跟踪标的(主判据) ----
    trk_out, trk_stat = _etf_track_groups(track_of, rets_map, mcap_map)
    trk_groups = {}
    for s, (rep, _n) in trk_out.items():
        trk_groups.setdefault(rep, []).append(s)
    all_syms = list(syms)
    for s in trk_out:                          # 官方组里可能含无收益序列的成员(新上市) -> 也入并查集
        if s not in cum:
            all_syms.append(s)
    parent = {s: s for s in all_syms}
    pc = {s: s for s in all_syms}              # 仅相关法视图(用于如实统计相关法的假阴性)

    def _mk_find(par):
        def find(a):
            while par[a] != a:
                par[a] = par[par[a]]
                a = par[a]
            return a
        return find

    find, findc = _mk_find(parent), _mk_find(pc)
    for _rep, ms in trk_groups.items():
        for s in ms[1:]:
            ra, rb = find(ms[0]), find(s)
            if ra != rb:
                parent[rb] = ra

    # ---- 2) 相关法补漏(全量两两, 不做跳过优化 — 见 docstring 性能说明) ----
    ns = len(syms)
    for i in range(ns):
        a = syms[i]
        for j in range(i + 1, ns):
            b = syms[j]
            if cum[b] - cum[a] > ETF_TWIN_PREF:
                break                          # 已排序 ⇒ 其后只会更大
            c = _corr(rets_map[a], rets_map[b])
            if c is None or c < ETF_TWIN_CORR:
                continue
            # ⚠️ 必须在循环内取根: a 可能在别的 i 迭代里作为 b 被并入别组,
            #    外层缓存的根会失效(虽不致错, 但会让树退化成链)。
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
            rc, rd = findc(a), findc(b)
            if rc != rd:
                pc[rd] = rc

    # ---- 3) 组装输出 + 自证统计 ----
    missed = sum(1 for _rep, ms in trk_groups.items() if len(set(findc(s) for s in ms)) > 1)
    grp = {}
    for s in all_syms:
        grp.setdefault(find(s), []).append(s)
    out = {}
    corr_only = 0
    for _root, members in grp.items():
        if len(members) < 2:
            continue
        if not any(s in trk_out for s in members):
            corr_only += 1                         # 完全靠相关法成组(官方缓存无这些代码)
        rep = max(members, key=lambda x: (mcap_map.get(x) or 0, x))
        for s in members:
            out[s] = (rep, len(members))
    _gn = {}
    for _rep, _n in out.values():
        _gn[_rep] = _n
    # 「官方判据额外并入了几只」= 最终同指数只数 − 单靠相关法能并到的只数(只级口径)。
    # 与 missed_by_corr(组级口径)并列: 组级说明"漏了几组", 只级说明"少盖了几只标的" —— 用户
    # 感知到的是后者(池子里多出几只重复标的), 数据自查靠前者。
    _cgn = {}
    for s in all_syms:
        _cgn.setdefault(findc(s), []).append(s)
    corr_members = sum(len(ms) for ms in _cgn.values() if len(ms) >= 2)
    stat = {"track_groups": trk_stat["groups"], "track_members": trk_stat["members"],
            "veto_neg": trk_stat["veto_neg"], "corr_only_groups": corr_only,
            "missed_by_corr": missed, "extra_merged": len(out) - corr_members,
            "judged": len(rets_map),
            "groups": len(_gn), "members": len(out),
            "folded": sum(_n - 1 for _n in _gn.values())}
    return out, stat


def _regime_of(ist, n_top, n_bot, rsi14, lb_top, lb_bot):
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
       反之板块处中枢上半/上方时, 成分顶背驰计数占优 → "顶背驰区" 语义成立, 保留。

    [R445] "集中"判据改规模可比口径 (lb_top/lb_bot = 95% Wilson 下界, 见 _wilson_lb):
    原判据 n_top>=2 是**绝对数**, 使 16~29 只成分的小板块几乎不可能触发, 而 300+ 只的
    大板块只要 2 只出信号就点亮 —— 与"集中"的语义相反。现改为「绝对数>=SIG_ABS_MIN
    **且** 置信下界>=DENS_LB_STRONG」双条件; 实测(09-11) 5 处修正: 交通运输(5/100)、
    传媒(3/92)、农林牧渔(4/82)、建筑材料(3/63)、石油石化(3/42) 由"顶背驰区"回落 ——
    这 5 个的"集中"在统计上站不住(下界 1.1~2.5%), 属**虚标**。反之小板块如煤炭
    (6/29, 下界 9.9%)、轻工制造(16/106, 9.5%) 凭密度上位, 不再被规模淹没。"""
    # R445: 「显著集中」双条件门 —— 不达标则计数归零, 下方全部分支自动沿用
    if not (n_top >= SIG_ABS_MIN and (lb_top or 0.0) >= DENS_LB_STRONG):
        n_top = 0
    if not (n_bot >= SIG_ABS_MIN and (lb_bot or 0.0) >= DENS_LB_STRONG):
        n_bot = 0
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

    # --- 抓K线: **两阶段取数**(R463; 并发, 全局限速由 tx_get/_tx_th 内 throttle 保证) ---
    #   阶段①: 全市场只取腾讯**第 1 页**(641 根) —— 足够判 `gate_prefix4` 那 4 项, 实测
    #          100% 深度无关(401 票 × 三个深度逐票 0 差异, _dbg/r463/stage2_ab.py)。
    #   阶段②: 只对"这 4 项全过"的票, 从续取游标再往回翻 2 页并与第 1 页**合并**。
    #   省下的是"那些注定被这 4 项剔除的票的第 2/3 页": 线上口径(09-14 产物)约
    #     **1423 只**(次新 233 + 停牌 17 + 低流动性 1172 + 一字板 1) = 20.4% 的票,
    #     按 3 页/票推算请求数 **-13.6%**(20940 → 18094)。请求数是唯一瓶颈(耗时 ≈ 请求数 /
    #     聚合 rps), 故这就是直接的时间收益, 且**不牺牲任何数据的正确性**(理由见下)。
    #
    #   ⚠ **实测对照(R463, 110 票四类桶全覆盖, _dbg/r463/count_reqs.py)**:
    #     上面那个 -13.6% 是**全市场推算**; 小样本实测是: **旧版 407 → 未修页序 444(+9%) → 修后 393(-3.4%)**
    #     tx 请求。三个数都要如实记, 别只看推算:
    #       · "省不出来"在小样本上是**样本结构**使然: 省的是"被剔除票的第 2/3 页", 而 110 票里
    #         被剔除的 40 只**大多本身就只有 1 页**(短历史, 阶段一没填满 ⇒ 旧版也只 1 页)
    #         ⇒ 几乎没有"页"可省。实测只有 **6 票**真省, 全部是"被前 4 项剔除的长历史票",
    #         各 -2~-3 请求 = **-14**。只有全市场规模下那 1423 只(低流动性票历史往往很长)才省得出来。
    #       · "+9%" 那一步**不是两阶段的固有成本, 而是页序缺陷**(R463 已修, 见下)
    #         —— 也是本轮唯一一个"实测推翻推算"的地方, 故单独记。
    #       · 修后**逐票核对**: 与旧版相比**只有 6 票**请求数变化且**全部为负**(净省 -14);
    #         分主机 **ifzq=110 与旧版逐项相同** ⇒ 页序已完全恢复。若 3 台都健康, 轮转本就
    #         零浪费, 此时两阶段的收益纯为"省掉的 2/3 页"(全市场 -13.6%)。
    #   ✅ **已修(R463)**: 让阶段二的**页序接着阶段一编**
    #     (`fetch_tx_qfq_paged(..., page0=1)`, 见该函数 docstring)。
    #     机制已由**逐票 diff 精确对上号**: 旧版 3 页的 prefer 序列 = [0,1,2]
    #     ⇒ 每台**恰好一次**, 第 3 页落在唯一可用的那台上(1 请求); 不续接时两段各自编号
    #     = [0] + [0,1] ⇒ **第 0 台被 prefer 两次** ⇒ 多一次白打。逐票实测(110 票):
    #     51 票 5→6(**+1**)、6 票(被前 4 项剔除的长历史票) 5/4→2(**-2/-3**),
    #     算术自洽 **+51-14 = +37**(与汇总 407→444 一致)。续接后回到与单阶段
    #     **逐票相同**的页序 ⇒ 对被剔除票只剩净省。
    #   ✅ **已做(R464)**: 让 prefer **跳过本轮已知不可用的主机** —— 但落地形态与当初设想**不同**:
    #     当初设想是升级 `_TX_HOST_OK` 为"已证可用主机集(LRU)", 实做时发现"**硬拉黑**"有真风险:
    #     拉黑要从候选里**删除**坏主机 ⇒ 只剩 1 台健康时它承担 100% 流量。故改为**只重排不删除**
    #     (`fetch_data._tx_order` + `_TX_HOST_BAD`: 失败计数排到队尾, 被试到且成功即清零) ——
    #     队首命中就 return ⇒ 队尾坏台**根本不发请求**(白打消失), 而全坏时仍逐个试到
    #     ⇒ **可达性不变 ⇒ 零正确性风险**。且不改 `_TX_HOST_OK` 语义 ⇒ 与 R458/R460 的
    #     限速/停用判定**完全解耦**(那些挂的是 `_tx_pace` 与源状态机, 不是本顺序函数)。
    #     单台不会被打爆: 吞吐上限由**按主机**节流器(0.35s/台)兜底, 而该值正是在"单主机
    #     连轴转"形态下实证的(R458) ⇒ 可直接继承。真相由线上 run 的 `meta.tx_host_bad` 给出。
    #     ★ 实测(110 票注入对照, _dbg/r464/): tx 393→223(**−43.3%**, 坏台 110/62→各 1 次,
    #     健康台 **221→221 不变**), 产物**门禁码/发布标签/深度 全 0 差异**;
    #     ⚠ 但**墙钟不变**(95.5s→97.7s): 耗时由最忙主机的 span(79.5s)决定, 坏台节流并行、
    #     非瓶颈 ⇒ 口径详见 fetch_data._TX_HOST_BAD 注释(省请求数, 不是提速)。
    #   ✅ **已做(R467)**: 修"**三台主机首尾接力**"这个真瓶颈 —— 阶段①固定 1 页 ⇒ 页序索引恒 0
    #     ⇒ `prefer` 对**全市场每一票完全相同** ⇒ 全压第 0 台, 该台连续 ~342 次后被腾讯 501,
    #     失败计数(绝对值>0 即排到队尾)把**全市场齐切**下一台, 再疲劳再齐切 ⇒ 最终只剩一台服务。
    #     实测(R465 请求时间线, 701 票): ifzq 342 → web 348 → proxy 685, **三台 span 零重叠**
    #     ⇒ 阶段① 231s ≈ 663/2.86rps(**恰好等于单台上限 1/TX_INTERVAL**), 而三台独立节流器
    #     本该给 3/TX_INTERVAL ≈ 8.6 rps。改法 = `page0` 传 `fd.tx_prefer_seed(sym)`(按票的
    #     稳定函数, 非枚举下标 —— 否则阶段①②会算出不同起点, 破坏 R463 的页序续接)。
    #     ★ 这是**纯负载顺序**改动: 三台镜像同源逐字节一致 ⇒ 不改变取到的任何数据。
    #     判读: 看 `meta.tx_host_bad` 与三台请求数的**分布**(而非总数)是否变均匀, 以及阶段①耗时。
    #   ✅ **已做(R467b)**: 上条落地后实跑发现收益**不是必然的** —— 五轮同为 701 票的对照:
    #     17:45 R467 {471/446/456} 阶段① 107s 墙钟 262s ｜ 18:00 旧码 {4/1365/4} 232s 533s
    #     ｜ 18:16 R467+阈值 {4/1365/3} 232s 526s ｜ 18:2x R467 {4/1365/3} 232s 526s
    #     ｜ **18:33 旧码 {300/640/434} ~231s 506s** —— 注意**最后一行是旧码却分散了**。
    #     ⇒ 结论: **新旧代码都会分散、也都会塌**, 差异不在 R467 的排序, 而在"本轮有没有台
    #       在**早期**失败过": 早期失败 ⇒ `_TX_HOST_BAD` 把它永久排到队尾 ⇒ 永不被试 ⇒
    #       永不成功 ⇒ 永不清零 ⇒ 整轮塌。⇒ 每轮的形态是**抛硬币**, 不可验收。
    #     ⚠ 归因过程如实记(两版结论都被实探推翻): ① 先怀疑"R464 的 bad 排序把 prefer 挤下去"
    #       ⇒ 加了 `prefer` 阈值豁免(`_TX_BAD_PIN`), **实跑仍塌回单台**; ② 继续钉住三台各发
    #       1 次请求看**响应体**: `ifzq` 确实 HTTP 501(**真坏**), 而 `web` **返回正常数据(健康)**,
    #       它只是被并发窗口内的 3 次抖动记成 bad=3 后**再无复探机会**。⇒ 真因是"**抖动后无复探**",
    #       与排序规则无关; 纯阈值豁免只把塌方推迟 2 次失败, 治不了。
    #     ⇒ 修法 = **断路器半开态**(`fetch_data._TX_BAD_COOLDOWN`): bad >= PIN 只表示"死透",
    #       距最近失败超过 60s 自动降回普通候选(允许再探一次) ⇒ 抖动代价从"整轮单台"降到
    #       "1 次请求 + 1 个冷却窗", 且**源恢复能被自动发现**。
    #     ★ 诚实边界: **冷却窗内仍会单台**(那正是断路器该做的: 死台不白撞) —— 本改动的价值在
    #       "窗口结束后**必定**恢复", 而旧规则是**永久**塌(它不看时间)。验收靠**注入对照**
    #       (可控且可复现, 不靠等主机抽风): `_dbg/r467/test_tx_order.py` ⑤⑥ 为机制级正控,
    #       `--inject-window`(count_reqs.py 新增) 为端到端对照 —— 对健康台注入"前 6s 失败后恢复",
    #       旧码整轮不再用它、新码在冷却窗后自动回到服务。
    #     ⇒ 线上该看的组合判据: `tx_host_cnt`(分布) + `tx_host_cold`(收尾时刻谁在冷却) +
    #       `tx_host_bad`(失败次数) —— 三者一起才能区分"源真坏"与"误拉黑"。
    got, fails = {}, {}
    # R463: 取数前**重置模块级游标表** —— `_SHALLOW_RESUME` 存的是"浅取末页首根 −1 天"。
    #   若同一进程里跑第二次 `main()`(测试脚本 / 批量重跑), 残留的旧游标会让阶段二
    #   从**过期的 end** 起翻页 ⇒ 少取一段历史(去重合并会掩盖重复, 但掩盖不了缺失)。
    #   生产是"一进程一 run"(无实际影响), 但重置成本为 0 ⇒ 不留"只在复用容器时才炸"的隐性状态。
    _SHALLOW_RESUME.clear()
    # R464 同理重置**主机失败计数**(但**保留** memo `_TX_HOST_OK`: 两者语义不同 ——
    #   memo 是"上次成功过的主机"(跨轮复用是有意的), bad 是"本轮的失败证据"(源可能已恢复,
    #   带着上轮黑名单进新轮是错的)。详见 fetch_data.tx_reset_host_state docstring)。
    fd.tx_reset_host_state()
    t_f = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for n_done, (sym, res) in enumerate(ex.map(_fetch_one_shallow, syms), 1):
            ks, src = res
            if ks:
                got[sym] = (ks, src)
            else:
                fails[sym] = src
            if n_done % 500 == 0:
                print("  拉取① 浅取 %d/%d  有效%d 失败%d  %.0fs" % (
                    n_done, len(syms), len(got), len(fails), time.time() - t0), flush=True)
    print("  拉取①(浅取 1 页)完成: 有效 %d / %d, 失败 %d, %.0fs"
          % (len(got), len(syms), len(fails), time.time() - t_f), flush=True)

    # --- 阶段②: 只对"前 4 项门禁全过"的票补深度 ---
    t_d = time.time()
    _n_shallow = len(got)               # 阶段①浅取成功的票数(落 meta.tx_stage2)
    _need = []
    for _sym, (_ks, _src) in got.items():
        _sh = shallow_stats(_sym, _ks)          # 与 analyze_one 同一函数 ⇒ 判定口径必然一致
        if _sh is None:
            continue
        if not gate_prefix4(_sh):
            _need.append((_sym, _ks))
    print("  阶段②筛选: 需补深度 %d / %d (前 4 项门禁已剔除 %d 只, 省其第 2/3 页请求)"
          % (len(_need), len(got), len(got) - len(_need)), flush=True)
    # R463: 量化"**阶段二失败**"这个风险格子(纯观测, 不参与判定) ——
    #   `resume` = 有续取游标的票(浅取填满了 641 根 ⇒ 阶段二**真的会**去翻页),
    #   `deep_miss` = 有游标但一页都没多取到 ⇒ 该票停在 641 根。
    #   为什么这个数要单独落痕: 门禁第 5 项「低自洽」对深度**敏感**(76/401 票翻转),
    #   而停在 641 根的票只能拿浅数据判它 ⇒ 门禁码可能与单阶段不同(实测该格子
    #   **76/334 = 22.8%**, 见 _dbg/r463/gate_stage2_risk.py)。`deep_miss` 就是
    #   "本轮可能漂移的票数"上界 —— 它是两阶段取数**等价性的适用边界**, 必须可见。
    #   (`resume - deep_miss` = 成功补到深度的票, 与旧版逐票深度一致, 零风险。)
    _n_resume = sum(1 for _s, _k in _need if _s in _SHALLOW_RESUME)
    _n_deep = 0
    if _need:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            for n_done, (sym, res) in enumerate(ex.map(_fetch_deep_one, _need), 1):
                if res:
                    got[sym] = res
                    _n_deep += 1
                if n_done % 500 == 0:
                    print("  拉取② 补深度 %d/%d  已补 %d  %.0fs" % (
                        n_done, len(_need), _n_deep, time.time() - t_d), flush=True)
    print("  拉取②(补深度)完成: %d / %d 票拿到更深序列, %.0fs"
          % (_n_deep, len(_need), time.time() - t_d), flush=True)
    if _n_resume > _n_deep:
        print("  ⚠ 阶段②有 %d 票**有游标却没补到深度**(停在 641 根 ⇒ 第5项按浅数据判,"
              " 与单阶段可能有差异, 实测该格子 22.8%%) — 见 meta.tx_stage2.deep_miss"
              % (_n_resume - _n_deep), flush=True)

    # --- R463: 落"**最终采纳**的腾讯序列深度"到 meta.tx_depth ---
    #   必须等到两个阶段都结束才扫: 阶段①记的是 641 根(中间态)、阶段②才可能变深。
    #   判据用 **src == "tx"**(最终采用的源) ⇒ 走 sina/em 兜底的票不计入, 与 R460 的
    #   "腾讯序列深度分布"语义一致; 且 `thin` 只在最终确实浅时才 +1(浅取不算 thin)。
    for _sym, (_ks, _src) in got.items():
        if _src == "tx":
            _record_tx_depth(_ks)

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
    # 取不到月线的票(非 _mb_syms) st 里不含 m_macd 键 —— 前端用
    # `mm==null` 宽松比较, undefined 同样命中 -> 渲染「月线—」中性徽章, 语义等价。
    # R428 更正: 旧注释写"st[\"m_macd\"]=null"不准确, 实际是键缺失(仅 _mb_syms 被赋值);
    # 不统一补 None 是为避免 6600+ 票各增一条键造成 radar.json 无谓膨胀。
    # R437c: 去掉北交排除 —— 实测 revpool 全量 82 只成员含 **6 只北交**
    #   (bj920719/920578/920405/920207/920189/920056), 它们既不在 signals(第二阶段
    #   覆盖不到)、又被此处排除 ⇒ 月线列整列「月线—」, 是用户「月线不全」的另一处缺口;
    #   北交月线实测可得(sina 聚合 56~69 根)。
    #   **ETF 排除刻意保留**: 前端 revCand 首行即 `if(v.type==="ETF")return null;`
    #   ⇒ ETF 永不进逆向观察池, 补拉无任何展示收益, 反增 49 次请求。而 signals 里的
    #   ETF 由第二阶段(sigrad 会显示)覆盖, 不依赖此处。
    _mb_syms = [s for s, st in sts.items()
                if st.get("scenario") == "背驰见底机会" and st.get("bottom_bc")
                and uni.get(s, {}).get("type") not in ("ETF",)]
    _m_macd_stat = None   # R400: 月线补拉统计(meta.m_macd 落痕; None=无底背驰票不补拉)
    if _mb_syms:
        _t_mb = time.time()
        _mb_f0 = _mb_net_fail["n"]
        _mb_d0 = _mb_down_fail["n"]
        _mb_s0 = _mb_sina_ok["n"]
        # R428: 批前显式复探腾讯源一次 —— 153 票 < TX_REPROBE_EVERY(300), 逐票 _src_down
        # 永不触发 probe, 整批只有这一次机会把停用态翻回来(成功→走腾讯 qfq 月K, 失败→全批
        # 落新浪聚合兜底)。09-10 CI 实况: 日K阶段连败停用后本块原样整块跳过 → 153 票 100%
        # 落空(线上 meta.m_macd down=153); 现改为"探测决定路径", 不再有整块跳过分支。
        if _tx_down.get("flag"):
            _pr = _tx_down.get("probe")
            _ok_pr = False
            if _pr:
                try:
                    _ok_pr = bool(_pr())
                except Exception:   # noqa: BLE001
                    _ok_pr = False
            if _ok_pr:
                with _tx_lock:
                    _tx_down["flag"] = False
                    _tx_down["count"] = 0
                    _tx_down["n"] = 0
                print("[scan_radar] 月线补拉前复探腾讯源成功, 本批走腾讯月K", flush=True)
            else:
                print("[scan_radar] 月线补拉前复探腾讯源失败, 本批走新浪日线聚合兜底", flush=True)
        with ThreadPoolExecutor(max_workers=4) as _mb_ex:
            _mb_res = list(_mb_ex.map(_month_macd, _mb_syms))
        for s, mm in zip(_mb_syms, _mb_res):
            sts[s]["m_macd"] = mm
            if mm is None and _mb_bars.get(s):      # R438: 供前端灰徽章显「月K不足 n 根」
                sts[s]["m_macd_bars"] = _mb_bars[s]
        _mb_net = _mb_net_fail["n"] - _mb_f0
        _mb_down = _mb_down_fail["n"] - _mb_d0
        _mb_sina = _mb_sina_ok["n"] - _mb_s0
        _mb_none = sum(1 for mm in _mb_res if mm is None)
        _mb_ok = len(_mb_syms) - _mb_none
        _mb_tx = _mb_ok - _mb_sina      # 腾讯 qfq 月K 命中 = 总成功 - 兜底成功
        # R428: short 口径变更 —— 原 `_mb_none - _mb_net - _mb_down` 是"减法反推", 有兜底后
        # 一票可既计腾讯失败(net/down)又获新浪成功(非 None), 相减会出负数。直接取最终无数据票数。
        _mb_short = _mb_none
        # R400 总失败率口径保留: 全败或任一原因失败>=20% 即 ⚠
        _mb_warn = (_mb_none == len(_mb_syms)
                    or _mb_none >= max(3, len(_mb_syms) // 5))
        _mb_line = ("  月线MACD状态 %d 票(腾讯 %d / 新浪兜底 %d / 数据不足 %d; "
                    "腾讯路径: 停用 %d 网络失败 %d) %.0fs"
                    % (len(_mb_syms), _mb_tx, _mb_sina, _mb_short, _mb_down, _mb_net,
                       time.time() - _t_mb))
        if _mb_warn:
            _mb_line += ("  ⚠ 月线键不完整 %d/%d(>20%%或全败), 未取到票记中性(名次未按月线校正)!"
                         % (_mb_none, len(_mb_syms)))
        print(_mb_line)
        _m_macd_stat = {"tried": len(_mb_syms), "ok": _mb_ok, "tx": _mb_tx,
                        "sina": _mb_sina, "short": _mb_short, "net": _mb_net,
                        "down": _mb_down, "warn": _mb_warn}

    # --- R447: 全池「低吸/趋势地基」—— 多周期动量 + 可比组内相对强度 + ETF 同指数分组 ---
    #   `_mom`/`_rs_pctile` 与行业**同源同实现**(零复刻); 基准换为「同行业 / ETF池」(见常量块)。
    #   mom 直接落 st(与 st.close 等同级), 随 universe/signals 两处同引用带走。
    _t_rs = time.time()
    for sym, st in sts.items():
        _g = got.get(sym)
        if not _g:
            continue
        for _w in MOM_WINS:
            st["mom%d" % _w] = _mom(_g[0], _w)
    # 可比组划分: 个股/北交按申万一级(ind 为 '-' 的 14 只弃牌不入组); ETF 单独一池
    _rs_grp = {}
    for sym in sts:
        _u = uni.get(sym)
        if not _u:
            continue
        if _u.get("type") == "ETF":
            _rs_grp.setdefault(ETF_KEY, []).append(sym)
        elif _u.get("ind") not in ("", "-"):
            _rs_grp.setdefault(_u["ind"], []).append(sym)
    _n_rs = 0
    for _g, _syms in _rs_grp.items():
        if len(_syms) < IND_RS_MIN:
            continue
        _pct = _rs_pctile([sts[s].get("mom20") for s in _syms])
        _nn = sum(1 for s in _syms if sts[s].get("mom20") is not None)
        for _s, _p in zip(_syms, _pct):
            if _p is not None:
                sts[_s]["rsind"] = _p
                sts[_s]["rsn"] = _nn
                _n_rs += 1
    # ETF 同指数分组(仅多成员组落 st.etf_grp/etf_gn, 单只不落 ⇒ 前端只需查键存在)
    # R448: **官方「跟踪标的」为主判据**(精确; 根治相关法对同指数异基金的假阴性 0.9800),
    #   相关法**保留作补漏**(缓存尚无该代码的新 ETF) ⇒ 缓存缺失时行为精确等于 R447, 零回退风险。
    #   判据细节、假阴性实证、missed_by_corr 自证字段见 _etf_twin_groups docstring。
    _etf_meta = _load_etf_meta()
    _track_of, _fee_of, _nav_of, _mcap_of = {}, {}, {}, {}
    for sym in sts:
        _u = uni.get(sym)
        if not _u or _u.get("type") != "ETF":
            continue
        _mcap_of[sym] = _u.get("mcap")       # **全 ETF** 都记: 官方组内可能含无收益序列的新基金
        _m = _etf_meta.get(str(_u.get("code") or sym[-6:]))
        if not _m:                           # 非 ETF 元数据(如 F10 页面结构变更) -> 不落键, 前端降级
            continue
        if _m.get("t"):
            _track_of[sym] = _m["t"]
        if _m.get("f") is not None:
            _fee_of[sym] = _m["f"]
        if _m.get("v") is not None:
            _nav_of[sym] = _m["v"]
    _etf_rets = {}
    for sym in sts:
        _u = uni.get(sym)
        if not _u or _u.get("type") != "ETF":
            continue
        _g = got.get(sym)
        if not _g or len(_g[0]) < ETF_TWIN_MINBARS:
            continue
        _r = _rets(_g[0])
        if _r:
            _etf_rets[sym] = _r
    _twins, _tw_stat = _etf_twin_groups(_etf_rets, _mcap_of, _track_of)
    for _s, (_rep, _n) in _twins.items():
        sts[_s]["etf_grp"] = _rep
        sts[_s]["etf_gn"] = _n
    print("  全池地基: 动量 %d 票 / 可比组分位 %d 票(%d 组) / ETF 同指数 %d 只"
          "(涉 %d 组; 官方跟踪标的 %d 组 + 相关法补漏 %d 组, **相关法漏 %d 组**)"
          " 元数据缓存 %d 只(命中 track %d) %.0fs"
          % (sum(1 for s in sts if sts[s].get("mom20") is not None), _n_rs, len(_rs_grp),
             _tw_stat["members"], _tw_stat["groups"], _tw_stat["track_groups"],
             _tw_stat["corr_only_groups"], _tw_stat["missed_by_corr"],
             len(_etf_meta), len(_track_of), time.time() - _t_rs),
          flush=True)
    # R447: ETF 同指数去重的**能力声明** —— 前端 revpool 放开 ETF 以此开关为条件。
    # 静态站 HTML 与 radar.json 是**独立部署**的: 新 HTML 上线后、下一次 CI 扫描前, 线上仍是
    # 旧 JSON(无 st.etf_grp)。此窗口内若照常放开 ETF, revpool 会冒出 15 只候选、其中 10 只是
    # 同一「科创人工智能」指数而**无法折叠**(ETF 在 universe 里没有价格序列, 前端算不出相关性)。
    # 故用能力开关而非版本号字符串比较: 有 etf_twin 键 = 本轮确实做过去重, 才允许放开。
    # ⚠️ _twins 是 {每个成员: (rep, n)} —— 同一组有 n 个成员就有 n 条记录。组数与可折叠数
    #    必须**按组去重**后再算: 直接 sum(n-1 for v in _twins.values()) 会把每组算 n 次
    #    (实测 1 组 5 只 ⇒ 错得 20, 正确 4)。此坑由产物核对(ck_artifact)自洽项抓出。
    _grp_n = {}
    for _rep, _n in _twins.values():
        _grp_n[_rep] = _n
    etf_twin_meta = {
        "win": ETF_TWIN_WIN, "corr": ETF_TWIN_CORR,
        "pref": ETF_TWIN_PREF, "minbars": ETF_TWIN_MINBARS,
        "judged": len(_etf_rets),
        "groups": len(_grp_n),
        "members": len(_twins),
        "folded": sum(_n - 1 for _n in _grp_n.values()),
        # R448: 双判据**贡献数**(如实声明, 供前端说明口径与自查"官方判据到底多修了多少")
        "src": "track+corr",
        "track_groups": _tw_stat["track_groups"], "track_members": _tw_stat["track_members"],
        "corr_only_groups": _tw_stat["corr_only_groups"],
        "missed_by_corr": _tw_stat["missed_by_corr"], "veto_neg": _tw_stat["veto_neg"],
        "extra_merged": _tw_stat["extra_merged"], "veto_line": ETF_TRACK_VETO,
        # R448: 官方元数据覆盖率(缓存有多少、本轮命中多少) + 选优判据的硬否决线
        "meta_n": len(_etf_meta), "tracked": len(_track_of),
        "fee_n": len(_fee_of), "nav_n": len(_nav_of),
        "clear_yi": ETF_CLEAR_YI, "thin_amt": ETF_THIN_AMT,
    }

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
        # R448: ETF 官方元数据(跟踪标的/合计费率/净资产规模) —— 前端「细分领域标注 + 同指数选优」
        #   的唯一数据源(universe 无价格序列, 前端算不出这些)。仅 ETF 且命中缓存才落键;
        #   未命中 ⇒ 键缺失 ⇒ 前端按缺失降级为旧展示(R447 版), **零回归**。
        if uni[sym]["type"] == "ETF":
            if sym in _track_of:
                row["etf_track"] = _track_of[sym]
            if sym in _fee_of:
                row["etf_fee"] = _fee_of[sym]
            if sym in _nav_of:
                row["etf_nav"] = _nav_of[sym]
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

    # --- R437: 信号个股月线补拉(第二阶段) ---
    # 背景: 第一阶段只覆盖「底背驰个股」(revpool 排序键用), 而 signals 里的**顶背驰
    # (风险)票**没有 m_macd 键 → 前端 `mm==null` 宽松比较命中 → 整列渲染「月线—」,
    # 风险板块看不到月线级别背景(月红=大方向向上→顶背驰或仅回调; 月绿加长=大方向
    # 向下→顶背驰更危险)。用户 09-11 反馈「有的有月线、有的没有」即此。
    # 范围刻意只取 signals, **不取全宇宙 top_bc 的 1352 只个股** —— 后者请求量 ×9.3,
    # 极易触发腾讯 WAF 限流(R429「整表落空」的根因)。
    # R437c: 去掉 ETF/北交 排除 —— 实测老 ETF/北交月线完全可得(sina 聚合:
    #   510210=69根 159880=67根 159697=41根 159731=58根 920857=56根 920770=44根
    #   920726=65根 920021=69根, 8/17 可得), 原先排除使风险板块的老 ETF 白白缺月线;
    #   真正不可得的只有「上市<40个月」的新 ETF(563020=34根 159201=20根 159233=16根…),
    #   与次新股同理, _month_macd 会写 None 并如实标「月线—」。
    #   边际成本可忽略(signals 内 ETF/北交仅 17 只 vs 个股 229 只)。
    # sig["st"] 与 universe[sym]["st"] 均为 sts[sym] 的**同一引用**, 故此处写入两处同步。
    _mb2_syms = [sym for sym, _sg in signals
                 if "m_macd" not in sts.get(sym, {})]
    _m_macd2_stat = None
    if _mb2_syms:
        _t2 = time.time()
        _b2_f0, _b2_d0, _b2_s0 = _mb_net_fail["n"], _mb_down_fail["n"], _mb_sina_ok["n"]
        with ThreadPoolExecutor(max_workers=4) as _mb2_ex:
            _mb2_res = list(_mb2_ex.map(_month_macd, _mb2_syms))
        _mb2_ok = 0
        for _s2, _mm2 in zip(_mb2_syms, _mb2_res):
            sts[_s2]["m_macd"] = _mm2     # 与第一阶段同款: 取不到也写 None(键存在=已尝试过)
            if _mm2 is None and _mb_bars.get(_s2):    # R438: 同上
                sts[_s2]["m_macd_bars"] = _mb_bars[_s2]
            if _mm2 is not None:
                _mb2_ok += 1
        _mb2_sina = _mb_sina_ok["n"] - _b2_s0
        _m_macd2_stat = {"tried": len(_mb2_syms), "ok": _mb2_ok,
                         "tx": _mb2_ok - _mb2_sina, "sina": _mb2_sina,
                         "short": len(_mb2_syms) - _mb2_ok,
                         "net": _mb_net_fail["n"] - _b2_f0,
                         "down": _mb_down_fail["n"] - _b2_d0}
        print("  月线MACD(信号补拉) %d 票(腾讯 %d / 新浪兜底 %d / 数据不足 %d; "
              "腾讯路径: 停用 %d 网络失败 %d) %.0fs"
              % (_m_macd2_stat["tried"], _m_macd2_stat["tx"], _m_macd2_stat["sina"],
                 _m_macd2_stat["short"], _m_macd2_stat["down"], _m_macd2_stat["net"],
                 time.time() - _t2), flush=True)

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
        # R496b: 行业**必须**显式给 draw_n=IND_KLINE_N —— 该端只入库 IND_KLINE_N 根 K 线
        #   （下方 ist["kline"] = iks[-IND_KLINE_N:]），而缠论跑的是 iks 合成全历史
        #   （~1386 根）⇒ 偏移会远超 320。若沿用个股的 660：`off ∈ [320,659]` 的标注
        #   后端照发、前端 `xi()` 因超出 `dates.length-1` 返回 null 被**静默剔除**
        #   （实测上证 sig 23 条里 5 条 off=322/331/368/382/447 属此类，无任何报错）。
        ist, ierr, imark = analyze_one("ind_" + ind, iks, draw_n=IND_KLINE_N)
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
        # --- R445: 规模可比口径(密度/Wilson 下界) + 多周期动量 + 距高回撤/价格分位 ---
        nm = len(members)
        lb_top = round(_wilson_lb(n_top, nm), 2)
        lb_bot = round(_wilson_lb(n_bot, nm), 2)
        mom5, mom20, mom60 = _mom(iks, 5), _mom(iks, 20), _mom(iks, 60)
        dd, pxq, _px_hi, _px_lo = _dd_pxq(iks)
        accel = (round(mom5 / 5.0 - mom20 / 20.0, 3)
                 if (mom5 is not None and mom20 is not None) else None)
        industries[ind] = {
            "n_member": nm,
            "n_total": ind_total.get(ind, len(members)),
            "n_sig_top": n_top, "n_sig_bot": n_bot,
            "dens_top": (round(n_top * 100.0 / nm, 2) if nm else None),   # 原始密度(展示值)
            "dens_bot": (round(n_bot * 100.0 / nm, 2) if nm else None),
            "sig_top_lb": lb_top, "sig_bot_lb": lb_bot,                   # Wilson 下界(判据值)
            "mom5": mom5, "mom20": mom20, "mom60": mom60,
            "accel": accel,
            "dd": dd, "pxq": pxq, "px_hi": _px_hi, "px_lo": _px_lo,
            "cap": round(total_cap / 1e8, 0),      # 亿元
            "chg1d": chg1d,
            "is_etf": 1 if ind == ETF_KEY else 0,
            "rsi14": rsi14,
            "amp20": _amp20(iks),
            "qual_rate": _qual_rate(ind, ind_total, ind_qual),
            "regime": _regime_of(ist, n_top, n_bot, rsi14, lb_top, lb_bot),
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
    # R445: 横截面相对强度分位 + 轮动四象限(须在全部行业算完后统一做, 故独立一趟)
    #   rs20/rs60 = 该板块 20/60 日动量在**同批 32 个板块**中的分位(0~100);
    #   用横截面而非上证基准 —— R443 实证指数基准会把 size/风格算进超额。
    _ind_keys = sorted(industries.keys())
    _rs20 = _rs_pctile([industries[k].get("mom20") for k in _ind_keys])
    _rs60 = _rs_pctile([industries[k].get("mom60") for k in _ind_keys])
    for _i, _k in enumerate(_ind_keys):
        industries[_k]["rs20"] = _rs20[_i]
        industries[_k]["rs60"] = _rs60[_i]
        industries[_k]["rot"] = _rot_quadrant(_rs20[_i], industries[_k].get("accel"))
    _rs_ok = sum(1 for k in _ind_keys if industries[k]["rs20"] is not None)
    print("  行业横截面相对强度 %d/%d 个可算" % (_rs_ok, len(_ind_keys)))

    print("  行业合成/分析 %d 个, %.0fs" % (len(industries), time.time() - t_ind))

    # --- meta ---
    # asof = 全市场最新交易日: 取 last 众数(set去重后取中位会落到日期值域正中, 曾误得2014)
    from collections import Counter as _Counter
    _lc = _Counter(s["last"] for s in sts.values() if s.get("last"))
    asof = _lc.most_common(1)[0][0] if _lc else ""
    # --- R457: ETF 折溢价率(场内收盘 vs 官方单位净值, 按**净值日期严格配对**) ---
    #   放在 asof 之后(统计里要用 asof 算「净值滞后」), meta 之前(结果要进 meta)。
    #   整段 non-fatal: 失败只缺字段, 前端按缺失降级, 绝不影响 K线/门禁/信号/画线。
    _csym = {str(_r.get("code")): _s for _s, _r in universe.items()
             if _r.get("type") == "ETF"}
    try:
        etf_prem_meta = _etf_prem_scan(universe, got, _csym, asof)
    except Exception as _e:                     # noqa: BLE001
        # ★ 必须是 non-fatal: 折溢价是**新增展示字段**, 与 K线/门禁/信号/画线无关;
        #   若此段异常炸出去, 会让整轮全量扫描无产物(等于为一个装饰性字段废掉当日数据)。
        #   失败 => 不落 meta 键 + 不落任何 row 字段 => 前端按缺失降级(与 R448 同款约定)。
        etf_prem_meta = None
        print("  !! ETF 折溢价段异常(已隔离, 不影响产物): %s: %s"
              % (type(_e).__name__, str(_e)[:120]), flush=True)
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
    # R460: 深度**观测**(取代 R458b 的 `tx_shallow` 落痕) —— 记录本轮 tx 序列的长度分布, 使
    #   "腾讯到底给了多深的历史"可查; 它不参与任何判定(不再有"根数短即判源坏"), 故不进程故障计数。
    tx_depth = {}
    if _tx_depth["n"]:
        tx_depth = {"n": _tx_depth["n"], "min": _tx_depth["min"], "max": _tx_depth["max"],
                    "med": round(_tx_depth["sum"] / _tx_depth["n"]),
                    # thin = 根数 < _TX_MIN_BARS 的票数 —— 分页后这**不是故障**, 而是
                    #   "上市晚(完整历史只有这么多)"或"腾讯侧确无更早数据"; 仅作规模观测。
                    "thin": _tx_depth["thin"], "max_pages": _TX_MAX_PAGES}
    # R458c: 「腾讯没给前复权序列」单独落痕(带票样前 8 只) —— 与 641 截断分开,
    #        否则"某票标着 tx 却是不复权"这类错标事后无从追查。
    if _tx_noqfq["n"]:
        _tx_r["tx_noqfq"] = _tx_noqfq["n"]
        _tx_r["tx_noqfq_samples"] = list(_tx_noqfq["samples"])
    _em_r = dict(_em_down.get("reasons") or {})
    src_fail = {}
    if _tx_r:
        src_fail["tx"] = _tx_r
    if _em_r:
        src_fail["em"] = _em_r
    # R444: 源停用/恢复轮次 —— 与 src_fail 联合判断降级的**形态**:
    # stops 与 resumes 同高 = 间歇抖动(加密复探有效, 复权源能抢回来); stops=1 且 resumes=0
    # = 整轮持续不可用(复探无意义, 该走降级预案)。09-11 只有 src_fail(累计失败数)可查,
    # 74 次失败到底是"1 轮持续"还是"5 轮间歇"无法区分 ⇒ 补此落痕。
    src_cycle = {}
    for _ck, _cd in (("tx", _tx_down), ("em", _em_down)):
        _cs, _cr = _cd.get("stops", 0), _cd.get("resumes", 0)
        if _cs or _cr:
            src_cycle[_ck] = {"stops": _cs, "resumes": _cr}
    meta = {
        "title": "A股全市场缠论雷达",
        "asof": asof, "build_time": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "version": "P3b-r30",   # r30=R496: **详情页 K 线标注(mark)编码与口径重做**。对**信号/门禁
                                #   零影响**（`st` 逐键深比较：除新增 `segzs_n` 外全等，正控生效），
                                #   但**产物契约变了**，故按规则 bump：
                                #   ① 锚改**偏移**：`off = n-1-idx`（0=末根），`dates[i]` 12B → 数字 4B；
                                #      前端 `kAt()/xi()` 同时认日期锚与偏移锚 ⇒ 新旧数据都能画（已实证）。
                                #   ② **去掉按条数/根数截断**，唯一上界 = `draw_n`（前端画得出的 K 线
                                #      根数：个股/ETF 660、行业 320）。旧代码六类全按条数截断
                                #      (`bis[-10:]`/`signals[-14:]`/`_ssig[-20:]`/`bc[-6:]`/`zss[-3:]`/
                                #      `_segzs[-2:]`) ⇒ 实测上证笔折线只覆盖末 11%、笔级买卖点只覆盖末
                                #      18%，而 dataZoom 默认显示末 70% = **可视区内大面积"有信号却没标"**。
                                #      这与主界面 report.py 当年 R472 删掉的 `cutoff = n-500` 同型
                                #      （主界面已改像素判据，雷达是第二个副本，本轮补上）。
                                #   ③ 笔折线改**扁平端点序列**（笔端点实测 100% 首尾相连 ⇒ 去重后
                                #      体积 -60%）；因元素取"全部 off≤draw_n-1"，体积有封顶、不随历史增长。
                                #   ④ 新增 `meta.seg_mark`（引擎侧全量 + 落盘覆盖度两组计数）+ 偏移上界，
                                #      供 CI 硬断言"后端产出 ⊆ 前端可绘制"（这类静默损耗不报任何错）。
                                #   ⚠ 下游预期变化：marks.json 由 9.99MB → ~18MB（Pages 走 gzip，
                                #      实传 ~1.65MB → ~3.2MB）；图上笔折线/买卖点会**显著变多**（属修复）。
                                # r29=R492: **`seg_bot`/`seg_top` 语义修复**(chanlun.classify 不再
                                #   用「全历史 any」而改「近 3 段活跃」)。这是**有正确性影响的变更**：
                                #   线上 radar.json 实证(2026-09-18, 6916 标的)修复前 seg_bot=True
                                #   **6412(92.7%)**、seg_top=True **6424(92.9%)** —— 即该字段实际表达的
                                #   是"本标的历史上曾出现过段级背驰", 几乎恒真。它被 `signal_of()`
                                #   用于强度升级 `strong = 2 if (趋势背驰 or seg_bot) else 1`
                                #   ⇒ **96% 的信号**都被升到 strong=2, 强度字段失去区分度
                                #   （"背驰见底机会"333 条里 320 条 seg_bot=True）。修复后该字段
                                #   回到它本来的语义:「当下是否还有一条段级背驰在起作用」。
                                #   ⚠ 因此 radar.json 的 seg_bot/seg_top 占比会**大幅下降**（属修复
                                #   而非退化）；下游若见到 strong=2 变少，是新口径的正常结果。
        # r28=R464+R467+R467b: **腾讯三台镜像的调度族**(R464 当时未单独 bump,
                                #   本轮与之同族, 一并标 r28)。三者都只改"**先问哪台**", 取到的数据
                                #   一律不变(三台同源逐字节一致), 故属**零正确性变更**。
                                #   · R464(已在 HEAD, 首次落版本号): 已证失败的主机**排到队尾**但不删除
                                #     (`_TX_HOST_BAD` + `_tx_order`) ⇒ 可达性不变, 白撞消失。
                                #     ⚠ 但它是**绝对值且不可恢复**: 队尾台永不被试到 ⇒ 永不成功 ⇒
                                #     永不清零 ⇒ **一旦失败即整轮压制**(见下)。
                                #   · R467: 阶段①固定 1 页 ⇒ 页序索引恒 0 ⇒ `prefer` 对全市场**完全相同**
                                #     ⇒ 全压第 0 台 → 打到疲劳 → 全市场齐切 → 最终只剩一台服务
                                #     (R465 实测 ifzq 342 / web 348 / proxy 685, **三台首尾零重叠**,
                                #     阶段① 231s ≈ 663/2.86rps = 恰好单台上限)。改为**按票分散**
                                #     (`fetch_data.tx_prefer_seed` = crc32(symbol)%3, 真实 701 池实测
                                #     极差 3.85%, 优于 `int(code)%3` 的 6.28%) ⇒ 三台并行。
                                #   · R467b: 修 R467 的**收益不稳定** —— 五轮同池对照实测为
                                #     471/446/456(262s) vs 4/1365/4(533s) vs 300/640/434(506s),
                                #     且**新旧代码都会分散也都会塌** ⇒ 变量是"本轮有没有台在**早期**失败":
                                #     一次瞬时失败即永久出局(并发 4 线程时约 4 次白撞后塌)。
                                #     改为**断路器半开态**: bad >= 3 且距最近失败 < 60s 才算冷却中,
                                #     到期自动回到普通候选(源恢复可被自动发现)。
                                #     ★ 端到端实证(**注入对照**, 可控可复现; 真实主机 501 是间歇的,
                                #       等它复现等于把结论交给运气): 对健康镜像注入"前 6 次失败后恢复" ——
                                #       旧码该台整轮**只拿到 1 次请求**(永久出局), 新码冷却窗到期后
                                #       **恢复服务 61 次**; 正控(全程失败)21 次 ≈ 运行秒数/冷却秒数,
                                #       即死台不会被反复白撞。见 `_dbg/r467/test_inject_recovery.py`。
                                #   · 新增落痕 `meta.tx_host_cnt`(三台实际请求数) / `meta.tx_host_cold`
                                #     (收尾时刻仍在冷却的台) —— 三者(tx_host_cnt/bad/cold)合用才能区分
                                #     "源真坏"与"误拉黑"。**判"源是否真挂"仍须守探针纪律**: 低速率
                                #     (每台 ≤5 次/轮) + 跨 ≥2 时点, 禁用批量探针(单主机 501 可自伤)。
                                #   · 零回归: 四组 701 票对照(含异码同窗 20 分钟那一对)signals 逐条一致、
                                #     门禁码 0 差异、票集合 0 差异、need 集合 0 差异; n_bars 差异只出现在
                                #     跨 15~45 分钟的对照里(1 只, 且同码异时同样出现) ⇒ 归因为**时间**,
                                #     不是代码。ETF `mcap` 差异(99~132 只)在**同码异时**组同样存在
                                #     ⇒ 东财规模快照的时间效应。见 `_dbg/r467/diff_r467.py` / `attrib_table.py`。
                                # r27=R463: **两阶段取数** —— 阶段①全市场只取 1 页(641 根)判
                                #   `gate_prefix4` 四项, 阶段②只对通过者从续取游标补第 2/3 页。
                                #   请求数 **−13.6%**(20940 → 18094; 被前置剔除 1423 只)。
                                #   性质: **结构性省请求, 零正确性变更** ——
                                #   · 前 4 项(n_bars/stop_days/avg_amt60/one_word)**实测 100% 深度无关**
                                #     (401 票 × 641/1282/1500 三深度逐票 0 差异, _dbg/r463/stage2_ab.py)
                                #     ⇒ 被前置剔除的票, 门禁码与全量**逐个相同**(它们本就判到那一步返回)。
                                #   · 「低自洽」**刻意不前置**: 它对全局笔序列敏感 —— 641 vs 1500 根有
                                #     **57/401 票"浅判低自洽→全量通过"的误剔**(反向 19 票; 双向、非单调,
                                #     且 agree 大量票卡在阈值 0.6 附近) ⇒ 前置 = 把这些票永久踢出信号,
                                #     是**净损失**, 不是"省事"。这条红线写进 gate_prefix4 的 docstring。
                                #   · 被前置的票仍拿**前复权**(只是 641 根深), 且 scen/顶底背驰/zs_last
                                #     实测对深度 **0.0% 敏感**(见 _TX_MAX_PAGES 上方 R460 深度 A/B 表)
                                #     ⇒ 展示层无实质变化。
                                #   · 阶段②拿不到更深 ⇒ **保留阶段①数据**, 绝不丢票(与"主动降页数"
                                #     有本质区别: 后者是放弃深度)。
                                #   落痕 `meta.tx_stage2{shallow,need,deep_ok,skip4}` 可事后核算。
                                # r26=R463: **第 3 台同源镜像 `proxy.finance.qq.com/ifzqgtimg` 接入轮页**。
                                #   性质: **吞吐优化, 零正确性变更** —— 请求数不变(仍 3 页/票)、
                                #   窗口不变、拼接基准不变, 只是聚合 rps **5.7 → 8.6 (+50%)**。
                                #   全市场耗时 = 请求数 / 聚合 rps, 故这是"缩短线上时间"里
                                #   **唯一零数据风险**的一条路(对比: 降页数/降深度是拿数据换速度)。
                                #   准入已实证(_dbg/r463/verify_mirror3.py, 6 次请求): 同一 end
                                #   窗口下三台 qfqday **逐根同价**(共同 641/640 日期, 价格差 0 个、
                                #   单边日期 0), 游标窗口边界一致(均 641 根/首根 2024-01-22 与
                                #   640 根/首根 2021-05-18) ⇒ 按页轮流 prefer **不会**取到不同窗口。
                                #   单主机速率**刻意不动**(0.35s = R458 实证安全值): 提速走"加
                                #   独立镜像"而非"压单主机间隔" —— 后者会把单台打到限流, 进而触发
                                #   源状态机"连续失败>=15 整段停用", 那正是 R460 要修掉的故障形态。
                                #   ⚠ 未实证: 第 3 台的**限流独立性**未做"打挂一台看另一台"的实验
                                #   (那违反探针纪律: 自伤会延长冷却, R458 踩过)。依据 = R458 已证的
                                #   家族性结论 + 本机实跑(bounded run); 若线上首轮 `src_fail.tx`
                                #   出现新原因键或 `src_cycle.tx.stops` 上升, 第一嫌疑即此处。
                                # r25=R460: **拆除腾讯 qfq 深度守卫 + 改为分页取全量前复权**。
                                #   行为变更(这是**修 bug**, 不是调参):
                                #   (a) fetch_data 增 `fetch_tx_qfq_paged` —— 641 根是"单次请求
                                #       上限"而非"腾讯没有历史": param 语义为
                                #       `code,period,start_date,end_date,count,fq`, 生产只填 count、
                                #       start/end 留空 ⇒ 永远只取"最新 640 根"。填 end 游标即可翻页
                                #       (2 页 = 1281 根, 首根 2021-06-07)。**拼接安全性已实证**:
                                #       同一日期用 6 个不同 end 窗口取, 2797 个(日期×窗口)对
                                #       **全部同价、最大差 0.000000** ⇒ 基准锚定固定、与窗口无关。
                                #   (b) 拆除 `_TX_SHALLOW_MAX` + `_tx_shallow` 一次性 latch: 其前提
                                #       "根数短 = 源坏"不成立(次新股根数天然少, 是**完整历史**),
                                #       而 latch 使**任意 5 只次新股**即可掐掉腾讯整源。净效果实测
                                #       为净退化: 09-11 tx=876/sina=5821(守卫尚未存在) →
                                #       09-14 tx=**1**/sina=6979(守卫上线当日)。
                                #   (c) 深度改为纯观测 meta.tx_depth{n,min,med,max,thin} ——
                                #       保留"腾讯给了多深"的可查性, 但不再由它判定收不收。
                                #   (d) ★ 日线路径**补上按主机节流**(fetch_data.TX_INTERVAL /
                                #       _tx_pace): 此前 `_tx_th.wait()` 只用于 _probe_tx 与月线,
                                #       **日线完全无节流** ⇒ 4 并发突发把记忆主机打到 501 ⇒ 源
                                #       状态机"连续失败>=15 整段停用" ⇒ 余下全市场裸价。
                                #       600 只实测: tx 仅 79/599、src_cycle.tx.stops=1、
                                #       degraded_pct=87(与 09-11 线上 87% 同签名)。现各主机
                                #       2.9 rps、翻页轮流两台 ⇒ 聚合 ~5.7 rps(R463 起三台 ~8.6)。
                                #   ⚠ 成本(实跑实测, 非估算): 152 只分层样本 **64s**@2页 / **71s**@3页
                                #       (含标的池+月线+ETF 折溢价) ⇒ ~0.4s/票; 样本源占比
                                #       tx:105 / sina:47, degraded_pct 100→**31**。
                                #   ⚠ 深度守卫为何**不能**只取 1~2 页: 实测 `gate` 会翻 19%(641 根)
                                #       / 6.2%(1281 根) ⇒ 那是"用一种不一致换另一种"; 只有 3 页
                                #       (深度与新浪一致)才让本变更成为纯粹的正确性修正。详见
                                #       _TX_MAX_PAGES 上方表格。
                                #   ⚠ 未测完: 腾讯**单 run 请求预算**仍未知 —— 本机出口被自己的
                                #       探针打到 501(两主机同款), 该测量中止; 见 _TX_MAX_PAGES 遗留段。
                                # r24=纯注释更正(零行为变更): 把 R458c 的暴露规模从
                                #   "300 只抽样 13 只/4.3%/多为 ETF" 更正为**全量产物反向度量**
                                #   的 60 只(全为 sh688*)、并写明"集中在科创板 ≠ 科创板普遍"与
                                #   "ETF/北交端未被该产物覆盖"。改的是**结论方向**(旧记会让读者
                                #   以为是 ETF 侧问题), 不是行为; 见 _TX_MIN_BARS 上方注释。
                                # r23=R458: **K线源主机级回退 + qfq 真伪守卫**。三件事:
                                #   (a) 腾讯两同源主机(ifzq / web.ifzq)**各自独立限流**, 谁被拦取决于
                                #       "这台最近被打得多少"(详见 fetch_data.TX_KLINE_HOSTS 的 R458c 更正:
                                #       原"WAF 来回翻、非限流"属**过度归因**)。R249 只修了
                                #       一半(硬编码单主机), 导致 09-11 线上 src_cnt={tx:876, sina:5821}
                                #       = **87% 走不复权裸价**, 且 _probe_tx 直连同一主机 ⇒ 复探
                                #       永远失败、状态机再无法恢复(死锁)。改 TX_KLINE_HOSTS 按序
                                #       回退 + 进程内记忆, 落 meta.tx_host 免事后反推。
                                #   (b) qfqday **恒定截断 641 根**(首根 2024-01-22; count/主机无关;
                                #       300 只样本 qfqday>=1200 的票 0 只) ⇒ 深度守卫拒之, 否则全市场
                                #       窗口从 5.7 年静默砍到 2.6 年。落 meta.src_fail.tx["tx_shallow:641根"]。
                                #   (c) ★ 另一类票腾讯**压根不给 qfqday**, 只给完整 `day`(裸价) ——
                                #       原 `qfqday or ... or day` or 链会把它**贴上前复权标签**,
                                #       而前端对 src!="sina" 直接跳过除权检查并显示
                                #       "(前复权)·除权已平滑" ⇒ 对用户是**肯定性的错误陈述**。
                                #       实测这类票与新浪裸价逐日完全相同(0/1300 差异) ⇒ 改走
                                #       fetch_tx_qfq 判 tx_noqfq 切源, **零数据变化、纯标签修复**。
                                #       落 meta.src_fail.tx["tx_noqfq"](含票样)。
                                #       ★ 规模(2026-09-14 全量产物反向度量, 非抽样; 详见 _TX_MIN_BARS
                                #       上方注释): **60 只** = tx 的 6.8% = 全市场 0.90%,
                                #       **全为 sh688*(科创板个股)** ⇒ "集中在科创板"而非"科创板普遍"。
                                #       (300 只抽样时的旧记"13 只/多为 ETF"已被全量推翻, 勿再引用。)
                                # r22=R457: **ETF 折溢价率**(场内收盘 vs 官方单位净值, 按净值日期
                                #   严格配对) / r21=R456: **ETF 标的池补全**。两件事同轮落地,
                                #   关系是「池补全让折溢价有意义」—— 缺口恰是折溢价最有区分度的
                                #   跨境/商品类, 只在旧池上做折溢价等于做了个看不见重点的功能。
                                #   r22 折溢价: 源/阈值/剔除规则/日期配对反例全见 EF_NAV_URL 与
                                #   EF_PREM_MAX 常量块; 覆盖自证落 meta.etf_prem(每日刷新数字)。
                                #   ⚠️ 折溢价是**纯展示新增字段**, 不进任何判据/排序/门禁 ⇒
                                #   对画线层零影响(已用全量穷举逐票签名 diff 取证: 变化集合为空)。
                                # r21 池补全: 原 `b:MK0021` 单板块只覆盖 **1326/1752 = 75.7%**
                                #   场内基金, 缺 426 只, 且缺口全是实操风险最高的一类:
                                #   **全部跨境 ETF**(241, 纳指/标普/恒生科技/中概互联…) +
                                #   **货币 ETF**(27) + **商品 ETF**(10)。⇒ 改为 ETF 四板块并集
                                #   `b:MK0021,b:MK0022,b:MK0023,b:MK0024`(= 1604 只)。
                                #   证据链 / 为何这是真缺口 / 缺口代价 / 为何不含 LOF, 全见
                                #   EM_FS_FUND 处长注释。该行唯一下游影响 = "标的池多 278 只 ETF";
                                #   对 个股/北交 的 K线/门禁/信号/行业聚合**按构造无影响**
                                #   (三源路径与聚合键完全隔离), 已用 `--src tx --only` 单源
                                #   确定性 A/B 取证(见 2026-09-14 验证记录)。
                                # r20=R448: ETF 同指数去重**换判据** + 官方元数据上屏(三件事)。
                                #   ★ 触发点 = R447 留下的**已知假阴性**: sz159527(云计算ETF广发)
                                #     与 sz159739(云计算ETF鹏华) 官方跟踪标的**字面完全相同**
                                #     「中证云计算与大数据主题指数」, 但 60 日收益相关仅 **0.9800**
                                #     ⇒ 未并组。而 0.98 档同时混着**不同指数**的近似产品(6 只
                                #     「现金流」系列两两 0.9175~0.9886 却属不同指数) ⇒ 阈值
                                #     **无处可调**(降必误并/升则漏并更多)。这不是"阈值不准",
                                #     是"判据本身有信息上限" ⇒ 换判据, 不调参。
                                #   ①**官方「跟踪标的」为主判据** —— 数据源 = 天天基金 fundf10
                                #     基本信息页(公开静态页免鉴权)。实测全量 1282 只 ETF:
                                #     **1282 成功 0 失败, track/费率/规模 100% 覆盖**, 10m12s
                                #     (串行 0.2s; 并发触发 WAF)。落 radar/etf_meta.json(132KB,
                                #     tracked) + radar/fetch_etf_meta.py(增量刷新/--force 全量)。
                                #     结果: 194 个同指数组 / 覆盖 1103 只(相关法只找到 0 组同规模)。
                                #   ②**相关法保留作补漏**(缓存尚无代码的新 ETF 兜底) ⇒ 缓存缺失
                                #     时行为**精确等于 R447, 零回退风险**; 加**自证字段**
                                #     meta.etf_twin.{missed_by_corr, extra_merged, corr_only_groups}
                                #     把"相关法漏了多少"变成每日刷新的数字, 而非一次性人工比对。
                                #   ③**同指数选优**上屏 —— 上游注入 row.etf_track/etf_fee/etf_nav。
                                #     实证支撑(全量 1282 只统计, 非估计):
                                #       · 组内费率极差 **96/194 组 ≥0.20pp**, 最大 0.20%~1.10%
                                #         (0.90pp/年) = **确定的**持有成本差;
                                #       · 「费率最低」≠「规模最大」的组 **132/194 = 68%**
                                #         ⇒ "挑最大的买"在三分之二的指数上是错的;
                                #       · 但唯费率会踩坑: 中证A50 组费率最低者 512240(0.20%)
                                #         规模仅 **0.2 亿**、60日均额 287 万 ⇒ 低于清盘线
                                #         ⇒ 选优必须**双约束**(费率优先 + 规模/流动性硬否决)。
                                #     ⚠️ 反证边界(必须同时上屏, 否则是夸大): K 线层**看不出**
                                #     小 ETF 价格更差 —— 同指数 10 只日均振幅 4.47~4.62%
                                #     (corr(振幅,规模) 仅 +0.045)。选大的理由是**执行成本 +
                                #     清盘尾部风险**, **不是价格质量**。
                                #     ⚠️ 官方判据唯一失效模式 = 反向产品声明同指数名, 故组内设
                                #     **强负相关否决**(<−0.6; 单元自测证实阈值取 0 会误拆 194→163 组)。
                                # r19=R447: 行业地基下沉到个股/ETF 层 —— 三件事, 口径全部与行业**同源
                                #   同实现**(_mom/_rs_pctile 直接复用, 零复刻):
                                #   ①**多周期动量** mom5/mom20/mom60 落全池 st(universe 6630 只
                                #     此前**零价格序列** ⇒ 前端无法兜底复算, 必须后端出字段)。
                                #   ②**可比组内相对强度** rsind/rsn —— 基准**不是**全市场横截面:
                                #     个股照搬全市场会被 size/风格主导(微盘 20 日涨 30% 是常态),
                                #     正是 R443 踩过的坑(沪深300 基准 +2.25pp vs 横截面 +0.58pp);
                                #     故个股/北交用**同申万一级内分位**(31 组), ETF 单独一池
                                #     (ETF 的 ind 恒为 '-')。组内 <IND_RS_MIN(5) 只不落分位。
                                #   ③**ETF 同指数去重** etf_grp/etf_gn —— ETF板块 11 只顶信号里 6 只
                                #     是「自由现金流」系列、revpool 候选 15 只里 10 只是「科创人工
                                #     智能」同一指数 ⇒ 一屏刷屏、真标的被淹。判定用 60 日日收益
                                #     相关系数 >=0.99: 名称**不可靠**("科创AIETF银华" 与 "科创人工
                                #     智能ETF" 名称不同、实测相关 0.995~0.998 同指数); 涨跌方向
                                #     指纹法实测 17 只**无一相同**(对跟踪误差过敏感)已弃用。
                                #     阈值标定(17 只真实 ETF 09-11): 同指数组内 0.994~0.999、
                                #     跨赛道 -0.397~0.819 ⇒ 余量极大零误合并。
                                #     ⚠️ **已知假阴性(实测)**: sz159527 与 sz159739 **同跟踪
                                #     中证云计算与大数据主题指数 930851**(广发官方申赎清单「拟合
                                #     指数代码: 930851」), 但 60 日相关仅 **0.9800** ⇒ 不合并。
                                #     相关法在同指数异基金上存在下限(申赎/现金替代/仓位偏离
                                #     会吃掉相关性), 而 0.98 档同时混着**不同指数**的近似产品
                                #     (6 只「自由现金流/全指现金流/现金流」两两 0.9175~0.9886 却是
                                #     不同指数) ⇒ 降阈值必误并。**根治须引入官方拟合指数代码**,
                                #     不能靠相关法(记为待议)。0.99 是有意的安全侧。
                                #   ④**能力开关 meta.etf_twin** —— 前端 revpool 放开 ETF 以此为
                                #     条件。HTML 与 radar.json **独立部署**: 新 HTML 上线到下次
                                #     CI 扫描之间线上仍是旧 JSON(无 etf_grp), 此窗口若照常放开会
                                #     冒出 10 只同指数 ETF 且**无法折叠**(universe 无价格序列,
                                #     前端算不出相关) ⇒ 有该键才放开。
                                #   ⚠️ 刻意**不加**「按动量排序」tab(个股按 mom60 降序 = 追涨视角,
                                #     与 revpool 逆向哲学方向相反), 也**不做**个股四象限(单票 5 日
                                #     波动绝大部分是噪声, 且会暗示预测力与 R443 自相矛盾)。
                                # r18=R445: 行业视图口径纠错 + 低吸/趋势地基补齐, 四处同改:
                                #   ①**信号集中度**改规模可比口径 —— n_sig 绝对数(板块成分
                                #     数 16~386 差 24 倍)换成 95% Wilson 置信下界(新增
                                #     sig_top_lb/sig_bot_lb/dens_top/dens_bot), 判据/排序/
                                #     色条/regime 四处一律用下界; 实测 5 处"顶背驰区"虚标
                                #     回落(交运5/100 传媒3/92 农林4/82 建材3/63 石化3/42),
                                #     小板块凭密度上位(煤炭 6/29 下界 9.9% → 全市场第一)。
                                #   ②新增多周期动量 mom5/mom20/mom60 + 横截面相对强度分位
                                #     rs20/rs60(32 板块内分位, 非指数基准) + 动量变化 accel
                                #     + 轮动四象限 rot。
                                #   ③新增"跌透度" —— 距近一年高点回撤 dd + 价格分位 pxq
                                #     (窗口 250 根 <= IND_KLINE_N, 可由入库 kline 完整复算)。
                                #   ④排序新增 RSI 超卖优先(设为默认)/趋势强度/相对强度/
                                #     跌透优先; 原默认"RSI 超买优先"对逆向视角方向相反。
                                # ⚠️ 全部新增字段均为**描述性**口径, 不含"该买"预测含义 ——
                                #    与 R443 结论(底背驰机会无统计显著性)保持一致。
                                # r17=R444: ①前端新增「信号实证效力」条 —— R443 五年回测结论常驻看板
                                #     (背驰见顶风险 −0.80pp p=0.018 唯一显著 / 底背驰机会 +0.58pp p=0.13
                                #     未获证实), 双卡片 + 可展开证据表; ②「已破」标记补**风险维度**提示
                                #     (20日内最大浮亏>5% 概率 72.1% vs 58.1%): 收益维度无差异(p=0.74)
                                #     但风险维度有效 —— 覆盖 6 处消费端; ③源降级治理: 复探周期 300→80
                                #     + 停用后首次调用即复探(抓短恢复窗口) + meta.src_cycle 落痕
                                #     (区分"整轮持续不可用"与"间歇抖动")。
                                # r16=R439: 背驰端点击穿标记 —— _bc_tail 加 ks 入参, 取背驰笔结束日
                                #     **之后**的极值对比 end_price(bottom 比 low / top 比 high),
                                #     破则附 broken/broken_price/broken_date。前端「底线」列加「已破」
                                #     红标 + tooltip。⚠️ 判据用**极值**, 与 revpool 剔票判据(收盘价,
                                #     有意避日内插针)**刻意不同** —— 反例 sh605020 09-11: 盘中最低
                                #     30.03 击穿 08-25 背驰低点 30.44, 收盘 30.76 收回 ⇒ 看板口径
                                #     仍在池、缠论口径一买已失效。两个问题不同, 故两个判据并存。
                                # r15=R438: 月线空值分因 —— 新增 st.m_macd_bars(月K不足时的实际根数),
                                #     前端灰徽章由「月线—」改「月K不足 n 根」(无根数=两源真取不到,
                                #     仍显「月线—」)。判定门槛 40 根**未改**(收敛实验证边界票不可靠)。
                                #     承用户第四次反馈「月线不齐全」—— 深查确认取数链路零缺口(416 只有键
                                #     =补拉池 171 ∪ signals 276), 26 只空值票两源重拉月K 全部 <40 根。
                                # r14=R437c: 第二阶段补拉放开 ETF/北交(signals 内 17 只, 实测 8 只可得,
                                #     老 ETF 月线此前被筛选白挡) + 前端 revpool 补「月红」徽章(修 R383「红柱
                                #     省位」致月线列整列空白, 与表头文案「月红柱=多头背景」自相矛盾)。
                                # r13=R437: 月线补拉加第二阶段「信号个股」(顶背驰风险票可见月线) +
                                #     meta.m_macd2 独立落痕 + 前端「月线—」归因文案改准(原称"两源不可用"
                                #     实为"未在补拉范围", 属 R425/R436「口径须与判据一致」同类)。
                                # r12=R429: 月线补拉批前复探腾讯源 + 新浪日线聚合兜底(修 CI 上月线 100% 落空);
                                #     meta.m_macd 落痕加 tx/sina 维。补记 R400(落痕首版)/R403/R404(灰字「月线—」徽章)
                                #     /R406(sigrad 同步月线徽章) 四轮改动**均未 bump 版本号**(R373 纪律漏执行) ——
                                #     后果: 线上 version 停在 r11, 无法从产物判断跑的是哪版代码(09-10 定位月线问题时
                                #     即受此扰, 只能靠 m_macd 落痕有无反推 R400 是否上线)。r12 起恢复"实质改动必 bump"。
                               # r11=R393: radar.html .stk-grid 加 760px 窄屏适配(5 关键列, 桌面 11 列零影响) — 全文件唯一无 980 变体的 grid 列表, 11列min宽~650px 在手机容器被 overflow:hidden 直裁右侧 4-5 列
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
        "tx_host": fd.tx_host(),               # R458: 腾讯实际服务主机(空=本轮从未成功)。
                                               # ifzq/web.ifzq 两个同源主机会被 WAF 轮流拦
                                               # (09-06 拦 web./09-14 拦 ifzq), 落痕后一眼看出
                                               # "哪台在服务", 不必再靠 87% 裸价反推。
        "tx_host_bad": fd.tx_host_bad(),       # R464: 本轮被降序的主机 → 失败次数(纯观测)。
                                               # ★ 空 {} = 三台都健康 ⇒ `_tx_order` 输出与改动前
                                               #   逐字节相同(零回归的天然判据); 非空则给出"哪几台
                                               #   被判死、各白打过几次"。与 tx_host 合用可完整还原
                                               #   本轮主机可用性, 也是"R464 是否真在省请求"的证据。
        "tx_host_cnt": fd.tx_host_count(),     # R467: 本轮各主机**实际发出**的请求数(纯观测)。
                                               # ★ 这是判"R467 按票分散是否生效"的唯一硬证据:
                                               #   旧版实测 342/348/685(三台首尾接力, 阶段①
                                               #   231s ≈ 663/2.86rps 单台上限); 新版同池实测
                                               #   471/446/456(阶段① 107s)。**总请求数不该变**。
        "tx_host_cold": fd.tx_host_cold(),      # R467b: **收尾时刻**仍处冷却期的主机(纯观测)。
                                               # ★ 与 tx_host_cnt 合用才能判"单台独大"的性质:
                                               #   若某台吃掉了绝大部分请求, 而它**不在**冷名单里
                                               #   ⇒ 另两台的失败是**实时**的(源真坏), 属正常退化;
                                               #   若冷名单里躺着**本可服务**的镜像 ⇒ 是冷却窗没走完
                                               #   (最坏 60s, 会自动恢复), 而**不是**永久压制。
        "src_cycle": src_cycle,                # R444: 各源停用/恢复轮次 {stops,resumes} —— 区分
                                               #   间歇抖动(resumes 高)与整轮不可用(stops=1,resumes=0)
        "tx_stage2": {                         # R463: 两阶段取数落痕(纯观测, 不参与任何判定)
            "shallow": _n_shallow,             #   阶段①(1 页/票)取到有效序列的票数
            "need": len(_need),                #   需补深度 = `gate_prefix4` 四项全过的票数
            "resume": _n_resume,               #   其中**有续取游标**的票数(浅取填满 641 根 ⇒
                                               #   阶段②真的会去翻页 —— 这才是会踩风险格子的集合)
            "deep_ok": _n_deep,                #   阶段②真正拿到**更深**序列的票数
            "deep_miss": _n_resume - _n_deep,  # ★ 有游标却没补到 ⇒ 该票停在 641 根, 门禁第 5 项
                                               #   只能在浅数据上判 ⇒ 与单阶段可能不同(该格子实测
                                               #   **22.8%** 会翻转, _dbg/r463/gate_stage2_risk.py)。
                                               #   这是"可能漂移的票数"**上界** ⇒ **期望 0**;
                                               #   非 0 = 等价性出现缺口(可用性导致, 非逻辑缺陷),
                                               #   采信名单前须与单阶段产物逐票对照。
            "skip4": _n_shallow - len(_need),  #   被前 4 项剔除 ⇒ 省掉其第 2/3 页请求的票数
        },                                     # 期望形态: skip4 ≈ 20%(线上 09-14 口径 1423/6906),
                                               #   deep_ok/need ≈ 100% **且 deep_miss = 0**;
                                               #   need 远小于 shallow 说明前置生效。
        "tx_depth": tx_depth,                  # R460: 腾讯 qfq 序列深度分布(纯观测, 不参与判定)
                                               # R463 起语义收紧为「**最终采纳**的序列深度」:
                                               #   仍记全部 src=="tx" 的票(含停在 641 的), 故
                                               #   min=641 = 有票最终只有 1 页 —— 其构成是
                                               #   "被前 4 项前置的票"(对照 tx_stage2.skip4) +
                                               #   "阶段②没补到的票"(对照 tx_stage2.deep_miss),
                                               #   thin 只在"最终确实浅"时计 —— 浅取的 641 根
                                               #   只是中间态, 不再计入, 否则每轮会凭空多几千个 thin。
                                               #       "整轮持续不可用"与"间歇抖动"(后者加密复探可救)
        "m_macd": _m_macd_stat,                # R400: 月线补拉统计 {tried,ok,short,net,down,warn}
                                               # (09-09 r11 首扫 143 票全败曾无痕; 落痕后看门狗/前端可查
                                               # 月线排序键失效 —— warn=true 时应视同降级提示)
        "m_macd2": _m_macd2_stat,              # R437: 信号个股月线补拉(第二阶段)独立落痕
                                               # {tried,ok,tx,sina,short,net,down}; 与 m_macd(仅底背驰
                                               # 个股=revpool 排序键)分开, 免污染前端 warn 文案口径
        "mkt_last": _mkt_last or "",           # R272: 市场末交易日锚(新浪探测; 空=探测失败回落窗口表)
        "etf_twin": etf_twin_meta,             # R447: ETF 同指数去重的**能力声明**(win/corr/pref/
                                               # minbars/judged/groups/folded)。前端 revpool 放开
                                               # ETF 以此为条件 —— HTML 与 radar.json 独立部署,
                                               # 旧 JSON(无 etf_grp)窗口内不能放开(会冒出 10 只
                                               # 同指数 ETF 且无法折叠)。有该键 = 本轮确实去重过。
        "sanit_drop_bars": _sanit_drop_bars,   # R275: 净化丢弃 bar 数(坏根量化诊断; 正常≈0, 激增=源数据异常)
        # R457: ETF 折溢价率的**能力声明 + 覆盖自证** —— 把"取到多少/为什么剔除"变成每日
        # 刷新的数字而不是口头声称。键存在 = 本轮确实抓过净值(前端据此区分"本轮没这项数据"
        # 与"这只恰好无净值日K线")。字段: n_etf/n_codes/n_nav/n_prem/n_nav_no_px(净值日无K线,
        # 主要是货币ETF按自然日发净值)/n_implausible(口径不可解释, 附样例)/n_stale(净值滞后)/
        # fail/hi1/hi3(|溢价|≥1%、≥3% 的只数)/max_abs(本轮最大|溢价|)/days(净值日期分布)/
        # max_prem, stale_days, workers, budget_s, secs, src。
        "etf_prem": etf_prem_meta,
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
    # R495: 段级标注产出率 —— **防静默失效**（与规则 4「白名单静默剥离」同族的坑）：
    #   mark 是整块写入、不经白名单，但若 chanlun 的 seg_signals / segments 接口改名或
    #   在某种数据形态下返回空，前端只会"图上少了段级点"——**不报任何错**。这里落成数字，
    #   供 CI 自检硬断言 + 线上人工核对，与 meta.marks_n 同性质。
    #   ⚠️ 分段统计: marks_out=个股/ETF（写 marks.json）; industries.*.mark=行业（写 radar.json）。
    _ind_marks = [(v.get("mark") or {}) for v in industries.values()]
    # R496b: 偏移上界的**结构性自检量** —— 「后端落盘窗口」与「前端画得出的窗口」是否一致。
    #   不变量：行业 mark 的 off 必须 ≤ IND_KLINE_N-1。若后端窗口大于该端入库的 K 线根数，
    #   多出来的标注前端 `xi()` 会返回 null 并**静默剔除**（图上"比后端少几条"，无任何提示）。
    #   R496b 之前实测：行业 sig 23 条中 5 条 off ∈ {322,331,368,382,447} 属此类。
    #   落成数字供 CI 硬断言 —— 这类"发得出、画不出"的损耗没有任何报错，只能靠断言兜。
    #   ★ enc=2 各族的偏移下标（**不要猜**，见 mark 段注释）：
    #       bc [type, off, price] → 1 · sig/segsig [dir, kind, off, price] → 2
    #       zs/segzs [zg, zd, off_s, off_e] → 2,3 · line [off, px, ...] → 偶数下标
    _OFF_IDX = {"bc": (1,), "sig": (2,), "segsig": (2,), "zs": (2, 3), "segzs": (2, 3)}

    def _off_max_of(ms):
        mx = -1
        for m in ms:
            for k, idxs in _OFF_IDX.items():
                for e in (m.get(k) or []):
                    for j in idxs:
                        if isinstance(e, list) and len(e) > j and isinstance(e[j], int):
                            mx = max(mx, e[j])
            _ln = m.get("line") or []
            if _ln and not isinstance(_ln[0], list):   # 扁平端点序列（enc=2）
                for j in range(0, len(_ln), 2):
                    if isinstance(_ln[j], int):
                        mx = max(mx, _ln[j])
        return mx

    meta["seg_mark"] = {
        "n_uni": len(universe),
        "n_uni_segsig": sum(1 for m in marks_out.values() if m.get("segsig")),
        "n_uni_segzs": sum(1 for m in marks_out.values() if m.get("segzs")),
        "n_uni_segsig_total": sum(len(m.get("segsig") or []) for m in marks_out.values()),
        "n_ind": len(_ind_marks),
        "n_ind_segsig": sum(1 for m in _ind_marks if m.get("segsig")),
        "n_ind_segzs": sum(1 for m in _ind_marks if m.get("segzs")),
        "n_bc_state_seg": sum(1 for row in universe.values()
                              if ((row.get("st") or {}).get("bc_state") or {}).get("seg")),
        # ===== R496: 两组计数，**不要互相替代** =====
        # ① `_eng` 后缀 = **引擎侧全量**（与 mark 的 draw_n 上界无关）—— CI 自检断言读这一组。
        #    理由：mark 只含"前端画得出的"，若断言仍读 mark 计数，就等于**用守卫去迁就前端
        #    契约**（哪天前端 K 线根数变了断言跟着变，跨轮不可比）；引擎侧计数回答的是
        #    "引擎有没有产出这一族"，正是 R495 那条断言原本的意图（防整族静默为空）。
        "n_uni_segsig_eng": sum(1 for row in universe.values()
                                if ((row.get("st") or {}).get("segsig_n") or 0) > 0),
        "n_uni_segzs_eng": sum(1 for row in universe.values()
                               if ((row.get("st") or {}).get("segzs_n") or 0) > 0),
        "n_ind_segsig_eng": sum(1 for v in industries.values()
                                if ((v.get("st") or {}).get("segsig_n") or 0) > 0),
        "n_ind_segzs_eng": sum(1 for v in industries.values()
                               if ((v.get("st") or {}).get("segzs_n") or 0) > 0),
        # ② 落盘覆盖度（**观测用**：R496 把六类从"按条数截断"改成"只受 draw_n 约束"，
        #    这组数字回答"改完到底有多少只标带上了标注"）。
        "n_uni_mark_sig": sum(1 for m in marks_out.values() if m.get("sig")),
        "n_uni_mark_line": sum(1 for m in marks_out.values() if m.get("line")),
        "n_uni_mark_bc": sum(1 for m in marks_out.values() if m.get("bc")),
        "n_uni_sig_total": sum(len(m.get("sig") or []) for m in marks_out.values()),
        "n_uni_line_pts_total": sum(len(m.get("line") or []) for m in marks_out.values()),
        "n_ind_mark_sig": sum(1 for m in _ind_marks if m.get("sig")),
        "n_ind_mark_line": sum(1 for m in _ind_marks if m.get("line")),
        # ===== R496b: 「后端产出 ⊆ 前端可绘制」不变量 =====
        # `_off_max` = 该端**全部**族（含中枢/笔折线）的偏移最大值。
        #   个股/ETF 上界 = FE_KLINE_N-1（前端 getStkData 取 660 根）
        #   行业      上界 = IND_KLINE_N-1（前端行业K线就是入库的那 320 根）
        #   越界即"后端发了、前端 xi() 返回 null 静默丢掉" ⇒ CI 硬断言拦（这类损耗无任何报错）。
        "fe_kline_n": FE_KLINE_N,
        "ind_kline_n": IND_KLINE_N,
        "ind_off_max": _off_max_of(_ind_marks),
        "uni_off_max": _off_max_of(list(marks_out.values())),
    }
    out = {"meta": meta,
           "signals": [{"sym": s, **sig} for s, sig in signals],
           "industries": industries,
           # R319: 白名单补 "src"(票级K线源 tx/em/sina) —— 此前漏写导致前端 srcTag(u.src)
           # 恒 undefined, R271 票级源徽标从未生效(R276 注释"已在顶层"系假修复, 后端从未写出)。
           # src 值域见 _src_degraded: tx/em=复权(绿标), sina=新浪裸价(橙标⚠, 除权日假跳空风险)。
           # ⚠️⚠️ R448 血泪: 白名单是**静默剥离**, row 里写对了也照样不落盘 —— 本轮
           #   etf_track/etf_fee/etf_nav 已在 L2078-2083 正确注入, 但**实跑产物里三者全 None**,
           #   而 meta.etf_twin.tracked/fee_n 却是 10(证明上游逻辑没错, 只错在这一行)。
           #   **给 universe row 加任何新字段, 必须同改此处**; 只读代码不实跑抓不到(R319 同款坑)。
           "universe": {s: {k: v for k, v in row.items()
                            if k in ("name", "type", "code", "gate", "gd", "ind", "mcap",
                                     "st", "ff", "lead", "src",
                                     # R448: ETF 官方元数据(前端「细分领域标注 + 同指数选优」)
                                     #   注意 etf_nav 是**净资产规模(亿元)**, 不是单位净值 ——
                                     #   历史命名, 与 R457 的 etf_uav 不可混用。
                                     "etf_track", "etf_fee", "etf_nav",
                                     # R457: ETF 折溢价(单位净值 / 净值日期 / 折溢价% / 对应日期)
                                     "etf_uav", "etf_uavd", "etf_prem", "etf_premd")}
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
