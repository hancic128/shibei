# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.7.1] - 2026-10-11

管理总览页「报告管理」卡片回归设计系统标准结构（纯前端样式修复）。

### 修复

- **卡片样式**：该卡片当初自绘标题块、绕过了设计系统的 `CardHeader`，导致标题 14px、描述 12px，且 `CardBody` 无上内边距（只有 `px-6 pb-6`），内容贴顶发挤。现改用 `CardHeader`（16px 标题 + 14px 描述），与设置页各卡片一致。
- **行内操作风格割裂**：「重跑」用 `secondary` 变体自带边框，与纯文字「删除」并排时不协调；两者统一为行内 `ghost`（重跑品牌色 / 删除玫红色），与「运行历史」的行内操作同款。
- **语言徽标顺序抖动**：徽标顺序随后端 `glob` 返回顺序变化，同一批数据里出现 `ZH EN` 与 `EN ZH` 混排；现固定中文在前。

### 新增

- **报告列表分页**：每页 8 条，底部显示「第 1–8 条 / 共 12 条」与上一页 / 页码 / 下一页，不足一页时整条不渲染；删除后条目变少会自动收敛页码。列表由 `ul` 改为表格（报告 / 更新时间 / 操作），复用此前闲置的 `admin.col*` 文案。

## [0.7.0] - 2026-10-11

### 新增

- **历史报告可删除 / 可重跑**：管理总览页（`/admin`）新增「报告管理」，列出全部历史报告（按基名合并中英版本），支持
  - **删除**：移除该日期（或全量总览）的全部语言版本，二次确认；任务运行中拒绝删除，避免边写边删。
  - **重跑**：用该日期已归档的帖子重新分析并覆盖该份报告（新增 CLI `--date YYYY-MM-DD`，读归档、不重新抓取、不更新增量进度）；全量总览行则触发一次全量分析。
  - 后端新增 `DELETE /api/reports/<name>`；`POST /api/run` 支持 `mode=date`（日期严格校验，拒绝 `2024-02-30` 之类）。
  - 报告写入改为原子写（tmp + replace），删除与读取并发时不再产生半截报告 / 500。

## [0.6.2] - 2026-09-18

修复 v0.6.1 遗留的白屏：前端资源 base 仍为构建期默认 `/shibei/`，根路径部署下 HTML 引用 `/shibei/assets/*` 被 nginx SPA fallback 成 HTML（MIME 错），页面无法加载。

### 修复

- 前端构建 `VITE_BASE` 改用 `/`（与 nginx 前缀、后端 `SHIBEI_BASE_PATH` 三者一致）。

## [0.6.1] - 2026-09-18

修复 v0.6.0 引入的部署回归：nginx 镜像构建时前缀写死为 `/shibei/`，而生产是根路径部署，导致线上 404。生产构建改用 `NGINX_PREFIX=/`（走 nginx-root.conf 直通）。

### 修复

- nginx 镜像前缀：CI 构建改用 `NGINX_PREFIX=/`（本地开发仍默认 `/shibei/`）。

## [0.6.0] - 2026-09-18

部署架构迁移到统一的 app-deploy 入口（无用户可见功能变化）。

### 变更

- **部署链路迁移**：业务仓库只推镜像到 ghcr；镜像分发到阿里云 ACR 与目标主机部署由 app-deploy 仓库统一执行。原腾讯云 TCR 与 Docker Hub 推送废弃。
- **版本变量统一**：`SHIBEI_VERSION` / `SHIBEI_TAG` 合并为 `TAG`（由 CI 按 git tag 名注入）。
- `/api/version` 返回值来源改为镜像构建时的 `TAG`（原 `VERSION`）。
- 仓库路径随 GitHub 账号更名：`Angryshark128/*` → `hancic128/*`。

### 新增

- `docker-compose.local.yml`：本地开发用 compose override（从源码构建）。

### 移除

- `.github/workflows/deploy.yml` 中的自建 SSH 部署、健康检查、版本比对、TCR 临时密码生成等步骤（迁移至 app-deploy）。
- 腾讯云 / Docker Hub 相关 secrets 依赖。

## [0.5.0] - 2026-09-16

报告支持中英双语切换；分析阶段对每条创意逐项评估打分，低质条目在生成时即被过滤，报告只保留评估后的结果。

### 新增

- **报告双语与语言切换**：界面与报告支持中/英切换，tab 标题、时区、错误文案随语言联动。
- **创意评估打分**：分析阶段对每个创意 / 痛点 / 机会逐条评分并给出概括评价。
- **前置过滤省 token**：按总分阈值分档输出（高质保留、中间档标注观察、低质不再输出），低质条目在模型输出阶段即被拦下，省去后续合并与渲染开销。
- **token 消耗日志**：逐批累计输入 / 输出 token，分析结束时打印摘要。

### 变更

- 报告优先按评估分档结构解析，缺少分档标记时回退按评分字段解析。

## [0.4.0] - 2026-09-10

报告结构收敛为「产品创意 / 用户痛点 / 潜在机会」三个文档（移除趋势洞察，来源不再显示标题、改为可点击链接）；报告页改为三级导航；修复 favicon 与悬浮组调色板问题。

