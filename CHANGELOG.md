# Changelog

本项目的所有重要变更都会记录在此文件，格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.3.0] - 2026-08-29

### 新增

- **智能体即服务（Agent-as-a-Service）** - 把智能体和所在项目资源发布成 **API**（`POST /api/p/{publish_id}/stream`，SSE 流式，`thread_id` 钉会话保多轮上下文）或 **系统内嵌页面**（`/p/{publish_id}`，SSO 登录后按访问者签发绑定（发布, 访问者）的 HMAC 查看令牌，以发布者身份执行）。两种形态都运行在**发布者绑定**的模型配置上。发布管理在 `/settings/publishes`：创建 / 开关页面与 API 入口 / API 密钥（SHA-256 哈希 + 4 位 hint，明文只展示一次，可吊销）/ 删除。共享发布会话归发布者所有，查看者不可见发布者的其他会话与记忆；`page_enabled=false` 与页面对外一律 404；进程级信号量限并发（超限 429），同会话并发 409。无头执行内核抽取为 `services/agent_turn.py`（WS 变薄壳，`ToolContext.interactive=False` 下确认型工具返回可读错误而非静默放行）
- **仓库知识图谱支持 Java** - 代码图新增 Java 语言解析（tree-sitter java grammar）：类 / 接口 / 枚举 / 方法符号，跨文件 import（含 static import）解析、继承（extends / implements）与构造调用边；语言按源根（src/main/java、src、仓库根）解析包路径。符号索引按语言隔离，避免 Java 与 TS 同名符号（如 Greeter）互相干扰
- **会话按最近活跃排序** - `conversations.updated_at` 自初始 schema 即存在但从未写入；现于每条消息持久化时更新，会话列表与 /latest 按 `COALESCE(updated_at, created_at) DESC` 排序——回到旧会话即重新置顶
- **中断任务续跑而非重跑** - 中断的运行不再提供 rerun 按钮：启动清扫把硬杀运行的 streaming_snapshot 物化为 interrupted 辅助消息（历史 + 下一轮智能体上下文），将陈旧 running agent_tasks 落为 failed；活跃计划上下文携带逐任务结果摘要，续跑轮可区分已完成与待重做的工作；`stream_end` 在部分输出落库后不再保留快照
- **超限请求降级而非硬失败** - 把字节量跟踪接入压缩决策并新增最小破坏降级链：超过网关字节上限的请求（CJK JSON 转义、base64 图片）自行收缩而非以不透明 413 失败
- **工作流驱动智能体恢复可见计划** - pentest-expert 的系统提示令其走固定工作流阶段而非调用 `plan_task`，整项目请求从不发出 `task_plan_created`，UI 无计划卡片；现将工作流阶段物化为显式任务计划后再按工作流文件执行
- **会话 / 工作区双页签** - 顶部导航「知识」「脚本」页签合并为「工作区」：左侧展示 `PROJECTS_ROOT/{slug}/` 下全部文件与脚本（懒加载树，`.git`/`.tmp` 跳过），Markdown 可查看与编辑保存（`knowledge/*.md` 保存后自动重索引），自定义脚本与 Playwright 测试可一键运行并实时流式查看日志。新增 `GET/PUT /api/projects/{pid}/workspace/{files,file}`（`resolve_within` 防路径逃逸）；旧 `/knowledge`、`/scripts` 路由重定向到 `/workspace`

### 移除

- **「版本更新」功能** - 删除侧栏「版本更新」按钮与发布说明弹窗（`WhatsNewDialog`）、`whatsNew` i18n 文案与样式、以及 `tour` store 的 `lastSeenVersion` / `dismissWhatsNew` 状态；后端 `user_preferences.last_seen_version` 字段与 PATCH 入参一并移除（迁移 `c5d7e9f1a234`）。引导导览与 `onboarding_completed` 状态保留

### 修复

