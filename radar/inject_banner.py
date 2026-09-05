# -*- coding: utf-8 -*-
"""首页(主报告 index.html)注入「全市场雷达入口按钮」 (radar/inject_banner.py)
========================================================
P2 改造(2026-09-05): 用户要求删除旧版常驻全宽信号横幅,
改为在主报告标题栏(header)右上角注入一个跳转按钮:
  📡 缠论雷达 ->   (点击进入全市场雷达页 radar/radar.html)

不再读取 radar/radar.json(按钮为纯静态入口, 雷达数据缺失不阻断主报告发布)。
旧版 build_banner 整条删除。

用法(替代 deploy.yml 里的 cp report.html index.html):
  python3 radar/inject_banner.py report.html
   -> 输出 index.html (report.html + header 右上按钮 + 适配CSS), 与 cp 同目录语义一致
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# P2: header 右上角胶囊按钮 + 三断点适配CSS(桌面/窄屏/矮横屏)。
# 桌面: header 非flex, 按钮 absolute 定位右上, h1 右侧留白避让;
#        header 自带 position:relative(主样式), 这里再保险声明一次。
# 窄屏(<=720px 手机竖屏): h1 标题占满整行, 按钮仍贴右上, p 说明文字加右距避让。
# 矮横屏(max-height:560px): report.html 自带 header flex(space-between)紧凑版,
#        按钮改为 flex 第三项贴右, 与 h1/p 同行, 不遮文字。
RADAR_BTN_CSS = """
<style>
/* ===== P2 全市场雷达入口按钮(替代旧顶部信号横幅) ===== */
.wrap > header { position: relative; }
.wrap > header > h1 { padding-right: 158px; box-sizing: border-box; }
a.radar-btn {
  position: absolute; top: 50%; right: 24px; transform: translateY(-50%);
  z-index: 3; display: inline-flex; align-items: center; gap: 5px;
  background: rgba(255,255,255,.13); border: 1px solid rgba(255,255,255,.30);
  color: #fff; font-size: 13px; font-weight: 600; line-height: 1;
  padding: 8px 14px; border-radius: 999px; text-decoration: none;
  white-space: nowrap; -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px);
  box-shadow: 0 2px 8px rgba(0,0,0,.16); transition: background .2s, transform .2s;
  font-family: inherit;
}
a.radar-btn:hover { background: rgba(255,255,255,.24); }
a.radar-btn .rb-dot {
  width: 7px; height: 7px; border-radius: 50%; background: #ffd43b;
  box-shadow: 0 0 0 0 rgba(255,212,59,.55); animation: rbPulse 2s infinite;
}
@keyframes rbPulse {
  0%   { box-shadow: 0 0 0 0 rgba(255,212,59,.5); }
  70%  { box-shadow: 0 0 0 6px rgba(255,212,59,0); }
  100% { box-shadow: 0 0 0 0 rgba(255,212,59,0); }
}
@media (max-width: 720px) {
  a.radar-btn { right: 12px; font-size: 12px; padding: 6px 11px; }
  .wrap > header > h1 { padding-right: 0; }
  .wrap > header > p { padding-right: 108px; }   /* 说明文字右端避让按钮 */
}
@media (max-height: 560px) {
  /* 矮横屏: report.html 自带 header flex(space-between)紧凑版 —— 改为首行左对齐,
     让按钮作为 flex 第三项 margin-left:auto 贴右, 与 h1/p 同行不遮字 */
  .wrap > header { justify-content: flex-start; flex-wrap: nowrap; }
  a.radar-btn { position: static; transform: none; margin-left: auto; flex: 0 0 auto; padding: 5px 10px; font-size: 12px; }
  .wrap > header > h1 { padding-right: 0; flex: 0 1 auto; }
  .wrap > header > p { padding-right: 0; flex: 0 1 auto; }
}
</style>
"""


def build_button():
    """P2: 纯静态跳转按钮(不依赖 radar.json, 恒可注入)。"""
    return ('<a class="radar-btn" href="radar/radar.html" title="全市场缠论信号雷达 · '
            '按申万一级行业聚合，点击行业看行业K线与个股详情">'
            '<span class="rb-dot"></span>📡 缠论雷达 →</a>')


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "report.html")
    if not os.path.exists(src):
        print("ERROR 源报告不存在: %s" % src, file=sys.stderr)
        sys.exit(1)
    with open(src, "r", encoding="utf-8") as f:
        html = f.read()

    # 1) 注入 CSS 到 </head> 前
    head_end = html.find("</head>")
    if head_end > 0:
        html = html[:head_end] + RADAR_BTN_CSS + "\n" + html[head_end:]
    else:
        print("WARN 未找到 </head>, 按钮样式追加到文件头", file=sys.stderr)
        html = RADAR_BTN_CSS + html

    # 2) 注入按钮到第一个 <header> 内(末尾, h1/p 之后作兄弟节点; 绝对定位不受 DOM 顺序影响,
    #    矮横屏 flex 下作为第三项由 margin-left:auto 贴右)
    hdr = re.search(r"<header[^>]*>.*?</header>", html, re.S)
    if hdr:
        tag_end = html.find("</header>", hdr.start())
        html = html[:tag_end] + build_button() + html[tag_end:]
    else:
        print("WARN 未找到 <header>, 按钮追加到 <body> 后", file=sys.stderr)
        b = html.find("<body")
        if b >= 0:
            j = html.find(">", b) + 1
            html = html[:j] + build_button() + html[j:]
        else:
            html = build_button() + html

    out = os.path.join(BASE, "index.html")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out)
    size_kb = os.path.getsize(out) // 1024
    has_btn = "class=\"radar-btn\"" in html
    print("index.html 就绪(按钮=%s, 样式=%s), %d KB"
          % ("有" if has_btn else "无", "有" if "a.radar-btn" in html else "无", size_kb))


if __name__ == "__main__":
    main()