### 变更

- **分析类别重组为三类**：`ideas`（产品创意）、`pain`（用户痛点）、`indie`（潜在机会）；移除「趋势洞察」。报告内仍按分类（H3）分组整理；来源条目改为不显示标题的可点击链接（`[来源](url)`）。
- **公开页改侧边栏布局**：品牌区（logo +「拾贝」+ slogan）与树状导航（日期可折叠展开 → 文档 → 分类）收进左侧固定侧边栏（窄屏为顶部区域），右侧为报告正文，左下角回到顶部悬浮按钮。移除页标题、「每日报告 / 全量总览」tab 与页内「管理」入口（管理页由 `/admin` URL 直接进入）。
- **报告标题改为日期**：正文首行由「拾贝 · 多来源分析」改为生成日期（如 `# 2026-09-09`）；一级标题为文档（产品创意 / 用户痛点 / 潜在机会），二级标题为分类。
- **生成逻辑省 token**：批次分析从「每批 × 每类各调一次」改为「每批一次输出全部类别」（帖子正文只发一次），合并与分类分组合并为每类一次调用，`BATCH_SIZE` 15→20；输入 token 约降 2/3、调用次数约降 60%+，并保留按类回退路径。

### 修复

- **favicon 在 SPA 深链下不显示**：`index.html` 用 `./favicon.ico` 相对路径，在 `/reports/<日期>` 等深链下会请求 `/reports/favicon.ico`（落 SPA 回退返回 HTML，浏览器拿不到图标）；改为根绝对路径，由 Vite 按部署 base 重写。
- **悬浮组调色板「点开即灭」无法选色**：鼠标移出组时 `relatedTarget` 为 null（触屏、窗口失焦、快速移动）会立即收起气泡；气泡打开期间不再因移出收起，仅点色块 / 再点调色板 / 点外部 / Esc 关闭。

## [0.3.0] - 2026-09-09

报告按日归档与按日导航、favicon 服务、抓取超时收敛；默认停用 Hacker News 来源。

### 新增

- **每日报告归档**：增量报告从覆盖式 `analysis_today.md` 改为按日归档 `data/analysis/YYYY-MM-DD.md`（每天一份、同日多次运行刷新），历史可回溯。
- **报告页按日导航**：Web 报告页新增「每日报告」视图——默认定位最新一天，支持 ◀▶ 逐日与日期胶囊切换；「全量总览」为独立 tab。API 报告列表区分 `daily`/`full` 并按日期倒序（`latest_daily` 供默认定位），详情接口白名单校验日期格式。
- **favicon.ico**：16/32/48 多尺寸 favicon（ICO 内嵌 PNG）；新增 `/favicon.ico`、`/favicon.svg` 静态服务路由（此前 svg 从未被 Flask 托管，浏览器一直无图标）。
- **部署参数化补登**：docker-compose / docker/nginx.Dockerfile 前缀参数化（`SHIBEI_BASE_PATH` / `SHIBEI_TAG`，09-08 生产环境已使用）；Dockerfile 与 compose 支持 `PIP_INDEX_URL` 自定义 pip 源（国内主机可用镜像站，如腾讯云）。

### 修复

- **运行历史开始时间错乱（显示 1970-01-22）**：任务时间戳为秒级 epoch，前端按毫秒解析导致；`time.ts` 统一做秒→毫秒归一化。
- 「没有新增帖子」的提示改为指向最近一份每日归档。

### 变更

- **默认停用 Hacker News 来源**（`config.json` `enabled=false`，设置页可随时重新启用）：增量任务曾因 HN 逐条评论串行抓取 + 30s 超时 × 3 重试，单源耗时约 141 分钟（一次增量任务总时长 211m）。
- **抓取超时收敛防黑洞**：全局网络超时 30s → 10s；HN 评论抓取单帖上限 25 条、失败降为重试 1 次；V2EX `max_retries` 降为 1。

## [0.2.0] - 2026-09-07

从「V2EX 单源 CLI」到「多来源 + Web 界面 + Docker 部署」的完整发布。

### 新增

- **多来源接入**（sources/ 新增 5 个实现 + 注册 + config 配置节）
  - Hacker News：Firebase JSON API，列表端点二次请求详情，评论 BFS 递归展平，deleted/dead 跳过。
  - Lobste.rs：JSON API，按标签分页；`submitter_user`/`commenting_user` 为字符串用户名，评论扁平列表。
  - Dev.to (Forem)：JSON API + 自定义 `Accept: application/vnd.forem.api-v1+json`，评论树递归展平。
  - 少数派：RSS 2.0 feed，无评论接口。
  - Product Hunt：RSS feed，无评论接口。