- **压缩导致的 LLM API 超时** - 自动压缩在大会话上频发 "LLM API error: timeout"：摘要输入无总量上限（非流式调用必然超时）、降级链二次压缩、以及 httpx 60s 标量超时被 openai SDK 采纳为每次请求的生效超时（大 prompt 首 token 延迟普遍超过 60s，SDK 原生 600s 默认被覆盖）。分相超时（read 300s / connect 10s / write 120s，max_retries=1）+ 摘要输入封顶 60k 字符（保尾部）+ 降级链幂等守卫（已压缩历史不再二次摘要）
- **手动压缩大会话必失败** - 30s 超时包不住无上限的摘要输入，最需要压缩的会话反而压不动；改为 map-reduce（~30k 字符/块、并发 3、总超时 300s），任一块失败仍不删除任何历史
- **字节估算阻塞事件循环** - ASCII 快路径跳过 CJK 正则、tool_calls 免整包 json.dumps、工具 schema 字节数提升到循环外计算一次，消除大会话每轮数十秒的同步 CPU
- **失败轮次静默丢失** - LLM 报错 / 拒答 / 上下文溢出 / 空输出等失败轮次此前零持久痕迹（无 assistant 行、run 标记 completed、error_message 为空），重开会话后错误气泡消失且无任何解释；`stream_end` 现把这些停止路径映射为 failed 并填充 error_message，回放可见
- **SSO 首次登录回跳与会话 cookie 漂移** - OAuth state nonce、重复回调恢复缓存与锚点 cookie 硬编码 600s 过期，登录往返超窗即被踢回登录页（SSO 页面停留或 api 容器重启时必现）；统一会话 cookie 生命周期并放开回跳窗口
- **lifespan 日志导入遮蔽** - 在四个 except 块内 `import logging` 使其成为整个函数局部变量，正常路径（无异常、导入未执行）走到 try 内打日志时触发 `UnboundLocalError`；会话运行 / 快照 / 任务清扫在恢复行数时必打 INFO，冷启动即现
- **SSRF 守卫放行 inet_aton 变体 IP** - `ipaddress.ip_address` 拒绝 glibc inet_aton 变体形式（`2130706433` == 127.0.0.1、`127.1`、八进制 `010.0.0.1` 等），在 SSRF_ENABLED 跳过 DNS 解析（默认）时直通回环 / 元数据；新增复刻 inet_aton 语义的映射器，在 `validate_url` 与 `api_request` 字面守卫双处复检
- **对话框在拖拽中误关** - 浏览器在按下 / 释放目标的共同最深祖先派发 click，对话框内按下、外部释放仍产生目标在外部的 click，拖拽中误关弹窗；改用共享 `useClickOutside` composable（mousedown 记录 + mouseup 判定），弹层用 contains()、全屏背板用直接命中检查
- **reindex_one 深度走查双范围歧义** - 全局知识与项目知识可共享 slug（项目遮蔽），父级深度走查双范围过滤命中两行导致 `MultipleResultsFound`；项目行优先取一，与 `loader._fetch_all_entries` 遮蔽语义一致
- **审计清扫验证缺陷加固** - 全部提供商 / 令牌端点（api_keys、model_configs、tokens、integrations、token_tests、github、jira、confluence）从错误体与 `str(exc)` 中擦除已解析密钥 / 邮箱；LLM 字符串化参数强制转换、工具循环耗尽告警等一并修复
- **仓库知识图谱静默输出全 0** - 构建只索引已提交（tracked）文件；仓库里存在未提交源码（如未 `git commit` 的 `.java`）时 `files=0 sym=0 edges=0` 且无任何告警，与空仓库无法区分。现默认在 summary 上报 `warning`（未跟踪文件计数 + 样例 + hint），并新增 `include_untracked`（`build_kg.py --include-untracked` / `repo_graph build` 参数）按需将未提交文件并入图谱；缓存记录构建模式，两种模式切换不会复用陈旧 manifest

## [1.2.0] - 2026-08-25

### 新增

- **渗透测试专家智能体** - 新增全局智能体 `pentest-expert`（渗透性测试专家）：白盒渗透测试工具链（sqlmap / semgrep / bandit / pip-audit / detect-secrets / sslyze / dirsearch / wafw00f，镜像内置并在构建期冒烟验证），配套 7 个 `security/*` 技能与 `full-project-pentest` 全项目渗透工作流；出站攻击请求保留 `api_request` 逐次确认门
- **会话运行持久化** - conversation runs 落库提供后台执行反馈；会话打开 / 重连时展示最近一次运行的中断 / 失败状态与恢复的部分文本，支持一键重发原始消息
- **git_repo remove_clone** - 删除克隆仓库及其代码图缓存；含未推送提交的脏工作区需用户确认后才可删除
- **api_request 免确认开关** - 智能体 frontmatter `api_request_no_confirm` 可按智能体关闭逐次确认
- **办公助手 PDF 生成**
- **工具循环预算提升** - 提升至 60/30，超限时上报告警事件
- **新会话智能体简介** - 新会话以纯文本展示智能体标签与描述，替代原引导开场

### 修复

- **渗透工具链在 shell 沙箱完全可用** - semgrep 独立 venv（mcp 版本冲突隔离）后，审计钩子按 `_AGENT_EXEC_ALLOWLIST` 放行其 os.exec；`~/.semgrep` 加入 per-user 工具状态根；子进程环境清理空值代理变量；镜像默认 `SEMGREP_SEND_METRICS=off`
- **shell 沙箱放行 multiprocessing worker fork** - detect-secrets 等库级并行工具恢复可用，直接 `os.fork` 仍被拒
- **Web** - 模型配置覆盖在重开时被还原；SSO 会话超时后首次登录回跳；聊天面板底部跟随、中途注入位置与压缩按钮；弹窗仅在完整外点点击时关闭；Mermaid 分块加载与源回退
- **QA** - 截图空心精确标注与 bbox 测量修正
- **构建与测试** - 可复现构建与静态资源 404；无 Chromium 环境自动跳过 browser bbox 测试

## [1.1.0] - 2026-08-21

### 新增

- **仓库知识图谱自动构建** - git clone / checkout / pull 后自动构建代码知识图谱
- **脚本执行器补全** - 顶部导航、Playwright 测试运行、创建表单
- **中英文界面切换** - vue-i18n 全站国际化
- **新手引导** - 引导式漫游 + what's-new 弹窗，用户偏好服务端持久化
- **script_writer 工具** - 配套 custom-script-writer 技能，技术负责人按需生成测试脚本
- **任务来源优先路由** - Jira / PR 任务先调用权威数据源
- **Gemini 迁移至 google-genai SDK**；api_request SSRF 校验后固定解析 IP；回复标注实际执行模型；「其他」分类更名「自定义项目」；「+ 新建对话」在智能体确定后自动开始会话

### 修复

- 运行失败展示与重载后运行状态保持；脚本运行器与媒体服务加固；Mermaid 图重复绘制；模型 id 变更保留手动 tier / 上下文窗口覆盖；静态扫描清理 undefined-name 运行时错误
- 工程化：Web lint 迁移 ESLint 9 flat config

## [1.0.0] - 2026-08-20

首个稳定版本。

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
