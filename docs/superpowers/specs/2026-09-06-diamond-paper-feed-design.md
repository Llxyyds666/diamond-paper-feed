# Diamond Paper Feed 设计说明

日期：2026-09-06

## 1. 背景与目标

本项目建立一个面向金刚石研究的自动文献监测仓库，仓库名为 `diamond-paper-feed`。项目以 `Jarvis-Towne/paper-feed` 的 AI 摘要能力为功能基线，并复用本地 `E:\Desktop\mcp\paper-feed` 中已经验证过的 RSS 抓取、失败分类和出版商兼容经验。

首版不限定具体子方向，优先保证召回率，同时控制 DeepSeek 的调用量和费用。系统应当：

- 覆盖期刊 RSS、OpenAlex、Crossref 和 arXiv。
- 自动收集、规范化、筛选、去重和保存金刚石相关论文。
- 提供 Zotero 可订阅的高召回 RSS。
- 使用 DeepSeek 进行相关性复筛、主题分类和中文摘要。
- 生成每日精选 RSS 与 GitHub Pages HTML 摘要页。
- 单个来源或 AI 服务故障时保留上一版可用结果。

首版不实现邮件、微信或即时通信推送，不引入数据库服务，也不自动下载论文全文。

## 2. 研究范围

纳入以金刚石本身、金刚石材料体系或金刚石器件为研究对象的论文，包括：

- 天然与合成金刚石、成核、生长、CVD、HPHT、刻蚀、抛光、键合和转移。
- 单晶、多晶、纳米晶、超纳米晶、纳米金刚石、薄膜和膜片。
- 掺杂、缺陷、晶界、色心以及 NV、SiV、GeV、SnV 等量子体系。
- 金刚石半导体、功率器件、电子、光电、光子和量子器件。
- 热学、力学、摩擦学、声学、压电、柔性器件和传感。
- 金刚石电极、电化学、催化、表面功能化和生物医学应用。
- 天然金刚石形成、包裹体、矿物学和相关地学研究。
- 类金刚石碳作为相邻方向收录，并标记为 `adjacent-dlc`。

排除珠宝、营销、体育、影视，以及纯数学或计算机语境中的 diamond graph、diamond norm 等结果。仅把金刚石压砧作为通用实验工具、而不研究金刚石材料或压砧性能的论文不进入 AI 精选；为保证召回率，其低置信度结果仍可保留在原始 RSS 中。

## 3. 总体架构

流水线由六个边界清晰的阶段组成：

1. **采集**：RSS、OpenAlex、Crossref 和 arXiv 适配器按时间窗口获取候选论文。
2. **规范化**：统一题名、摘要、作者、期刊、日期、DOI、URL、来源和来源 ID。
3. **去重**：优先使用规范化 DOI；缺少 DOI 时使用规范化题名与年份。
4. **规则召回**：使用宽松主题词和少量明确排除规则产生高召回候选集。
5. **AI 处理**：DeepSeek 判断相关性、分类、给出置信度并生成中文摘要。
6. **发布**：输出原始 RSS、AI RSS、HTML 摘要、状态和故障报告。

内部采用统一 `PaperRecord` 数据结构，来源适配器只负责产生该结构，筛选、去重、AI 和渲染模块不依赖具体来源实现。这样可以独立替换失效的 RSS 或 API，而不影响其余流水线。

## 4. 数据来源

### 4.1 RSS

以本地以下文件作为起点：

- `E:\Desktop\mcp\paper-feed\paper-feed\journals.dat`
- `E:\Desktop\mcp\paper-feed\paper-feed\期刊.txt`
- `E:\Desktop\mcp\paper-feed\paper-feed\fetch_failures.tsv`
- `E:\Desktop\mcp\paper-feed\paper\paper-search-semiconductor\期刊.txt`

复用其中材料、物理、半导体、纳米、器件、化学和生物医学相关来源。补充金刚石与碳材料专门期刊，以及金刚石论文高频出现的表面科学、等离子体、宽禁带半导体、量子技术和高压地学来源。每个新增地址必须通过真实 GET 和完整抓取验证，不能仅凭 URL 形式猜测。

RSS 故障按现有规范分类：稳定 404/410、稳定解析错误和已确认停用端点属于硬失败；超时、临时网络错误和空 feed 属于软失败。删除硬失败来源前优先寻找官方替代地址。

### 4.2 OpenAlex 与 Crossref

两个接口按最近更新时间增量查询，并以重叠时间窗口抵抗延迟收录。查询词族覆盖 `diamond material`、`CVD diamond`、`nanodiamond`、`diamond membrane`、`boron-doped diamond`、`diamond semiconductor`、`diamond photonics`、各类 vacancy center、`diamond electrode` 和 `diamond-like carbon` 等。

API 查询结果仍经过统一规则和 AI 复筛，不把数据库搜索排序当作最终相关性判断。接口限流时指数退避，并保存游标或最近成功时间，避免从头重抓。

### 4.3 arXiv

使用少量覆盖面广的查询词族，而不是为每个细分词各发一次请求。查询间串行限速，遇到 429 时停止本轮 arXiv 抓取并保留上一轮状态，避免重试风暴。

## 5. 规则筛选与去重

规则层只承担高召回初筛，不负责最终精准判断。

- 包含词分为材料形态、生长加工、缺陷量子、器件性能、化学表面和地学六组。
- 支持短语、同义词、连字符差异和常见缩写。
- 明确的非科研语义可直接排除；有歧义的候选交给 AI。
- DOI 统一转为小写并移除 URL 前缀。
- 无 DOI 条目对 Unicode、标点、空白和大小写规范化，再按题名与年份匹配。
- 同一论文多来源命中时合并来源列表，优先保留出版商 URL、较完整摘要和正式发表元数据。

