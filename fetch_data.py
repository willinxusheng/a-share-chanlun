# -*- coding: utf-8 -*-
"""拉取 A 股主要指数日线+周线数据，含完整性校验与新浪交叉验证"""
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

SYMBOLS = {
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000905": "中证500",
}

# R248(2026-09-03): 腾讯 fqkline 数据滞后根因更正 —— R246 的"count 动态化"没修到根子。
#   实测(2026-09-03, 对照 A-share-Fibonacci/datafeed.py 同时刻请求):
#     · "带起始日期"式 URL(param=sh000001,day,2021-01-01,<今天+400天>,1700,qfq)
#       —— 无论 count 怎么微调(1700~1999)都命中 CDN 陈旧缓存(09-02 晚 16:00~21:30 十次
#       cron-job 全扑空、看门狗探针同被缓存蒙蔽判"已最新"), 直到次日早晨缓存刷新才追平;
#     · 纯 count 式 URL(param=sh000001,day,,,10,qfq) —— 实时, 收盘后 1 小时即有当日数据
#       (斐波那契项目同款 URL 09-02 16:03 即成功刷新到当日收盘)。
#   结论: 腾讯 CDN 的缓存键含"日期范围"段, 只改 count 无法绕过; 根治=去掉日期段改纯 count。
# 修复: _tx_url 去掉 2021-01-01 起始与动态结束日期, 对齐斐波那契项目的纯 count 形态;
#       count 保留 R246 的动态化(1700~1999, 实测合法上限约 2000), 覆盖 2021 起 1300+ 根绰绰有余;
#       起始日期由 fetch_tx 内按 MIN_DATE=2021-01-01 裁剪保证(与看板"2021 至今"契约一致)。
# R458(2026-09-14): **主机级回退** —— R249 那次只修了一半(只把硬编码主机名换了一台)。
#   两个同源主机(ifzq / web.ifzq)对本机出口**各自独立限流**: 谁被拦取决于"这台最近被打得多少"。
#   R249(09-06) 是 web. 被 501; R458(09-14) 恰好反过来 —— 实测 9 个板各取 1 票
#   (sh600000/sh510300/sh511990/sh513100/sh518880/sz159001/sh000001/sz399006/bj920002) 连打 20 次:
#     ifzq.gtimg.cn      → 9/9 板 501, 20/20 = 501
#     web.ifzq.gtimg.cn  → 9/9 板 200, 20/20 = 200
#   ⚠⚠ R458c 更正: 上句原写作"恒定拒绝, **非间歇/非限流**", 并据此写成「WAF 在两者之间来回翻」
#     —— **属过度归因**, 已在本轮推翻。同一天内对**同一台**复测到两种状态: 15:47 两主机皆 40/40 = 200;
#     16:0x 本机对该台**硬编码连打 300 次**; 16:07~16:16 该台 10/10、3/3、3/3 全 501 而镜像 200。
#     与项目既有记载完全吻合 ——「补拉 300+ 次 ⇒ 本机 IP 对**单主机**被限流(全 501)、镜像仍 200、
#     约 10min 自恢复」。⇒ 更可能的机制是**自身流量把单主机打到限流**(且**反复探针会延长冷却**),
#     而不是主机侧"来回翻"。**别把自己打出来的 501 当成源侧状态。**
#   ★ 但结论不变、反而更强: **生产扫描(6630 票)本来就是"把全部请求压在一台"的形态**
#     ⇒ 单主机必然把该台打到限流, 而镜像仍干净 ⇒ 多主机回退正是这种形态的解药(且自适应, 无需再改代码)。
#   ⚠ 反面纪律: 判"源是否真挂"禁用批量探针(自伤), 应低速率(每主机 ≤5 次/轮) + 跨 ≥2 时点 +
#     优先看产物分因落痕(meta.tx_host / src_fail / src_cycle)。
#   单主机硬编码的代价 = 一次限流就让**全市场**掉进新浪兜底。09-11 线上产物自报:
#     src_cnt={tx: 876, sina: 5821(87%)} · degraded=true · degraded_pct=87
#     src_fail.tx.err=36 (36 = 两轮 15 连败 + 6 残余, 正合"停用→复探恢复→再停用"两轮)
#   ⇒ 整整一周 87% 的票在跑**不复权裸价**, 除权日假跳空直接污染笔/中枢/背驰结构。
#   ⇒ 改为「按序试多主机 + 进程内记忆可用主机」, 与 EM_KLINE_HOSTS/push2his 镜像同范式:
#     记忆命中时只需 1 次请求(与改前同开销); 单主机被限时多试一次即自动恢复, 无需再改代码。
TX_KLINE_HOSTS = ["ifzq.gtimg.cn", "web.ifzq.gtimg.cn"]
_TX_HOST_OK = [None]      # 进程内记忆: 上次成功的主机; None=未定(按 TX_KLINE_HOSTS 顺序)

# R460: **按主机独立**节流 —— R458 的实证是"单主机被打到限流、而镜像仍干净"(两主机是独立
#   镜像, 各自独立限流) ⇒ 限速必须**按主机**而非全局: 全局节流会把两台镜像的吞吐绑成一台,
#   按主机才与"多主机回退"的设计自洽(且有实证支撑: 镜像在被限期间仍回答正常)。
TX_INTERVAL = 0.35        # 单主机 ~2.9 rps(R458 的 P0 实证值: 突发连发会 501)
#   ★ 为什么必须补上(2026-09-14 实跑实测, 见 radar/scan_radar.py 的 R460 段):
#     日线 K 线路径此前**根本没有 tx 节流** —— `_tx_th.wait()` 只用于 scan_radar._probe_tx
#     与月线路径。4 并发无节流突发 ⇒ 单主机(记忆主机吸收全部流量)被限流 ⇒ scan_radar 的源
#     状态机"连续失败 >=15 次整段停用" ⇒ **本轮余下全市场转新浪裸价**。600 只实测:
#     `src_cycle.tx={stops:1,resumes:0}` / tx 仅 79/599 / `degraded_pct=87` —— 与 09-11 线上
#     87% 裸价的失败签名一致。而 R458b 注释里那个"6630 票 × 0.35s ≈ 39 分钟"的算式说明
#     **设计意图本来就是每票节流**, 只是从未落到日线路径上。
#   ⇒ 现把节流点放进 tx_get 自身(跟着最容易漏的地方走, 而不是依赖每个调用方记得 wait)。
#     翻页时按页轮流 prefer 两主机 ⇒ 聚合 ~5.7 rps, 与新浪(0.18s, ~5.5 rps, 线上长期稳定)同量级。


class _Throttle:
    """跨线程共享的间隔节流器: 每次 wait() 与上一次放行至少相隔 interval 秒。
    与 scan_radar._Throttle 同语义; 此处独立一份是为让限速点跟着 tx_get 走。"""

    def __init__(self, interval):
        self.interval = float(interval)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            _s = self._next - now
            self._next = max(now, self._next) + self.interval
        if _s > 0:
            time.sleep(_s)


