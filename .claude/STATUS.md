# 拾贝 · 项目状态

## [2026-10-11] 报告管理卡片 UI 修复（v0.7.1）

### 现状
- **起因**：用户反馈「报告管理」卡片样式不对——字体偏小、内边距缺失、重跑按钮带边框、列表无分页。
- **根因**：该卡片当初手搓标题块（`<h2 class="text-sm">` + `<p class="text-xs">`）塞进 `CardBody`，绕过了设计系统的 `CardHeader`（16px 标题 + 14px 描述 + `pt-6`），且 `CardBody` 本身只有 `px-6 pb-6`，故内容贴顶。
- **改动**：① 改用 `Card` + `CardHeader`；② 列表由 `ul` 改为表格（报告 / 更新时间 / 操作，启用此前闲置的 `admin.colReport/colUpdated/colActions`），表头灰带、行悬停对齐「运行历史」表；③ 「重跑」由带边框的 `secondary` 改为行内 `ghost`，与「删除」同款（brand / rose）；④ 新增 `frontend/src/components/ui/pagination.tsx`（8 条/页，区间文案 + 上一页/页码/下一页，单页不渲染，删后自动收敛页码）；⑤ 语言徽标固定 zh 在前（原随后端 `glob` 顺序抖动）；⑥ 加载态骨架、空态内边距。
- **验证**：前端 `tsc -b && vite build` 通过；真机截图核对中文 / 英文 / 暗色三种。
- **验证方式说明**：截图需登录，而改测试凭据的操作被权限分类器拦截（拒绝轮换线上管理员密码）。改为 `rsync` 复制后端到 `/tmp` 跑隔离副本（`SHIBEI_PASSWORD` 引导独立凭据 + 造 12 份陪测报告），真实 `data/` 全程未被写入，副本与端口已清理。
- 质量门：无 Python 变更；前端构建通过。

### 下一步
- 后续若列表继续变长（一年 365 份日报），可考虑加日期区间筛选 / 关键词搜索，当前仅分页。

## [2026-10-11] 历史报告可删除 / 可重跑（后台）

### 现状
- **需求**：历史报告可在后台删除或重跑。选定实现——「重跑」= 按日期用**已归档帖子**重算该天每日报告；入口**并入**管理总览页（`/admin`），不新开页面。
- **analyzer.py**：新增 CLI `--date YYYY-MM-DD`（与 `--full` 互斥）。`regenerate_report()` 只读归档帖子（按 `created` 落在该本地日 `[00:00, 次日 00:00)` 过滤，次日零点用 `datetime + timedelta(days=1)` 求，避开 DST）、重新 LLM 分析、覆盖写回 `YYYY-MM-DD[.en].md`；**不抓取、不写 state**（不影响增量进度）。prompt 增量措辞经 `_PERIOD` 全局改用目标日期。`write_report` 改原子写（tmp + replace），并支持 `date` 参数。
- **web**：`runner.TaskManager.start(mode, …)` 支持 `mode="date"`（argv 追加 `--date`，任务记录含 `date`）；`POST /api/run` 接受 `mode=date` 并严格校验日期；新增 `DELETE /api/reports/<name>`（需登录 + 同源，删该名全部语言版本，任务运行中返回 409）。`_report_list` / `report_detail` 对文件消失（与删除并发）容错。
- **前端**：管理总览页新增「报告管理」卡片——列出报告（按基名合并 zh/en），每行「重跑 / 删除」按钮，删除走二次确认；语言徽标、更新时间、体积。`api.run` 支持 `date`，新增 `api.deleteReport`；`TaskItem.mode` 增 `date`；运行历史显示「按日重算」徽标。
- 质量门：pytest **198 绿**（新增 analyzer 6、runner 4、web 5 用例）；`ruff check` / `ruff format --check` / `pyright` 0 错；前端 `tsc -b && vite build` 通过。

### 下一步
- 如需删除「单语言」而非整份报告，再评估（当前删除即移除该日期中英全部版本）。
- 历史报告重跑依赖归档帖子仍在 `data/<来源>/<节点>/`；若数据被清理则重算结果为空（提示「没有已归档的帖子」）。
- 其余见下条（09-16 遗留的部署凭据 + 09-13 的 V2EX 覆盖率）。

