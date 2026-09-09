# -*- coding: utf-8 -*-
"""主页面注入「国家队持仓入口按钮」(national/inject_banner.py)
========================================================
P1(2026-09-09): 用户在主报告顶部要求「与缠论雷达一样」放一个跳转按钮,
指向同仓子站 national/national.html。

设计原则 (深度美化优先, 不破坏 radar 契约):
  - 共用 a.radar-btn 同款胶囊样式 + pulse 动效 (R398/409 已验证审美达预期);
  - 但用冷蓝脉冲(雷达=暖黄) 颜色区分, 视觉层次清晰;
  - 竖排堆叠 (2026-09-09 用户审美反馈, 否定初版横向并排): 两按钮 absolute top:50%
    right:24px, 以 header 垂直中线为对称轴, 雷达钮 translateY(-100%-5px) 居上、
    国家队钮 translateY(5px) 居下, 间隙 10px → 右侧纵向均匀分布、整体垂直居中;
  - 标题右距: 竖排只占单钮宽, h1 padding-right 176px(较 radar 单钮 158 略留呼吸),
    p 说明文字 156px 避让按钮组;
  - 窄屏(<=720px): 按钮组缩小, 右上角竖排, p 右距 128px;
  - 矮横屏(max-height:560px): 高度不足竖排, 两按钮退化为 static flex 子项横向一行
    (雷达 margin-left:auto 贴右 / 国家队 margin-left:8px 次右), 保持紧凑不遮字。

用法(挂在 deploy.yml radar/inject_banner.py 之后, audit_report_runtime.py 之后):
  python3 national/inject_banner.py index.html
  → 输出 index.html(就地追加国家队按钮+CSS)
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CSS 后写覆盖 radar 注入的同名选择器(CSS cascade 后写胜出 + specificity 同)。
# 雷达按钮位置: right 24px → 140px 让位;  h1 标题右距 158px → 296px 给两按钮让位。
NATIONAL_BTN_CSS = """
<style>
/* ===== P1 国家队持仓入口按钮 — 竖排堆叠方案 (2026-09-09 用户审美反馈) =====
   两按钮不再横向并排, 改为 header 右上角纵向均匀分布:
   以 header 垂直中线为对称轴 — 上: 雷达钮(暖黄), 下: 国家队钮(冷蓝),
   各自 translateY 相对中线偏移, 两钮间隙恒 10px, 视觉整体垂直居中。 */
a.radar-btn, a.national-btn {
  position: absolute; top: 50%; right: 24px; transform: translateY(0);
  z-index: 3; display: inline-flex; align-items: center; gap: 5px;
  background: rgba(255,255,255,.13); border: 1px solid rgba(255,255,255,.30);
  color: #fff; font-size: 13px; font-weight: 600; line-height: 1;
  padding: 8px 14px; border-radius: 999px; text-decoration: none;
  white-space: nowrap; -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px);
  box-shadow: 0 2px 8px rgba(0,0,0,.16); transition: background .2s, transform .2s;
  font-family: inherit;
}
/* 竖排锚点: 雷达钮底缘距中线 5px / 国家队钮顶缘距中线 5px → 间隙 10px */
a.radar-btn { transform: translateY(calc(-100% - 5px)); }
a.national-btn { transform: translateY(5px); }
a.radar-btn:hover, a.national-btn:hover { background: rgba(255,255,255,.24); }
a.national-btn .nb-dot {
  width: 7px; height: 7px; border-radius: 50%; background: #74c0fc;
  box-shadow: 0 0 0 0 rgba(116,192,252,.55); animation: nbPulse 2.4s infinite;
}
@keyframes nbPulse {
  0%   { box-shadow: 0 0 0 0 rgba(116,192,252,.5); }
  70%  { box-shadow: 0 0 0 6px rgba(116,192,252,0); }
  100% { box-shadow: 0 0 0 0 rgba(116,192,252,0); }
}
/* 标题/说明右距: 竖排只占单钮宽(约150px), h1 176px 留呼吸; p 156px 避让按钮组 */
.wrap > header > h1 { padding-right: 176px !important; box-sizing: border-box; }
.wrap > header > p { padding-right: 156px; }

