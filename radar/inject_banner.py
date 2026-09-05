# -*- coding: utf-8 -*-
"""首页(主报告 index.html)顶部注入「全市场雷达信号条」 (radar/inject_banner.py)
========================================================
在 report.py 生成的 report.html 顶部注入一条常驻信号横幅:
  今日信号 N 个(截至 asof) -> 点击进入全市场雷达页 radar/radar.html
读取 radar/radar.json(由 radar/scan_radar.py 每日产出, CI radar-scan.yml 提交)。
radar.json 缺失/异常时退化为「仅 cp 不注入」, 绝不阻断主报告发布。

用法(替代 deploy.yml 里的 cp report.html index.html):
  python3 radar/inject_banner.py report.html
   -> 输出 index.html (report.html + 横幅), 与 cp 同目录语义一致
"""
import os
import re
import sys
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def report_build_time(html):
    """从主报告 report.html 提取生成时间(BUILD_TIME__="...")。主报告时间戳才是首页时间轴。"""
    m = re.search(r'BUILD_TIME__\s*=\s*"([^"]+)"', html)
    if m:
        return m.group(1).strip()
    m = re.search(r"生成时间[：:]\s*([^<\s][^<\n]*)", html)
    return m.group(1).strip() if m else ""


def build_banner(radar_json, deploy_ts=""):
    """由 radar.json 拼横幅 HTML; 任何异常返回 None(调用方走纯cp)。
    deploy_ts = 主报告生成时间(与首页其他时间戳一致), 为空则不显示时刻。"""
    try:
        meta = radar_json.get("meta", {})
        signals = radar_json.get("signals", []) or []
        n = len(signals)
        asof = meta.get("asof", "")
        strong = [s for s in signals if s.get("strong", 0) >= 2][:3]
        pills = ""
        for s in strong:
            nm = s.get("name", "")
            dr = "底背驰" if s.get("dir") == "bottom" else "顶背驰"
            pills += ('<span style="display:inline-block;background:rgba(255,255,255,.14);'
                      'border-radius:10px;padding:1px 9px;margin:0 4px;font-size:12px">%s %s</span>'
                      % (nm, dr))
        col = "#ffd43b"
        if n == 0:
            title = "今日全市场缠论信号：暂无近端背驰信号"
        else:
            title = "今日全市场缠论信号：%d 个" % n
        inner = []
        inner.append('<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">')
        inner.append('<span style="font-weight:700">🎯 %s</span>' % title)
        if asof:
            inner.append('<span style="opacity:.8;font-size:12px">数据截至 %s%s</span>'
                         % (asof, " · 更新于 " + deploy_ts if deploy_ts else ""))
        inner.append(pills)
        inner.append('<a href="radar/radar.html" style="margin-left:auto;color:%s;font-weight:700;'
                     'text-decoration:none;font-size:13px;white-space:nowrap">查看全市场雷达 →</a>' % col)
        inner.append('</div>')
        return (
            '<div style="position:sticky;top:0;z-index:99;background:linear-gradient(135deg,#16233c,#1d3a63);'
            'color:#fff;font:13px/1.6 \'Microsoft YaHei\',\'PingFang SC\',sans-serif;'
            'padding:9px 16px;box-shadow:0 2px 8px rgba(0,0,0,.18)">'
            + "".join(inner) + '</div>')
    except Exception:   # noqa: BLE001
        return None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "report.html")
    if not os.path.exists(src):
        print("ERROR 源报告不存在: %s" % src, file=sys.stderr)
        sys.exit(1)
    with open(src, "r", encoding="utf-8") as f:
        html = f.read()
    radar_path = os.path.join(BASE, "radar", "radar.json")
    banner = ""
    if os.path.exists(radar_path):
        try:
            radar_json = json.load(open(radar_path, "r", encoding="utf-8"))
            b = build_banner(radar_json, report_build_time(html))
            if b:
                banner = b
        except Exception as e:   # noqa: BLE001
            print("WARN 横幅注入失败(降级纯cp): %s" % e, file=sys.stderr)
    if banner:
        if "<body" in html:
            j = html.index("<body")
            i = html.index(">", j) + 1
            html = html[:i] + banner + html[i:]
        else:
            print("WARN 未找到 <body>, 横幅追加到文件头", file=sys.stderr)
            html = banner + html
    out = os.path.join(BASE, "index.html")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out)
    print("index.html 就绪(横幅=%s), %d KB" % ("有" if banner else "无", os.path.getsize(out) // 1024))


if __name__ == "__main__":
    main()
