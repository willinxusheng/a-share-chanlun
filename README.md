# A股缠论分析看板（a-share-chanlun）

基于缠论（K线包含处理 → 分型 → 笔 → 中枢 → MACD背驰 → 买卖点 → 走势分类）对 A股主要指数做结构复盘与未来推演的自包含分析工具。生成物为单个 HTML 报告（内嵌 SVG，涨红跌绿，含缩放导航条、成交量、未来走势置信锥）。

## 环境要求

- Python 3.8+（**无需 pip 安装任何第三方包**，全部使用标准库）

## 本地生成报告（两步）

```bash
python fetch_data.py   # 1. 抓取 5 大指数日线/周线行情，落盘 data.json
python report.py       # 2. 运行缠论分析并生成 report.html
```

用浏览器打开 `report.html` 即可（支持缩放导航条、悬浮查看 OHLC、未来走势置信锥）。

## 多电脑同步工作流（核心）

> 本仓库**只跟踪源码**（本文件 + 17 个 `.py` + `.github/workflows/deploy.yml`）。`data.json`、`report.html`、`quality_cert.json` 是生成产物，已写入 `.gitignore`，**不入库**。
> 这样任何一台电脑都不会修改同一个被追踪文件，天然避免合并冲突。

新电脑首次使用：

```bash
git clone <本仓库地址> chanlun
cd chanlun
python fetch_data.py && python report.py   # 本地生成最新报告
```

日常更新（任一电脑）：

```bash
git pull            # 拉取最新源码（算法/脚本改动）
python fetch_data.py && python report.py   # 本地重建数据+报告
```

- 想改分析逻辑：改 `.py` → `git commit` → `git push` → 其他电脑 `git pull` 即可。
- 报告内容每台电脑用各自最新行情生成，**互不覆盖、互不影响**。
- 唯一可能的冲突只发生在多人同时改 `.py` 源码时，正常 `pull` 后再改即可规避。

## 在线网页版（GitHub Pages，无需跑脚本）

仓库已配置 `.github/workflows/deploy.yml`：每次推送 `main` 分支，GitHub Actions 会自动拉取最新行情、生成报告并发布到 Pages。

**每日自动更新**：workflow 另配置了定时任务，每个交易日**北京时间 16:00**（周一~周六，含调休补班交易日）自动重新抓取当日收盘行情、生成报告并发布——无需手动推代码，看板始终是最新一个交易日的数据。

启用一次（仓库拥有者操作）：

1. 打开 `https://github.com/willinxusheng/a-share-chanlun/settings/pages`
2. Source 选择 **Deploy from a branch** → 分支选 **gh-pages**（Actions 会自动建）→ Save
   - 或 Source 选 **GitHub Actions**（推荐，与本 workflow 的 `deploy-pages` 动作配套）
3. 稍等 1~2 分钟，访问 `https://willinxusheng.github.io/a-share-chanlun/`

此后：在任一电脑改完 `.py` 并 `git push`，网页版会自动更新，手机直接看。

> 注意：CI 在境外 runner 上运行，主数据源为腾讯公开行情接口（通常可达）；若某次构建因网络失败变红，可先在能联网的电脑本地生成 `report.html`，再手动提交到 `gh-pages` 分支兜底。

## 文件说明

| 文件 | 作用 | 是否入库 |
|------|------|----------|
| `fetch_data.py` | 拉取行情（腾讯主源 + 新浪交叉验证） | ✅ |
| `chanlun.py` | 缠论算法：笔/中枢/背驰/买卖点/回测/健康度 | ✅ |
| `report.py` | 生成自包含 HTML 报告（内嵌 ECharts 前端） | ✅ |
| `gen_quality_cert.py` | 生成数据质量证书（口径/偏置/校准汇总） | ✅ |
| `audit_*.py`（13 个） | 校准/监控门禁：区间得分、尾部覆盖、概率校准、共识偏置、情绪条件化等 | ✅ |
| `.github/workflows/deploy.yml` | GitHub Actions：云端独家抓取 data/ + 生成报告 + 发布 Pages | ✅ |
| `data.json` | 行情快照（脚本生成） | ❌ 本地 |
| `report.html` | 最终报告（脚本生成） | ❌ 本地 |
| `quality_cert.json` | 质量证书（脚本生成） | ❌ 本地 |

## 免责声明

本报告基于缠论技术分析的自动化结构划分与历史统计，属概率性分类框架，非精确预测，更不构成任何投资建议。市场有风险，决策需独立。