- **base.py 通用助手**：`http_get_json` 支持 `extra_headers`；新增 `http_get_xml` / `parse_atom_feed`（Atom 与 RSS 2.0 兼容）/ `strip_html` / `iso_to_unix`（ISO 8601 与 RFC 2822）。
- **爬取与 CLI 增强**：来源必须出现在 config 且 enabled 才启用；`run_crawl` 来源间并行（`ThreadPoolExecutor`）；进度日志带来源标识；Ctrl+C 优雅退出（原子写 + 断点续传）；分析结论强制中文。
- **Web 界面与 Docker 部署**（`web/` + `frontend/` + Dockerfile + docker-compose + nginx `/shibei/` 子路径反代）
  - 浏览器查看/触发：报告页（全量/今日报告 Markdown 渲染、数据概览）、运行历史（状态 + 实时日志弹窗）、设置页。
  - 主动触发：增量 / 全量分析按钮；analyzer 子进程运行（`web/runner.py`），单任务并发（运行中触发 409），状态索引 + 日志落盘 `data/tasks/`，重启容器自动标中断。
  - 停止任务：运行中任务可随时停止（SIGTERM → `interrupted`，已抓数据保留续传）。
  - 登录保护：scrypt 密码哈希 + 签名会话 cookie；初始账号 `SHIBEI_USERNAME` / `SHIBEI_PASSWORD`。
  - AI 配置：base_url / model / max_tokens / API Key 界面可存（Key 存 `data/web_secrets.json` 0600）；**连通性测试按钮**（一次最小对话，展示服务商错误）。
  - 每日定时调度（`web/scheduler.py`）：**默认每天 00:00 增量分析**，可开关、可设时间（5 分钟步进），同天只触发一次（last_fired 落盘）。
  - Webhook 通知（`web/webhook.py`）：任务完成（成功/失败）时 POST JSON 到配置地址，支持自定义 Header Key + Token，设置页可发测试。
  - 前端（React 18 + Vite + Tailwind，遵循前端约束规范）：5 主题色 × 明暗、中英双语、悬浮控制按钮组（默认折叠）、favicon；时区仅上海/UTC。
  - nginx 配置内嵌镜像（`docker/nginx.Dockerfile`），`SHIBEI_BASE_PATH` 可配置子路径。
- **运行时依赖**：pyproject 声明 flask + waitress（仅 Web 部署需要；CLI 仍零第三方依赖）。

### 修复

- 悬浮按钮组：鼠标移向主题色气泡不再误收起（relatedTarget 判定）；展开/折叠位移 + 透明度动画。
- 报告任务结束状态区分：`interrupted`（停止/中断）不再误报失败。

### 计划（见 [Unreleased]）

## [Unreleased]

### 新增

- **中英双语报告**：报告内容可选中文 / English——语言由 CLI `--lang` / `ANALYZE_LANG` 或 Web 设置页「报告语言」决定（默认中文）。英文报告存 `analysis.en.md` / `YYYY-MM-DD.en.md`（中文沿用原名）；报告页按界面语言自动显示对应版本，暂缺该语言时回退显示并提示。任务日志、CLI 提示随语言输出，分析缓存按语言隔离；每次分析只生成一种语言，LLM 成本不变。
- **界面标语**改为「独立开发者的社区情报」/「Community Intelligence for Indie Developers」，点明面向独立开发者。

### 修复

- **界面语言切换不彻底**：浏览器 tab 标题（`<title>`）、显示时区选项（改为语言无关的 `Asia/Shanghai (UTC+8)` / `UTC (UTC+0)`）、网络错误提示与关闭/密码可见性等 `aria-label` 此前在英文界面下仍显示中文，现全部跟随语言。
- **英文标语被截断**：报告页侧边栏与顶栏的 slogan 由单行截断改为可换行，完整显示。
- **报告语言与界面语言不一致**：右下角语言切换按钮现在会同步设置「报告语言」（设置页可再手动覆盖）。

### 计划

- 更多来源：Reddit（OAuth 商用授权）、即刻（逆向）等门槛更高的社区
- 报告增强：分类标签、历史对比、导出其它格式
- list_nodes 增强（SSPai / Product Hunt 无节点概念）

## [0.1.0] - 2026-08-02

初始版本：V2EX 单一来源、命令行工具、多来源可扩展架构。

### 新增

- **爬虫**（crawler.py + sources/）
  - 来源抽象层：`Source` 基类、`http_get_json` 通用助手、`SOURCES` 注册表。
  - V2EX 来源：帖子/回复/节点抓取，归一为统一帖子结构。
  - 今日列表缓存 + 分页去重 + 断点续传（已存在跳过）。
  - 增量爬取（`--today`，按 `last_crawl`）。
  - `crawler.py list` 列节点。
- **分析**（analyzer.py）
  - 单一入口：自动爬取 → 自动分析 → 打印报告绝对路径。
  - 四个分析维度（创意/痛点/独立开发/趋势），批次 × 类别并行，run_id 缓存。
  - 层级合并、`[#id]` 锚点 → 可点击原帖链接还原。
  - 增量 / 全量模式（`--full`）。
  - OpenAI 协议 `call_api`：错误透传、4xx 直报、429/5xx 指数退避重试。
- **配置**：config.json 按来源分节 + `llm` 节；`OPENAI_API_KEY` / `OPENAI_BASE_URL` 必填，无内置默认。
- **工程**：uv、ruff、pyright、pytest（45 用例）、零运行时依赖、MIT 许可。

### 修复
- 无。
