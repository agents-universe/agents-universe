# Agents Universe

[![CI](https://github.com/agents-universe/agents-universe/actions/workflows/ci.yml/badge.svg)](https://github.com/agents-universe/agents-universe/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Live Site](https://img.shields.io/website?url=https%3A%2F%2Fagents-universe.com&label=agents-universe.com&color=7c3aed)](https://agents-universe.com)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Vue 3](https://img.shields.io/badge/Vue-3.5-42b883.svg)](https://vuejs.org/)

> **智能体宇宙 —— 智能体即服务。**

Agents Universe 是一个开源（Apache-2.0）的企业级 AI Agent 平台，核心是一套 **Agentic RAG** 知识系统：项目知识**全量加载**打底，Agent 按用户目标**渐进加载**最相关的上下文（detail 知识、相关技能、代码图谱……），工作全程**读写沉淀**——而且这份上下文**属于整个团队**：所有人共享同一份项目知识，任何人（或任何智能体）的每一次澄清、写回与修正，都让下一次对话的起点更高。你再把外部系统授权给它——Jira、Confluence、Git、MCP——智能体就成为你的**数字分身**，以你的身份与权限**并发地干活**，留下可审计的轨迹。

![Agents Universe 系统界面](docs/images/system_screenshot.webp)

*Codex 式三栏界面：左侧会话与项目导航，中间对话流，右侧计划与知识上下文（点击可查看大图）*

**30 秒了解 Agents Universe**

- **是什么** — 开源企业级 AI Agent 平台：多 LLM 统一接入、12 个开箱即用的智能体、Docker 一键自托管。详见[核心特性](#核心特性)
- **解决什么** — 智能体的两极困局：开发者框架门槛高、经验不可传承；低代码平台没有身份、权限与可审计轨迹。详见[为什么是 Agents Universe](#为什么是-agents-universe)
- **有何不同** — Agentic RAG + 团队共享上下文 + 数字分身：全量加载代替向量检索，Agent 按目标渐进加载 detail 知识、相关技能与代码图谱，经验写回后全团队复用；授权 Jira / Confluence / Git / MCP 后，智能体以你的身份**并发干活**。详见[知识系统：Agentic RAG](#知识系统agentic-rag)与[数字分身](#数字分身授权即能力并发干活)
- **怎么参与** — **贡献不需要会写代码**：注册后与 Product Owner 聊需求即可提交 PR；或 `docker compose up --build` 自托管。详见[零代码共建](#零代码共建用对话为项目做贡献)与[快速开始](#快速开始)

> ### 🚀 它正在管理它自己
>
> **本项目已部署在 [agents-universe.com](https://agents-universe.com)，并且正在被它自己管理。** 平台上的 **Product Owner** 与 **Tech Lead** 智能体，就是这个仓库的日常维护者：需求、代码、PR 都由智能体们在对话中完成。
>
> 因此——**给这个项目做贡献，不需要会写代码**。注册账号、配置你自己的模型 API Key 与 Git Token，然后和 Product Owner 聊需求，聊得差不多 **@Tech Lead** 告诉它要实现哪个，即可完成协作提交 PR。详见 [零代码共建](#零代码共建用对话为项目做贡献)。

## 目录

- [快速开始](#快速开始)
  - [环境要求](#环境要求)
  - [Docker 一键启动](#docker-一键启动)
  - [本地开发](#本地开发)
- [快速搭建示例：从零做一个审批智能体](#快速搭建示例从零做一个审批智能体)
- [为什么是 Agents Universe](#为什么是-agents-universe)
  - [智能体的两极困局](#智能体的两极困局)
  - [我们的答案：Agentic RAG + 团队共享上下文](#我们的答案agentic-rag--团队共享上下文)
  - [愿景：技术平权](#愿景技术平权)
- [知识系统：Agentic RAG](#知识系统agentic-rag)
- [核心特性](#核心特性)
- [内置智能体与协同工作](#内置智能体与协同工作)
  - [Product Owner：需求侧](#product-owner需求侧)
  - [Tech Lead：实现侧](#tech-lead实现侧)
  - [Quality Assurance：验证侧](#quality-assurance验证侧)
  - [办公助手：把内容变成交付物](#办公助手把内容变成交付物)
  - [渗透测试专家：授权范围内的白盒渗透测试](#渗透测试专家授权范围内的白盒渗透测试)
  - [性能测试专家：授权范围内的 API 压测](#性能测试专家授权范围内的-api-压测)
  - [智能体协同：@提及，即插即用](#智能体协同提及即插即用)
- [数字分身：授权即能力，并发干活](#数字分身授权即能力并发干活)
- [零代码共建：用对话为项目做贡献](#零代码共建用对话为项目做贡献)
- [系统架构](#系统架构)
- [数据库配置（多数据库支持）](#数据库配置多数据库支持)
- [技术栈](#技术栈)
- [Monorepo 结构](#monorepo-结构)
- [核心设计](#核心设计)
- [子项目工作区](#子项目工作区)
- [安全设计](#安全设计)
- [智能体即服务（Agent-as-a-Service）](#智能体即服务agent-as-a-service)
- [定时任务（Scheduled Tasks）](#定时任务scheduled-tasks)
- [测试](#测试)
- [参与贡献与延伸阅读](#参与贡献与延伸阅读)

## 快速开始

> 不想搭环境？直接在 [agents-universe.com](https://agents-universe.com) 上体验，并按 [零代码共建](#零代码共建用对话为项目做贡献) 的方式参与共建。

### 环境要求

- Docker & Docker Compose
- Node.js 20+（本地开发前端）
- Python 3.12+（本地开发后端）
- SQL Server ODBC Driver 17（仅本地开发直连 SQL Server 需要；Docker 部署镜像已内置，改用 PostgreSQL/MySQL/SQLite 时也无需安装）

### Docker 一键启动

```bash
# 1. 复制环境变量与编排文件（docker-compose.yml 未入库，必须从 example 复制）
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml

# 2. 生成 SECRET_KEY 并写入 .env（启动时会拒绝一切 change-me 占位值）
#    bash / macOS / Linux:
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
#    PowerShell:
#    Add-Content .env "SECRET_KEY=$(python -c 'import secrets;print(secrets.token_hex(32))')"

# 3. 启动全部服务（首次构建含 Playwright 与渗透测试工具链，视网络约 10-30 分钟）
docker compose up --build
```

服务地址：
- Web UI / API: http://localhost:8000（同一 nginx 反代 `/api`、`/ws`，健康检查 `/health`）
- 热重载前端（可选）: `docker compose --profile dev up` → http://localhost:5173

几点说明：
- `.env` 里**不需要** LLM API Key：启动后在 Settings → AI Models 按用户配置（加密存库）；`SYSTEM_DEFAULT_*` 仅作可选兜底。
- 默认 `AUTH_BYPASS_ENABLED=true` 免登录，开箱即用，仅供本地体验；生产部署请关闭并配置 OAuth SSO。
- 数据库迁移与全局知识索引由容器启动脚本自动执行。
- 首次构建默认使用中国境内镜像源；网络不通时改用官方源构建：

  ```bash
  docker compose build \
    --build-arg NPM_REGISTRY=https://registry.npmjs.org \
    --build-arg PIP_INDEX_URL=https://pypi.org/simple \
    --build-arg PIP_TRUSTED_HOST=pypi.org \
    --build-arg APT_MIRROR_HOST=deb.debian.org \
    --build-arg NODE_BASE_URL=https://nodejs.org/dist \
    --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.playwright.dev
  docker compose up
  ```

#### 使用预构建镜像（Docker Hub）

发布版本会自动把合并镜像（nginx Web + Python API）推送到 Docker Hub，按版本号拉取即可，跳过本地构建：

```bash
docker pull chas3/agents-universe:v1.7.0
```

镜像打 `vX.Y.Z` / `X.Y.Z` / `latest` 三个 tag：`latest` 跟随最近一次发布，要求可复现的部署请 pin 精确版本。运行方式是在 compose 栈里替换掉 `build:` 块：

```yaml
# docker-compose.yml（从 docker-compose.example.yml 复制后）：
services:
  api:
    image: chas3/agents-universe:v1.7.0   # 替换 build: 块，不再本地构建
    # ports / environment / env_file / volumes / depends_on 保持不变
```

其余步骤与「Docker 一键启动」相同：`cp .env.example .env`、生成 `SECRET_KEY`、`docker compose up`。

几点说明：
- 容器启动即执行数据库迁移（`alembic upgrade head`），**必须先有可用数据库**，否则容器直接退出。
- 环境契约（见 `docker-compose.example.yml`）：`SECRET_KEY`（必填、拒绝 `change-me` 占位值）、`DB_*` / `DB_TRUST_CERT`、`REDIS_URL`、`APP_BASE_URL` / `WEB_BASE_URL` / `CORS_ORIGINS`；`PROJECTS_ROOT=/app/projects` 已烘焙进镜像，持久数据走 `projects` / `maven` / `gradle` 三个卷；健康检查 `:8000/health`。

### 本地开发

```bash
# 0. 依赖服务（SQL Server + Redis；compose 文件同上，从 example 复制）
docker compose up -d sqlserver redis

# 1. 环境变量：cp .env.example .env，生成 SECRET_KEY（同上），
#    并把 PROJECTS_ROOT 设为已存在的绝对路径（子项目工作区目录）

# 2. API (packages/api/)
cd packages/api
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ../agent-core -e .
PYTHONPATH=src python -m uvicorn api.main:app --port 8000 --reload
# Windows PowerShell: $env:PYTHONPATH='src'; python -m uvicorn api.main:app --port 8000 --reload

# 3. Frontend (packages/web/)
cd packages/web
npm install
npm run dev                         # http://localhost:5173，代理到 :8000

# 4. 数据库迁移（API 启动时也会自动执行，手动跑仅为确认）
cd packages/api
alembic upgrade head

# 5. 全局知识索引（本地开发不会自动执行，Docker 部署由 entrypoint 执行；在仓库根目录运行）
python -m agent_core.knowledge.index --global-dir ./knowledge
```

> 本地开发直连 uvicorn，API Docs 在 http://localhost:8000/docs；Docker 部署下该路径由 nginx 交给前端 SPA，不提供 Swagger。

## 快速搭建示例：从零做一个审批智能体

平台搭好后，最快的上手方式是把一个真实业务场景完整走一遍。下面以「**报销审批智能体**」为例：创建项目 → 定制知识与智能体 → 发布成 API 供业务系统调用，全程用对话完成，不写一行代码。

### 1. 创建项目

新建项目，分类选 **自定义项目**（`other`——审批类业务不属于内置的软件/数据/客服等分类，该分类只初始化 `domain/context` 与 `system/history` 两个知识条目，其余由访谈逐步填充）。创建后获得隔离的工作区（`{PROJECTS_ROOT}/{slug}/`），后续所有知识、智能体、技能都沉淀在这里，全团队共享。

### 2. 与「项目定制专家」访谈，定义项目与用户

选中项目，找 **项目定制专家** 说明这个项目是做什么的、给谁用。它会做结构化访谈（每轮 3-5 个问题，不替你做假设），典型问题：

- 这个项目是做什么的？最终交付物是什么？
- 主要参与者是谁——申请人、部门审批人、财务复核，各自职责是什么？
- 涉及哪些单据、外部系统与信息源？
- 智能体主要帮做什么？

根据你的回答，它会裁剪不适用的模板知识条目、创建自定义知识条目（如 `domain/approval-rules` 审批规则、`domain/role-matrix` 角色矩阵），并把访谈结论用 `knowledge_rw` 写入项目知识——不确定处标记 `[待确认]`，写入前先跟你确认。

### 3. 定义知识条目：把审批规则沉淀为项目知识

审批规则放在哪都行，按性质分工：

- **知识条目（推荐）** — 金额权限、驳回条件、加签规则等业务规则写进知识条目：业务方修订规则时只改知识、不动智能体，下一次对话立即生效，且全团队共享、每次变更记入 `system/history` 可审计
- **智能体定义** — 与审批流程强绑定的判定口径、输出格式要求，可直接写进智能体正文

规则条目与其它知识通过 `[[slug]]` 交叉引用，审批智能体执行时会按目标加载相关条目作为判定依据。

### 4. 与「智能体定制专家」定制审批智能体

告诉 **智能体定制专家** 你要一个审批智能体，它会先读项目知识再访谈（每轮 3-5 个问题），确认后在项目工作区生成专属定义 `agents/{project-slug}--approval.agent.md`，**重新打开智能体选择器即可选用，无需重启**。一份审批智能体定义通常包含：

- **职责边界** — 受理哪些审批（报销 / 采购 / 合同）、明确不做什么、哪些动作需要 `user_confirm` 确认
- **处理流程** — 沉淀为项目专属 workflow（`workflows/*.workflow.md`）：读取单据 → 校验必填项与金额 → 比对审批规则 → 输出通过/驳回结论与理由
- **文件类型** — 声明受理的单据格式，由 `doc_reader` 解析：`.pdf` / `.docx` / `.xlsx` / `.xlsm` / `.txt` / `.md` / `.csv`
- **输出契约** — 给发布 API 的调用方约定**结构化返回**：要求每轮以固定 JSON 代码块收尾，例如 `{"decision": "approved|rejected", "amount": ..., "matchedRule": ..., "reason": ..., "followUps": [...]}`——字段与判定口径写死在定义里，业务系统拼接 `stream_delta` 后提取 ```json``` 块即可稳定解析，不用猜自然语言的版式
- **工具矩阵** — 按最小能力声明 `filesystem`、`knowledge_rw`、`doc_reader`、`user_confirm` 等，引用的每个工具/技能/工作流都经 `definition_check` 校验真实存在

生成的定义通过 `definition_check`（`ok: true`）才宣告完成，后续可随时再找它调整职责、增删技能。

### 5. 发布成 API，给业务系统调用

SSO 登录后进入项目的 **发布** 页签（`/projects/{id}/publishes`，旧地址 `/settings/publishes` 会自动跳转）：

1. 选择该项目（需管理权）、审批智能体、你自己的一个模型配置，创建发布
2. 开启「API」入口，创建密钥——**明文只返回一次**，之后仅存 SHA-256 哈希

发布会话绑定在所选项目上：外部调用触发的每一轮都会加载**该项目的完整知识上下文**（审批规则、角色矩阵……），并固定运行在你绑定的模型配置上——相当于把「项目 + 项目上下文 + 智能体」整体发布成一个 API。

业务系统这样调用（默认 SSE 流式返回标准 agent 事件帧；创建发布时把「响应模式」选成 JSON 则一次性返回一个 JSON 对象）：

```bash
# 带附件调用：先上传（一次一个文件），返回的记录直接放进 attachments 数组
UPLOAD=$(curl -s -X POST "$BASE/api/p/$PUBLISH_ID/upload?thread_id=expense-2026-041" \
  -H "Authorization: Bearer $PUBLISH_KEY" \
  -F "file=@invoice.pdf")

curl -N -X POST "$BASE/api/p/$PUBLISH_ID/stream" \
  -H "Authorization: Bearer $PUBLISH_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"审批这笔报销：市场部差旅 4800 元，附行程单与发票\", \"thread_id\": \"expense-2026-041\", \"attachments\": [$UPLOAD]}"
```

上传返回 `{url, name, media_type, size}` 引用（`url` 指向该发布会话的 `/api/media/...` 路径），放进 `attachments` 数组发出即可——附件字节只驻留内存（1 小时 TTL、按线程配额），智能体经既有管线读取（图片走视觉、文本/表格内联、其余经 `doc_reader` 解析）。`thread_id` 用业务单据号即可：同一单据的多次追问钉在同一条会话上保留上下文，不同单据互不串扰。

流式模式下，每帧一行 `data: {json}`，`stream_delta` 是审批意见的流式输出，`stream_end` 表示本轮结束——调用方累积 `stream_delta` 拼出完整回复，按智能体定义里的输出契约提取 ```json``` 代码块，即得到可直接入库/流转的结构化审批结论。JSON 模式则直接返回 `{content, message_id, images, files, usage, stop_reason, ...}`：`content` 即完整回复文本，调用方检查 `stop_reason` 与 `error` 判定成败，不用再拼帧。

两点边界，设计时需知道：

- **附件走两步上传**——先 `POST /api/p/{id}/upload` 拿引用，再随 `message` 发送（每次 ≤10 个）；超大文档仍可走工作区路径（把文件放进项目工作区后在消息中指明路径，由 `doc_reader` 解析）
- **发布运行是非交互模式**——`user_confirm` 等确认型工具会返回可读错误而非弹窗，因此发布给外部调用的智能体应把审批规则写成可自主执行的判定逻辑，人工确认环节留在平台内使用时

页面形态（`/p/{publish_id}` 内嵌聊天页）与完整安全模型见 [智能体即服务](#智能体即服务agent-as-a-service)。

## 为什么是 Agents Universe

为什么需要再做一个智能体平台？先看行业的两极困局，再看 Agents Universe 的答案与愿景。

### 智能体的两极困局

今天的智能体产品站在两个极端。

举个常见的例子：同一个项目，两个人各自调教智能体——资深工程师的智能体读过全部文档、记住所有口径，能独立交付一整个功能；同事打开同样的框架，得到的却是一个"会聊天但干不了活"的助手。更难受的是，项目背景、业务口径、踩过的坑，散落在各人各自的对话记录里——每个人的智能体都只知道自己那一份，换个人来问，它一无所知。

**一端是开发者框架 —— Claude Code、VS Code AI 等**，自由度与上限都极高，但：

- **门槛极高** — 要懂代码、会写 Prompt、会管理上下文与工具，非技术人员几乎不可用
- **效果天差地别** — 每个人的智能体是不同的智能体，结果不可复现
- **上下文锁在个人会话里** — 项目背景、业务口径、调教经验都只属于自己那份对话，团队无法共享同一个上下文；经验无法沉淀、无法传承，换个人、换台机器，一切从头再来

**另一端是低代码平台 —— Dify 等**，上手简单，但设计理念从"知识库 + 应用配置"出发：

- **智能体能力是补充而非核心** — 自主规划、任务执行、工具使用被简化成配置节点
- **更像"应用"而不是"员工"** — 没有个人身份、最小权限与可审计的执行轨迹
- **企业落地不适配** — 共享权限难以追责，复杂场景触及平台能力边界

### 我们的答案：Agentic RAG + 团队共享上下文

| Agentic RAG · 团队共享 | Agents Universe 的做法 |
|---|---|
| **知识加载**：读全打底、按目标渐进拉取，而非碎片检索 | 项目选中即全量加载 primary 打底；此后 Agent 按用户目标渐进加载最相关上下文——detail 知识（`knowledge_rw`）、相关技能与工具（Jira / Confluence 等）、代码知识图谱（`repo_graph` 几 token 定位代替通读文件）；`[[slug]]` 显式表达业务结构，拿不准主动澄清 |
| **任务执行**：规划 → 工具 → 身份执行 | `plan_task` 拆解任务树；按上下文选择工具；个人身份与权限执行；WebSocket 实时可见 |
| **上下文共享**：一份知识，全团队复用与优化 | 知识条目、技能、工作流都是项目里的普通文件——谁写回、谁受益；不再锁在个人会话里，换人接手同一份上下文 |
| **经验沉淀**：工作读写变成资产 | 每次口径裁定、测试校正写回知识条目；开源共建，人人可用 |
| **数字分身**：授权即能力，并发干活 | 你授权 Jira / Confluence / Git / MCP 等外部系统，智能体以你的身份与权限执行；多会话、定时任务与智能体接力并行推进，全程可审计 |

### 愿景：技术平权

今天，一个智能体好不好用，几乎取决于"是谁调教的"。智能体的能力被锁在个人的 Prompt 与上下文里：人均而异、无法复制、不可传承——团队里每个人都守着自己的一套上下文，却没有人能接上别人的。

我们相信，这不应该是一种常态：

- **开源** —— 平台、技能、工作流、知识全部开放，任何人都可以查看、修改、分发；
- **共建** —— 经验沉淀为可共享的资产：一个人写好的技能，一万个人可以直接用；
- **平权** —— 不懂代码的人，也能拥有和资深工程师同等级的智能体。

## 知识系统：Agentic RAG

Agents Universe 的知识系统建立在三条底层设计之上：**知识靠读写、执行靠边界、能力靠传承**。它不是传统 RAG——不靠 embedding 把文档切成碎片再按相似度召回，而是 **Agentic RAG**：由智能体自主决定读什么、何时补读、写了什么——以全量加载为底座，**按用户输入的目标渐进加载最相关的上下文**，detail 知识、相关技能、代码图谱、外部系统都按需拉取。同时，这份上下文**属于项目团队而非个人**：知识条目是项目里的普通文件，任何人的每一次写回都让全团队下一次对话的起点更高——这正是对「上下文锁在个人会话里」的直接回答。

```
加载项目知识  →  工作中读写沉淀  →  经验沉淀为资产
      ↑                             │
      └────── 新项目继承资产 · 再出发 ──┘
```

- **加载：项目知识全量读入，而非碎片检索。**
  - 新项目创建时按分类（软件项目 / 旧系统改造项目 / 数据分析 / 智能客服 / 文档知识库）自动初始化领域骨架；随后由用户驱动把 Confluence 页面、Swagger/API 文档、PRD、测试说明发给智能体——粘贴、发 URL 或上传附件都可以
  - 按 `knowledge-ingestion` 工作流归档：判断文档归属（业务规则进 `domain/`、接口进 `technical/`、操作手册进 `skills/`）、提取有用信息、总结成带 `[[slug]]` 交叉引用的结构化知识条目写回项目——API 文档按"索引 + 按服务 detail"两级归档，发现已有相似条目会先问你是更新还是新建，绝不重复
  - 之后每次选中项目，`knowledge/loader` 两级加载：primary 文件全文读入上下文，detail 文件按需再取。**靠"读"，不靠"搜"**——没有向量检索的碎片召回，全量加载是"读完"而非"搜过"，`[[slug]]` 是目录与参考文献式的显式结构，拿不准就主动澄清；上下文完整，理解才完整，工作才不会跑偏
- **写回：工作过程持续读写沉淀。**
  - 全程通过 `knowledge_rw` 把新学到的东西写回知识条目：新接口进 `api-map.md`、指标口径进 `metric-catalog.md`、测试经验进 `test-patterns.md`。**靠"写"，不靠"收藏"**——只提取信息、归类整理，而不是把原文堆在上下文里
  - 每次写入立即重新索引：完整度打分更新、旧版本全文留档（`knowledge_versions`）、WebSocket 实时同步进度；API 文档超过阈值时强制拆成"索引 + 按服务 detail"两级结构，防止知识文件长成无法通读的一团
  - 每次测试校正、每次口径裁定、每个新接口都回写知识；项目越用越懂自己。这些知识存在项目目录里，全团队共享——换人接手、换会话继续，上下文不丢失
- **渐进加载：按目标拉取最相关的上下文。** 全量加载只是起点——此后由 Agent 根据用户目标自行决定补什么：
  - **detail 知识**：按任务 `load` 进上下文，任务完成自动 `unload` 释放；超出上下文预算的文件只列目录供按需读取
  - **相关技能与工具**：`jira`、`confluence`、`confluence-reader` 技能等，按任务需要调用，把外部系统的实时信息拉进对话
  - **代码知识图谱**：`repo_graph` 在克隆仓库上自动构建确定性结构图，「定义在哪、谁调用、影响面多大」几 token 查询作答，代替通读整个文件；语义再读原文
  - **项目图谱**：聚合各仓库代码节点与知识文档节点，结构层默认轻量呈现，符号层按需展开
  手边只放当前目标需要的东西，用完即放
- **更新：显式退役，错了就改。** 过时条目标注 `[stale]`，经用户确认后退役；全部变更记入 `history.md`，残留的索引行由 `purge` 清理。退役不是数据丢失，而是可审计的修正
- **记忆：分层沉淀。** 会话笔记（临时）、个人记忆（跨会话的用户与项目事实，注入系统提示词）、情景记忆（历史摘要回放），与项目知识（L5）、全局框架知识（L7）逐级沉淀，各司其职——从 L0 上下文一路分层到 L7 全局系统
- **执行有边界。** `plan_task` 先规划再动手；密钥加密、明文不进 LLM 上下文，以个人身份与最小权限执行；思考与执行全程 WebSocket 可见可审计
- **传承：经验离开个人对话，成为共享资产。**
  - 智能体之间的差距往往不在模型，而在上下文里沉淀的经验——可惜这些经验过去锁在私人对话里。我们把经验提取成普通文件：技能、工作流、知识条目与分层记忆，再叠加开源
  - 技能、工作流、知识条目都是普通文件——可复制、可提交、可开源；一个人沉淀好的经验，一万个人可以直接用，新项目又会继承这些资产从完整上下文出发
  - 能力从此离开个人，成为可读取、可复制、可传承的组织资产——**这就是"技术平权"得以实现的机制**。闭环由此闭合：**知识随使用不断生长，团队共享同一份上下文，能力可复制可传承。**

更细的代码入口与内部机制见[实现原理指南：从聊天到可审计的企业智能体](docs/agents-universe-implementation-guide.zh-CN.md)。

## 核心特性

- **多 LLM 提供商** — Anthropic / OpenAI / Azure OpenAI / Gemini 统一接入；用户自配模型与密钥（AES-256-GCM 加密），会话级切换，失败自动回退
- **开箱即用的角色** — Product Owner / Tech Lead / QA、数据分析专家、办公助手、智能客服、渗透测试专家、性能测试专家等 12 个智能体选定即用，职责与用途详见[内置智能体与协同工作](#内置智能体与协同工作)
- **分类知识条目** — 创建项目时选择分类（软件项目 / 旧系统改造项目 / 数据分析 / 智能客服 / 文档知识库 / 其他），自动初始化对应的知识条目子集，新项目第一天就带着领域骨架
- **定时任务** — 在「定时任务」页签按 cron 周期自动跑脚本、Playwright 测试或智能体提示词，运行历史与实时日志可查，结果摘要还能回到会话里继续追问；也可以在对话里直接让 Tech Lead / QA 帮你建
- **上下文压缩** — 长会话自动生成历史摘要，长时间工作不掉线
- **企业就绪** — 双层执行沙箱、项目隔离与最小权限、JWT 鉴权、SQL Server + Redis、Docker 一键部署

## 内置智能体与协同工作

平台内置多种开箱即用的智能体，全部**选定即用**，不需要写任何 Prompt 或配置——像一支 24 小时在线的小团队。其中 **Product Owner / Tech Lead / Quality Assurance** 三个敏捷角色组成一条完整的 需求 → 实现 → 验证 交付链路：它们都遵循"知识优先"原则——选中项目即全量加载项目上下文（业务背景、词汇表、页面地图、API 清单、测试模式、历史记录……），通过 `[[slug]]` 交叉引用互相衔接，任何工作都建立在项目的全部上下文之上，而不是孤立的一次性对话。

| 智能体 | 职责 | 典型用途 |
|---|---|---|
| **Product Owner** | 需求澄清、故事卡、Jira 跟踪 | "聊聊这个需求" |
| **Tech Lead** | 读代码、写实现、跑测试、提 PR | "把这张卡做出来" |
| **Quality Assurance** | 测试设计、Playwright 脚本、回归 | "验证这张卡" |
| **办公助手** | PPT / Excel / Word / 网页演示 / ZIP 打包解压 | "做一份汇报 PPT""整理成 Excel""把这个文件夹打包成 zip" |
| **数据分析专家** | 对齐项目指标口径取数（业务库 + 本地文件），覆盖异动归因、例行报告与图表看板 | "解释这个指标为什么涨" |
| **智能客服** | 严格依据项目知识作答的客服问答、政策咨询、自动转人工 | "查一下退换货政策" |
| **渗透测试专家** | 授权范围内白盒渗透、动态验证、修复建议 | "给整个项目做一次渗透测试" |
| **性能测试专家** | API 性能/负载测试场景设计、Jira/Confluence 回写、Python 压测与 MD+PDF 报告 | "给订单接口做一次基线压测" |
| **招聘助理** | 简历邮件收取、附件解析、四维初筛评分与 shortlist 报告 | "筛一下这批简历" |
| **项目定制专家** | 项目知识、配置与工作区定制 | "帮我建立这个项目的知识" |
| **智能体定制专家** | 智能体定义、职责边界、工具组合定制 | "创建一个财务审核智能体" |
| **外部系统集成专家** | 外部系统接入、认证配置、联调排障 | "接入我们的 CRM 系统" |

### Product Owner：需求侧

- **对话式澄清需求** — 用自然语言描述想法，PO 会通过**选择卡片**（给出具体选项，而非开放式提问）逐项澄清范围边界、用户角色、验收标准等歧义点，最多 2-3 轮即可收敛，绝不从模糊输入直接开工
- **拆分故事卡** — 把澄清后的需求拆成可测试的故事卡：清晰的摘要、描述、可验证的验收标准，落进 Jira 跟踪
- **自动估点** — 每张卡按 BE/FE 拆分并给出点数估计（story-point-estimator），假设条件显式声明
- **文档协同** — 从 Confluence 拉取文档沉淀进项目知识、同步 Jira 状态、生成发布说明与变更日志

### Tech Lead：实现侧

- **把卡直接变成代码** — 给一张 Jira 卡就能完整交付：克隆 → 读代码 → 写实现 → 跑测试 → commit → 创建 PR；脏工作区、main 非快进、测试失败都是硬性门禁，通过后才继续
- **跨仓库一次交付** — 一张卡同时涉及多个项目时，每个仓库独立分支、独立 PR，一次对话提交全部改动
- **PR 对照卡片审查** — 审阅 PR 时把实现和卡的描述**联合起来对照**：验收标准是否满足、状态流转与字段映射是否一致、有没有范围蔓延，输出"实现 vs 卡"的一致性结论与风险点
- **兜底流程** — 无推送权限时自动走 fork → push → 创建 PR 的流程，贡献开源仓库同样可以

### Quality Assurance：验证侧

- **对话造数据** — 通过 SQL 查询、Kong/API 调用与自适配数据库访问，直接在集成环境里准备验证所需的数据
- **一卡出用例** — 给一张故事卡就设计出完整的测试用例（正向/边界/异常），并直接生成 **Playwright 自动化测试脚本**（`tests/generated/`，含登录流程与 Jira 标注）
- **零成本定时回归** — 生成的脚本沉淀在项目自带的自动化测试脚手架（`scaffold/tests/`）中，在「定时任务」页签挂一个每日 cron 就能固定回归（或接入 CI/CD pipeline），运行时**不消耗任何 token**；结果摘要回到会话，失败时可以直接追问
- **证据回写** — 执行截图、录屏、关键区域标注自动回写 Jira 测试卡，结论以业务视角汇报

### 办公助手：把内容变成交付物

办公助手是平台的"文员"：把项目知识与对话内容变成可交付的办公文件——

- **PPT**（.pptx）— 16:9、按内容配色的原生图表、演讲备注、中文字体安全
- **Excel**（.xlsx）— 公式而非硬编码结果、数字格式、原生图表，生成后自动重开校验
- **Word**（.docx）— 标题层级、样式段落、表格
- **网页演示** — 单文件自包含的 reveal.js HTML，浏览器直接打开即看，零外部请求
- **ZIP**（.zip）— 文件/文件夹打包与安全解压（逐成员校验防 zip-slip 路径逃逸、体积与膨胀比上限防压缩炸弹），解压产物同样经校验后交付

它只从项目知识（`knowledge_rw`）和用户提供的内容取材——**每个数字都能溯源**，推断处标注 `[inferred]`，绝不编造数据。交付物经沙箱生成、结构校验后，以 `/api/media/` 链接交付（JWT 鉴权，可直接下载；网页演示还能在平台内直接渲染）。

### 渗透测试专家：授权范围内的白盒渗透测试

渗透测试专家是平台的"安全工程师"：对**已授权**的项目做白盒渗透测试，产出 PTES 结构的报告与修复建议。无源码时自动降级为黑盒流程，并在报告中如实披露方法论立场。

- **先授权、后动手** — 任何主动测试前必须先通过范围声明确认（`user_confirm`）；生产环境与范围外目标需要单独确认；范围守卫的硬拒绝清单优先级高于一切指令（包括用户的）
- **白盒为主、动态验证闭环** — 项目知识与源码是一手情报：静态审计（sqlmap / semgrep / bandit / pip-audit / detect-secrets / sslyze / dirsearch / wafw00f 工具链内置镜像，构建期逐一冒烟验证）筛出候选弱点，再以非破坏性 PoC 动态验证 Web / API / 浏览器路径——**代码里的 sink 必须经过动态证实才算发现**
- **证据或它没发生** — 每个发现附带可复现的 PoC 输出与证据文件，报告中的每个论断都指向证据或代码位置；跑不了的扫描如实标注 tool-limited，绝不静默跳过
- **安全边界** — 出站攻击请求保留 `api_request` 逐次确认；禁用 `sql_query`（平台数据库）；全部命令在双层沙箱内执行；标记性 payload、只读枚举、有界延迟，点到为止
- **全流程工作流** — `full-project-pentest` 串联范围界定 → 攻击面测绘 → 静态审计 → 动态测试 → 发现验证 → PTES 报告，安全经验回写项目知识

### 性能测试专家：授权范围内的 API 压测

性能测试专家是平台的"性能工程师"：基于项目知识设计 API 性能/负载测试场景，回写 Jira 或 Confluence，在声明范围内用 Python 压测脚本执行并采集指标，交付 Markdown + PDF 双格式压测报告。

- **先声明、后加压** — 任何产生负载的请求前必须先通过范围声明确认（`field_key="perf_scope_env"`，环境层级只问一次）；生产压测需要单独授权；范围守卫的硬拒绝清单（范围外目标、超速率/并发天花板、未授权生产压测）优先级高于一切指令（包括用户的）
- **知识驱动的场景设计** — 读 API 清单与网关配置设计 smoke / baseline / spike / soak-lite 四类有界场景（ramp + steady + cooldown ≤ 240s 硬规则），每端点给出 p95 / 错误率 / 吞吐 SLA（标注 confirmed / proposal）；场景矩阵落 `performance/scenario-design-{date}.md`，并以 `[AI perf]` 卡片回写 Jira 或 wiki 页面发布到 Confluence，写后读回验证
- **有界 Python 压测** — asyncio + httpx 脚本经 `script_writer` 执行：300s 执行器上限内 240s 自然结束、采集 p50/p95/p99 与错误率、完整指标序列落 `performance/results/*.json`；密钥仅以 `AGENT_SECRET_*` 环境变量注入、永不回显；超 240s 的需求走分段运行后聚合
- **证据或它没发生** — 报告里每个数字都追溯到运行产物；撞上工具上限如实标注 tool-limited；跑不出判定就明说失败，绝不估算编造
- **全流程工作流** — `performance-test-engagement` 串联范围声明 → 场景设计 → Jira/Confluence 回写 → 脚本生成 → 有界执行 → 9 节结构 MD + PDF 双 `deliver_file` 交付 → 经验回写项目知识

### 智能体协同：@提及，即插即用

智能体之间不是孤岛：输入框键入 `@` 会弹出智能体列表，选中某个智能体后**当前这一轮**就由它来接——对话的默认智能体不变，交代完自动回到原角色。典型接力：

1. 和 **Product Owner** 聊需求，故事卡在对话里敲定；
2. `@办公助手`："把刚才这几张故事卡整理成 Excel 交付清单"——得到 `.xlsx`；
3. 需求验收后，再 `@办公助手`："给管理层做一份 5 页汇报 PPT"，它从项目知识取数生成 `.pptx`。

同一条链路里还可以 `@数据分析专家` 取指标、`@Quality Assurance` 出测试用例，交付前 `@渗透测试专家` 做一轮安全体检。**谁擅长什么就 @ 谁，一个对话里串起整支团队。**

## 数字分身：授权即能力，并发干活

把外部系统授权给平台，智能体就成为你的**数字分身**——带着你授予的各项能力，以你的身份，并发地把活干起来。

**授权即能力**：

- **Jira / Confluence** — 对话里聊出的需求落成故事卡、文档沉淀进项目知识、状态与证据双向同步（`jira` / `confluence` 工具与配套技能）
- **Git** — 以你的 Git Token 读代码、提交、创建 PR，进入你授权的仓库
- **MCP 服务器** — 按 Model Context Protocol 声明接入的外部工具，`mcp` / `mcp:<slug>` 按需启用，能力随插随用
- **凭据托管** — `user_tokens` / `project_secrets` 两级 AES-256-GCM 加密，明文永不进入 LLM 上下文，每次执行以授权者身份留痕

**并发干活**：

- **多线推进** — 多会话、多项目同时开工；@提及与委托让多个智能体在同一条链路上接力
- **无人值守** — 「定时任务」按 cron 周期自动跑脚本、Playwright 回归与智能体任务，结果摘要回到会话接着追问
- **全程可见** — 思考与工具调用 WebSocket 实时推送，谁在什么时候以你的身份做了什么，可审计

**你不在场，分身也在干活。**

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
├── docker-compose.example.yml  # 容器编排示例（复制为 docker-compose.yml 后使用）
├── docker-entrypoint.sh  # 容器入口：DB 迁移 → 全局知识索引 → nginx → uvicorn
├── nginx-combined.conf   # nginx 反代配置（Web UI 与 /api 转发）
└── Dockerfile            # Multi-stage 构建
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

**自动模型路由**：Composer 模型选择条可切换到「自动」，按任务复杂度在用户已配置的模型间路由：

- 每个模型在 Settings 中标注档位（轻量/标准/旗舰，`complexity_tier`）；添加时按 model_id 关键词自动推断默认值，可手动修改
- 普通轮次：先用最便宜的档位模型做一次轻量复杂度预分类（单次小调用，失败静默回退），再按结果选择模型
- `plan_task` 子任务：直接按 `estimated_complexity` 路由，不额外调用
- 缺档就近回退（成本优先：缺哪档先退到更便宜的档位）；所有档位都缺失时退化为默认选择
- 每个子任务实际执行的模型在任务卡片中可见（`modelName`），消息徽章显示「自动 → 实际模型」

### 知识系统

- **无嵌入模型** — 项目选中时全量加载知识文件，无向量搜索
- **两级加载** — primary 文件全量载入上下文；`knowledge_level: detail` 文件仅索引元数据与摘要，Agent 通过 `knowledge_rw` 工具按需加载、任务完成即释放
- **渐进加载** — 全量加载只是起点：Agent 按用户目标按需拉取 detail 知识、相关技能/工具（Jira / Confluence）、代码知识图谱（`repo_graph`）与项目图谱；动态 load 事件跨轮持久（`knowledge_load_events`）
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

创建项目时按分类（软件项目 / 旧系统改造项目 / 数据分析 / 智能客服 / 文档知识库 / 其他）从 `knowledge/_template/` 初始化对应的知识条目子集——例如「数据分析」项目自动带上数据源清单、指标口径字典、分析场景、SQL 模式等 13 个知识条目；「旧系统改造」项目自动带上旧系统架构、数据模型、接口、业务流程、资源接入，新系统架构、接口、目标流程，以及新旧差异对照与迁移改造计划等 17 个知识条目；「其他」分类则进入项目定制专家访谈，由对话逐步填充知识。`PROJECTS_ROOT` 是必需的环境变量，指向仓库外部的目录。

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

## 智能体即服务（Agent-as-a-Service）

把任意智能体和它所在项目的资源发布成 **API** 或 **系统内嵌页面**，两种形态都运行在**发布者绑定**的模型配置上。

**发布管理**（SSO 登录后，`/settings/publishes`）：

- 选择一个项目（需有管理权）、一个智能体、一个你自己的模型配置，即可创建发布；「响应模式」可选 **SSE 流式**（默认）或 **JSON 一次性**，之后也能在卡片上随时切换
- 发布可分别开关「页面」与「API」两个入口；删除发布会同时删除其全部密钥
- 每次新建密钥只返回一次明文，之后仅存 SHA-256 哈希 + 4 位提示（`key_hint`）

**API 形态** — `Authorization: Bearer <key>` 鉴权，响应形态由发布配置决定：

```bash
curl -N -X POST "$BASE/api/p/{publish_id}/stream" \
  -H "Authorization: Bearer $PUBLISH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "thread_id": "any-caller-thread-id"}'
```

- **流式**（`response_mode: "stream"`）— 返回标准 agent 事件帧（`stream_delta` / `tool_call_start` / `stream_end` …），每帧一行 `data: {json}`
- **JSON**（`response_mode: "json"`）— 跑完一轮返回单个 JSON：`{thread_id, message_id, content, thinking, images, files, usage, stop_reason, error}`，调用方检查 `stop_reason`/`error` 判定成败

**附件上传** — `POST /api/p/{publish_id}/upload?thread_id=...`（multipart，一次一个文件，≤10MB）返回 `{url, name, media_type, size}` 引用，随 `RunRequest.attachments`（≤10 个）发出；字节驻留内存 1 小时、按线程配额，深度校验与注入复用平台内会话的同一条管线。`thread_id` 把同一调用方的多轮对话钉在专属会话上以保留上下文。

**页面形态** — `/p/{publish_id}` 是系统内的一个链接：访问者必须已 SSO 登录（与主应用同一会话），页面按访问者签发**绑定（发布, 访问者）的 HMAC 查看令牌**（服务端派生、不落库），后续 `POST /api/p/{publish_id}/session/run` 用该令牌以**发布者身份**执行，走与 API 相同的 SSE 流。

**安全模型**：

- API 密钥只存在于请求头，永不进 URL、永不回显、不落明文；哈希比较用 `hmac.compare_digest`
- 共享的发布会话归**发布者**所有：查看者不拥有会话，也看不见发布者的其他会话/记忆/项目
- 页面与 `page_enabled=false` 的发布对外一律 404，无存在性泄露
- 进程级信号量（16 并发）限制同一 worker 上的并发运行，超限即 429；同一会话并发运行返回 409
- 非交互模式（`interactive=False`）下 `user_confirm` 等确认型工具直接返回可读错误，不会静默放行

## 定时任务（Scheduled Tasks）

项目布局的第四个顶层页签：让平台按时间自己跑起来，不用人记得喊一句。

**三种运行目标**：

| 目标 | 说明 | 前置 |
|---|---|---|
| 自动化脚本 | 项目里的 python / bash 脚本 | 先用脚本执行器或让 Tech Lead 写好并跑通 |
| Playwright 测试 | `tests/generated/*.spec.ts` | QA 生成的 E2E 脚本 |
| 智能体任务 | 一段提示词交给指定智能体无头执行 | 选择投递会话 |

**周期**：标准 5 字段 cron（分 时 日 月 周），时区可配；日与周同时被限定时按 POSIX 的「或」语义匹配。界面上给出常用预设与接下来三次运行时间预览。

**运行历史与日志**：每次运行落一条记录（排队中 / 运行中 / 成功 / 失败 / 已跳过），脚本与 Playwright 运行点开即接现有实时日志流；结果摘要按需投递到指定会话，用户可以接着追问。

**对话式定制**：Tech Lead 与 Quality Assurance 都带 `scheduler` 工具与 `generation/scheduled-task-writer` 技能——「每天早上 9 点跑一下 XX 脚本」即可创建，并可随时改周期、启停、立即运行。

**行为边界**：

- 同一任务上一次还没结束，本次记为「已跳过」，不叠加，也不打断正在该会话里对话的用户
- 服务停机期间错过的周期**跳过不补跑**，只排下一个未来时间点
- 脚本 / Playwright 与页面手工运行共用全局 3 个并发槽；智能体任务并发上限 2
- 智能体任务以无交互模式运行：需要人工确认的写操作会返回可读错误，而不是静默放行
- 任务本身不存任何凭据；提示词明文存库并每次投递，禁止写入密码 / token / 密钥

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

## 参与贡献与延伸阅读

- [贡献指南](CONTRIBUTING.md) — 开发环境、提交规范、硬性准则，以及[零代码共建](CONTRIBUTING.md#零代码共建用对话为项目做贡献)的完整流程
- [实现原理指南](docs/agents-universe-implementation-guide.zh-CN.md) — 从聊天到可审计的企业智能体：代码入口与内部机制
- [安全政策](SECURITY.md) — 漏洞报告方式
- [更新日志](CHANGELOG.md) — 版本演进记录
- [许可证 Apache-2.0](LICENSE) — 平台、技能、工作流、知识全部开放
