# -*- coding: utf-8 -*-
"""
audit_report_runtime.py — A股缠论看板「报告运行时 / NaN·Infinity 数据字面量」门禁 (R169 新增)。

R168 已修 forecast_svg 的 NaN 路径守卫(B1 空bis / B2 空收益窗口 ZeroDivision);
本门禁作为**回归护栏**, 扫描生成的 report.html 是否仍混入非法的数值字面量:
  - 独立 `NaN` (非 isNaN() 函数调用) —— 数据 NaN 流入 JS/图表, 渲染成 "NaN" 文本或破坏 JSON。
  - `Infinity` / `-Infinity` 作为**数据值**(排除良性的 `var lo = Infinity, hi = -Infinity;` 循环初值)。
  - `undefined` 作为**数据值**(仅 WARN, 不阻断; JS 中 `=== undefined` 属正常)。

这些字面量在嵌入的 JS 数据对象里是非法 JSON/JS 数值, 会令 ECharts option 解析失败或图表显示
"NaN"/"Infinity" 文本。当前 report.html 经 R168 修复后应为 0 处(仅余良性循环初值), 本门禁守未来回归。

阻断 CI (退出码 0=通过 1=失败): 报告出现 NaN/Infinity 数据字面量 = 渲染必然出错。

用法: python audit_report_runtime.py  (report.html 缺失时自动重生成)
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(os.path.abspath(__file__))

# 良性: ECharts y 轴范围循环初值 `var lo = Infinity, hi = -Infinity;` —— 非数据字面量。
SAFE_INF = re.compile(r"var\s+lo\s*=\s*Infinity\s*,\s*hi\s*=\s*-Infinity\s*;")
NAN_RE = re.compile(r"\bNaN\b")          # 独立 NaN(不匹配 isNaN 内的 NaN)
INF_RE = re.compile(r"-?Infinity")
UNDEF_RE = re.compile(r"[=:]\s*undefined")  # 数据值上下文的 undefined(WARN)


def run():
    print("=== 报告运行时/NaN·Infinity 护栏 (audit_report_runtime, R169) ===")
    path = os.path.join(BASE, "report.html")
    if not os.path.exists(path):
        print("  report.html 缺失, 尝试重生成...")
        r = subprocess.run([sys.executable, "report.py"], cwd=BASE)
        if r.returncode != 0 or not os.path.exists(path):
            print("  FAIL: report.py 重生成失败 (exit=%s)" % r.returncode)
            return False
    try:
        html = open(path, encoding="utf-8").read()
    except Exception as e:
        print("  FAIL: report.html 读取失败: %s" % e)
        return False

    if "<html" not in html or "</html>" not in html:
        print("  FAIL: report.html 疑似损坏 (size=%d)" % len(html))
        return False

    stripped = SAFE_INF.sub("", html)        # 去掉良性循环初值
    nan_hits = NAN_RE.findall(stripped)
    inf_hits = INF_RE.findall(stripped)
    undef_hits = UNDEF_RE.findall(stripped)

    fails = []
    if nan_hits:
        fails.append("NaN 数据字面量 %d 处(非 isNaN 调用)" % len(nan_hits))
    if inf_hits:
        fails.append("Infinity/-Infinity 数据字面量 %d 处(非良性循环初值)" % len(inf_hits))
    warns = []
    if undef_hits:
        warns.append("undefined 数据值 %d 处(WARN)" % len(undef_hits))

    ok = len(fails) == 0
    print("  NaN=%d  Infinity(残留)=%d  undefined(值)=%d  size=%d" % (
        len(nan_hits), len(inf_hits), len(undef_hits), len(html)))
    for w in warns:
        print("    WARN: %s" % w)
    if ok:
        print("  OK: 无 NaN/Infinity 数据字面量, 报告渲染安全")
    else:
        print("  FAIL:")
        for f in fails:
            print("    - %s" % f)
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