## [2026-09-16] 创意评估打分 + tag 触发部署（v0.5.0）

### 现状
- **创意评估打分**：分析阶段对每条创意 / 痛点 / 机会逐条评分并给出概括评价，报告只输出评估后的结果，不再直接抛原始条目。
- **前置过滤省 token**：按总分阈值分档（高质保留 / 中间档标注观察 / 低质不再输出），低质条目在模型输出阶段即被拦下；逐批累计 token 并在分析结束时打印摘要。
- **部署自动化（本次新增）**：`.github/workflows/deploy.yml`——打 `v*` tag（或手动 `workflow_dispatch`）→ runner rsync 源码到 sh 主机 `/root/shibei` → sh 本地 `docker compose up -d --build`。不推送镜像（sh 拉不动官方源），与 acme-cron 同模式。
- **数据保护**：rsync 排除 `.env` / `data/` / `.v2ex-grey-backup` 等。已逐文件核对服务器与仓库差异——服务器独有文件**只有 `.env`**（在排除列表内），`data/` 不被触碰，不重置数据。
- **凭据约定**：复用本机统一命名 `HOST_SH_IP` / `HOST_SH_USER`（Variables）+ `HOST_SH_SSH_KEY`（Secrets，base64 私钥），与 acme-cron / tj-quant 同名。**注意：GitHub secrets 是仓库级的，值不可读也不可跨仓库共享，shibei 仓库需单独配置一次。**
- **当前阻塞**：shibei 部署未跑通——Deploy #1 在「配置 SSH」6 秒失败，根因是 `HOST_SH_IP` 为空（`ssh-keyscan -H ""` 退出码 1，已复现）。配置好三项凭据后重打 tag 即可。
- **CI 修绿**：`ruff format --check` 此前失败（analyzer.py 函数签名换行漂移），已 `ruff format` 修正（`d86b8de`，CI 已通过）。
- 质量门：pytest 178 绿、`ruff format --check` / `ruff check` 0 错。

### 下一步
- 在 shibei 仓库 Settings → Secrets and variables → Actions 补齐三项凭据后重跑部署
- 其余见下条（09-13 遗留的 V2EX 覆盖率等）

## [2026-09-13] 抓取请求量收敛：V2EX 分页参数无效

### 现状
- **实测：V2EX API v1 分页参数无效**。`/api/topics/show.json?node_name=X&p=N` 的 `p`（含 `page`）取 1/2/3/4、四个节点，返回的都是**逐 id 相同**的 10 条（HTTP 200）。`pages_per_node: 6` 每节点白跑 5 次请求。
- **已改**：`config.json` 的 v2ex `pages_per_node` 6 → 1，请求数 96 → 16（16 节点）/ 次运行，内容不变；单节点实测 6 页 26.4s → 1 页 0.11s。
- **已部署到线上（09-13）**：生产权威配置是部署机的 `data/web_config.json`——容器启动时由它反写 `/app/config.json`，analyzer 读的是后者；宿主 `/root/shibei/config.json` 不挂载、改了不生效（镜像里的那份启动即被覆盖）。已改该文件的 v2ex `pages_per_node` 并 `docker restart shibei-web` 重载，健康检查与 `/api/reports` 均 200。但生产 v2ex 当前处于**停用**状态（09-11 设置页关闭，生产实际跑 lobsters / devto / sspai），故本次无即时提速，重新启用时生效。
- **顺带修好缓存**：命中条件 `len(cached) >= pages * TOPICS_PER_PAGE` 在 6 页时需 30 条，而每节点只有 10 条 → V2EX 今日缓存**从未命中**；改 1 页后同日重跑 0 请求（实测）。
- **未采纳「按时间提前停止分页」**：三个分页来源前提均不成立（V2EX `p` 无效；Dev.to 页内不按时间序且跨页区间重叠；Lobste.rs 本机网络不可达，且 `hottest` 按分数排序）。详见 DECISIONS。
- 质量门：pytest 126 绿。本机无 uv / ruff / pyright，未跑静态检查——本次仅改 config.json，无 Python 变更。