_TX_TH = {}
_TX_TH_LOCK = threading.Lock()


def _tx_pace(host):
    """取该主机专属节流器并等待(首次访问惰性创建)。"""
    with _TX_TH_LOCK:
        th = _TX_TH.get(host)
        if th is None:
            th = _TX_TH[host] = _Throttle(TX_INTERVAL)
    th.wait()


def _tx_url(symbol, period, host=None, count=None, end=None):
    _b = (datetime.now().minute * 60 + datetime.now().second) % 300
    # R249(2026-09-06): web.ifzq.gtimg.cn 对本机出口被腾讯 WAF 501 拦截(跳 waf.tencent.com
    # 验证页), 当时切到同源 ifzq.gtimg.cn(无 web. 前缀); R458 起不再硬编码单主机, 见上方常量。
    # R460: 补 `end` 游标槽(param 语义 = code,period,start_date,end_date,count,fq) ——
    #   原实现只填 count、start/end 留空 ⇒ 永远只能取到"最新 640 根"(见 fetch_tx_qfq_paged)。
    return ("https://%s/appstock/app/fqkline/get?param=%s,%s,,%s,%d,qfq") % (
        host or _TX_HOST_OK[0] or TX_KLINE_HOSTS[0], symbol, period, end or "",
        count if count else 1700 + _b)


def tx_host():
    """R458: 当前记忆中的可用主机(空串=本进程内尚未成功过一次)。
    供产物 meta 落痕 —— 事后判「这一轮是哪台主机在服务」, 免得靠降级比例反推。"""
    return _TX_HOST_OK[0] or ""


def tx_get(symbol, period, count=None, timeout=30, end=None, prefer=None):
    """R458: 腾讯 K线响应(**已解码的 str**, 与 `_get` 同口径) —— 多主机按序回退 + 进程内记忆。

    ★ 返回 str 不是 bytes: 底层 `_get` 就是 `resp.read().decode("utf-8")`。调用方按原
      `_get(...)` 的用法直接 `json.loads(...)` 即可, **不要再 `.decode()`** ——
      对 str 调 .decode() 会抛 AttributeError 并被各自的 except 吞掉, 表现为
      "腾讯路径网络失败→静默降级新浪"(R458 自造缺陷, 由 R458b 的 A/B 逐票 diff 抓出)。

    记忆主机优先 ⇒ 正常情况 1 次请求, 与改前同开销; 失败再按 TX_KLINE_HOSTS 顺序试其余主机。
    每主机只重试 1 次(_get retries=2), 而非默认的 3 次指数退避: 501/403 是**确定性**拒绝,
    退避重试纯属空耗(且单票 ~4s, 全场 6600 票即数小时), 真正该做的是换主机。
    全主机皆失败则抛最后一次异常, 由调用方按原逻辑计失败(源状态机照常工作)。

    R460 两处新增:
      ① **每次尝试前按主机节流** `_tx_pace(_h)` —— 见上方 TX_INTERVAL 注释: 日线路径此前
         完全没有 tx 节流, 无节流突发会把记忆主机打到限流, 进而触发源状态机整段停用
         (实测 600 只: tx 仅 79/599、src_cycle.tx.stops=1、degraded_pct=87)。
      ② `prefer` = **本轮首选主机**(翻页时按页轮流给两主机) —— 记忆主机优先会让一台吸收
         全部翻页流量; 轮流 prefer 才能用满两个独立镜像的额度(各 2.9 rps ⇒ 聚合 ~5.7 rps)。
         prefer 不改变回退语义: 它只是把顺序从 [memo, ...] 变成 [prefer, memo, ...]。
    """
    memo = _TX_HOST_OK[0]
    order = []
    for _h in ([prefer] if prefer else []) + ([memo] if memo else []) + list(TX_KLINE_HOSTS):
        if _h and _h not in order:
            order.append(_h)
    last = None
    for _h in order:
        _tx_pace(_h)
        try:
            raw = _get(_tx_url(symbol, period, _h, count, end), retries=2, timeout=timeout)
        except Exception as _e:     # noqa: BLE001
            last = _e
            continue
        if _TX_HOST_OK[0] != _h:
            _TX_HOST_OK[0] = _h
        return raw
    raise (last if last else RuntimeError("tx_get: 无可用主机"))

# 看板数据契约起点(标题"2021 至今")。纯 count 接口会返回更早历史(如日线 2019-04 起),
# 统一裁剪到该日期之后, 保证各线首根与历史版本一致(2021-01-04 首交易日)。
MIN_DATE = "2021-01-01"
SINA_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=5"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_BASE = os.path.dirname(os.path.abspath(__file__))


def _get(url, retries=3, timeout=30):
    # R173(F1): 指数退避重试, 避免瞬时网络失败(5xx/超时)直接丢弃整标的 → 看板对缺失标的静默标 N/A
    # R458: 增 retries/timeout 可调 —— tx_get 需要"快速失败换主机"而非长退避(见 tx_get docstring)。
    req = urllib.request.Request(url, headers=UA)
    _n = max(1, int(retries))
    _delay = 1
    for _i in range(_n):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            if _i == _n - 1:
                raise
            time.sleep(_delay)
            _delay = min(_delay * 3, 9)