/* 窄屏手机竖屏: 按钮组缩小, 右上角竖排 */
@media (max-width: 720px) {
  a.radar-btn, a.national-btn { right: 10px; font-size: 11.5px; padding: 6px 10px; }
  a.radar-btn { transform: translateY(calc(-100% - 4px)); }
  a.national-btn { transform: translateY(4px); }
  .wrap > header > h1 { padding-right: 0 !important; }
  .wrap > header > p { padding-right: 128px; }   /* 说明文字右端避让竖排按钮组 */
}
/* 矮横屏 (R398 验证: header 此时 flex 紧凑版) — 高度不足竖排两钮,
   退化为横向一行(雷达贴右/国家队次右), 保持不遮字 */
@media (max-height: 560px) {
  .wrap > header { justify-content: flex-start; flex-wrap: nowrap; }
  a.radar-btn, a.national-btn { position: static; transform: none !important; flex: 0 0 auto; padding: 5px 10px; font-size: 12px; }
  a.radar-btn { right: auto !important; margin-left: auto; }
  a.national-btn { margin-left: 8px; }
  .wrap > header > h1 { padding-right: 0 !important; flex: 0 1 auto; }
  .wrap > header > p { padding-right: 0 !important; flex: 0 1 auto; }
}
</style>
"""


def build_button():
    """国家队入口按钮 (不依赖 national.json, 纯静态跳转)。"""
    return ('<a class="national-btn" href="national/national.html" title="国家队持仓走势看板 · '
            '汇金·证金·社保养老历史持仓季度跟踪, 逆向投资温度计">'
            '<span class="nb-dot"></span>🏛️ 国家队持仓 →</a>')


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "index.html")
    if not os.path.exists(src):
        print("ERROR 源 index.html 不存在: %s" % src, file=sys.stderr)
        print("提示: deploy.yml 链尾应先跑 `python radar/inject_banner.py report.html` 生成 index.html", file=sys.stderr)
        sys.exit(1)
    with open(src, "r", encoding="utf-8") as f:
        html = f.read()

    # 幂等 guard: 已含国家队按钮则跳过防双份 CSS/按钮 (R353 同款教训)
    if 'class="national-btn"' in html:
        print("WARN 源 %s 已含国家队按钮, 跳过注入(防双份 CSS/按钮)" % src, file=sys.stderr)
        return 0

    # 守卫: 确认 radar 已先注入按钮 (避免 national 在 radar 前跑导致布局错乱)
    if 'class="radar-btn"' not in html:
        print("WARN 源 %s 不含 radar 按钮, 国家队按钮依赖于 radar 按钮位置,"
              "请确认 deploy.yml 链: radar/inject_banner.py → national/inject_banner.py", file=sys.stderr)

    # 1) 注入 CSS 到 </head> 前 (后写覆盖 radar 注入的同名选择器, 见 CSS 注释)
    head_end = html.find("</head>")
    if head_end > 0:
        html = html[:head_end] + NATIONAL_BTN_CSS + "\n" + html[head_end:]
    else:
        print("WARN 未找到 </head>, 国家队按钮样式追加到文件头", file=sys.stderr)
        html = NATIONAL_BTN_CSS + html

    # 2) 注入按钮到第一个 </header> 前 (与 radar 按钮同为绝对定位兄弟, DOM 顺序不影响布局)
    hdr = re.search(r"<header[^>]*>.*?</header>", html, re.S)
    if hdr:
        tag_end = html.find("</header>", hdr.start())
        html = html[:tag_end] + build_button() + "\n" + html[tag_end:]
    else:
        print("WARN 未找到 <header>, 国家队按钮追加到 <body> 后", file=sys.stderr)
        b = html.find("<body")
        if b >= 0:
            j = html.find(">", b) + 1
            html = html[:j] + build_button() + html[j:]
        else:
            html = build_button() + html

    tmp = src + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, src)
    size_kb = os.path.getsize(src) // 1024
    has_btn = "class=\"national-btn\"" in html
    print("index.html 就绪(国家队按钮=%s), %d KB" % ("有" if has_btn else "无", size_kb))


if __name__ == "__main__":
    main()