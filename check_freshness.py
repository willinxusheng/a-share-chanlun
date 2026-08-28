# -*- coding: utf-8 -*-
"""R237 线上数据新鲜度看门狗。

每个交易日盘后自动校验「线上已部署报告的数据截止日」是否与「源端最新交易日」一致：
  - 一致    -> 退出 0（当日数据已上线）
  - 落后    -> 先尝试自动触发一次重新部署自救，再以退出码 1 让 workflow 失败 -> GitHub 告警

为什么需要它：
    此前"云端没更新"完全靠旭总人工发现再来反馈，导致同一问题反复折腾多轮(R230~R236)。
    本脚本把这件事变成"自动发现 + 自动补救 + 自动告警"，不需要人盯，也不会再靠肉眼判断。

判定为何不误报：
    不依赖任何节假日日历 —— 直接取源端(腾讯 gtimg，海外 CI 可达)的最新日线日期作为
    "应有的最新交易日"，与线上报告的数据截止日比对。节假日源端不前进、线上也不前进，
    天然不会误报；只有"源端已有新交易日、线上却还停在旧日期"才算真正落后。

仅依赖 Python 标准库，与仓库其余脚本一致（无需 venv / 第三方包）。
用法：
    python check_freshness.py            # 正常巡检
    python check_freshness.py --no-fix   # 只体检不触发自救（用于手动排查）
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 线上报告地址（GitHub Pages）。可用环境变量覆盖以便本地调试。
SITE_URL = os.environ.get("SITE_URL", "https://willinxusheng.github.io/a-share-chanlun/")

# 源端探针：腾讯 gtimg 上证日线。只取最后几根即可拿到最新交易日，省流量也更快。
# 选腾讯的原因：东财在海外 CI 会被限流，腾讯在 CI/沙箱均实测可达（见 R232）。
SOURCE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
              "?param=sh000001,day,,,10,qfq")


def _http_get(url, timeout=30):
    """带 UA 与超时的 GET，返回文本。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def fetch_deployed_date(url=None, bust_cache=True):
    """抓取线上报告，解析「数据区间：A ~ B」中的 B（已部署数据截止日）。

    加时间戳参数绕开 GitHub Pages CDN 边缘缓存，否则可能读到旧副本造成误报
    （同类问题见 R229：服务端已更新但边缘节点吐旧版）。
    """
    url = url or SITE_URL
    if bust_cache:
        url += ("&" if "?" in url else "?") + "_=%d" % int(time.time())
    html = _http_get(url)
    # 形如：数据区间：2021-01-04 ~ 2026-08-28
    m = re.search(r"数据区间[^0-9]{0,10}[\d-]{8,10}\s*[~～]\s*([\d]{4}-[\d]{2}-[\d]{2})", html)
    if m:
        return m.group(1), html
    return None, html


def fetch_source_date():
    """取源端最新交易日（腾讯 gtimg 上证日线末根日期）。"""
    raw = _http_get(SOURCE_URL)
    node = (json.loads(raw).get("data") or {}).get("sh000001") or {}
    kl = node.get("day") or node.get("qfqday") or []
    if not kl:
        raise RuntimeError("源端未返回任何日线，无法判定最新交易日")
    return kl[-1][0]


def trigger_redeploy():
    """自救：触发一次部署工作流。失败不影响主判定（仅记录）。"""
    if not os.environ.get("GH_TOKEN"):
        return False, "未设置 GH_TOKEN，跳过自动触发（请在 workflow 中传入 secrets.GITHUB_TOKEN）"
    try:
        r = subprocess.run(
            ["gh", "workflow", "run", "deploy.yml", "--ref", "main"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return True, "已触发 deploy.yml 重新部署"
        return False, "gh workflow run 失败: %s" % (r.stderr or r.stdout or "").strip()[:200]
    except Exception as e:
        return False, "自动触发异常: %r" % (e,)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    auto_fix = "--no-fix" not in argv

    print("=== 线上数据新鲜度巡检 (R237) ===")
    print("巡检地址: %s" % SITE_URL)

    # 1) 源端最新交易日（判定基准）
    try:
        src_date = fetch_source_date()
    except Exception as e:
        print("ERROR 无法获取源端最新交易日: %r" % (e,))
        # 拿不到基准时不误判失败，退出 0 避免噪音告警
        return 0
    print("源端最新交易日: %s" % src_date)

    # 2) 线上已部署数据截止日
    try:
        dep_date, _html = fetch_deployed_date()
    except Exception as e:
        print("ERROR 无法读取线上报告: %r" % (e,))
        return 0
    if not dep_date:
        print("ERROR 线上报告未解析到「数据区间」，无法判定（可能是页面结构变更）")
        return 0
    print("线上数据截止日: %s" % dep_date)

    # 3) 比对
    if dep_date >= src_date:
        print("✅ 数据已最新（线上 %s >= 源端 %s），无需处理" % (dep_date, src_date))
        return 0

    print("❌ 数据落后：线上 %s < 源端 %s" % (dep_date, src_date))
    if auto_fix:
        ok, msg = trigger_redeploy()
        print("自动补救: %s" % ("成功" if ok else "未生效") + " -> " + msg)
    else:
        print("自动补救: 已跳过(--no-fix)")
    # 退出 1 -> workflow 失败 -> GitHub 告警（旭总不必再靠肉眼发现）
    return 1


if __name__ == "__main__":
    sys.exit(main())
