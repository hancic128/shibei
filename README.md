# 拾贝

**面向独立开发者的社区情报工具**：从 V2EX 等社区抓取帖子，调用 LLM 提炼**产品创意 / 用户痛点 / 潜在机会**，输出带可点击来源链接的 Markdown 报告，帮你低成本发现「值得做的东西」。

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![CLI](https://img.shields.io/badge/CLI%20deps-0-brightgreen) ![Web](https://img.shields.io/badge/Web-Flask%20%2B%20React-blue)

**在线体验**：[shibei.hancic.site](https://shibei.hancic.site/)（报告页免登录，管理后台 `/admin` 需登录）

## 特性

- **为独立开发者而生**：四类洞察直指「做什么、做给谁、值不值得做」——创意点子、用户痛点、低门槛机会、技术趋势。
- **单一入口**：`analyzer.py` 一个命令自动完成「爬取 → 分析 → 打印报告绝对路径」，无需手动干预是否爬取。
- **自动增量 / 全量**：有数据时只抓新增、只分析新增；首次运行或数据为空时自动全量。
- **来源可插拔**：新增社区只需实现一个来源类并注册，主流程与分析模块不动。
- **CLI 零第三方依赖**：命令行工具只用标准库 `urllib`，开箱即跑；Web 界面独立提供，不影响 CLI。
- **LLM 自带 URL + Key**：只支持 OpenAI 协议（DeepSeek / OpenRouter / Ollama / vLLM 等均兼容），URL 与 Key 缺失即退出，不误发到官方端点。
- **可点击来源**：报告每条洞察的「来源帖子」是代码还原的真实原帖链接，不经过 LLM，杜绝杜撰 URL。
- **中英双语**：界面、报告内容、任务日志均可切换中文 / English。分析语言由 CLI `--lang` / `ANALYZE_LANG` 或 Web 设置页「报告语言」决定；报告页按界面语言显示对应版本，暂缺时回退并提示。
- **省成本**：今日列表缓存 + 分析 run_id 缓存，重跑不重复请求；正文/回复截断控制 token。

## 环境要求

| 项 | 要求 |
|---|---|
| **Python** | 3.10+（**运行时零第三方依赖**，仅标准库 `urllib`） |
| **开发工具**（可选） | [uv](https://docs.astral.sh/uv/) —— 仅开发/安装 dev 工具时需要；直接运行无需 |
| **网络** | 能访问来源社区 API（V2EX / Hacker News / Lobste.rs / Dev.to / 少数派 / Product Hunt）用于爬取；能访问你自带的 LLM API（OpenAI 兼容）用于分析 |
| **LLM 配置** | `OPENAI_API_KEY`（环境变量，必填）；`OPENAI_BASE_URL` 与 `ANALYZE_MODEL` 必填（环境变量或 config.json `llm` 节二选一） |

> 运行时零依赖：CLI 装好 Python 后可直接 `python3 analyzer.py` 运行；uv 与 dev 依赖（ruff / pytest / pyright / pre-commit）仅用于开发与测试。Web 部署用 Docker，见下文。

## 工作原理

```
V2EX (或其他社区) ──爬取──▶ data/{source}/{node}/{id}.json ──分析──▶ LLM 提炼 4 类洞察
                                                                        │
                                                                        ▼
                                                   data/analysis/analysis.md（可点击原帖链接）
```

1. 爬虫抓取帖子与回复，归一为统一 JSON 结构。
2. 分析模块按 4 个维度（创意/痛点/独立开发/趋势）分批并行调 LLM。
3. 层级合并去重 → 代码还原来源链接 → 输出报告并打印绝对路径。

## 快速开始

```bash
# 1. 安装（零运行时依赖，仅 dev 工具）
uv sync

# 2. 配置 LLM（用户自带 URL + Key + 模型，三者必填）
export OPENAI_API_KEY=sk-xxx
# 下面的 base_url / model 默认取 config.json 的 llm 节，也可用环境变量覆盖：
#   export OPENAI_BASE_URL=https://api.deepseek.com/v1
#   export ANALYZE_MODEL=deepseek-v4-flash
# 常用服务商组合见「常用 OpenAI 协议 API 示例」

# 3. 分析（唯一入口：自动爬取 + 分析 + 打印报告路径）
uv run python analyzer.py

# 强制全量重分析
uv run python analyzer.py --full

# 生成英文报告与英文日志（等价于 ANALYZE_LANG=en）
uv run python analyzer.py --lang en

# 按日期重算某天的每日报告（读已归档帖子，不重新抓取、不影响增量进度）
uv run python analyzer.py --date 2026-08-03
```

首次运行会自动全量爬取并全量分析；之后每天跑一条 `analyzer.py` 即可。

### 输出示例

控制台日志（首次运行 / 数据为空时自动全量，最后打印报告绝对路径）：

```text
$ uv run python analyzer.py
首次运行或数据为空，自动全量爬取 ...
[V2EX] 新增 20 帖
共 20 个帖子待分析（来源: v2ex(programmer, python)）...

报告已写入：/Users/shark/Project/shibei/data/analysis/analysis.md
```

生成的报告 `analysis.md`（每条来源为可点击的原帖链接）：

```markdown
# 拾贝 · 多来源分析

来源: v2ex(programmer, python)

基于 20 个帖子自动生成

## 产品创意

- 把 Python 脚本编译成无依赖的单文件可执行工具 — [来源](https://www.v2ex.com/t/1224588)
- 在线 Python 编辑器 + 运行终端 — [来源](https://www.v2ex.com/t/1226355)

## 用户痛点

- 申请 TG API 用 Google Voice 一直失败 — [来源](https://www.v2ex.com/t/1229505)

## 潜在机会

- 开源的 AI 文本拟人化工具集，适合独立开发者推广 — [来源](https://www.v2ex.com/t/1213910)
```

> 说明：以上为示例输出；实际内容由你的 LLM 从抓取的帖子中提炼。

## Web 界面（Docker 部署）

自带浏览器界面：**查看报告、手动触发分析、配置 AI 与来源开关**，登录保护，可部署在任意站点子路径（nginx 反代）。

```
浏览器 ── /shibei/ ──▶ nginx（反代，剥前缀）──▶ Flask(:8000) ──▶ analyzer 子进程 ──▶ data/
                └── 前端静态资源（构建进镜像）
```

### 功能

- **登录保护**：用户名 + 密码（scrypt 哈希落盘），会话 cookie；登录页右下角可切换主题/语言。
- **报告**：全量报告与今日增量报告（Markdown 渲染，来源帖子可点击跳原帖）；数据概览统计。
- **报告管理**：管理总览页可删除历史报告，或按日期重跑（用该日已归档的帖子重新分析并覆盖该份报告，不重新抓取）。删除会移除该日期全部语言版本。
- **主动触发与停止**：增量分析 / 全量分析按钮，运行中实时日志，任务可随时停止（已抓数据保留下次续传）。
- **每日定时调度**：默认每天 00:00 自动增量分析，可开关、可改时间（5 分钟步进）。
- **Webhook 通知**：任务完成（成功/失败）时向自定义地址发送 JSON，支持 Header Key + Token，界面可发测试。
- **AI 配置**：接口地址 / 模型 / API Key / 最大 Token 界面保存，支持连通性测试（展示服务商原始错误）；API Key 只存本机（`data/web_secrets.json`，0600）。
- **来源开关**：停用不需要的社区。
- **主题**：5 主题色 × 明暗，中英双语（界面 + 报告内容），符合[前端约束规范]设计令牌体系。
- **报告语言**：设置页可选中文 / English，决定新分析生成的报告与任务日志语言；切换界面语言时会同步更新此项（设置页可再手动覆盖）。历史报告保持原语言，报告页自动回退显示并提示。

### 快速开始

```bash
# 1. 配置初始管理员密码（必填；其余可用默认）
cp .env.example .env          # 编辑 SHIBEI_PASSWORD
# 2. 构建并启动（web + nginx 反代）
docker compose up -d --build
# 3. 访问 http://<host>:8080/shibei/ 并登录
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SHIBEI_PASSWORD` | 无（未设置则自动生成并打印到容器日志） | 初始管理员密码，仅首次启动建号时生效 |
| `SHIBEI_USERNAME` | `admin` | 初始管理员用户名 |
| `SHIBEI_HTTP_PORT` | `8080` | nginx 对外端口（浏览器访问端口） |
| `SHIBEI_BASE_PATH` | `/shibei/` | 浏览器侧访问前缀，需与 nginx `location` 一致 |
| `TZ` | `Asia/Shanghai` | 容器时区 |

要点：

- 数据全部持久化在宿主 `./data`（帖子、报告、`web_config.json`、凭据），容器重建不丢；删除该目录即重置。
- 改完 `.env` 后 `docker compose up -d` 重建生效；已建号后改 `SHIBEI_PASSWORD` 不生效，可在界面「设置 → 账号」改密。
- 前端在容器内以 `VITE_BASE=/shibei/` 构建；若自定义 `SHIBEI_BASE_PATH`，需同步修改 `docker-compose.yml` 中 `SHIBEI_BASE_PATH`、`deploy/nginx.conf` 的 `location` 与镜像构建参数 `VITE_BASE`（`docker compose build --build-arg VITE_BASE=/xxx/`）。
- 需要 HTTPS / 域名：在更外层网关（如 Caddy / Nginx Proxy Manager）把流量转给本 compose 的 `8080` 即可，或自行加证书。

### 架构与目录

| 部分 | 说明 |
|---|---|
| `frontend/` | React 18 + Vite + Tailwind + shadcn/ui 风格组件，构建产物进镜像 |
| `web/app.py` | Flask 应用：登录会话 / AI 配置 / 来源开关 / 任务 API / 静态托管 |
| `web/runner.py` | analyzer 子进程管理：单任务并发、状态索引与日志落盘 `data/tasks/` |
| `deploy/nginx.conf` | `/shibei/` 子路径反代示例（剥前缀转发 `web:8000`） |
| `Dockerfile` | 多阶段：node 构建前端 → python:3.12-slim（flask + waitress） |

### 本地开发

```bash
# 后端（需 Python ≥3.10；uv 安装依赖）
uv sync
SHIBEI_BASE_PATH=/ SHIBEI_PASSWORD=dev123 uv run python -m web.app   # :8000
# 前端（另开终端；/shibei/api 自动代理到 :8000）
cd frontend && npm install && npm run dev                             # :5173
```

## 配置

### config.json

```json
{
  "sources": {
    "v2ex": {
      "enabled": true,
      "nodes": ["programmer", "python"],
      "pages_per_node": 1,
      "request_delay": 1.2,
      "max_retries": 3
    }
  },
  "llm": {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-v4-flash",
    "max_tokens": 4096
  }
}
```

| 配置项 | 说明 |
|---|---|
| `sources.{来源}.nodes` | 要爬的节点列表（V2EX 节点见 `uv run python crawler.py list`） |
| `sources.{来源}.pages_per_node` | 每个节点往后翻几页（**V2EX 的 API 忽略分页参数，填 1 即可**——多填只是重复请求同一批帖子） |
| `sources.{来源}.request_delay` | 请求间隔（秒），控制限流 |
| `sources.{来源}.max_retries` | 请求失败重试次数 |
| `llm.base_url` | OpenAI 兼容 API 地址 |
| `llm.model` / `llm.max_tokens` | 模型名 / 单次输出上限 |

> 完整配置见仓库 `config.json`：已内置 hackernews / lobsters / devto / sspai / producthunt 五节的默认节点与参数；去掉某节（或 `enabled: false`）即可停用该来源。

### 环境变量（优先级：环境变量 > config.json）

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | LLM API Key，仅环境变量，不入库 |
| `OPENAI_BASE_URL` | ✅ | API 地址（或用 config.json 的 `llm.base_url`） |
| `ANALYZE_MODEL` | ✅ | 模型名（或用 config.json 的 `llm.model`），无内置默认 |
| `ANALYZE_MAX_TOKENS` | | 单次输出上限（默认取 `llm.max_tokens`，兜底 4096） |
| `ANALYZE_LANG` | | 报告与任务日志语言：`zh`（默认）/ `en`；`--lang` 参数优先于此 |

### 常用 OpenAI 协议 API 示例

拾贝只要求接口兼容 OpenAI 协议，服务商任选。下表 **base_url 与模型名均按各服务商官方文档核实**（模型会随厂商更新，使用前以官方文档为准；填错会收到 400 `model not found` 之类报错）：

| 服务商 | `base_url` | 示例 `model` | 官方文档 |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o`、`gpt-4o-mini` | [platform.openai.com/docs/models](https://platform.openai.com/docs/models) |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-v4-pro`、`deepseek-v4-flash`（v4 现行；旧名 `deepseek-chat`/`deepseek-reasoner` 已弃用） | [api-docs.deepseek.com](https://api-docs.deepseek.com) |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o`、`anthropic/claude-sonnet-4`（`厂商/模型` 格式） | [openrouter.ai/docs](https://openrouter.ai/docs) |
| Moonshot（Kimi） | `https://api.moonshot.cn/v1` | `moonshot-v1-32k`、`kimi-k2` | [platform.moonshot.cn/docs](https://platform.moonshot.cn/docs) |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash`、`glm-4-plus` | [open.bigmodel.cn/dev/api](https://open.bigmodel.cn/dev/api) |
| 通义千问（DashScope） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus`、`qwen-turbo` | [百炼·模型列表](https://help.aliyun.com/zh/model-studio/) |
| 硅基流动（SiliconFlow） | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3`、`Qwen/Qwen3-Max`（`组织/模型` 格式，区分大小写） | [docs.siliconflow.cn](https://docs.siliconflow.cn) |
| Ollama（本地） | `http://localhost:11434/v1` | 本地已拉取的模型 tag，如 `llama3.1` | [docs.ollama.com](https://docs.ollama.com) |

注意事项：

- DeepSeek 的 `/v1` 只是 OpenAI 兼容后缀，与模型版本无关（官方 base_url 为 `https://api.deepseek.com`）。
- 智谱等厂商的兼容端点要求路径精确到 `/chat/completions`，`base_url` 填到 `.../paas/v4` 即可（拾贝会自动补 `/chat/completions`）。
- 自部署网关（vLLM / LiteLLM）base_url 形如 `http://localhost:8000/v1`，模型名以部署的模型为准。

例如用 DeepSeek：

```bash
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export ANALYZE_MODEL=deepseek-v4-flash
uv run python analyzer.py
```

## 数据与报告

```
data/
├── state.json                 # 各来源 last_crawl / last_analysis
├── analysis/
│   ├── analysis.md            # 全量报告
│   ├── analysis.en.md         # 全量报告（英文）
│   ├── YYYY-MM-DD.md          # 每日增量报告（每天一份，同日多次运行刷新）
│   └── YYYY-MM-DD.en.md       # 每日增量报告（英文）
├── .cache/                    # 列表缓存 + 分析缓存（分析后自动清理）
└── {source}/{node}/{id}.json  # 统一结构帖子（每来源一目录）
```

报告示例（每条来源均可点击跳回原帖）：

```markdown
# 拾贝 · 多来源分析

来源: v2ex(programmer, python)

基于 1234 个帖子自动生成

## 产品创意

- {创意描述} — [来源](https://www.v2ex.com/t/1229217)

## 用户痛点

- {痛点描述} — [来源](https://www.v2ex.com/t/1229505)

## 潜在机会

- {机会描述} — [来源](https://www.v2ex.com/t/1229217)
```

> 完整报告示例见 [examples/analysis.md](examples/analysis.md)。

## 已接入数据源

| 来源 | 接入方式 | 特点 |
|---|---|---|
| V2EX | JSON API（免认证） | 节点可配置，`crawler.py list` 查看全部节点 |
| Hacker News | Firebase JSON API（免认证） | Show / Ask / Top / New 四类；评论递归展平 |
| Lobste.rs | JSON API（免认证） | 按标签爬取 + `hottest` 首页 |
| Dev.to (Forem) | JSON API（免认证，自定义 Accept） | 按 tag 爬取，含评论树 |
| 少数派 | RSS 2.0 | 全站 feed，无评论接口 |
| Product Hunt | RSS | 每日新品，无评论接口 |

所有来源输出统一结构帖子 JSON（`data/{source}/{node}/{id}.json`），限流由各来源配置节独立控制，来源间并行爬取互不影响。

## 多来源扩展

新增社区只需三步（详见 `docs/design.md` §5.4）：

1. 新建 `sources/{name}.py`，继承 `Source` 实现 `fetch_topics` / `fetch_replies` / `list_nodes`。
2. 在 `sources/__init__.py` 注册。
3. 在 config.json 加一节来源配置。

通用助手在 `sources/base.py`：`http_get_json`（可传 `extra_headers`）、`http_get_xml` / `parse_atom_feed`（Atom 与 RSS 2.0）、`strip_html`、`iso_to_unix`（ISO 8601 / RFC 2822）。

## 命令行参考

```
# analyzer.py（单一入口）
uv run python analyzer.py               # 自动增量爬取 + 增量分析（空数据自动全量）
uv run python analyzer.py --full        # 强制全量
uv run python analyzer.py --lang en     # 英文报告与日志（zh/en，默认 zh）

# crawler.py（可选独立工具）
uv run python crawler.py                # 全量爬取
uv run python crawler.py --today        # 增量爬取
uv run python crawler.py list [关键词]   # 列出可用节点
uv run python crawler.py --today --lang en   # 英文日志
```

## 开发

```bash
uv run ruff format .
uv run ruff check .
uv run pyright .
uv run pytest
uv run pre-commit run --all-files        # 见 .pre-commit-config.yaml
```

## 路线图

- [ ] 更多来源：Reddit（OAuth 商用授权）、即刻（逆向）等门槛更高的社区
- [x] Docker 镜像 + Web 界面（报告查看 / 主动触发 / AI 配置 / 登录，nginx 子路径部署）
- [x] 每日定时调度（默认 00:00 增量分析，Web 可配置）
- [ ] 报告增强：分类标签、历史对比、导出其它格式
- [ ] 防爬机制：遵守 robots.txt，并加防御性措施（限速、指数退避、随机抖动、并发上限），避免过度占用目标站点带宽

## 贡献

欢迎提交 issue 与 PR，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可

[MIT](LICENSE)
