#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calc_national.py — 国家队持仓走势子站 · 数据脚本 (national/ 子站, 与 radar/ 同模式)

数据源: 东财 datacenter RPT_F10_EH_FREEHOLDERS (十大流通股东, CI 可直连)
  https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_EH_FREEHOLDERS
  单票 pageSize=500 → 50 个报告期 (约 2014 起), pages 翻页拿全。

主体词典 (股东名称精确匹配, 全部为 A 股上市公司定期报告披露口径):
  汇金投资 / 汇金资管 / 证金 / 社保养老 / 梧桐树 / 国新 / 诚通

清洗规则:
  * 只保留季度末报告期 (03-31/06-30/09-30/12-31), 剔除非季末权益变动公告日;
  * 同一 (code, 季末, bucket) 多行取 HOLD_NUM 最大 (防同主体多席位重复计入);
  * 环比变动 = 同 code 同 bucket 上一季末差 (自算, 不依赖接口变动字段)。

产物: national/national.json (tracked, 由 CI 在季度披露结束后重跑并提交)
  meta  : generated_at(UTC)/version/pool/failed/latest_report/latest_notice
  series: 每季末聚合行 [{d, hj_tz, hj_zg, zj, sb, wt, hjx, ncov}]  (亿股单位在 HTML 换算)
  top   : 最新报告期命中明细 [{code, name, bucket, hold, free_ratio}] 按持股数降序
  stocks: 每票每主体季末序列 {code:{name, b:{hj_tz:[[d,hold]...]}}}   (个股走势, 备用)

