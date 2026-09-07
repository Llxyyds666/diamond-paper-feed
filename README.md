# Diamond Paper Feed

## Overview

Diamond Paper Feed 是一个面向金刚石研究的高召回文献监测项目。它每 6 小时从期刊 RSS、OpenAlex、Crossref 和 arXiv 收集论文，统一元数据、按 DOI 或题名去重，并先发布不依赖 AI 的 `filtered_feed.xml`。每天北京时间 08:00，独立的摘要工作流使用 DeepSeek 对固定数量的待处理论文做相关性复筛、分类和中文摘要，再发布 AI RSS 与网页。

采集和 AI 摘要完全解耦。第一次运行会回溯数据库最近 30 天，可能一次抓到很多论文；这些候选会先安全地进入持久队列，而不是全部提交给 API。每天最多只向 AI 提交 40 篇候选，整轮最多发起 5 次请求（失败重试也计入），因此大规模首次抓取不会突破固定调用预算。

项目生成：

- `filtered_feed.xml`：规则筛选后的高召回 RSS，最多 2000 条，适合立即订阅。
- `ai_summary_feed.xml`：DeepSeek 判定相关的中文精选 RSS。
- `ai_summary.html`：按主题展示的中文摘要页面。
- `state.json`：去重论文、来源水位和待处理 AI 队列。
- `ai_usage.json`：每日候选数、成功数、请求数和 token 用量，不含提示词或凭据；`token_usage_complete=false` 表示超时请求可能已在服务端计费，应以 DeepSeek 控制台为准。
- `fetch_failures.tsv`：当前一轮来源故障分类。

## Covered diamond categories

- 天然与合成金刚石、CVD、HPHT、成核、生长、刻蚀、抛光、键合和转移。
- 单晶、多晶、纳米晶、超纳米晶、纳米金刚石、薄膜与膜片。
- 掺杂、缺陷、晶界、NV、SiV、GeV、SnV 等色心和量子体系。
- 金刚石半导体、功率电子、光电、光子与量子器件。
- 热学、力学、摩擦学、声学、压电、柔性器件和传感。
- 金刚石电极、电化学、催化、表面功能化和生物医学应用。
- 天然金刚石形成、包裹体、矿物学和高压地学。
- 类金刚石碳（DLC）作为相邻方向，分类为 `adjacent-dlc`。

珠宝、营销、影视、体育以及纯数学/计算机中的 diamond graph、diamond norm 等明显歧义会被规则排除。仅将金刚石压砧作为通用实验工具、而不研究金刚石本身或压砧性能的论文不会进入 AI 精选。

## Source architecture

所有适配器都转换为同一种 `PaperRecord`，后续筛选、去重和发布不依赖具体来源：

1. `config/rss_sources.tsv` 保存经真实 GET 验证的官方 RSS/Atom 地址。
2. OpenAlex、Crossref 和 arXiv 使用 30 天首次回溯及后续来源水位补足没有稳定 RSS 的期刊。OpenAlex 与 Crossref 使用数据库游标逐页抓取：每个来源每轮最多解析 2000 条；若仍有后页，`state.json` 会同时保存原始起始日期和不透明续页游标，不推进该来源水位，下一轮从该页继续。只有完整走完该日期范围后才清除续页并更新水位。
3. 宽松规则产生高召回候选，DOI 优先去重；无 DOI 时按规范化题名和年份去重。
4. 候选先写入 `state.json` 的 `pending_ai`，再由独立摘要任务按日限额消费。

当前清单含 158 个活动 RSS 源。2026-09-07 的无 AI 凭据 fresh smoke 中，154 个 RSS 源成功，Crossref 与 arXiv 完成并写入水位；OpenAlex 在后续页发生瞬时网络故障，已保留 368 条规则筛选后的 OpenAlex 记录、原始起始日期和失败页游标，且未推进 OpenAlex 水位。确定性噪声复核后，最终状态和 RSS 各含 723 篇，失败报告含 2 个空 feed、2 个 HTTP 403 和 1 个网络错误。

