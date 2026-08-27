"""R230 回归护栏: 比对 calc_v2 产物与已提交快照的 asof。

根因: 东财情绪源(含成交额/换手率)在云端 CI 被限流, fetch_em 频繁失败/返回 stale 数据,
update_sentiment_txts 已改为"仅当抓到更新数据才覆盖", 但 calc_v2 仍可能因部分符号 stale
而算出比已提交快照更旧的 asof 并覆盖部署 -> 线上情绪数据永久滞后且静默。

本脚本在 calc_v2 之后运行: 若产物 asof 早于已提交快照(本地刷新推送的新鲜值),
则 git checkout 还原为已提交快照, 并打印醒目 WARN, 杜绝"静默回退还假装更新"。
"""
import json
import subprocess


def _asof(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("asof")
    except Exception:
        return None


def _committed_asof():
    try:
        out = subprocess.run(["git", "show", "HEAD:sentiment/sentiment_v2.json"],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out).get("asof")
    except Exception:
        return None


def main():
    new = _asof("sentiment/sentiment_v2.json")
    committed = _committed_asof()
    if new and committed and new < committed:
        print("WARN: calc_v2 产物 asof %s 早于已提交快照 %s, 还原为已提交(防东财限流导致情绪数据回归)"
              % (new, committed))
        subprocess.run(["git", "checkout", "HEAD", "--", "sentiment/sentiment_v2.json"], check=True)
    elif new and committed and new == committed:
        print("INFO: 情绪 asof %s 与已提交一致(东财在 CI 未刷新, 沿用已提交快照)" % new)
    else:
        print("INFO: 情绪 asof 推进至 %s" % new)


if __name__ == "__main__":
    main()