### 下一步
- **V2EX 覆盖率**（本次新发现）：每节点只能取到最新 10 帖，繁忙节点一天新帖可能超过 10 条。补齐需换 V2EX API v2（需用户提供 Personal Access Token）或走节点页面分页，另行评估。
- 报告增强（P2）；更多来源（Reddit / 即刻）
- 防爬机制（P2）

## [2026-09-12] 中英双语（界面 + 报告内容）

### 现状
- **报告语言可切换**：语言由 CLI `--lang` / `ANALYZE_LANG` 或 Web 设置页「报告语言」（`data/web_report_lang.json`）决定，默认中文。分类表 `CATEGORIES_BY_LANG`、prompt 语言指令（zh「一律用中文回答」/ en「Answer in English」）、空结果词（无 / None）、报告模板、CLI 与任务日志全部语言化；批次缓存按语言隔离（`{run_id}_b{i}_multi_{lang}.json`）。
- **报告文件命名**：英文 `analysis.en.md` / `YYYY-MM-DD.en.md`，中文沿用原名（向后兼容）。`/api/reports` 每项带 `lang`；`/api/reports/<name>?lang=` 按语言读取；新增 `GET/PUT /api/report-lang`。
- **手动与定时任务统一**：`_run_env()` 注入 `ANALYZE_LANG`，任务索引记录 `lang`（运行历史显示语言徽标）。
- **界面**：设置页新增「报告语言」卡片；报告页按界面语言取对应版本，暂缺该语言时回退显示并提示；标语改为「独立开发者的社区情报」/「Community Intelligence for Indie Developers」。
- **已上线**（09-13）：部署机 `172.81.241.149` 的 `/root/shibei`（`SHIBEI_TAG=apps`、根路径、端口 18080，外部入口 `https://shibei.hancic.site/`）。代码用 rsync 同步（排除 `.env` / `config.json` / `data`），部署机备份 `/root/shibei-backup-20260913-1124.tar.gz`（上一版 `-20260912-2359`）。
- **语言切换彻底化**（commit `1de4e30`）：浏览器 tab 标题、显示时区选项（label 改为语言无关的 `Asia/Shanghai (UTC+8)`）、网络错误提示与关闭/密码可见性等 aria-label 全部跟随语言；英文标语不再被截断（改换行）；右下角语言切换按钮同步设置「报告语言」。
- 质量门：pytest 126 绿、ruff / pyright 0 错、tsc + vite build 过；web 接口冒烟（报告列表/详情语言、lang 接口鉴权与校验、任务 lang、`ANALYZE_LANG` 注入）全过。

### 下一步
- 需要历史报告双语并存时，用另一语言重跑对应分析（当日无新帖可先 `--full`）
- 报告增强（P2）；更多来源（Reddit / 即刻）

## [2026-09-07 · 晚] v0.2.0 发布 + 生产部署 wjbd.site/shibei/

### 现状
- **v0.2.0 已打 tag**：多来源 + Web + Docker 全量发布（CHANGELOG 已整理）。
- **生产部署**：my-nginx（/root/nginx/sites/usa.wjbd.site.conf）加 `location /shibei/` → host.docker.internal:18080（shibei-nginx），`https://wjbd.site/shibei/` 可用。改配置经 `docker run -v /root/nginx:/ng` 写（conf.d 容器内 ro；本机 daemon 只认 /root 视图）。旧配置备份 .bak-20260907。
- **每日定时调度默认开启 00:00**（web/scheduler.py，20s 心跳守护线程，last_fired 落盘防重启重复）；Webhook 任务完成通知（header key + token + 测试）。
- **停止任务**：POST /api/tasks/<id>/stop（SIGTERM → interrupted）；报告页横幅 + 运行历史行均可停止（ConfirmDialog）。
- **UI 调整**：悬浮组配色气泡可点（relatedTarget 判定）、折叠动画；时区仅上海/UTC 下拉向上；AI 卡加测试按钮（/api/config/test 用当前表单值最小对话）；来源节点描述自动换行；移除「清除 Key/Token」UI；favicon.svg。
- **事故教训**：V2EX API 黑洞（30s 超时×重试串行）会让全量任务看起来卡死 → 增加停止能力；另冒烟 fake key 曾污染 data/web_secrets.json（已清理，用户需重填真实 key）。
- 质量门：ruff/pyright 0 错、pytest 108 绿、tsc + vite build 过；容器 e2e（调度触发/Webhook 送达/stop→interrupted）全过。