以下期刊族没有通过有界真实 GET 获得稳定官方 RSS，或官方端点受反爬限制，因此标记为“仅数据库覆盖”。它们仍由 OpenAlex、Crossref 和 arXiv 查询覆盖，不应把猜测 URL 加入 RSS 清单：

| 期刊族 | 仅数据库覆盖原因 |
| --- | --- |
| ACS Applied Materials & Interfaces | 官方 RSS 返回 HTTP 403 |
| ACS Nano | 官方 RSS 返回 HTTP 403 |
| American Mineralogist | 未找到稳定的官方 RSS 地址 |
| Applied Physics Letters | 官方 RSS 已停用并返回 HTTP 404 |
| Carbon | 官方 RSS 未通过有界 GET 验证 |
| Carbon Trends | 官方 RSS 未通过有界 GET 验证 |
| Crystal Growth & Design | 官方 RSS 返回 HTTP 403 |
| Diamond and Related Materials | 官方 RSS 未通过有界 GET 验证 |
| Earth and Planetary Science Letters | 官方 RSS 未通过有界 GET 验证 |
| Journal of Applied Physics | 官方 RSS 已停用并返回 HTTP 404 |
| Journal of Carbon Research | 官方 RSS 返回 HTTP 404 |
| Journal of Physics D: Applied Physics | 官方 RSS 重定向到机器人验证页 |
| Nano Letters | 官方 RSS 返回 HTTP 403 |
| Physics of the Earth and Planetary Interiors | 官方 RSS 未通过有界 GET 验证 |
| Quantum Science and Technology | 尚无经验证的稳定官方 RSS 恢复路径 |
| Semiconductor Science and Technology | 官方 RSS 重定向到机器人验证页 |
| Surface and Coatings Technology | 官方 RSS 未通过有界 GET 验证 |

## Local setup

要求 Python 3.11 和 Git。在 Windows PowerShell 中执行：

```powershell
git clone https://github.com/Llxyyds666/diamond-paper-feed.git
Set-Location diamond-paper-feed
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

本地采集不需要 DeepSeek 凭据：

```powershell
python -m diamond_feed.collect --config paper_feed_config.json --state state.json
```

成功后用 PowerShell 验证输出：

```powershell
[xml](Get-Content -Raw filtered_feed.xml) | Out-Null
Get-Content -Raw state.json | ConvertFrom-Json | Out-Null
Import-Csv -Delimiter "`t" fetch_failures.tsv | Format-Table
```

AI 摘要建议通过 GitHub Actions Secret 运行。若确实需要本地运行，应由操作系统或本地秘密管理器把凭据注入名为 `DEEPSEEK_API_KEY` 的环境变量，然后执行：

```powershell
python -m diamond_feed.summarize --config paper_feed_config.json --state state.json
```

不要把凭据写入 `.env`、PowerShell 脚本、JSON、日志或 Git 提交。

## Configuration

公开配置位于 `paper_feed_config.json`，查询词和排除词位于 `config/queries.json`，RSS 清单位于 `config/rss_sources.tsv`。默认值也是运行时强制的安全边界：

- `collection.lookback_days`：数据库首次回溯 30 天。
- `collection.raw_feed_max_items`：原始 RSS 最多 2000 条。
- OpenAlex 单页最多 200 条、Crossref 单页最多 1000 条；每来源每轮合计最多 2000 条，未完成范围由状态中的数据库游标续抓。
- `collection.http_timeout_seconds` / `http_attempts`：网络超时和有界重试。
- `ai.base_url`：`https://api.deepseek.com`。
- `ai.model`：`deepseek-v4-flash-vision-exp`。
- `publication.base_url`：`https://llxyyds666.github.io/diamond-paper-feed`。

修改配置后先运行 `python -m pytest -q`。代码会拒绝非正数、非 HTTPS AI 地址、未知模型，以及不能为最终摘要预留一次请求的预算组合。

## DeepSeek Secret setup

