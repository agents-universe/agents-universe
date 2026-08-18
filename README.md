# Agents Universe

[![Live Site](https://img.shields.io/website?url=https%3A%2F%2Fagents-universe.com&label=agents-universe.com&color=7c3aed)](https://agents-universe.com)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![CI](https://github.com/agents-universe/agents-universe/actions/workflows/ci.yml/badge.svg)](https://github.com/agents-universe/agents-universe/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Vue 3](https://img.shields.io/badge/Vue-3.5-42b883.svg)](https://vuejs.org/)

> **开源共建 · 技术平权 —— 让智能体像人一样学习和工作。**

Agents Universe 是一个开源的企业级 AI Agent 平台。它不是又一个"更聪明的聊天框"，而是要让智能体真正成为能干活的人：入职读懂文档、接任务先规划、以个人身份与权限执行、留下可审计的轨迹——并把这一切沉淀为每个人都能复用、共同建设的资产。

![Agents Universe 系统界面](docs/images/system_screenshot.webp)

*Codex 式三栏界面：左侧会话与项目导航，中间对话流，右侧计划与知识上下文（点击可查看大图）*

> ### 🚀 它正在管理它自己
>
> **本项目已部署在 [agents-universe.com](https://agents-universe.com)，并且正在被它自己管理。** 平台上的 **Product Owner** 与 **Tech Lead** 智能体，就是这个仓库的日常维护者：需求、代码、PR 都由智能体们在对话中完成。
>
> 因此——**给这个项目做贡献，不需要会写代码**。注册账号、配置你自己的模型 API Key 与 Git Token，然后和 Product Owner 聊需求，聊得差不多 **@Tech Lead** 告诉它要实现哪个，即可完成协作提交 PR。详见 [零代码共建](#零代码共建用对话为项目做贡献)。

## 愿景：技术平权，始于开源

今天，一个智能体好不好用，几乎取决于"是谁调教的"。资深工程师的智能体可以独立交付整个功能，而普通人面对同样的框架，得到的往往是一个"会聊天但干不了活"的助手。智能体的能力被锁在个人的 Prompt 与上下文里：人均而异、无法复制、不可传承。

我们相信，这不应该是一种常态：

- **开源** —— 平台、技能、工作流、知识全部开放，任何人都可以查看、修改、分发；
- **共建** —— 经验沉淀为可共享的资产：一个人写好的技能，一万个人可以直接用；
- **平权** —— 不懂代码的人，也能拥有和资深工程师同等级的智能体。

## 为什么做这件事：智能体的"两极困局"

今天的智能体产品站在两个极端。

**一端是开发者框架 —— Claude Code、VS Code AI 等。** 自由度与上限都极高，但：门槛极高（要懂代码、会写 Prompt、会管理上下文与工具）；效果天差地别（每个人的智能体是不同的智能体，结果不可复现）；经验无法传承（调教技巧留在个人对话里，团队无法共建、复用）；非技术人员几乎不可用。

**另一端是低代码平台 —— Dify 等。** 上手简单，但设计理念从"知识库 + 应用配置"出发：智能体能力是补充而非核心（自主规划、任务执行、工具使用被简化成配置节点）；更像"应用"而不是"员工"（没有个人身份、最小权限与可审计的执行轨迹）；企业落地不适配（共享权限难以追责，复杂场景触及平台能力边界）。

### Agents Universe 的答案：让智能体像人一样

| 像人一样 | Agents Universe 的做法 |
|---|---|
| **学习**：入职读文档，干活中不断更新 | 项目选中即全量加载知识；交给它的文档提取后整理进对应知识条目，工作全程持续迭代；`[[slug]]` 显式表达业务结构；模糊决策主动向用户澄清 |
| **工作**：接任务、拆任务、执行、汇报 | `plan_task` 拆解任务树；按上下文选择工具；个人身份与权限执行；WebSocket 实时可见 |
| **传承**：老带新，经验沉淀 | 技能（Skills）、工作流（Workflows）与知识条目把个人经验变成组织资产；开源共建，人人可用 |

## 为什么能做到：经验即资产

"像人一样"不是宣传语，而是三条底层设计的结果——**学习靠读写、工作靠边界、能力靠传承**。

- **学习靠"读"，不靠"搜"。** 人的学习来自上下文：通读文档、翻旧代码、问同事，然后才谈得上干活；而向量检索只能返回碎片，理解需要整体。所以 Agents Universe 不用嵌入模型，让 Agent 读完整项目：全量加载是"读完"而非"搜过"，`[[slug]]` 是目录与参考文献式的显式结构，拿不准就主动澄清。上下文完整，理解才完整，工作才不会跑偏。
- **学习靠"写"，不靠"收藏"。** 读了要消化成自己的笔记才算学会：把 Confluence 页面、Swagger API 说明、测试说明等各种文档交给智能体，它会提取其中有用的信息，整理进对应的知识条目——接口进 `api-map.md`、指标进 `metric-catalog.md`、测试经验进 `test-patterns.md`，而不是把原文堆在上下文里。工作过程就是迭代过程：每次测试校正、每次口径裁定、每个新接口，都通过 `knowledge_rw` 回写；过时条目标注 `[stale]`、确认后退役，全部变更记入 `history.md`。项目越用越懂自己，这就是智能体的"工作经验"。
- **工作靠"边界"，不靠"蛮力"。** 能干活的人，是"有计划、有身份、有交代"的人：`plan_task` 先规划再动手；密钥加密、明文不进 LLM 上下文，以个人身份与最小权限执行；思考与执行全程 WebSocket 可见可审计。没有边界的自主是失控，没有自主的边界是脚本。
- **能力靠"传承"，不靠"个人调教"。** 资深工程师的智能体更强，不是模型更好，而是上下文里沉淀了多年经验——可惜这些经验过去锁在私人对话里。我们把经验提取成普通文件：技能、工作流、知识条目（新项目按分类自带领域骨架，等于岗前培训）、记忆分层（L0 上下文 → L7 全局系统，对应人的工作记忆与组织知识），再叠加开源。能力从此离开个人，成为可读取、可复制、可传承的组织资产——**这就是"技术平权"得以实现的机制**。

## 核心特性

- **多 LLM 提供商** — Anthropic / OpenAI / Azure OpenAI / Gemini 统一接入；用户自配模型与密钥（AES-256-GCM 加密），会话级切换，失败自动回退
- **开箱即用的角色** — 内置 Product Owner / Tech Lead / QA / 数据分析专家等智能体，选定即用；数据分析专家对齐项目指标口径取数（业务库 + 本地文件），覆盖异动归因、例行报告与图表看板
- **分类知识条目** — 创建项目时选择分类（软件项目 / 数据分析 / 文档知识库 / 其他），自动初始化对应的知识条目子集，新项目第一天就带着领域骨架
- **上下文压缩** — 长会话自动生成历史摘要，长时间工作不掉线
- **企业就绪** — 双层执行沙箱、项目隔离与最小权限、JWT 鉴权、SQL Server + Redis、Docker 一键部署

## 敏捷开发三智能体：一支 24 小时在线的小团队

平台内置的三个敏捷角色——**Product Owner / Tech Lead / Quality Assurance**——组成一条完整的 需求 → 实现 → 验证 交付链路。它们都遵循"知识优先"原则：选中项目即全量加载项目上下文（业务背景、词汇表、页面地图、API 清单、测试模式、历史记录……），通过 `[[slug]]` 交叉引用互相衔接，任何工作都建立在项目的全部上下文之上，而不是孤立的一次性对话。

### Product Owner — 需求侧

- **对话式澄清需求** — 用自然语言描述想法，PO 会通过**选择卡片**（给出具体选项，而非开放式提问）逐项澄清范围边界、用户角色、验收标准等歧义点，最多 2-3 轮即可收敛，绝不从模糊输入直接开工
- **拆分故事卡** — 把澄清后的需求拆成可测试的故事卡：清晰的摘要、描述、可验证的验收标准，落进 Jira 跟踪
- **自动估点** — 每张卡按 BE/FE 拆分并给出点数估计（story-point-estimator），假设条件显式声明
- **文档协同** — 从 Confluence 拉取文档沉淀进项目知识、同步 Jira 状态、生成发布说明与变更日志

### Tech Lead — 实现侧

- **把卡直接变成代码** — 给一张 Jira 卡就能完整交付：克隆 → 读代码 → 写实现 → 跑测试 → commit → 创建 PR；脏工作区、main 非快进、测试失败都是硬性门禁，通过后才继续
- **跨仓库一次交付** — 一张卡同时涉及多个项目时，每个仓库独立分支、独立 PR，一次对话提交全部改动
- **PR 对照卡片审查** — 审阅 PR 时把实现和卡的描述**联合起来对照**：验收标准是否满足、状态流转与字段映射是否一致、有没有范围蔓延，输出"实现 vs 卡"的一致性结论与风险点
- **兜底流程** — 无推送权限时自动走 fork → push → 创建 PR 的流程，贡献开源仓库同样可以

### Quality Assurance — 验证侧

- **对话造数据** — 通过 SQL 查询、Kong/API 调用与自适配数据库访问，直接在集成环境里准备验证所需的数据
- **一卡出用例** — 给一张故事卡就设计出完整的测试用例（正向/边界/异常），并直接生成 **Playwright 自动化测试脚本**（`tests/generated/`，含登录流程与 Jira 标注）
- **零成本定时回归** — 生成的脚本沉淀在项目自带的自动化测试脚手架（`scaffold/tests/`）中，可以接入 CI/CD pipeline **固定每日执行**——像经典自动化测试框架一样定时回归，运行时**不消耗任何 token**
- **证据回写** — 执行截图、录屏、关键区域标注自动回写 Jira 测试卡，结论以业务视角汇报

## 零代码共建：用对话为项目做贡献

给这个项目做贡献**不需要写一行代码**，传统流程中 fork → clone → 写代码 → PR 的全部工作，在这里变成四步对话：

1. **注册** — 打开 [agents-universe.com](https://agents-universe.com) 注册账号
2. **配置模型** — Settings → **AI Models**，填入你自己的 LLM API Key（支持 Anthropic / OpenAI / Azure OpenAI / Gemini），密钥经 AES-256-GCM 加密，只属于你
3. **配置 Git Token** — Settings 中添加你自己的 Git Token（支持 GitHub Enterprise 自定义端点），提交以你的身份、进入你授权的仓库
4. **聊天驱动开发** — 创建或加入项目，用自然语言和 **Product Owner** 聊需求；聊得差不多时 **@Tech Lead**，告诉它要实现哪张卡即可完成协作：读代码、写实现、跑测试、提交 commit 并创建 Pull Request；**QA 智能体**负责测试把关

> 💡 你负责提需求和评审，代码交给智能体——像带一支真正的团队，而不是敲键盘。

会写代码的人可以贡献得更深：任何能沉淀为资产的成果都欢迎——

- **技能** — `agents/skills/**/*.md`，四类技能：指引、模板、可执行、复合
- **工作流** — `workflows/*.workflow.md`，把完整工作方法沉淀为可复用的步骤
- **知识** — `knowledge/`，系统文档、技术文档、项目知识条目
- **Agent 定义** — `agents/*.agent.md`，定义新角色的行为与边界

每个人写下一个技能，就是为所有人解除一份门槛。

## 快速开始

> 不想搭环境？直接在 [agents-universe.com](https://agents-universe.com) 上体验，并按 [零代码共建](#零代码共建用对话为项目做贡献) 的方式参与共建。

### 环境要求

- Docker & Docker Compose
- Node.js 20+（本地开发前端）
- Python 3.12+（本地开发后端）
- SQL Server ODBC Driver 17（仅默认 SQL Server 部署需要；改用 PostgreSQL/MySQL/SQLite 时无需安装）

### Docker 一键启动

```bash
# 复制环境变量
cp .env.example .env
# 编辑 .env 填入 LLM API Key 等配置

# 启动全部服务
docker compose up --build
```

服务地址：
- Web UI: http://localhost:5173
- API: http://localhost:8000
- API Docs: http://localhost:8000/docs

### 本地开发

```bash
# API (packages/api/)
cd packages/api
python -m venv .venv && source .venv/bin/activate
pip install -e ../agent-core -e .
PYTHONPATH=src python -m uvicorn api.main:app --port 8000 --reload

# Frontend (packages/web/)
cd packages/web
npm install
npm run dev

# 数据库迁移
cd packages/api
alembic upgrade head
```

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (Vue 3)                          │
│            Codex-style 三栏布局 + WebSocket 实时流               │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP / WebSocket
┌────────────────────────────▼────────────────────────────────────┐
│                     FastAPI (Python 3.12)                        │
│         REST API · OAuth · JWT · Alembic Migrations             │
├─────────────────────────────────────────────────────────────────┤
│                    Agent Core (Python 3.12)                      │
│    LLM Orchestration · Agentic Loop · Knowledge Loader          │
│    Skills Engine · Workflow Runner · Tool System                 │
└──────┬──────────────────┬──────────────────┬────────────────────┘
       │                  │                  │
┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
│  SQL Server │   │    Redis    │   │  LLM APIs   │
│  (数据持久化) │   │ (会话/缓存)  │   │ (多提供商)   │
└─────────────┘   └─────────────┘   └─────────────┘
```

## 数据库配置（多数据库支持）

系统以四个数据库为一等公民运行：SQL Server（默认，`mssql+aioodbc`）、PostgreSQL（`postgresql+asyncpg`）、MySQL（`mysql+aiomysql`）、SQLite（`sqlite+aiosqlite`，仅开发/测试）。切换方式见 `.env.example` 的 Database 段：

```bash
# 最高优先级：完整 URL 覆盖一切（需安装对应驱动，见 packages/api/pyproject.toml extras）
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/agentsuniverse
# 不设 DATABASE_URL 时回退到 DB_* 字段构造的 mssql+aioodbc URL（默认生产形态）
# MSSQL_CONNECTION_STRING 为更早的 legacy 回退，仅两者都未设时才参与
```

本地起备选库：`docker compose --profile postgres up -d postgres` 或 `docker compose --profile mysql up -d mysql`（MySQL 容器已配 `utf8mb4_0900_bin` 大小写敏感排序规则）。

数据库迁移与多方言适配：

```bash
# 从 packages/api/ 执行；迁移链对所有方言自动编译为对应 DDL（含 T-SQL 分支）
alembic upgrade head
alembic revision --autogenerate -m "description"
```

## 技术栈

| 层 | 技术 |
|---|---|
| Frontend | Vue 3.5 + TypeScript + Vite + TailwindCSS + CodeMirror 6 + Pinia |
| API | Python 3.12 + FastAPI + SQLAlchemy 2.x + Alembic |
| Agent Core | Python 3.12 — LLM 编排引擎（纯库，无 HTTP） |
| Database | 多数据库一等公民：SQL Server (mssql+aioodbc) / PostgreSQL (asyncpg) / MySQL (aiomysql) / SQLite (aiosqlite)；经 `DATABASE_URL` 切换 |
| Cache/Session | Redis 7 |
| Browser Automation | Playwright (Chromium) |
| Container | Docker multi-stage build, docker-compose |

## Monorepo 结构

```
agents-universe/
├── packages/
│   ├── agent-core/       # LLM 编排引擎 (纯 Python 库)
│   │   └── src/agent_core/
│   │       ├── agent.py          # Agent 主循环
│   │       ├── compressor.py     # 上下文压缩
│   │       ├── session.py        # 会话管理
│   │       ├── providers/        # LLM 提供商适配器
│   │       ├── knowledge/        # 知识加载与索引
│   │       ├── skills/           # 技能引擎
│   │       ├── tools/            # Agent 工具系统
│   │       ├── sandbox.py        # 双层沙箱（命令验证 + 文件守卫）
│   │       ├── sandbox_guard/    # CPython audit hook 文件守卫
│   │       └── workflows/        # 工作流执行器
│   │
│   ├── api/              # FastAPI Web 服务
│   │   └── src/api/
│   │       ├── main.py           # 应用入口
│   │       ├── config.py         # 配置管理
│   │       ├── database.py       # DB 连接（多方言，DATABASE_URL）
│   │       ├── routers/          # REST 路由
│   │       ├── models/           # SQLAlchemy ORM 模型
│   │       ├── services/         # 业务逻辑
│   │       ├── middleware/       # Auth、CORS 等中间件
│   │       ├── websocket/        # WebSocket 实时通信
│   │       └── dependencies/     # FastAPI 依赖注入
│   │
│   └── web/              # Codex-style Browser UI
│       └── src/
│           ├── components/       # Vue 组件
│           ├── stores/           # Pinia 状态管理
│           ├── composables/      # WebSocket、路由等组合式函数
│           ├── router/           # Vue Router 路由定义
│           ├── pages/            # 页面视图
│           ├── layouts/          # 页面布局
│           └── api/              # REST API 客户端
│
├── agents/               # Agent 定义 (*.agent.md) + 技能 (skills/**/*.md)
├── workflows/            # 工作流定义 (*.workflow.md)
├── knowledge/            # 全局知识库
│   ├── system/           # 系统级知识
│   ├── technical/        # 技术知识
│   ├── categories.yaml   # 项目分类注册表（各分类的知识条目子集）
│   └── _template/        # 新项目知识条目源文件
├── scaffold/             # 子项目测试脚手架（Playwright 配置，新项目 tests/ 初始化源）
├── docker-compose.yml    # 容器编排（另含 .local 本地 / .example 示例变体）
├── docker-entrypoint.sh  # 容器入口：DB 迁移 → nginx → uvicorn
├── nginx-combined.conf   # nginx 反代配置（Web UI 与 /api 转发）
├── Dockerfile            # Multi-stage 构建
└── *.ps1                 # Windows 本地开发与部署脚本（dev-start / deploy-local / build-deploy）
```

## 核心设计

### 多 LLM 支持

通过提供商适配器接入多家 LLM（`providers/registry.py` 懒加载，未安装的 SDK 不影响启动）：

- **anthropic** — Claude 系列
- **openai** — GPT 系列及兼容端点
- **azure_openai** — Azure OpenAI（需配置 endpoint + deployment）
- **google_gemini** — Gemini 系列

模型配置与选择机制：

- 用户在 **Settings → AI Models** 配置自己的模型（provider + model + API Key），存于 `user_model_configs` 表，API Key 经 AES-256-GCM 加密
- 每个会话可在 UI 中指定使用的模型；未指定时使用第一个可用配置
- 回退链：用户模型配置 → 系统默认模型（环境变量配置）
- 上下文压缩（`compressor.py`）使用当前会话的同一模型生成历史摘要

### 知识系统

- **无嵌入模型** — 项目选中时全量加载知识文件，无向量搜索
- **两级加载** — primary 文件全量载入上下文；`knowledge_level: detail` 文件仅索引元数据与摘要，Agent 通过 `knowledge_rw` 工具按需加载、任务完成即释放
- **交叉引用** — Markdown 文件中使用 `[[slug]]` 语法互相引用，索引时解析为 `knowledge_id`
- **项目隔离** — 知识查询始终限定 `project_id = :current OR project_id IS NULL`
- **溢出机制** — 超出 token budget 时，列出未加载文件供 Agent 按需读取
- **持续迭代** — 工作全程通过 `knowledge_rw` 写入、合并、退役知识条目，每次变更记入 `history.md`；过时条目标注 `[stale]`、经用户确认后退役

### Agentic Loop

- `plan_task` 工具触发结构化任务规划
- 任务追踪在 `agent_tasks` 数据表
- WebSocket 实时推送思考过程和工具调用

### 技能与工作流

- **技能类型**: `guidance`（LLM 指令）、`template`（代码模板）、`executable`（可执行代码块）、`composite`（技能链）
- **工作流**: Markdown 格式定义，Agent 读取并按步执行，无 YAML 引擎
- **记忆分层**: L0 上下文 → L4 会话摘要 → L5 项目知识 → L7 全局系统，逐级沉淀

## 子项目工作区

通过 `POST /api/projects` 创建的每个子项目拥有隔离的工作区：

```
{PROJECTS_ROOT}/{slug}/
├── agents/         ← 项目级 Agent 定义（{slug}--{name}.agent.md，懒同步，可覆盖全局）
├── skills/         ← 项目级技能（同名覆盖全局技能）
├── workflows/      ← 项目级工作流
├── knowledge/      ← 按项目分类初始化知识条目子集
├── tests/          ← 初始化自带 Playwright 脚手架（scaffold/tests/），QA Agent 生成测试脚本
└── .tmp/
    ├── media/{conversation_id}/   ← 截图和生成的图片
    └── work/                      ← 临时工作文件
```

创建项目时按分类（软件项目 / 数据分析 / 文档知识库 / 其他）从 `knowledge/_template/` 初始化对应的知识条目子集——例如「数据分析」项目自动带上数据源清单、指标口径字典、分析场景、SQL 模式等 11 个知识条目；「其他」分类则进入项目定制专家访谈，由对话逐步填充知识。`PROJECTS_ROOT` 是必需的环境变量，指向仓库外部的目录。

## 安全设计

- **Token 加密** — AES-256-GCM 加密存储，永不记录日志或输出到错误信息
- **JWT 认证** — API 和媒体端点统一 JWT 鉴权
- **OAuth 会话** — Redis 存储，键 `session:{session_id}`，TTL=24h
- **项目隔离** — 所有查询强制 project_id 作用域，禁止跨项目访问

### 执行沙箱

Agent 执行的每条命令、每段代码都运行在**双层沙箱**中，文件访问被限制在当前项目工作区内（实现见 `agent_core/sandbox.py`、`agent_core/sandbox_guard/`、`agent_core/tools/shell.py`、`agent_core/tools/code_executor.py`）。

**第一层：命令级验证（Shell 工具）**

- **命令白名单** — 仅放行 git / npm / node / python / java / mvn 等常用命令；危险模式黑名单拦截 `rm -rf`、`sudo`、`curl`、`wget`、`pip install`、`find -delete` 等
- **复合命令逐段校验** — 用管道/`&&`/`;` 串联的每个命令段都须独立通过白名单，`find \( ... \)` 表达式分组不受影响
- **逐 token 路径校验** — 绝对路径、`~`、`..` 段一律拒绝；重定向目标（`>` / `>>`）同样校验（系统临时目录除外）；`git -C`、`--git-dir`、`--work-tree` 与 `git config --global/--system` 被拒绝
- **命令替换与变量逃逸** — `$(...)` / 反引号禁止；路径参数中的 `$VAR` 必须在本条命令内以 `VAR=value` 赋值过（其值也已校验），防止 `p=../proj-b; cat $p/x` 把越界路径当作数据带出去
- **数据参数豁免** — 提交消息（`git -m`）、grep 模式、`python -c` 代码等纯数据参数不做路径检查，避免误伤

**第二层：Python 运行时文件守卫（audit hook）**

- 每个工具派生的 Python 子进程通过 `sitecustomize` 自动安装 **CPython audit hook**（`sys.addaudithook`，用户代码无法移除），拦截所有文件读写：写操作限定在项目工作区 + 系统临时目录 + 用户工具缓存（`~/.cache`、`~/.npm` 等）；读操作额外放行 Python 安装目录（标准库与 site-packages）
- 越界访问抛出带 `[agent-guard]` 前缀的 `PermissionError`，让 LLM 明白这是沙箱的正常拒绝而非程序错误
- 守卫经 `PYTHONPATH` 注入并随进程树自动继承；容器镜像还会把守卫装入 site-packages（见 `Dockerfile`），即使命令自带 `PYTHONPATH=...` 覆盖也无法解除
- `python -S/-I/-E`（会跳过 sitecustomize 从而解除守卫）与 `os.system` / `os.exec` 直接拒绝
- **严格模式**（code_executor）额外阻断 `subprocess.Popen`（仅放行 matplotlib 字体探测的 `fc-list` / `fc-cache`），杜绝 Python 代码再派生任意进程

**代码执行器（code_executor）** — 30 秒超时、无网络；Python 代码经文本级正则封禁 `subprocess` / `os.system` / `pty` / `ctypes` 等；Bash 代码逐行做路径逃逸校验；工作目录与临时文件全部落在项目内 `.tmp/work/`，产出图片同样在项目内并经 JWT 鉴权的媒体端点提供

**密钥保护** — `env_refs` 在服务端把保险库中的密钥解析进子进程环境，明文永不进入 LLM 上下文，返回的输出在截断前统一脱敏为 `[REDACTED:名称]`；子进程环境经 `safe_env()` 剥离 SECRET_KEY、数据库连接串等凭据类变量

**已知边界**（纵深防御，非绝对隔离）

- CPython 3.12 的 `os.stat` / `os.access` 不产生 audit 事件，纯路径存在性探测不被拦截——文件内容读写仍全部经过被 hook 的 `open`
- Node.js 无等效运行时 hook，node 脚本的越界读取依赖命令级验证约束
- 非严格模式下，受守卫的 Python 派生的非 Python 子进程不在文件守卫覆盖内

## 测试

```bash
# Agent Core — pytest + pytest-asyncio, Mock LLM via VCR cassettes
cd packages/agent-core && pytest

# API — pytest + httpx AsyncClient, SQLite 文件库跑真实 alembic 迁移链；
# TEST_DATABASE_URL 可切换方言（CI 另有 PostgreSQL 实库 job）
cd packages/api && pytest

# Web — Vitest + Vue Test Utils
cd packages/web && npm test
```
