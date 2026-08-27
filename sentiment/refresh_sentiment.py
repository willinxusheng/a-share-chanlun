# -*- coding: utf-8 -*-
"""本机盘后情绪自动刷新 + 推送脚本 (sentiment/refresh_sentiment.py)

根因(R230 + 用户诉求"实时更新情绪面板"):
  情绪 4 指数走东财源(含成交额/换手率), 在 GitHub CI 被限流、沙箱代理截断,
  云端永远刷不了 -> 线上情绪面板永久滞后到已提交旧快照(曾卡 08-26)。
  本脚本运行在【用户本机 Windows】(南通, 东财可达), 盘后定时执行:
    1) fetch_data.update_sentiment_txts()  刷新 4 个东财 txt(仅抓到更新才覆盖);
    2) python sentiment/calc_v2.py         重算 sentiment_v2.json;
    3) 比对 新算 asof vs 已提交快照 asof:
       - 推进(>)  -> git add 4txt+sentiment_v2.json + commit + push(触发 CI 立即部署最新面板);
       - 持平(==) -> 跳过推送(零噪声), 并把本地重算产物还原为已提交快照(保持工作树干净);
       - 倒退(<)  -> 异常(东财部分失败), 还原 HEAD 并告警(护栏, 同 guard_sentiment_fresh)。
  配套 refresh_sentiment.bat + Windows 任务计划程序 每个交易日 15:35 触发,
  情绪面板即"每天自动更新到最新交易日", 不再卡在旧快照。

依赖: 仅 Python 3 标准库(fetch_data/calc_v2 均为 stdlib), 无需 venv/第三方包。
用法:
  python sentiment/refresh_sentiment.py            # 正式: 刷新 + 推进则推送
  python sentiment/refresh_sentiment.py --dry-run  # 演练: 跑全流程但不 commit/push, 末尾还原工作树
"""
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # chanlun/
SENT_DIR = os.path.join(REPO, "sentiment")
SNAPSHOT = os.path.join(SENT_DIR, "sentiment_v2.json")
TXT_FILES = ["sh_long.txt", "sz_long.txt", "sh50.txt", "zz1000.txt"]
SENT_TRACKED = [os.path.join("sentiment", f) for f in TXT_FILES] + ["sentiment/sentiment_v2.json"]
PY = sys.executable


def _asof(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("asof")
    except Exception:
        return None


def _committed_asof():
    try:
        out = subprocess.run(["git", "show", "HEAD:sentiment/sentiment_v2.json"],
                             cwd=REPO, capture_output=True, text=True, check=True).stdout
        return json.loads(out).get("asof")
    except Exception:
        return None


def _git(args, check=True, capture=True):
    return subprocess.run(["git"] + args, cwd=REPO,
                          capture_output=capture, text=True, check=check)


def _restore_snapshot():
    """把本地重算产物还原为已提交快照, 保持工作树干净(仅用于跳过/异常分支)。"""
    try:
        _git(["checkout", "HEAD", "--"] + SENT_TRACKED, check=False)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="演练: 不 commit/push, 末尾还原工作树")
    args = ap.parse_args()
    dry = args.dry_run

    print("[1/3] 刷新东财情绪日线 txt ...")
    sys.path.insert(0, REPO)
    import fetch_data
    fetch_data.update_sentiment_txts()

    print("[2/3] 重算情绪模型 sentiment_v2.json ...")
    r = subprocess.run([PY, "sentiment/calc_v2.py"], cwd=REPO, capture_output=True, text=True)
    for line in (r.stdout or "").splitlines():
        if line.startswith("asof:") or "ERROR" in line or "WARN" in line:
            print("   calc_v2> " + line)
    if r.returncode != 0:
        print("ERROR: calc_v2 执行失败(rc=%s), 中止(不推送)" % r.returncode)
        if dry:
            _restore_snapshot()
        return 2

    new = _asof(SNAPSHOT)
    committed = _committed_asof()
    print("[3/3] 情绪 asof: 新算=%s 已提交=%s" % (new, committed))

    if not new:
        print("ERROR: calc_v2 未产出 asof, 中止(不推送)")
        if dry:
            _restore_snapshot()
        return 2

    if committed and new <= committed:
        if new < committed:
            print("WARN: 新算 asof %s 早于已提交 %s(东财部分失败), 还原 HEAD 防回归" % (new, committed))
        else:
            print("INFO: 情绪数据未推进(%s), 跳过推送(无变化不污染提交历史)" % new)
        _restore_snapshot()  # 持平/倒退都还原, 保持工作树干净(已提交快照即权威)
        return 0

    # ===== 推进 -> 提交 + 推送 =====
    print("OK: 情绪数据推进至 %s, 准备提交推送" % new)
    if dry:
        print("DRY-RUN: 将执行 git add 4txt+sentiment_v2.json + commit + push origin main (演练跳过)")
        _restore_snapshot()
        return 0

    # 先同步远端(避开 CI keep-alive 提交的 non-ff 拒绝): 本地落后则变基到 origin/main
    _git(["fetch", "origin", "main"], check=False)
    if _git(["merge-base", "--is-ancestor", "origin/main", "HEAD"], check=False).returncode != 0:
        rb = _git(["rebase", "origin/main"], check=False, capture=True)
        if rb.returncode != 0:
            print("ERROR: 变基 origin/main 失败, 中止推送(避免覆盖):\n%s" % (rb.stdout + rb.stderr))
            _git(["rebase", "--abort"], check=False)
            _restore_snapshot()
            return 3

    _git(["add"] + SENT_TRACKED)
    if _git(["diff", "--cached", "--quiet", "sentiment/"], check=False).returncode == 0:
        print("INFO: 暂存区无差异, 跳过 commit")
        return 0
    _git(["commit", "-m", "chore: 本机盘后刷新情绪快照 (asof %s)" % new])
    p = _git(["push", "origin", "main"], check=False, capture=True)
    if p.returncode != 0:
        print("ERROR: 推送失败(云端不会更新最新情绪)! 请检查网络/权限:\n%s" % (p.stdout + p.stderr))
        _git(["rebase", "--abort"], check=False)
        return 4
    print("DONE: 已推送情绪快照 asof=%s, 云端 CI 将立即(或下次定时)部署最新面板" % new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
