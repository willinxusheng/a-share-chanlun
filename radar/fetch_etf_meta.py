# -*- coding: utf-8 -*-
"""ETF 官方元数据刷新 (radar/fetch_etf_meta.py)
=========================================================
产出 radar/etf_meta.json —— scan_radar.py 消费的**官方「跟踪标的」缓存**。

为什么需要它(R448 实证):
  R447 用「60 日日收益相关系数 ≥0.99」判同指数, 有**假阴性下限** ——
  sz159527(广发云计算) 与 sz159739(鹏华云计算) 官方跟踪标的**字面相同**
  「中证云计算与大数据主题指数」, 但 60 日相关仅 0.9800 ⇒ 未并组。
  而 0.98 档同时混着**不同指数**的近似产品(6 只「自由现金流/全指现金流/现金流」
  两两 0.9175~0.9886 却属不同指数) ⇒ **阈值无处可调**。只能换判据。

数据源: 天天基金基本信息页 https://fundf10.eastmoney.com/jbgk_<6位代码>.html
  公开静态页, 无需鉴权; 一页同时给出: 基金简称 / 跟踪标的 / 业绩比较基准 /
  管理费率 / 托管费率 / 净资产规模 / 成立日期。
  实测(2026-09-13) 全量 1282 只: 成功 1282 / 失败 0, 有跟踪标的 1282, 有费率 1282,
  有净资产规模 1282, 用时 10 分 12 秒(串行 0.2s 间隔)。

⚠️ 抓取纪律
  · 串行 + 间隔 0.2s(并发会触发 WAF; 全量约 10 分钟, 低频任务可接受)
  · 单个失败重试 1 次(退避 0.8s), 仍失败则**跳过不写**(下次刷新自动补)
  · 增量: 未缓存的代码才抓(``--force`` 全量重抓; 费率/规模会变, 建议每月一次)
  · TLS **保持默认校验**(已实测 fundf10 证书链正常, 无需降级)

用法:
  python3 radar/fetch_etf_meta.py                 # 从 radar/radar.json 取 ETF 代码, 增量刷新
  python3 radar/fetch_etf_meta.py --force         # 强制全量重抓
  python3 radar/fetch_etf_meta.py --codes 159527,159739
统计口径: 仅统计本次运行的结果, 不覆盖既有缓存(增量语义)。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
META_PATH = os.path.join(_HERE, "etf_meta.json")
RADAR_JSON = os.path.join(_HERE, "radar.json")

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
      "Referer": "https://fund.eastmoney.com/"}

SLEEP = 0.2                    # 串行间隔(秒) —— 并发会触发 WAF
RETRY = 1                      # 单只重试次数
TIMEOUT = 20


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")


def _num(s):
    if not s:
        return None
    m = re.search(r"([\d.]+)", s)
    return float(m.group(1)) if m else None


def _nav_yi(s):
    """「12.34亿元（截止至：…）」-> 12.34(亿元)。单位只有 亿/万 两种。"""
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*(亿|万)", s)
    if not m:
        return None
    v = float(m.group(1))
    return round(v, 4) if m.group(2) == "亿" else round(v / 1e4, 6)


def _est_date(s):
    """「2022年01月20日 / 2.347亿份」-> 2022-01-20。"""
    if not s:
        return None
    m = re.search(r"(\d{4})年(\d{2})月(\d{2})日", s)
    return "%s-%s-%s" % m.groups() if m else None


def fetch_meta(code, retry=RETRY):
    """code: 6 位纯数字(不带 sh/sz 前缀)。成功返回紧凑 dict, 失败返回 {"err": ...}。

    紧凑键(为控制入库体积, 1282 只约 190KB):
      n=基金简称  t=跟踪标的  f=合计费率%(管理+托管)  v=净资产规模(亿元)  e=成立日期
    """
    t = None
    for att in range(retry + 1):
        try:
            t = _get("https://fundf10.eastmoney.com/jbgk_%s.html" % code)
            break
        except Exception as e:                                   # noqa: BLE001
            if att == retry:
                return {"err": str(e)[:80]}
            time.sleep(0.8)

    def grab(pat):
        m = re.search(pat, t, re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None

    mgmt, cust = _num(grab(r"管理费率</th><td[^>]*>(.*?)</td>")), _num(grab(r"托管费率</th><td[^>]*>(.*?)</td>"))
    out = {
        "n": grab(r"基金简称</th><td[^>]*>(.*?)</td>"),
        "t": grab(r"跟踪标的</th><td[^>]*>([^<]{2,80})</td>"),
        "f": round(mgmt + cust, 3) if (mgmt is not None and cust is not None) else None,
        "v": _nav_yi(grab(r"净资产规模</th><td[^>]*>(.*?)</td>")),
        "e": _est_date(grab(r"成立日期/规模</th><td[^>]*>(.*?)</td>")),
    }
    if not out["t"] and not out["n"]:
        # 页面结构变了/返回了错误页 —— 视为失败, 不落盘以免污染缓存
        return {"err": "no-field"}
    return out


def radar_etf_codes():
    """从 radar/radar.json 的 universe 取全部 ETF 的 6 位代码(去重保序)。"""
    d = json.load(open(RADAR_JSON, encoding="utf-8"))
    out, seen = [], set()
    for sym, row in (d.get("universe") or {}).items():
        if row.get("type") != "ETF":
            continue
        c = (row.get("code") or sym[-6:])[-6:]
        if c.isdigit() and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def load_meta():
    if not os.path.exists(META_PATH):
        return {"_meta": {}, "data": {}}
    d = json.load(open(META_PATH, encoding="utf-8"))
    if "data" not in d and isinstance(d, dict):        # 兼容旧/裸 dict 格式
        d = {"_meta": {}, "data": d}
    d.setdefault("_meta", {})
    d.setdefault("data", {})
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="全量重抓(忽略已有缓存)")
    ap.add_argument("--codes", default="", help="逗号分隔的 6 位代码; 缺省从 radar.json 取")
    a = ap.parse_args()

    codes = [c.strip()[-6:] for c in a.codes.split(",") if c.strip()] or radar_etf_codes()
    if not codes:
        print("无 ETF 代码可抓(radar/radar.json 缺失?)", flush=True)
        return 1

    meta = load_meta()
    data = meta["data"]
    todo = codes if a.force else [c for c in codes if not (data.get(c) or {}).get("err") and c not in data]
    print("ETF 元数据: 目标 %d 只, 本次需抓 %d 只(缓存已有 %d) -> %s"
          % (len(codes), len(todo), len(data), META_PATH), flush=True)

    t0, ok, fail = time.time(), 0, 0
    for i, c in enumerate(todo):
        r = fetch_meta(c)
        if r.get("err"):
            fail += 1
            print("  ! %s %s" % (c, r["err"]), flush=True)
        else:
            data[c] = r
            ok += 1
        if (i + 1) % 50 == 0:
            print("  %4d/%d  %.0fs  成功%d 失败%d" % (i + 1, len(todo), time.time() - t0, ok, fail), flush=True)
        time.sleep(SLEEP)

    n_track = sum(1 for v in data.values() if v.get("t"))
    n_fee = sum(1 for v in data.values() if v.get("f") is not None)
    meta["_meta"] = {
        "src": "fundf10.eastmoney.com/jbgk_<code>.html",
        "upd": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n": len(data), "n_track": n_track, "n_fee": n_fee,
        "note": "官方「跟踪标的」/费率(管理+托管)/净资产规模/成立日期; 供 scan_radar 精确去重与同指数选优",
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print("完成: 缓存 %d 只(有跟踪标的 %d, 有费率 %d), 本次 成功%d 失败%d, 用时 %.0fs, %.0fKB"
          % (len(data), n_track, n_fee, ok, fail, time.time() - t0,
             os.path.getsize(META_PATH) / 1024.0), flush=True)
    # 部分失败**不算错**: 失败项未写入缓存, 下次刷新会自动补抓(增量语义)。
    return 0


if __name__ == "__main__":
    sys.exit(main())
