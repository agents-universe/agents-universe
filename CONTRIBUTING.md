# 贡献指南

欢迎参与共建！Agents Universe 的理念是**开源共建、技术平权**——每个人贡献一份能力，所有人共享一份便利。

> 💡 **不写代码也能贡献** — 本项目已部署在 [agents-universe.com](https://agents-universe.com)，并且正在被它自己管理：平台上的 **Product Owner** 与 **Tech Lead** 智能体就是这个仓库的日常维护者。注册并配置模型与 Git Token，用对话即可让 Tech Lead 为你写代码并提交。详见下文 [零代码共建](#零代码共建用对话为项目做贡献)。

## 可以贡献什么

| 方向 | 位置 | 说明 |
|---|---|---|
| 技能 | `agents/skills/**/*.md` | 四类技能：`guidance`（指令）、`template`（模板）、`executable`（可执行代码块）、`composite`（技能链） |
| 工作流 | `workflows/*.workflow.md` | 把一套完整工作方法沉淀为可复用的步骤 |
| 知识 | `knowledge/` | 系统文档、技术文档、新项目模板 |
| Agent 定义 | `agents/*.agent.md` | 定义新角色（前端工程师、QA、分析师……）的行为与边界 |
| 代码 | `packages/` | agent-core（Python 库）、api（FastAPI）、web（Vue 3） |

**不熟悉代码也没关系**——技能、工作流、知识都是 Markdown 文件，写 Markdown 就是贡献；而且平台上还有一条纯对话的零代码路线，见下文。

## 零代码共建：用对话为项目做贡献

Agents Universe 部署在 [agents-universe.com](https://agents-universe.com)，而且它正在管理它自己：平台上运行的 **Product Owner** 与 **Tech Lead** 智能体，就是构建这个仓库的"员工"。需求由 Product Owner 澄清、拆解并确认验收标准；代码由 Tech Lead 阅读、实现、测试并提交。这意味着给这个项目做贡献**不需要写一行代码**：

1. **注册** — 打开 [agents-universe.com](https://agents-universe.com) 注册账号
2. **配置模型** — Settings → **AI Models**，填入你自己的 LLM API Key（支持 Anthropic / OpenAI / Azure OpenAI / Gemini），密钥经 AES-256-GCM 加密，只属于你
3. **配置 Git Token** — Settings 中添加你自己的 Git Token（支持 GitHub Enterprise 自定义端点），提交以你的身份、进入你授权的仓库
4. **聊天驱动开发** — 创建或加入项目，用自然语言向 **Product Owner** 描述需求，再让 **Tech Lead** 动手：读代码、写实现、跑测试、提交 commit 并创建 Pull Request

**欢迎修复 bug 和提交小修改** — 遇到 bug、文档笔误、体验问题，不必自己动手：直接把这些需求告诉 Product Owner 和 Tech Lead，由智能体修复并提交 PR；也可以在 [Issues](https://github.com/agents-universe/agents-universe/issues) 里提出来，方便维护者跟进。

> 💡 传统开源贡献流程是 fork → clone → 写代码 → PR；在这里，它变成了一段对话。你负责提需求和评审，代码交给智能体。下面的章节是给想深入开发的人准备的。

## 开发环境

### Docker 一键启动

```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
docker compose up --build
```

Web UI: http://localhost:5173 · API: http://localhost:8000 · Docs: http://localhost:8000/docs

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

# 数据库迁移 (packages/api/)
alembic upgrade head
```

## 提交规范

- **分支**：从 `main` 新建分支，命名如 `feat/skill-java-qa`、`fix/knowledge-overflow`
- **提交信息**：`<type>: <简述>`，type 取 `feat` / `fix` / `docs` / `refactor` / `test` / `chore`
- **测试**：代码改动需通过对应包的测试（`pytest` / `npm test -- --run`），CI 会自动运行
- **PR 描述**：说明改动目的与验证方式；UI 改动附截图

## 硬性准则

1. **不提交任何秘密** — API Key、令牌、域名、内部路径只进 `.env`（已被 gitignore），提交前检查 diff
2. **知识文件用 `[[slug]]` 交叉引用**，不要复制粘贴内容
3. **DB 主键始终 `UNIQUEIDENTIFIER`**，不用 `IDENTITY`
4. **SQL Server 驱动用 `mssql+aioodbc`**，不用 `pymssql`
5. **项目隔离** — 所有知识查询限定 `project_id` 作用域，禁止跨项目访问
