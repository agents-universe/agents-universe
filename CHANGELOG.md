# Changelog

本项目的所有重要变更都会记录在此文件，格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **项目分类** — 创建项目时选择分类（软件项目 / 数据分析 / 文档知识库 / 其他），不同分类初始化不同的知识条目子集；分类注册表 `knowledge/categories.yaml`，前后端共用；「其他」分类项目创建后自动路由到项目定制专家进行知识定制访谈；「数据分析」分类新增 7 个数据专用知识条目（数据源清单、表模型、加工链路、指标口径字典、分析场景、SQL 模式、分析模式），组成 11 个知识条目的子集
- **数据分析专家智能体** — 新增全局智能体 `data-analyst`（数据分析专家）：外部业务库经 `shell` env_refs 密钥注入只读取数、本地 CSV/Excel/Parquet 经 `code_executor` 分析，指标口径对齐项目 metric-catalog，产出对话报告 / PNG 图表 / 自包含 HTML 看板；配套 6 个 `analysis/*` 技能（sql-crafter、local-file-analyst、data-profiler、metric-investigator、dataviz、report-writer）与 4 个工作流（ad-hoc-analysis、metric-deep-dive、recurring-report、data-source-onboarding）；agent-core 新增 pandas / matplotlib / openpyxl / pyarrow 运行时依赖

## [0.1.1] - 2026-08-09

### 文档

- 贡献指南新增「零代码共建」章节 — 通过 [agents-universe.com](https://agents-universe.com) 配置模型与 Git Token，与 Product Owner / Tech Lead 对话即可让智能体写代码并提交贡献；欢迎修复 bug 与提交小修改

## [0.1.0] - 2026-08-08

首个公开版本。全栈企业级 AI Agent 平台，让智能体像人一样学习和工作。

### 新增

- **Agentic Loop** — `plan_task` 结构化任务规划，任务树持久化到 `agent_tasks`，WebSocket 实时推送执行过程
- **多 LLM 提供商** — Anthropic / OpenAI / Azure OpenAI / Google Gemini 适配器，懒加载注册，用户自配模型（AES-256-GCM 加密存储）
- **知识系统** — 无嵌入模型、项目全量加载 + `[[slug]]` 显式交叉引用；两级加载（primary 全量 / detail 按需），溢出文件列表机制
- **技能系统** — 四类技能：guidance / template / executable / composite
- **工作流系统** — Markdown 定义（`.workflow.md`），Agent 读取并执行，无 YAML 引擎
- **Codex 风格 Web UI** — Vue 3 + CodeMirror 6，三栏布局，WebSocket 实时流
- **子项目工作区** — 每个项目隔离的 knowledge / tests / .tmp 目录
- **企业安全基线** — JWT 认证、OAuth SSO、项目隔离、最小权限执行
- **容器化部署** — Docker multi-stage 构建 + docker compose，SQL Server + Redis