### 下一步
- 用户重填真实 API Key（fake 已清）
- 报告增强（P2）；更多来源（Reddit/即刻）
- 防爬机制（P2）：遵守 robots.txt + 防御性措施（限速、指数退避、随机抖动、并发上限），避免过度占用目标站点带宽/触发封锁；现状仅有 request_delay 与列表缓存

## [2026-09-07] Web 界面 + Docker 部署

### 现状
- **Web 后端**（`web/app.py` + `web/runner.py`，Flask + waitress）
  - 登录保护：scrypt 密码哈希 + 签名会话 cookie（path 随 `SHIBEI_BASE_PATH`）；初始账号 `SHIBEI_USERNAME` / `SHIBEI_PASSWORD`，未设密码自动生成打印日志。
  - AI 配置：base_url / model / max_tokens 落 `data/web_config.json` 并同步仓库 `config.json`（CLI 与 Web 一致）；API Key 存 `data/web_secrets.json`（0600）不进仓库。
  - 来源开关（PUT /api/sources/{name}）、改密、`/api/reports|tasks|run` 等接口。
  - 任务运行：analyzer 子进程，单任务并发（运行中触发 409），状态索引 + 日志落 `data/tasks/`，重启容器标 interrupted。
  - CSRF：JSON 写接口校验 Origin hostname（nginx `$host` 无端口，勿用 netloc 全等比较）。
- **前端**（`frontend/`，React 18 + Vite + Tailwind，遵循 Trilium 前端约束规范）
  - 页面：登录（居中卡片）/ 报告（概览统计 + 全量/今日报告 + 增量/全量触发 + 运行 banner）/ 运行历史（表格 + 日志 Modal）/ 设置（AI/来源开关/改密/时区）/ 帮助。
  - 令牌体系：5 主题色（indigo 默认）× 明暗双轴，CSS 变量 RGB 通道 + `<alpha-value>`；悬浮控制按钮组（默认折叠，hover 展开）；中英双语 i18n；dayjs 6 时区。
  - 构建：`VITE_BASE=/shibei/` 产物进镜像；tsc 全绿、vite build 通过。
- **部署**：Dockerfile（node 多阶段 → python:3.12-slim）+ docker-compose（web + nginx 反代 `/shibei/`）；nginx 配置**内嵌镜像**（docker/nginx.Dockerfile）——本机 daemon 对 `/home/shark` 下单文件 bind 挂载异常（文件被当目录），改 COPY 规避。
- **质量**：ruff / pyright 0 错；pytest 108 全绿（web 冒烟另用 API 脚本 15 项 + docker 内 e2e：子路径页面/静态/登录/cookie path/配置/报告/任务 409/真实 analyzer 子进程日志均过）。
- 注：uv.lock 已含 flask/waitress（pyproject dependencies 更新）；config.json 的运行时同步由 `data/web_config.json` 驱动（本地直接跑 web 会改仓库 config.json → 用后 git checkout 还原）。

### 计划
1. 更多来源：Reddit（OAuth）、即刻（逆向）等。
2. cron 定时部署（Web 触发已就绪）。

### 待办
- [x] Docker 镜像 + Web 界面（查看/触发/配置/登录/子路径）— P0
- [ ] 生产部署（目标机 `docker compose up -d --build` + 外层 HTTPS）
- [ ] list_nodes 增强（SSPai / Product Hunt 无节点概念）— P4
- [ ] 报告增强：分类标签、历史对比、导出其它格式 — P2
- [ ] v0.2.0 发布（README/CHANGELOG 已更新待 tag）

## [2026-08-03] 多来源接入（5 个新来源）