用法: python3 calc_national.py [--out national.json] [--pool 精简子集名]
约束: 请求间 0.22s 节流 + 单票 2 次重试; UA/Referer 齐全; 失败票进 meta.failed 不 abort。
"""
import argparse, json, ssl, sys, time, urllib.request, collections

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Referer": "https://emweb.securities.eastmoney.com/"}
CTX = ssl.create_default_context()
QUARTER_END = {"03-31", "06-30", "09-30", "12-31"}

VERSION = "n1"

# ---------- 主体词典 (顺序: 先长名后短名, 前缀匹配) ----------
def bucket_of(name: str):
    if "中央汇金投资" in name: return "hj_tz"          # 中央汇金投资有限责任公司
    if "中央汇金资产" in name: return "hj_zg"          # 中央汇金资产管理有限责任公司
    if "中国证券金融" in name: return "zj"              # 中国证券金融股份有限公司(含资管计划)
    if ("全国社会保障基金理事会" in name) or ("社保基金" in name) or ("基本养老保险基金" in name): return "sb"
    if "梧桐树投资" in name: return "wt"
    if ("中国国新" in name) or ("国新投资" in name): return "gx"
    if ("中国诚通" in name) or ("诚通金控" in name) or ("诚旸投资" in name): return "ct"
    return None

SUBJECT_CN = {"hj_tz": "中央汇金投资", "hj_zg": "汇金资管", "zj": "证金",
              "sb": "社保/养老", "wt": "梧桐树", "gx": "国新", "ct": "诚通"}

# ---------- 核心池 (~88 只: 金融蓝筹 + 中字头能源 + 大消费医药制造 + 2015 救市重仓; SH/SZ) ----------
POOL = {
    # 银行 (19)
    "601398.SH": "工商银行", "601939.SH": "建设银行", "601288.SH": "农业银行",
    "601988.SH": "中国银行", "601328.SH": "交通银行", "600036.SH": "招商银行",
    "601166.SH": "兴业银行", "600000.SH": "浦发银行", "601818.SH": "光大银行",
    "600016.SH": "民生银行", "601998.SH": "中信银行", "601169.SH": "北京银行",
    "600015.SH": "华夏银行", "601229.SH": "上海银行", "601009.SH": "南京银行",
    "600919.SH": "江苏银行", "601916.SH": "浙商银行", "002142.SZ": "宁波银行",
    "000001.SZ": "平安银行",
    # 保险 (5)
    "601318.SH": "中国平安", "601628.SH": "中国人寿", "601336.SH": "新华保险",
    "601601.SH": "中国太保", "601319.SH": "中国人保",
    # 券商 (15)
    "600030.SH": "中信证券", "601995.SH": "中金公司", "601688.SH": "华泰证券",
    "600999.SH": "招商证券", "601211.SH": "国泰君安", "601066.SH": "中信建投",
    "601881.SH": "中国银河", "600958.SH": "东方证券", "601377.SH": "兴业证券",
    "601788.SH": "光大证券", "601901.SH": "方正证券", "601878.SH": "浙商证券",
    "600109.SH": "国金证券", "000166.SZ": "申万宏源", "002736.SZ": "国信证券",
    # 能源/中字头 (19)
    "600028.SH": "中国石化", "601857.SH": "中国石油", "601088.SH": "中国神华",
    "601898.SH": "中煤能源", "600900.SH": "长江电力", "600019.SH": "宝钢股份",
    "601668.SH": "中国建筑", "601390.SH": "中国中铁", "601186.SH": "中国铁建",
    "601800.SH": "中国交建", "601766.SH": "中国中车", "601618.SH": "中国中冶",
    "601111.SH": "中国国航", "600050.SH": "中国联通", "601728.SH": "中国电信",
    "600941.SH": "中国移动", "601669.SH": "中国电建", "601989.SH": "中国重工",
    "601766.SH": "中国中车",
    # 消费/医药/制造 (12)
    "600519.SH": "贵州茅台", "601888.SH": "中国中免", "600887.SH": "伊利股份",
    "600276.SH": "恒瑞医药", "601899.SH": "紫金矿业", "600585.SH": "海螺水泥",
    "600104.SH": "上汽集团", "600690.SH": "海尔智家", "600031.SH": "三一重工",
    "600089.SH": "特变电工", "601012.SH": "隆基绿能", "603288.SH": "海天味业",
    # 深市蓝筹 (17)
    "000002.SZ": "万科A", "000063.SZ": "中兴通讯", "000333.SZ": "美的集团",
    "000651.SZ": "格力电器", "000725.SZ": "京东方A", "000858.SZ": "五粮液",
    "002304.SZ": "洋河股份", "002415.SZ": "海康威视", "002475.SZ": "立讯精密",
    "300059.SZ": "东方财富", "300750.SZ": "宁德时代", "000776.SZ": "广发证券",
    "002594.SZ": "比亚迪", "300124.SZ": "汇川技术", "000538.SZ": "云南白药",
    "000625.SZ": "长安汽车", "000568.SZ": "泸州老窖",
}

# 移除重复键 (601766 重复定义, 保后者同名)
POOL = dict(collections.OrderedDict((k, v) for k, v in POOL.items()))


def http_get(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=20, context=CTX).read().decode("utf-8")
            return json.loads(raw)
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def fetch_holders(secucode: str):
    """拉单票全历史十大流通股东, 返回行 list。"""
    rows, pn = [], 1
    while pn <= 4:
        url = (f"{API}?reportName=RPT_F10_EH_FREEHOLDERS&columns=ALL"
               f"&pageNumber={pn}&pageSize=500&sortColumns=END_DATE&sortTypes=-1"
               f"&filter=(SECUCODE%3D%22{secucode}%22)&source=HSF10&client=PC")
        d = http_get(url)
        r = d.get("result") or {}
        data = r.get("data") or []
        rows += data
        if pn >= (r.get("pages") or 1):
            break
        pn += 1
        time.sleep(0.22)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="national.json")
    args = ap.parse_args()

    # code -> {name, bucket -> {date -> [hold, free_ratio]}}
    per_stock = collections.defaultdict(lambda: {"name": "", "b": collections.defaultdict(dict)})
    failed = []

    for i, (code, name) in enumerate(POOL.items(), 1):
        try:
            rows = fetch_holders(code)
        except Exception as e:
            print(f"[FAIL] {code} {name}: {e}")
            failed.append(code)
            time.sleep(0.5)
            continue
        per_stock[code]["name"] = name
        hits = 0
        for x in rows:
            dt = (x.get("END_DATE") or "")[:10]
            if not dt or dt[5:] not in QUARTER_END:
                continue
            hname = x.get("HOLDER_NAME") or ""
            b = bucket_of(hname)
            if not b:
                continue
            try:
                v = int(x.get("HOLD_NUM") or 0)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            rr = x.get("FREE_HOLDNUM_RATIO")
            try:
                rr = None if rr is None else float(rr)
            except (TypeError, ValueError):
                rr = None
            cur = per_stock[code]["b"][b].get(dt)
            if cur is None or v > cur[0]:
                per_stock[code]["b"][b][dt] = [v, rr]
                hits += 1
        print(f"[{'OK ' if hits else '---'}] {i:>2}/{len(POOL)} {code} {name}: 国家队行 {hits}")
        time.sleep(0.22)

    # ---- 聚合季末序列 ----
    all_dates = sorted({dt for c in per_stock.values() for b in c["b"].values() for dt in b})
    series = []
    for dt in all_dates:
        row = {"d": dt, "hj_tz": 0, "hj_zg": 0, "zj": 0, "sb": 0, "wt": 0, "gx": 0, "ct": 0, "ncov": 0}
        for c in per_stock.values():
            hit_any = False
            for b, mp in c["b"].items():
                if dt in mp:
                    row[b] += mp[dt][0]
                    hit_any = True
            if hit_any:
                row["ncov"] += 1
        series.append(row)

    latest = series[-1]["d"] if series else None

    # ---- 最新报告期 top 明细 (code, bucket) ----
    top = []
    if latest:
        for code, c in per_stock.items():
            for b, mp in c["b"].items():
                if latest in mp:
                    top.append({"code": code.split(".")[0], "name": c["name"],
                                "bucket": b, "subject": SUBJECT_CN[b],
                                "hold": mp[latest][0],
                                "free_ratio": mp[latest][1]})
    top.sort(key=lambda r: r["hold"], reverse=True)

    # ---- 个股全序列 (备用) ----
    stocks = {}
    for code, c in per_stock.items():
        bk = {}
        for b, mp in c["b"].items():
            arr = [[dt, mp[dt][0], mp[dt][1]] for dt in sorted(mp)]
            if arr:
                bk[b] = arr
        if bk:
            stocks[code.split(".")[0]] = {"name": c["name"], "b": bk}

    out = {
        "meta": {
            "title": "国家队持仓走势 · 核心重仓池",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": VERSION,
            "scope": "A股核心蓝筹池(定期报告前十大流通股东口径, 非全市场)",
            "pool_size": len(POOL),
            "failed": failed,
            "latest_report": latest,
            "subject_map": SUBJECT_CN,
        },
        "series": series,
        "top": top,
        "stocks": stocks,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n_hit_stock = sum(1 for c in per_stock.values() if any(c["b"].values()))
    print(f"\n写入 {args.out}: pool={len(POOL)} 命中票={n_hit_stock} 报告期={len(series)} "
          f"最新={latest} failed={len(failed)}")
    if latest and series:
        r0 = series[-1]
        print(f"最新期({latest}): 汇金投资={r0['hj_tz']/1e8:.1f}亿 汇金资管={r0['hj_zg']/1e8:.1f}亿 "
              f"证金={r0['zj']/1e8:.1f}亿 社保/养老={r0['sb']/1e8:.1f}亿 覆盖票数={r0['ncov']}")
        if len(series) > 1:
            r1 = series[-2]
            print(f"环比: 证金 {(r0['zj']-r1['zj'])/1e8:+.1f}亿 汇金投资 {(r0['hj_tz']-r1['hj_tz'])/1e8:+.1f}亿")


if __name__ == "__main__":
    sys.exit(main())