1. 在 GitHub 创建空的公开仓库 `Llxyyds666/diamond-paper-feed`，不要自动生成 README、License 或 `.gitignore`。
2. 推送代码后打开仓库的 **Settings → Secrets and variables → Actions**。
3. 选择 **New repository secret**，名称填写 `DEEPSEEK_API_KEY`，值在 GitHub 页面内粘贴并保存。
4. 不要把 Secret 放进 Repository variables；不要在 issue、日志、提交或聊天中粘贴它。

只有 `.github/workflows/summarize.yml` 读取这个 Secret；采集工作流不接收 AI 凭据。

## Manual workflow runs

在仓库页面打开 **Actions**：

1. 选择 **Collect diamond literature**，点击 **Run workflow**，分支选 `main`。它会测试、采集并仅提交 `filtered_feed.xml`、`state.json` 和 `fetch_failures.tsv`。
2. 等采集完成后选择 **Summarize diamond literature**，点击 **Run workflow**，分支选 `main`。它会按硬限制处理队列，并仅提交 `ai_summary_feed.xml`、`ai_summary.html`、`ai_usage.json` 和 `state.json`。
3. 在日志和 `ai_usage.json` 中确认 `candidates <= 40`、`requests <= 5`，并确认工作流没有输出凭据。

定时计划为：采集每 6 小时一次；摘要每天 UTC 00:00，即 Asia/Shanghai 08:00。两个工作流共用同一并发组并在推送前 rebase，避免同一分支重叠写状态。

## GitHub Pages and Zotero URLs

在仓库 **Settings → Pages** 中将 Source 设为 **Deploy from a branch**，选择 `main` 和 `/(root)`，保存并等待部署。公开地址为：

- 高召回 RSS：<https://llxyyds666.github.io/diamond-paper-feed/filtered_feed.xml>
- AI 精选 RSS：<https://llxyyds666.github.io/diamond-paper-feed/ai_summary_feed.xml>
- 中文摘要页：<https://llxyyds666.github.io/diamond-paper-feed/ai_summary.html>

在 Zotero 中选择 **File → New Library → New Feed → From URL**（中文界面为“文件 → 新建文献库 → 新建订阅 → 从 URL”），粘贴上面的任一 RSS 地址并保存。建议先订阅高召回 RSS；需要更少、带中文摘要的结果时再订阅 AI 精选 RSS。若 Pages 刚启用返回 404，等待本次 Pages 部署完成后刷新。

## AI hard limits

- 模型固定为 `deepseek-v4-flash-vision-exp`，Base URL 固定为 `https://api.deepseek.com`。
- 每天最多选择 40 篇候选提交给 AI，按最旧待处理项优先。
- 每批最多 10 篇，因此筛选阶段最多 4 个成功批次。
- 每轮最多 5 次 API 请求，所有失败重试也计数；第 5 次后无条件停止。预留的一次用于最终摘要。
- 每篇发给 AI 的摘要先截断到 1200 个 Unicode 字符。
- 每个筛选请求的输出上限为 4096 token；最终摘要请求的输出上限为 8192 token。
- 采集会先持久化候选。首次 30 天回溯形成的大队列会跨天保留并按每天 40 篇逐步处理，不会扩大当天请求预算。
- `ai_usage.json` 只保存日期、候选数、成功数、请求数、token 统计及其完整性标记。超时不会自动重试；`token_usage_complete=false` 时，文件中的 token 只是已收到响应的部分，应以 DeepSeek 控制台为准。

## Failure report meanings

`fetch_failures.tsv` 的列为 `timestamp`、`category`、`url`、`detail`。每次采集会重新生成本轮报告，单个来源失败不阻断其他来源。

- 硬失败：稳定 `http_404`、`http_410`、稳定 `parse_error` 和已确认停用的端点。RSS 清单验证时连续确认后才移除，并优先寻找官方替代地址。
- 软失败：`timeout`、临时 `url_error`/`network_error`、HTTP 429/5xx 等瞬时网络问题，以及 `empty_feed`。这些来源保留在清单中，等待下轮恢复。
- HTTP 403 可能是出版商反爬。没有经真实 GET 验证的官方替代时，期刊改由数据库覆盖。

