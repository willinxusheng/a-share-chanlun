#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R204b-A: 同步另一斐波那契项目(A-share-Fibonacci)的波浪节点快照。

每次部署前调用: 从公开 raw URL 拉取另一项目的 data.js, 解析 subForecast.points
波浪节点(date/price/lo/hi/label/side), 导出为 sentiment/other_fib_nodes.json,
供 report.py 的 load_other_fib_nodes 优先读取(线上 CI 稳定可用)。

设计要点:
- 另一仓库已设为 public, 无需 token, 直接 curl 公开 raw 文件。
- 解析失败 / 网络失败 -> 以非 0 退出, 调用方(deploy.yml) 捕获后沿用已提交的内置
  快照兜底(不阻断发布)。
- 仅读取、不修改任何预测数学(合规 R76)。
"""
import os
import re
import sys
import json
import ast
import urllib.request
import ssl

# 另一项目已 public, 用 raw.githubusercontent.com 公开 URL (无 token 可读)
RAW_URL = "https://raw.githubusercontent.com/willinxusheng/A-share-Fibonacci/main/data/data.js"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "other_fib_nodes.json")


def fetch_src(url):
    """拉取 data.js 文本, 失败抛异常。"""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "a-share-chanlun-sync/1.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s" % r.status)
        return r.read().decode("utf-8", errors="replace")


def parse_nodes(src):
    """从 data.js 文本解析 subForecast.points 波浪节点(括号平衡提取)。"""
    start = src.find('"subForecast"')
    if start < 0:
        raise ValueError("subForecast not found in source")
    seg = src[start:start + 8000]
    k = seg.find('"points"')
    if k < 0:
        raise ValueError("points not found")
    sub = seg[k:]
    i = sub.find('[')
    if i < 0:
        raise ValueError("points array not found")
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
        raise ValueError("unbalanced points array")
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
    if not nodes:
        raise ValueError("no valid nodes parsed")
    return nodes


def main():
    try:
        src = fetch_src(RAW_URL)
    except Exception as e:
        sys.stderr.write("FETCH_FAIL: %s\n" % e)
        return 1
    try:
        nodes = parse_nodes(src)
    except Exception as e:
        sys.stderr.write("PARSE_FAIL: %s\n" % e)
        return 2
    meta = {
        "source": "A-share-Fibonacci/data/data.js",
        "synced_from": RAW_URL,
        "note": ("另一斐波那契项目(艾略特波浪+斐波那契比率)波浪节点静态快照, "
                 "R204 锚点对照用; 由 sentiment/sync_other_fib.py 每次部署前从公开仓库同步; "
                 "该项目为人工维护买/卖点, 非每日变动, 此处内置快照作为 CI 兜底"),
        "nodes": nodes,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    sys.stderr.write("OK: synced %d wave nodes -> %s\n" % (len(nodes), OUT_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