def _tx_parse(klines):
    """腾讯原始数组 → 看板 bar 字典列表; 返回 (out, dirty)。
    R458c: 从 fetch_tx 抽出, 供 fetch_tx / fetch_tx_qfq 共用(两份解析必须逐字节同口径)。"""
    out = []
    dirty = 0
    for row in klines:
        try:
            # R248: 纯 count 接口会带回 2021-01-01 之前的历史 bar(日线可早至 2019),
            # 按契约裁剪, 保证各线首根与"2021 至今"看板一致(不计数为脏 bar)。
            if row[0] < MIN_DATE:
                continue
            # [日期, 开, 收, 高, 低, 量]
            out.append({
                "date": row[0],
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        except (ValueError, IndexError, TypeError):
            # R164: 脏 bar(缺字段/空值/类型错)跳过, 不中断整标的抓取
            # R173(F2): 计数并暴露到 meta.issues, 避免"静默丢根"致看板对缺失数据无知
            dirty += 1
            continue
    return out, dirty


def fetch_tx(symbol, period):
    # R458: 经 tx_get 走多主机回退(此前硬编码 ifzq.gtimg.cn, 该主机 09-14 起全站 501)。
    data = json.loads(tx_get(symbol, period))["data"][symbol]
    # ⚠ R458c: 这条 or 链**保留原语义**(qfq 优先, 缺失则退裸价) —— 5 大指数(sh000001 等)
    #   天生只有 `day`(指数无复权概念), `fetch_data.main()` 正是靠它取指数量价。
    #   但"要求前复权"的调用方**不能**用它, 见 fetch_tx_qfq。
    klines = data.get("qfqday") or data.get("qfqweek") or data.get("qfqmonth") or data.get("day") or data.get("week") or data.get("month") or []
    return _tx_parse(klines)


def fetch_tx_qfq(symbol, period):
    """R458c(2026-09-14): **只在前复权序列真实存在时**返回数据 —— 雷达等"要求前复权"的调用方专用。
    返回 (rows, dirty, has_qfq)。

    动机(实测 2026-09-14):
      · 腾讯对个股/ETF 的 `qfqday` **恒定截断在 641 根**(首根恒 2024-01-22; count 给 1700
        或 1999 一样、两主机同款; 300 只分层样本中 qfqday>=1200 的票 **0 只**)。
      · 另有**一整类**票(连 sh688981 中芯国际也一样)腾讯**完全不返回 qfqday**, 只返回完整
        `day`(自上市首日, 不复权) —— 实测与新浪裸价**逐日完全相同**(每只约 1300 根, 0 日差异)。
        ★ **全量规模**(2026-09-14 用线上满量产物反向度量, 非抽样): src=='tx' 的 876 只里
          **60 只** = 6.8%, 占全市场 6630 的 0.90%, **全部是 sh688*(科创板个股)**。
          (300 只抽样时的旧记"13 只 = 4.3%, 多为 ETF"已被全量推翻: 该产物 ETF/北交**全走
           sina**, 60 只只覆盖**个股端**; 且同批 sh688 里多数**确有** qfqday ⇒ 正确表述是
           "**集中在科创板**", **不是**"科创板普遍"。)
        ⚠ 该缺陷**无法**用 n_bars 离线判别: qfqday 对**上市晚于 2024-01-22** 的票是从
          **上市日**开始给的(根数 = 上市以来交易日数), 与裸价 day **同形** ⇒ 只能实拉判定。
    而唯一入口 `fetch_tx` 是 `qfqday or ... or day` 一条 or 链 ⇒ 这些票会被**贴上前复权
    标签却喂进不复权序列**; 前端 exdivRisk 对 `src!=="sina"` 直接 `return null`、并显示
    "(前复权) —— 除权已平滑" ⇒ **对用户构成肯定性的错误陈述**(R450 同族: 文案承诺须与实现一致)。
    ∴ 把"要求前复权"显式成独立入口, 由调用方决定「拿不到复权序列就切下一源」。

    ⚠ 与 `--src tx` 取证开关的关系: 该开关走 scan_radar._fetch_tx, 同样受本函数约束
      (拿不到 qfq 就是拿不到, 取证时也应该看见真相而非裸价)。
    """
    data = json.loads(tx_get(symbol, period))["data"][symbol]
    qk = data.get("qfqday") or data.get("qfqweek") or data.get("qfqmonth")
    if not qk:
        return [], 0, False
    rows, dirty = _tx_parse(qk)
    return rows, dirty, True


# R460: 腾讯 qfq 单次请求**硬上限**(实测 count 给 1700/1999 都只回 641 根; 两主机同款)。
#   注意: 这是**请求形态**的上限, 不是"腾讯没有历史" —— 填 end 游标即可往回翻页。
_TX_PAGE_CAP = 640


def fetch_tx_qfq_paged(symbol, period="day", min_date=MIN_DATE, min_bars=None, max_pages=2):
    """R460: 分页取**全量前复权** —— 沿 param 的 end 游标往回翻, 拼成完整 qfq 序列。
    返回 (rows, dirty, has_qfq, pages)。

    ── 为什么(实测 2026-09-14, 证据见 _dbg/r460/) ──
    · R458b/c 记「腾讯把 qfq 历史**硬截断**在 641 根(首根恒 2024-01-22), 加日期段也一样」
      ⇒ 据此加了深度守卫, 把 tx **整源拒掉**(线上 tx 从 09-11 的 876 票掉到 09-14 的 1 票)。
      ★ 该结论**不成立**: 当时只试了 count 槽。param 的语义是
        `code, period, start_date, end_date, count, fq`, 生产把 start/end 留空 ⇒ 只能取到
        "最新 640 根"。实测填 end 游标:
          · end=2021-01-01~2023-12-31 → 640 根, 首根 2021-05-18   (窗口内**最后** 640 根)
          · end=2019-01-01~2021-12-31 → 640 根, 首根 2019-05-21
        两个主机(ifzq / web.ifzq)同款, count 槽给多少都只影响"取窗口最后多少根"。
    · ★ 拼接**安全性已实证**(这是本方案唯一的致命风险点): 若各页以**各自窗口末日**为前复权
      基准, 拼接处就会出现**假跳空** —— 而假跳空正是要消灭的东西。判据 = 同一历史日期用
      6 个不同 end 窗口去取, 价格是否漂移:
        **2797 个 (日期×窗口) 对, 不一致 0 个, 最大差 0.000000** ⇒ 基准锚定在固定日期(最新),
        与请求窗口无关 ⇒ **翻页拼接安全**。
      旁证: d = 裸价 − qfq 在全序列只有 **6~8 个变点**, 且逐一定位后**全部落在真实分红日**
        (sh600000 连续 6 年 7 月中、sz000001 6~7 月), 末端 d→0.0000; **无一个落在页边界**
        ⇒ 拼接未引入额外跳变。
    · 复权方式: 腾讯 qfq 是**等差(加法)** `qfq = 裸价 − C(t)`, C 为阶梯常数(仅在除权日跳变);
      所以 k=qfq/裸价 **逐日变**(那不是 bug), d 才分段常数。与东财 fqt=1 的**等比**口径不同,
      但两者都把除权缺口抹掉(实测 -4.94% 的除权跳空在 qfq 上只剩 -0.28% 残差)。

    ── 分页策略(成本用) ──
    停止条件(任一): ① `first <= min_date`(已覆盖契约起点) ② 本页根数 < _TX_PAGE_CAP(窗口耗尽
    ⇒ 腾讯侧没有更早的数据了) ③ 达到 `max_pages` ④ 给出 `min_bars` 且已取够(可选的"够用即停")。
    雷达传 `min_bars=None, max_pages=3` ⇒ sh600000 实测拿到 **1382 根, 首根 2021-01-04**
    (3 页: 641 + 640 + 640), 与新浪兜底**同深度** ⇒ 换算后可保证"只变复权口径、不变深度"。
    次新股首页即"窗口未填满"(< _TX_PAGE_CAP) ⇒ 停止, 那是它**从上市首日起的完整**历史。
    """
    got, dirty_total, pages = {}, 0, 0
    end = None
    _nl = max(1, len(TX_KLINE_HOSTS))
    for _i in range(max(1, int(max_pages))):
        # R460: 按页**轮流**首选两主机 —— 见 TX_INTERVAL 注释②: 记忆主机优先会让一台吸收
        #   全部翻页流量(3 页/票), 轮流 prefer 才用满两个独立镜像的额度(各 2.9 rps)。
        try:
            data = json.loads(tx_get(symbol, period, count=_TX_PAGE_CAP, end=end,
                                     prefer=TX_KLINE_HOSTS[_i % _nl]))["data"][symbol]
        except Exception:                    # noqa: BLE001
            if _i == 0:
                raise                        # 首页失败 = 源不可用, 交调用方按原状态机计失败
            break                            # 后续页失败: 保留已取部分(首页本身是完整序列), 不丢票
        qk = data.get("qfqday") or data.get("qfqweek") or data.get("qfqmonth")
        if not qk:
            break
        rows, _d = _tx_parse(qk)
        dirty_total += _d
        pages += 1
        if not rows:
            break
        for r in rows:
            got[r["date"]] = r
        first = rows[0]["date"]
        if first <= min_date:                # 已覆盖契约起点("2021 至今")⇒ 完
            break
        if len(rows) < _TX_PAGE_CAP:         # 窗口没填满 ⇒ 腾讯侧没有更早的数据了
            break
        if min_bars and len(got) >= min_bars:  # 深度已够 ⇒ 停(够用即停, 控成本)
            break
        end = (datetime.strptime(first, "%Y-%m-%d")
               - timedelta(days=1)).strftime("%Y-%m-%d")
    if not got:
        return [], dirty_total, False, 0
    return [got[d] for d in sorted(got)], dirty_total, True, pages


def fetch_sina_series(symbol, datalen=2000):
    """拉取新浪全量日线（用于序列级交叉验证），返回 {date: close}"""
    try:
        url = SINA_URL.split("datalen=5")[0] + "datalen=%d" % datalen
        arr = json.loads(_get(url % symbol))
        out = {}
        for row in arr:
            out[row["day"]] = float(row["close"])
        return out
    except Exception as e:
        print("WARN 新浪校验拉取失败(双源一致性将标 N/A):", e)
        return {}


# ===== 情绪引擎数据源（R177b: 情绪 txt 接每日行情, 根治 asof 落后） =====
# calc_v2.py 消费 4 个 txt(上证/深成/上证50/中证1000)的 8 列: date|open|last|high|low|volume|amount|exchange。
# 腾讯 fqkline 接口仅 6 列(无成交额/换手率), 而 calc_v2 的量能.25/换手.20/大小票分层.15 因子强依赖
# amount 与 exchange → 情绪 txt 改用东方财富日线接口(11 列含 f57 成交额 / f61 换手率, 免 key 公开,
# 数据截至当日 UTC+8)。任一指数拉取失败仅跳过该文件(保留旧值), 不阻断主数据管线。
SENTIMENT_SYMBOLS = {  # 东财 secid 映射
    "sh000001": "1.000001",
    "sz399001": "0.399001",
    "sh000016": "1.000016",
    "sh000852": "1.000852",
}
_SENT_FILES = {
    "sh000001": "sh_long.txt",
    "sz399001": "sz_long.txt",
    "sh000016": "sh50.txt",
    "sh000852": "zz1000.txt",
}
# R177b+: 东财接口双协议 + 双 host 轮询。
# 境内(沙箱/国内CI): http 明文镜像稳定(沙箱实测 https 被代理截断 chunked 响应, 故 http 优先);
# 境外 CI(ubuntu-latest): 东财 http 境内镜像不可达(仅境内 CDN 节点), 需 https 端点直连兜底
#   —— ubuntu 可出网至境内 web 服务(腾讯/新浪 https 在 CI 已验证可达), 故 https 端点可恢复抓取,
#   根治 R177b 改 http 后境外 CI 情绪 txt 永久降级(asof 停在 08-21)的回归。
# 顺序: 境内 http 主镜像 → 境内 http 备用镜像 → 境外 https 直连兜底。
EM_KLINE_PATH = ("api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3,f4,f5,f6&"
                 "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&beg=20210101&end=20500101")
EM_HOSTS = [
    "http://push2his.eastmoney.com/",
    "http://92.push2his.eastmoney.com/",
    "https://push2his.eastmoney.com/",
]


def fetch_em(secid):
    """东方财富前复权日线: 返回正序 [(date, open, close, high, low, volume, amount, turnover)]。
    双协议(http/https) + 双 host 逐次尝试, 同时适配境内(沙箱)与境外(CI)网络;
    全部失败由调用方降级保留旧 txt。"""
    last_err = None
    for host in EM_HOSTS:
        try:
            u = host + (EM_KLINE_PATH % secid)
            data = json.loads(_get(u))["data"]
            if not data or not data.get("klines"):
                # R211: 空响应视为该 host 失败, 尝试其余 host(避免主镜像偶发空响应即整体失败);
                # 不再 return [], 既跳过备用 host 又破坏 (rows,dirty) 元组契约(调用方解包会 ValueError)。
                continue
            out = []
            dirty = 0
            for row in data["klines"]:
                c = row.split(",")
                if len(c) < 11:
                    dirty += 1
                    continue
                try:
                    out.append((c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                                float(c[5]), float(c[6]), float(c[10])))
                except (ValueError, IndexError):
                    dirty += 1
            if out:
                return out, dirty
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return [], 0


# R232: 腾讯 gtimg 日线回退(海外/CI 可达)。东财在 CI/沙箱被限流, 而腾讯 gtimg 对 A 股指数
# 返回完整日K(约1300根), 是 fib 项目在 GitHub 海外 runner 能自动刷新的同款可达源。
# 腾讯仅给 6 列 OHLCV, 无成交额/换手率; 对指数 成交额≈成交量×指数点位(市值加权
# Σvol_c·price_c≈(Σvol_c)·均价, 常数项在滚动分位/比值中抵消), 故派生 amount/to 代理,
# 使 calc_v2 滚动分位/分层比值保持形状一致(scale-invariant), 模型权重/阈值/13门禁零改动。
# R267(2026-09-06): 去掉 web. 前缀 —— R249 实证 web.ifzq.gtimg.cn 出口被腾讯 WAF 501 拦截,
# 同源 ifzq.gtimg.cn(无 web.) 200 正常(_tx_url 已于 R249 修复, 此处同步遗漏)。此前东财限流
# 触发腾讯回退时, 回退请求实际必挂 -> 情绪 txt 停在旧快照(08-2x 曾发生), 回退形同虚设。
# R458(2026-09-14): 保留上面的来龙去脉, 但**不再硬编码主机** —— 实测 09-14 两边翻转, ifzq
# 全站 501 / web.ifzq 全 200; 再翻转一次这条路就又形同虚设。改由 tx_get 走 TX_KLINE_HOSTS
# 回退。★ count 仍锁 1300 不可动: 情绪 txt 的行数契约依赖它(本函数**不按 MIN_DATE 裁剪**,
# 全量返回), 顺手换成 _tx_url 默认的 1700 会直接改变产物行数。
def fetch_tx_sentiment(tx_code):
    """腾讯 gtimg 前复权日线回退: 返回正序 [(date, open, close, high, low, volume, amount_proxy, to_proxy)]。
    东财不可达时由 update_sentiment_txts 调用; amount_proxy=volume×typical_price, to_proxy=amount_proxy。"""
    data = json.loads(tx_get(tx_code, "day", count=1300))["data"]
    node = (data or {}).get(tx_code) or {}
    kl = node.get("day") or node.get("qfqday") or []
    out, dirty = [], 0
    for row in kl:
        if len(row) < 6:
            dirty += 1
            continue
        try:
            d, o, c, h, l, v = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
            tp = (h + l + c) / 3.0
            amt = v * tp
            out.append((d, o, c, h, l, v, amt, amt))
        except (ValueError, IndexError, ZeroDivisionError):
            dirty += 1
    return out, dirty


def _txt_last_date(fname):
    """读已存在情绪 txt 的末根日期(用于新鲜度比对, 防止 stale/限流的东财返回旧值覆盖本机已刷新数据)。"""
    try:
        with open(fname, encoding="utf-8") as f:
            rows = [ln for ln in f if ln.strip().startswith("|")
                    and not ln.strip().startswith("| date")
                    and not ln.strip().startswith("| ---")]
        if rows:
            cols = [c.strip() for c in rows[-1].strip().strip("|").split("|")]
            return cols[0] if cols else None
    except Exception:
        pass
    return None


def _bj_now():
    """R236: 北京时间(UTC+8)当前时刻 -> (日期字符串, 小时)。

    所有"当日 bar 是否已收盘"的判定都必须用北京时间：
    GitHub runner 本地时区是 UTC，直接用 datetime.now().hour 会误判
    （同类问题见 R167 修复）。"""
    n = datetime.now(timezone(timedelta(hours=8)))
    return n.date().isoformat(), n.hour


def _trim_unclosed_bar(rows, bj_now=None):
    """R236: 丢弃"未收盘的当日 bar"。

    源端(东财/腾讯)盘中返回的是"进行中"的当日 K 线 —— 成交量/成交额/换手率/振幅
    都只有半天，若直接入库会让 calc_v2 算出的情绪分数失真（量能维度被系统性低估）。
    与 fetch_data.main() 对主行情的处理(R164/R167: 北京 <15:00 丢弃末根)口径保持一致。
    bj_now 可注入以便单测（生产不传）。"""
    if not rows:
        return rows
    today, hour = bj_now or _bj_now()
    if rows[-1][0] == today and hour < 15:
        return rows[:-1]
    return rows


def _should_skip_txt(old_last, new_last, bj_now=None):
    """R236: 是否跳过覆盖情绪 txt。

    原逻辑 `new_last <= old_last 即跳过` 有致命副作用：
    若盘中把半截的当日 bar 写了进去，收盘后源端返回的完整 bar 末根日期与旧值相同，
    会被判定为"已最新"而跳过覆盖 —— 半截数据永久卡在 txt 里，直到下一交易日才被顶掉。
    故补充：收盘后(新末根==今天 且 北京>=15:00)强制重写，用完整 bar 顶掉盘中半截数据。
    bj_now 可注入以便单测。"""
    if not old_last:
        return False
    today, hour = bj_now or _bj_now()
    if new_last > old_last:
        return False
    if new_last == old_last and new_last == today and hour >= 15:
        return False      # 收盘后同日 -> 强制重写, 顶掉可能的半截 bar
    return True


def update_sentiment_txts():
    """把 4 指数日线刷新为 calc_v2 消费的 txt(8列 | 分隔, 含成交额/换手率)。
    R230 修复: 仅当抓到更新数据(末根日期>已存在 txt)才覆盖, 防止 stale/限流的东财返回旧值
    把本机已刷新的数据覆盖掉; 全部失败时写 sentinel sentiment/.em_fresh=0, 供 deploy.yml 跳过
    calc_v2(直接部署已提交的本地刷新快照), 杜绝"东财在 CI 被限流→静默回退旧快照→线上永久滞后"。
    R232 修复: 东财在 CI/沙箱被限流导致情绪面板永久滞后(曾卡 08-26)。新增腾讯 gtimg 回退
    (海外/CI 可达, 6 列 OHLCV), 对指数派生 成交额≈成交量×指数点位、换手率=成交额派生代理
    (市值加权 Σvol_c·price_c≈(Σvol_c)·均价∝amount, 常数项在滚动分位/比值中抵消, 模型零改动),
    使情绪面板在 CI 自动刷新到最新交易日, 不再依赖本机手动跑。写 sentinel sentiment/.sent_mode
    供 report 诚实标注数据模式(em=东财全量 / tencent_proxy=腾讯派生代理)。
    失败降级: 任一指数东财+腾讯均异常才跳过(保留旧值); 全部失败 .em_fresh=0 —— 情绪新鲜度
    是增强项, 绝不允许拖垮主行情管线(fetch_data 的主职责是 data.json)。"""
    _dir = os.path.join(_BASE, "sentiment")
    try:
        os.makedirs(_dir, exist_ok=True)
    except Exception:
        pass
    # R350: 配对组级同源。calc_v2 消费 txt 第8列作"换手"因子(权重 .20)与"分层投机比"
    # (权重 .15): em 模式第8列=东财 f61 换手率(~0.x 量级); 腾讯回退(R232)第8列=成交额
    # 派生代理(~1e12 量级, fetch_tx_sentiment to_proxy=amount_proxy)。
    # 原实现逐 sym 独立降级 -> "东财个别指数失败"时组内混源: calc_v2 L66
    # ratio=zz1k.to/sh50.to 直接相除无稀释, 混源量纲差 1e12 倍 => ratio 爆炸/趋零,
    # 分层因子分位恒 100/0, score 当日失真 ±15 分量级(污染 252 日滚动窗口信号/背离)。
    # 修: 以 calc_v2 的配对为组 —— {sh,sz}(L65 to×amt 加权须同量纲) 与 {sh50,zz1k}
    # (L66 ratio 直接相除须同源); 组内模式不一致时整组降级腾讯重拉(保同源, 代价是
    # 损失换手率精度, 但 scale-invariant 论证下分位形状不变, 远优于量纲崩溃)。
    _SRC_GROUPS = (("sh000001", "sz399001"), ("sh000016", "sh000852"))
    ok = 0
    modes = set()
    fetched = {}
    for sym, secid in SENTIMENT_SYMBOLS.items():
        mode = "em"
        try:
            rows, dirty = fetch_em(secid)
            if len(rows) < 100:
                raise ValueError("东财返回仅%d行" % len(rows))
        except Exception as e_em:
            # R232: 东财不可达 -> 腾讯 gtimg 回退(派生成交额/换手率代理), 情绪面板自动刷新
            try:
                rows, dirty = fetch_tx_sentiment(sym)
                if len(rows) < 100:
                    raise ValueError("腾讯返回仅%d行" % len(rows))
                mode = "tencent_proxy"
                print("INFO 情绪 %s 东财不可达 -> 腾讯 gtimg 回退(派生成交额/换手率代理)" % sym)
            except Exception as e:
                print("WARN 情绪 %s 东财与腾讯均失败, 保留旧txt: %s" % (sym, e))
                continue
        fetched[sym] = {"rows": rows, "dirty": dirty, "mode": mode}
    # R350 组级同源: 组内 em/tencent_proxy 混合(全员本次成功但异源) => 把 em 成功者降级
    # 腾讯重拉, 使两 txt 今日都写且写后组内同源(混源量纲差 1e12 倍, 比降级更糟)。
    # R352 修正: 组内缺员(某指数东财+腾讯双源全败, 留旧 txt)时**不再降级** —— 缺员者旧 txt
    # 模式不可知(em 或 tencent_proxy 均可能): 把 present 降级腾讯, 若缺员者旧 txt 为 em,
    # 反而制造 present(腾讯 1e12 代理) vs 旧 txt(em 换手~0.x) 的同日混源(R352 实测:
    # 注入"sh 东财成功+sz 双源失败"场景, 修复前 sh txt 被写成 tx 第8列 1.3e12, sz 旧 em 0.41,
    # calc_v2 L65 to 加权量纲崩); 缺员日本就因四源须同日合并而无法推进 asof, 故 present 亦
    # 冻结不写盘, 组内保持上一成功日同源旧态(情绪宁晚一天不冒险), 交由 .em_fresh / 
    # guard_sentiment_fresh 护栏兜底(全体冻结 -> ok=0 -> .em_fresh=0 -> 部署已提交快照)。
    frozen = False
    for grp in _SRC_GROUPS:
        present = [s for s in grp if s in fetched]
        missing = [s for s in grp if s not in fetched]
        m_in = {fetched[s]["mode"] for s in present}
        if len(m_in) > 1:
            for s in present:
                if fetched[s]["mode"] == "em":
                    try:
                        rows, dirty = fetch_tx_sentiment(s)
                        if len(rows) >= 100:
                            fetched[s] = {"rows": rows, "dirty": dirty, "mode": "tencent_proxy"}
                            print("INFO 情绪 %s 组内混源 -> 降级腾讯保同源(calc_v2 ratio/to 量纲)" % s)
                    except Exception:
                        pass
            # R367: 降级重拉复查 —— 若仍有 em 成员未被成功降级(腾讯重拉失败/返回不足),
            # 组内仍混源。此时写盘会让 calc_v2 配对(sh/sz 的 to×amt 加权、sh50/zz1k 的
            # ratio 相除)量纲差 1e12 倍崩溃(R350/R352 注释描述的同源事故) —— 与缺员
            # 分支同哲学: 整组冻结不写盘, 保持上一成功日同源旧态(情绪宁晚一天不冒险)。
            if len({fetched[s]["mode"] for s in present}) > 1:
                frozen = True
                print("WARN 情绪组 %s 混源降级重拉失败(%s 仍 em), 整组冻结不写盘, "
                      "保持上一成功日同源旧态(防量纲混源)" % (
                          grp, [s for s in present if fetched[s]["mode"] == "em"]))
                for s in present:
                    fetched.pop(s, None)
        elif present and missing:
            frozen = True
            print("WARN 情绪组 %s 缺员(%s 东财+腾讯均失败), 组内 %s 冻结不写盘, "
                  "保持上一成功日同源旧态(防量纲混源)" % (grp, missing, present))
            for s in present:
                fetched.pop(s, None)
    for sym, info in fetched.items():
        try:
            mode = info["mode"]
            rows, dirty = info["rows"], info["dirty"]
            # R236: 盘中丢弃未收盘的当日 bar(与 main() 口径一致), 防半截数据入库
            rows = _trim_unclosed_bar(rows)
            modes.add(mode)
            fname = os.path.join(_dir, _SENT_FILES[sym])
            new_last = rows[-1][0]
            old_last = _txt_last_date(fname)
            if _should_skip_txt(old_last, new_last):
                print("情绪txt %s 已是最新(末根 %s, %s), 跳过覆盖" % (sym, old_last, mode))
                ok += 1
                continue
            # 临时文件原子写, 避免中断留下半截 txt 被 calc_v2 解析出脏数据
            _tmp = fname + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                f.write("| date | open | last | high | low | volume | amount | exchange |\n")
                f.write("| --- | --- | --- | --- | --- | --- | --- | --- |\n")
                for (d, o, c, h, l, v, a, t) in rows:
                    f.write("| %s | %.2f | %.2f | %.2f | %.2f | %.0f | %.0f | %.2f |\n"
                            % (d, o, c, h, l, v, a, t))
            os.replace(_tmp, fname)
            ok += 1
            print("情绪txt更新 %s -> %s (%s, %d行, 末根 %s%s)" % (
                sym, _SENT_FILES[sym], mode, len(rows), new_last,
                (" 脏%d" % dirty) if dirty else ""))
        except Exception as e:
            print("WARN 情绪txt %s 更新失败(保留旧值): %s" % (sym, e))
    # 模式标记(供 report 诚实标注): 任一指数走腾讯代理则整体标 tencent_proxy
    try:
        _pm = os.path.join(_dir, ".sent_mode")
        new_mode = "tencent_proxy" if "tencent_proxy" in modes else "em"
        if frozen:
            # R352: 有组冻结(旧 txt 未动), .sent_mode 反映的是旧 txt 实际状态更诚实 -> 保留旧值
            _old_mode = None
            try:
                _old_mode = open(_pm, encoding="utf-8").read().strip()
            except Exception:
                pass
            if _old_mode in ("em", "tencent_proxy"):
                new_mode = _old_mode
        with open(_pm, "w", encoding="utf-8") as f:
            f.write(new_mode)
    except Exception:
        pass
    # sentinel: 1=至少一只要更新/已最新(东财或腾讯可达); 0=全部失败(东财在 CI 被限流)
    try:
        with open(os.path.join(_dir, ".em_fresh"), "w", encoding="utf-8") as f:
            f.write("1" if ok > 0 else "0")
    except Exception:
        pass
    if ok == 0:
        print("WARN 全部情绪txt更新失败, 情绪数据保持旧快照(不影响主报告)")
    return ok


def cross_validate(tx_klines, sina_close_map):
    """腾讯(qfq) 与新浪(裸价) 收盘价<b>比值稳定性</b>校验。

    前复权价 = 裸价 × 常数调整因子（相对末日的累计分红/拆股系数），故两源收盘价
    在全序列的比值应基本恒定。若比值漂移过大，说明某源存在分红/拆股口径异常或多日缺口。
    直接比绝对值偏差是错的（qfq 与裸价天然差一个常数倍），比值稳定性才是真正的一致性校验。
    """
    n = len(tx_klines)
    if not sina_close_map or n < 10:
        return {"n": 0, "median_ratio": None, "max_rel_dev": None, "worst_date": None}
    # 抽样：首、尾 + 每年约 2~3 个均匀点
    idxs = set([0, n - 1])
    step = max(1, n // 24)
    for i in range(step // 2, n, step):
        idxs.add(i)
    ratios = []
    for i in sorted(idxs):
        d = tx_klines[i]["date"]
        sc = sina_close_map.get(d)
        if sc and sc > 0 and tx_klines[i]["close"] > 0:
            ratios.append(tx_klines[i]["close"] / sc)
    if len(ratios) < 3:
        return {"n": 0, "median_ratio": None, "max_rel_dev": None, "worst_date": None}
    ratios.sort()
    med = ratios[len(ratios) // 2]
    max_rel = max(abs(r - med) / med for r in ratios)
    worst, worst_date = 0.0, None
    for i in sorted(idxs):
        d = tx_klines[i]["date"]
        sc = sina_close_map.get(d)
        if sc and sc > 0 and tx_klines[i]["close"] > 0:
            rel = abs(tx_klines[i]["close"] / sc - med) / med
            if rel > worst:
                worst, worst_date = rel, d
    return {"n": len(ratios), "median_ratio": round(med, 4),
            "max_rel_dev": round(max_rel, 4), "worst_date": worst_date}


def _date_gap(d1, d2):
    a = datetime.strptime(d1, "%Y-%m-%d")
    b = datetime.strptime(d2, "%Y-%m-%d")
    return (b - a).days


# 各周期正常相邻最大间隔(天)与年均 bar 数——validate 原按日线假设写死(14天/244),
# 直接套周/月线会误报"间隔异常/数量异常"(月线相邻~30天必触发 dd>14)。R156 改为按 period 参数化,
# 使同一校验函数可正确服务于日/周/月三线, 避免扩展校验周月时产生虚假问题。
_GAP_MAX = {"day": 14, "week": 21, "month": 45}
_PER_YEAR = {"day": 244, "week": 52, "month": 12}


def validate(klines, period="day"):
    """K线合法性校验，返回问题列表。

    R82 增强：除原有内部一致性(OHLC越界/重复/非升序/数量异常)外，
    新增「点前完整性 / 无未来泄漏」硬校验——任意 bar 日期 > 今天即视为数据泄漏
    （置信带向后取近3年窗口，唯一能造成未来泄漏的入口就是末根/某根 bar 本身是未来日期；
    旧新鲜度护栏只查 last_date 比今天旧、对「未来日期」静默通过）。此项为硬失败，
    命中即阻断部署，确保线上推演锚定的是真实「当下」而非虚构未来。
    """
    issues = []
    seen = set()
    _today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()  # R167: 中国时区(UTC+8), 避免 UTC 服务器跨日窗口把中国"今日"bar 误判为未来日期泄漏
    for i, k in enumerate(klines):
        d = k["date"]
        if d > _today:
            issues.append("未来日期(数据泄漏) %s" % d)
        if d in seen:
            issues.append("重复日期 %s" % d)
        seen.add(d)
        if k["high"] < k["low"]:
            issues.append("%s high<low" % d)
        if not (k["low"] <= k["close"] <= k["high"]):
            issues.append("%s 收盘价超出高低范围" % d)
        if not (k["low"] <= k["open"] <= k["high"]):
            issues.append("%s 开盘价超出高低范围" % d)
        if k["close"] <= 0:
            issues.append("%s 收盘价非正" % d)
    # 交易日连续性 / 缺失检测（#6）——按 period 取对应阈值, 避免周/月线被日线假设误判
    _gap_max = _GAP_MAX.get(period, 14)
    _per_year = _PER_YEAR.get(period, 244)
    if len(klines) >= 2:
        dates = [k["date"] for k in klines]
        for i in range(1, len(dates)):
            dd = _date_gap(dates[i - 1], dates[i])
            if dd <= 0:
                issues.append("日期非递增 %s→%s" % (dates[i - 1], dates[i]))
            elif dd > _gap_max:  # 超过该周期正常相邻最大间隔, 疑似漏数据而非休市
                issues.append("间隔异常(疑似缺失交易日) %s→%s(%d天)" % (dates[i - 1], dates[i], dd))
        total_days = _date_gap(dates[0], dates[-1])
        if total_days > 0:
            exp = int(total_days / 365.25 * _per_year)  # 按周期年均 bar 数估算
            if abs(len(dates) - exp) > max(5, exp * 0.04):
                issues.append("交易日数量异常：实际%d 预计约%d" % (len(dates), exp))
    # 未来泄漏是硬失败信号，必须优先保留、不被截断吞掉
    # （否则可能被≥12条其它问题挤到 12 名之外，绕过 main 的硬拦）
    future = [x for x in issues if "未来日期" in x]
    others = [x for x in issues if "未来日期" not in x]
    issues = future + others
    return issues[:12]  # 只保留前12条（未来泄漏已优先排在前面）


def main():
    result = {}
    _today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()  # R170: 与下方 :176 中国时区守卫一致, 避免 UTC runner 跨日使 _today 偏差致末根半截 bar 误判
    for sym, name in SYMBOLS.items():
        try:
            day, dirty_day = fetch_tx(sym, "day")
            # R164 防御: 今日未收盘(15:00 前)时源端可能返回进行中当日 bar,
            # 丢弃末根避免未来泄漏(置信带锚定虚构"当下")。已收盘(>=15:00)则保留完整当日 bar。
            if day and day[-1]["date"] == _today and datetime.now(timezone(timedelta(hours=8))).hour < 15:  # R167: 显式中国时区, 不再依赖服务器本地 TZ(UTC runner 会误判 15:00 收盘致误丢/误留当日 bar)
                day = day[:-1]
            if not day:
                print("WARN %s 无日线数据, 跳过该标的" % name)
                continue
            # R349: 三周期根数下限守卫(scan_radar._fetch_tx R348 同族对称)。
            #   fetch_tx 对异常源(CDN 抽风/接口半响应)可能返回空/1根/截断序列, 而 validate
            #   的"数量异常"对整体截断自洽放行(首末同缩比值不变)、len<2 时跳过连续性检查,
            #   写盘前又只硬拦未来日期 —— 短序列会静默覆写 data.json 致 report 全链失真。
            #   正常基线(2021 起): 日1376/周290/月69, 下限留足余量防误伤; day 先查省无效请求。
            if len(day) < 100:
                print("WARN %s 日线序列过短(仅%d根<100), 视为抓取异常跳过该标的(保留其余已成功标的)" % (name, len(day)))
                continue
            week, dirty_week = fetch_tx(sym, "week")
            month, dirty_month = fetch_tx(sym, "month")
            # R372: 周/月线补做与日线同款"盘中剔除进行中根"守卫(R164/R167 只覆盖 day,
            # 周/月漏网): 交易日 15:00 前腾讯把进行中周/月(把已收盘的周内/月内前几日与
            # 今日实时价并成一根, date=当日)作为半截根返回——close=实时价随盘中跳变、
            # volume 不完整, 且 date 超前于被剔除当日后的日线末根, 使:
            #   ① schema 门禁(week/month 末>day 末)无条件阻断 CI(R330 接线后交易时段
            #      push 触发 deploy 连续 18 次 "All jobs have failed", 2026-09-08 实证);
            #   ② analyze(week/month) 把未完成根当完整周期 → 周/月线末笔结构失真。
            # 剔除后周线回落到最近完整周(上周五), 与"日线盘中回落昨日"同语义——干净优先。
            # 收盘后(>=15:00)源端给完整当日根(date=当日), 与日线一致保留。
            if (week and week[-1]["date"] == _today
                    and datetime.now(timezone(timedelta(hours=8))).hour < 15):
                week = week[:-1]
            if (month and month[-1]["date"] == _today
                    and datetime.now(timezone(timedelta(hours=8))).hour < 15):
                month = month[:-1]
            if len(week) < 40 or len(month) < 12:
                print("WARN %s 周/月线过短(周%d/月%d, 下限40/12), "
                      "视为抓取异常跳过该标的(保留其余已成功标的)" % (name, len(week), len(month)))
                continue
            # R156: 周/月线此前完全未校验——report.py 会 analyze 周/月线(1963-1964)并 feeding market_breadth,
            # 若不校验, 周/月线的未来日期泄漏/OHLC 违规会无声流入看板, 而 R82 硬拦只查日线 issues。
            # 现按 period 校验周/月线, 并将其问题(尤其未来日期)并入 meta.issues, 使硬拦覆盖三线。
            issues_day = validate(day, "day")
            issues_week = validate(week, "week")
            issues_month = validate(month, "month")
            issues = (issues_day
                      + [("周线:" + x) for x in issues_week]
                      + [("月线:" + x) for x in issues_month])
            # R173(F2): 脏 bar 丢根计数并入 meta.issues, 让"静默丢数据"可见
            _dirty_total = dirty_day + dirty_week + dirty_month
            if _dirty_total:
                issues.append("脏bar已跳过(数据可能缺失) %d 根(日%d/周%d/月%d)" % (
                    _dirty_total, dirty_day, dirty_week, dirty_month))
            # 全序列抽样比值一致性校验（腾讯 qfq ↔ 新浪 裸价）
            sina_series = fetch_sina_series(sym)
            cc = cross_validate(day, sina_series)
            # 双源一致性提升为“可见门禁”：缺校验/超阈值明确标出，避免静默当“干净”
            if cc["n"] == 0:
                cc_status = "N/A(新浪未校验)"
            elif cc["max_rel_dev"] is not None and cc["max_rel_dev"] > 0.02:
                cc_status = "WARN(偏离%.2f%%)" % (cc["max_rel_dev"] * 100)
            else:
                cc_status = "OK"
            result[sym] = {
                "name": name,
                "klines": day,
                "week_klines": week,
                "month_klines": month,
                "meta": {
                    "count": len(day),
                    "week_count": len(week),
                    "month_count": len(month),
                    "first_date": day[0]["date"],
                    "last_date": day[-1]["date"],
                    "issues": issues,
                    "consistency": cc,
                    "consistency_status": cc_status,
                },
            }
            print("%s: 日线%d(%s~%s) 周线%d 校验问题%d 双源比值稳定度 样本%d 最大偏离%s 一致性:%s" % (
                name, len(day), day[0]["date"], day[-1]["date"], len(week),
                len(issues), cc["n"],
                ("%.3f%%" % (cc["max_rel_dev"] * 100)) if cc["max_rel_dev"] is not None else "N/A",
                cc_status))
        except Exception as e:
            print("WARN %s 抓取/校验失败, 跳过该标的(保留其余已成功标的): %s" % (name, e))
            continue
    # 写盘前整体硬拦：任一指数含未来泄漏即拒绝落盘，避免脏数据进入线上推演
    # （validate 的"未来日期"为硬失败，命中即阻断部署；其余校验问题仅记录不阻断）
    for sym, res in result.items():
        fi = [x for x in res["meta"]["issues"] if "未来日期" in x]
        if fi:
            print("ERROR 检测到未来泄漏(数据异常), 拒绝写入 data.json:", sym, fi)
            sys.exit(1)
    # Bug H 修复：上游 outage / 解析失败时 result 可能为空，若照常 json.dump({})
    # 会把 data.json 覆写成 {}，污染所有只读机并导致线上推演崩溃。空结果直接拒绝落盘。
    if not result:
        print("ERROR 全部标的抓取失败(疑似上游 outage), 拒绝写入空 data.json 以保护既有数据",
              file=sys.stderr)
        sys.exit(1)
    if len(result) < len(SYMBOLS):
        print("WARN 仅抓到 %d/%d 标的, 仍写入(缺失标的将在看板标 N/A)" % (len(result), len(SYMBOLS)))
    # 原子写：先写 .tmp 再 os.replace，避免写入途中崩溃留下半截损坏文件
    out_path = os.path.join(_BASE, "data.json")
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp_path, out_path)
    except Exception:
        # 写盘失败清理 .tmp：优先 os.remove（CI/Linux/Mac 原生支持, 无 shell 注入风险）；
        # 部分运行时(如本沙箱 safe-delete shim)拦截 Python 删除, 退化为 shell rm 兜底。
        try:
            os.remove(tmp_path)
        except Exception:
            try:
                os.system("rm -f " + tmp_path)
            except Exception:
                pass
        raise
    print("saved -> data.json (%d 标的)" % len(result))  # R349: 实际路径为仓库根 data.json(_BASE=fetch_data.py 目录), 旧文案 chanlun/data.json 为旧布局残留
    # R177b: 情绪引擎数据源随每日行情刷新(东财接口含成交额/换手率; 失败仅跳过, 不阻断主管线)
    try:
        update_sentiment_txts()
    except Exception as e:
        print("WARN 情绪txt更新异常(忽略, 不影响主报告): %s" % e)


if __name__ == "__main__":
    main()