### 现状
- **新增来源**：Hacker News、Lobste.rs、Dev.to (Forem)、少数派 (SSPai)、Product Hunt 已实现并在 `sources/__init__.py` 注册，config.json 增加对应配置节（均 enabled）。
- **通用助手**：base.py 新增 `http_get_xml` / `parse_atom_feed`（Atom 与 RSS 2.0 兼容）/ `strip_html` / `iso_to_unix`（ISO 8601 与 RFC 2822）；`http_get_json` 支持 `extra_headers`（Dev.to 用 Forem header）。
- **按真实 API 适配**（指南与实测不符处）：
  - Lobste.rs 的 `submitter_user` / `commenting_user` 是**字符串用户名**而非对象；评论为扁平列表（children 多为 null），`comment_plain` 优先于 `comment`(HTML)。
  - 少数派 feed 实为 **RSS 2.0**（指南称 Atom），`pubDate` 为 RFC 2822 → `iso_to_unix` 同时支持两种格式。
  - Dev.to 评论 id 字段为 `id_code`（非 `id`），且带 `children` 嵌套。
  - Product Hunt 未实测（尊重用户拒绝），解析器兼容 Atom / RSS 2.0 两种形态。
- **行为调整**：`get_sources` 改为来源必须出现在 config 配置节且 `enabled: true` 才启用（此前缺失配置节默认启用，多来源后是隐患）。详见 DECISIONS。
- **测试**：101 用例全绿（新增 56 个：base 助手 + 5 来源归一化/分页/回复展平），ruff / pyright 全绿。
- 注：lobsters 配置节点比指南多加了 `hottest`（热门首页，代码本就支持该路径）。

### 计划
1. 更多来源：Reddit（OAuth）、即刻（逆向）等。
2. Docker 部署。

### 待办
- [x] 多来源接入（HN / Lobste.rs / Dev.to / 少数派 / Product Hunt）— P0
- [x] base.py 通用助手（extra_headers / RSS 解析 / HTML 去标签 / 时间戳）— P0
- [ ] Docker 部署 — P3
- [ ] list_nodes 增强（SSPai / Product Hunt 无节点概念）— P4

## [2026-08-02] 定位明确 + v0.1.0 发布

### 现状
- **定位**：面向独立开发者的社区情报工具（README / design / pyproject 已统一）。
- **环境声明**：README 新增「环境要求」（Python 3.10+ 零运行时依赖 / uv 仅开发 / 网络与 LLM 配置要求）。
- **已发布**：GitHub 仓库 `hancic128/shibei`（Public），main 分支已推送，tag `v0.1.0`。
- 注：本环境 github.com HTTPS 不通，push 走 SSH over 443（`ssh.github.com:443`）。
- 45 测试 / ruff / pyright 全绿；pre-commit 钩子全过。

## [2026-08-02] 开源标准完善

### 现状
- **设计文档重写**：docs/design.md 已按当前实现完全对齐（单一入口、run_crawl、URL/Key 必填、max_tokens、错误透传、[#id] 锚点链接还原）。
- **开源标准文件**：LICENSE(MIT)、README 重写（特性/快速开始/配置/扩展/路线图）、CONTRIBUTING.md、CHANGELOG.md。
- **工程配置**：pyproject 元数据（license/classifiers/URLs）、.pre-commit-config.yaml（local 钩子：ruff/pyright/pytest，离线可用）、.editorconfig、.github（CI + issue/PR 模板）。
- **已 git init 并提交基线**：27 个文件入库。
- 注意：pyproject `[project.urls]` 用 `<your-org>` 占位，发布前替换为真实仓库地址；README 徽章同理。
- 注意：V2EX API 大量爬取后可能临时 403/不可达（限流 ~600 次/小时），需等待重置。

### 计划
1. Docker 部署。
2. 更多来源接入。
3. 发布到真实 GitHub 仓库（替换占位 URL）。

### 计划
1. P3：Docker 部署。

### 计划
1. P2：分析侧缓存与层级合并已在 P1 内实现；剩余可选优化。
2. P3：Docker 部署。

### 待办
- [x] 重写 design.md，落实多来源抽象 — P0
- [x] crawler.py 基础抓取 — P0
- [x] 帖子 JSON 统一数据结构 — P0
- [x] 来源抽象层（base/v2ex/注册表）— P0
- [x] analyzer.py 单批次分析（4 分类 + prompt + 链接还原）— P1
- [x] 增量模式（分析侧 since 过滤 + last_analysis 更新）— P1
- [x] 分析侧 run_id 缓存 + 层级合并 — P2（顺带完成）
- [ ] Docker 部署 — P3
- [ ] list_nodes / 节点浏览增强 — P4