`filtered_feed.xml` 保存规则命中的高召回结果。AI 未处理、低置信度或服务暂时失败不会导致候选从该 feed 消失。

## 6. DeepSeek 集成

DeepSeek 使用 OpenAI 兼容的 Chat Completions 接口。默认配置：

- Base URL：`https://api.deepseek.com`
- 模型：`deepseek-v4-flash-vision-exp`
- 密钥环境变量：`DEEPSEEK_API_KEY`

Base URL 和模型名存入公开配置；API Key 只存在于 GitHub Actions Secret。代码、配置、提交、测试夹具、日志和生成文件不得包含密钥或密钥片段。官方接口与模型说明：

- <https://api-docs.deepseek.com/guides/function_calling>
- <https://api-docs.deepseek.com/quick_start/pricing>

AI 返回严格 JSON，至少包含：`relevant`、`confidence`、`category`、`matched_topics`、`summary_zh` 和 `reason`。响应先做结构校验；无法解析时按失败处理，不猜测或丢弃候选。

主题分类为：

- `growth-processing`
- `films-membranes`
- `doping-defects`
- `quantum-color-centers`
- `electronics-optoelectronics`
- `thermal-mechanical-acoustic`
- `piezoelectric-sensing`
- `electrochemistry-catalysis`
- `nanodiamond-biomedical`
- `natural-diamond-geoscience`
- `adjacent-dlc`
- `other-diamond`

## 7. API 用量保护

第一次运行可能产生大量候选，因此 AI 处理与采集完全解耦：采集结果先持久化，AI 按固定配额逐日消费，未处理论文留在队列中。

默认硬限制：

- 首次数据库回溯窗口：30 天。
- 每日最多提交给 AI：40 篇。
- 每批论文：10 篇。
- 每次运行最多 AI 请求：5 次，包含失败重试。
- 每篇传入摘要最多：1200 字符。
- 单批筛选输出最多 4096 token，最终 HTML 摘要输出最多 8192 token。
- AI 每天只运行一次，默认北京时间 08:00。

第 5 次请求后无条件停止本轮 AI 处理。失败条目不标记为已完成，下一天继续处理。`ai_usage.json` 记录日期、候选数、成功数、请求数和 API 返回的 token 用量，不记录请求头、完整提示词或密钥。

## 8. 输出与状态

- `filtered_feed.xml`：规则层高召回 RSS，最多保留 2000 条。
- `ai_summary_feed.xml`：AI 判定相关的中文精选 RSS。
- `ai_summary.html`：按主题分组的最新一期中文摘要页面。
- `state.json`：来源游标、最近成功时间、论文去重键和 AI 待处理队列。
- `ai_usage.json`：每日 AI 使用统计。
- `fetch_failures.tsv`：来源失败分类和时间。

HTML 与 RSS 条目展示题名、中文摘要、分类、作者、期刊、日期、DOI、原文链接和来源。所有输出采用 UTF-8，并通过 XML/HTML 基础合法性检查。

## 9. 自动化与部署

GitHub Actions 使用两个独立工作：

1. `collect.yml` 每 6 小时执行采集、规范化、规则筛选、去重和原始 RSS 发布。
2. `summarize.yml` 每天北京时间 08:00 执行 DeepSeek 队列消费与摘要发布，也支持手动触发。

两个工作使用并发组避免同分支重叠运行。只有生成文件发生变化时才提交；推送前执行 rebase，降低计划任务与人工提交冲突的概率。GitHub Pages 从 `main` 分支发布 RSS 和 HTML。

新 GitHub 仓库需要用户创建，并在 `Settings -> Secrets and variables -> Actions` 中手动添加 `DEEPSEEK_API_KEY`。当前 GitHub 连接器不提供仓库创建和 Secrets 管理接口，因此密钥配置不通过自动提交完成。

## 10. 错误处理

- 单个 RSS 或数据库失败不会终止整批采集。
- 429、超时和 5xx 使用有上限的指数退避；稳定 404/410 记录为硬失败。
- 所有来源都失败时不覆盖上一版 feed。
- DeepSeek 认证失败、余额不足、限流、超时或 JSON 无效时保留上一版摘要和待处理队列。
- 状态文件采用临时文件写入后原子替换，防止中途退出造成损坏。
- 日志只记录来源、状态码、重试次数和简短错误，不记录密钥、Authorization 请求头或完整 AI 请求体。

## 11. 测试与验收

单元测试覆盖：

- 查询词和规则匹配，包括 diamond 的典型歧义。
- DOI 规范化、题名规范化和跨来源去重。
- RSS、OpenAlex、Crossref、arXiv 响应到 `PaperRecord` 的转换。
- AI JSON 校验、分类、失败重试和待处理队列恢复。
- RSS XML 生成、非法字符清理和 HTML 转义。
- 每日论文数与请求数硬限制，包括失败重试计数。

集成测试使用本地夹具，不依赖真实 API Key。发布前进行一次联网采集冒烟测试和一次使用 GitHub Secret 的手动摘要任务。验收标准：

- 至少一个 RSS 来源和一个数据库来源能进入统一 feed。
- 同一 DOI 的多来源记录只发布一次，并保留来源信息。
- 明显的非材料 diamond 语义不进入 AI 精选。
- 首次运行无论候选数量多少，AI 请求不超过 5 次。
- DeepSeek 故障不会清空现有摘要或丢失队列。
- Zotero 能订阅并正确显示题名、期刊、日期、DOI 和链接。

## 12. 实施边界

项目从干净的本地 Git 仓库开始，参考上游实现但不复制其生成数据、`vendor/`、`__pycache__/` 或用户特定配置。实施完成后再连接新的 GitHub 远端。任何密钥只由用户写入 GitHub Secret，绝不进入本地受版本控制文件。