当全部来源失败时，采集以非零状态退出，不覆盖上一版 `filtered_feed.xml` 或 `state.json`。DeepSeek 认证、余额、限流、超时或 JSON 校验失败时，待处理论文不会丢失，最后一版有效的 `ai_summary_feed.xml` 和 `ai_summary.html` 也不会被覆盖；部分成功只移除已成功处理的队列项。

## Adding sources

只添加出版商官方 RSS/Atom，并使用真实 GET 验证；不要根据 URL 规律猜测。清单必须保留制表符分隔的 `name`、`category`、`url` 三列。

从一个或多个本地文本清单导入、规范化并去重：

```powershell
python scripts/import_rss_sources.py --input C:\path\to\journals.dat --input C:\path\to\期刊.txt --output config\rss_sources.tsv
```

导入会覆盖输出清单，所以先在分支中运行并检查 `git diff -- config/rss_sources.tsv`。随后对完整清单做真实 GET 验证：

```powershell
python scripts/validate_rss_sources.py --sources config/rss_sources.tsv --failures fetch_failures.tsv
python -m pytest tests/test_source_tools.py tests/test_rss_source.py -q
```

验证器会二次确认硬失败、从活动清单中移除稳定硬失败，并保留和记录软失败。若期刊没有稳定官方 RSS，把期刊族加入 `scripts/validate_rss_sources.py` 的 `DATABASE_ONLY_COVERAGE`，在本节同步说明，并依赖 OpenAlex/Crossref/arXiv；不要加入死链。

## State recovery

`state.json` 与输出文件都使用同目录临时文件后原子替换。恢复时不要手工删掉 `pending_ai` 或 `source_continuations`；前者是 AI 故障后继续处理的依据，后者保存 OpenAlex/Crossref 尚未完成范围的原始日期和数据库游标，删除会造成漏抓或重复抓取。

- 工作流失败但 `state.json` 可解析：保留文件并重新运行失败的工作流。队列和上一版摘要会继续使用。
- `state.json` 损坏：先在 GitHub 的提交历史中找到最近一个通过测试的版本，下载或 `git restore --source=<good-commit> -- state.json`，运行 `python -m pytest -q`，再手动运行采集。这样保留已有去重键和队列。
- 必须从空状态重建：先把损坏文件改名保存到仓库外，再删除工作副本中的 `state.json` 并运行采集。系统会重新回溯 30 天，形成新的大队列，仍只按每天 40 篇处理。确认恢复完成前不要提交损坏备份。
- 摘要文件损坏但状态正常：从最近有效提交恢复 `ai_summary_feed.xml`、`ai_summary.html` 和 `ai_usage.json`，再运行摘要工作流。AI 失败不会主动覆盖最后有效摘要。

恢复后运行：

```powershell
python -m pytest -q
[xml](Get-Content -Raw filtered_feed.xml) | Out-Null
Get-Content -Raw state.json | ConvertFrom-Json | Out-Null
```

## Security

- 唯一允许的 AI 凭据名称是 `DEEPSEEK_API_KEY`；值只保存在 GitHub Actions Secret 或本地秘密管理器。
- 不跟踪 `.env`、`vendor/`、`__pycache__/`、虚拟环境、用户配置或复制的上游输出 feed。
- 不记录 Authorization 头、完整 AI 请求体、Secret 值或其片段。
- Collection 工作流不接收 AI Secret；Summary 工作流只在运行摘要命令的单个步骤注入 Secret。
- 发布前运行 `python -m pytest -q`、敏感串扫描和 `git diff --check`，并只暂存任务声明的文件。
- 如果凭据曾进入文件或提交，应先在 DeepSeek 控制台撤销并换新，再清理 Git 历史；仅删除当前文件并不能从历史中移除它。
