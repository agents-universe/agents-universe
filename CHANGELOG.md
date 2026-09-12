# Changelog

本项目的所有重要变更都会记录在此文件，格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **定时任务（Scheduled Tasks）** - 项目布局新增第四个顶层页签「定时任务」：按 5 字段 cron（时区可配）定时触发**自动化脚本 / Playwright 测试 / 智能体提示词**三类目标，卡片显示人类可读周期、下次运行、上次状态与启停开关，运行历史逐条可查；脚本类运行点开即接现有实时日志流（`/ws/script-runs/{run_id}`）。每次运行的结果摘要可投递到指定会话（投递后向该会话推送 `conversation_updated`，已打开的页面即时刷新），用户可继续在会话里追问。Tech Lead 与 Quality Assurance 新增 `scheduler` 工具与 `generation/scheduled-task-writer` 技能，对话里一句话即可创建、修改、启停、立即运行任务。调度为进程内 asyncio 循环（单副本假设），以 `next_run_at` 比较交换抢占到期任务；服务停机错过的周期只排下一个未来时间点不补跑，启动清扫把遗留 `running` 运行落为 `failed`。cron 解析器落在 agent-core（`agent_core.scheduling.cron`，无新依赖），脚本 / Playwright 与人工运行共用全局 3 槽，agent 轮次并发上限 2 且以 `interactive=False` 无头执行
- **智能客服项目分类与智能体** - 新增 `customer-service` 项目分类（8 个知识条目模板：`domain/faq` FAQ、`domain/service-policies` 服务政策、`domain/escalation-rules` 转人工规则、`skills/support-scripts` 话术库 + 背景/历史/第三方 API 与 MCP 集成），新建项目即获客服知识骨架；新增全局智能体「智能客服」（严格依据项目知识作答、答不出明确转人工、经自定义 API 与 MCP 只读查询业务系统）与 2 个客服技能（`support/customer-reply` 应答规范、`support/escalation` 转人工判断）；智能体选择器新增「智能客服」分组

- **提交卫生检查：身份邮箱 + URL 主机白名单** - 约定 14「No corporate identifiers」此前只能人工自查，现由 `scripts/check_commit_hygiene.py` 自动执行。pre-commit 新增两道钩子：`commit-identity` 拒绝 author/committer 邮箱命中禁用子串的提交（读 `git var`，即 git 真正会用的身份，含环境变量覆盖）；`url-policy` 要求内容里的 URL 主机属于保留的 `example` 家族 / 测试域（`.test`/`.invalid`/`localhost`）、回环与文档地址段，或显式白名单里的公开域名，其余（未知名主机 ≈ 疑似内网或公司域名）一律拒绝并给出文件:行号。策略集中在新增的 `.commit-hygiene.toml`：禁用邮箱子串、公开域名白名单、内网后缀黑名单（`.corp`/`.internal` 等）、云元数据 IP 豁免、生成文件跳过，以及 `hygiene:allow-url` 行内豁免（安全夹具与引用敌对主机的文档用）。CI 新增 `hygiene` job，用同一脚本检查推送/PR 范围（新分支或强推回退全历史）与全部跟踪文件——服务端 clone（平台智能体的提交）装不到本地钩子，靠它兜底。随迁现有 15 处占位符与夹具主机（`your-company.com`、`your-org.atlassian.net`、`proxy.corp`、`evil.com`、`h.com`）改为 `example.com` 家族；`LICENSE`、`package-lock.json` 与约 40 处 SSRF 安全夹具（数字型回环、云元数据 IP、compose 服务名）无需改动

### 修复

- **外部集成配多了，MCP 服务器整块消失** - 「AI 模型」弹窗的「外部集成」页签里，集成系统一多，下方的「MCP 服务器」就无踪可寻，滚动也翻不出来。根因是 MCP 区块与页签面板共用 `token-section` 类：面板自身的样式是「滚动用 flex 列」（`flex: 1` 即 `flex-basis: 0`），嵌套的 MCP 区块沿用后，一旦上方集成行把面板撑过 `max-height: 88vh`，剩余空间转负、而基线为 0 的项再缩也缩不到负数，该块高度坍缩为 0（只剩 `padding-top`），内容又被继承来的 `overflow-y: auto` 裁掉。现让该区块只取内容高度、不做内部滚动（`flex: 0 0 auto; overflow: visible`），由面板统一滚动

- **主按钮里的图标不再把文字顶到第二行** - Tailwind preflight 把 `svg` 统一设为 `display: block`，在 flex 容器里无碍，但在普通按钮里图标会独占一行：带图标的主按钮（运行选中内容、新建发布 / 定时任务 / 项目）文字被挤到图标下方。现 `.btn-primary` 改为 `inline-flex` 横向排列（垂直居中、6px 间距、不换行），图标与文字恒在同一行

- **脚本运行日志 WebSocket 被 `/api` 前缀吞掉** - 前端连 `/ws/script-runs/{run_id}`，但 `scripts.py` 的 `APIRouter(prefix="/api")` 同样作用于 `@router.websocket`，实际注册为 `/api/ws/script-runs/{run_id}`；vite dev 代理与合体镜像 nginx 都只对 `/ws/` 转发 Upgrade，握手必然失败，前端每次运行都显示「连接中断，脚本最终状态未知」，而后台脚本其实正常执行。现拆出无前缀的 `ws_router` 并在 `main.py` 的 WebSocket 区块注册，路径回到约定的 `/ws/script-runs/{run_id}`；补真实路由握手测试（未知 run 应得到 handler 的 4003，而非路由缺失的 1000）与前端 socket 路径精确断言，防止前缀回归

- **定义文件校验前置到写入时（自定义智能体第一次就能用）** - 「智能体定制专家」定制的项目智能体第一轮常在选择器里看不到、第二轮让它确认时才发现 frontmatter 有问题。根因是注册为懒同步（`GET /api/agents` 触发 `sync_agents_dir`），YAML 解析失败、缺 `slug`、slug 非法（大写 / 下划线 / 点 / 中文）或缺 `{项目 slug}--` 前缀时只写服务端日志然后 `continue`——既不建行也不报错，DB 里没有这一行，选择器里就完全不出现；写文件的模型没有任何自查手段，只能下一轮自己重读文件。现新增 `agent_core.definition_check`，`filesystem` 工具在写入 / 读取 `agents/*.agent.md`、项目 `skills/**/*.md`、`workflows/*.workflow.md` 时随结果返回 `definition_check`（写入照常成功；`ok: false` 表示该文件不会被注册，需按 `errors` 修正后覆盖写同一路径）。错误项：YAML 解析失败、缺 `slug`、`slug` 字符集非法、`slug` 与文件名主干不一致（运行期按 `agents/{slug}.agent.md` 查找，不一致会「列表可见但一执行就报错」）、项目智能体缺 `{项目 slug}--` 前缀、定义不在 `agents/` 下或被写进项目工作区的 `agents/skills/`。警告项（不影响 `ok`）：`tools` 声明了 registry 中不存在的工具（`mcp` / `mcp:<slug>` 标记不误报）、`skills` / `workflows` 引用找不到对应文件、缺 `display_name`、列表字段写成字符串标量，以及 `workflows/x.workflow.md` 缺 `slug` 时会以 `x.workflow` 注册的 stem 陷阱和文件后缀不符约定。结果同时给出可直接采用的具体值（`expected.slug` / `expected.path` / `suggested_frontmatter`）。slug 规则单点定义在 agent-core，`api.services.agent_sync` 改为复用同一份常量与校验函数，两份实现不会漂移；技能与工作流的轻量校验复用同一入口。智能体定制专家的提示词改为闭环：写完必须读 `definition_check`，`ok` 不为 `true` 时同一轮内修正重写、不得报告完成，并补上可直接复制的 frontmatter 模板与文件名 / slug 一致性规则；`filesystem` 工具说明、`knowledge/system/tool-reference.md` 与应用注入的工作区约定同步说明该字段

- **沙箱代理与浏览器工具对齐** - `code_executor` 此前不做任何代理解析，子进程只是继承进程环境：走的是「碰巧有什么就用什么」而不是应用配置的代理，`.env` 的空占位 `HTTPS_PROXY=` 也会被继承进沙箱，宿主只设了 `ALL_PROXY` 时浏览器直连而沙箱走代理。现与浏览器工具共用同一套解析（设置项 → 大写环境变量 → 小写拼写），以 `HTTPS_PROXY/HTTP_PROXY/https_proxy/http_proxy` 四种拼写注入子进程，未被解析产出的 `ALL_PROXY` 一律删除，`NO_PROXY` 保持继承不变（`main.py` 合并进去的 LLM 主机绕行列表必须存活），解析为空时空占位一并清除。同时堵上代理凭证回显：代理 URL 含 `user:pass` 时，返回给模型的 stdout/stderr 与服务端日志中的整段 URL、userinfo 与口令（含百分号编码及解码形式）都替换为 `[REDACTED:PROXY_*]`；沙箱内用 Playwright 启动的浏览器仍需显式传 `proxy=`（Playwright 不读代理环境变量），已写入工具说明与 `knowledge/system/tool-reference.md`
- **交互提问不再重复弹出** - `user_confirm` 在同一轮内按「决策」（field_key + 归一化问题）记账已答 / 超时 / 被关闭三种结局，重复提问直接返回已记录的答案（`reused`）而不再次弹窗；并行任务同时问同一问题时共用一个弹窗，等待方复用答案。超时不再诱导重问：工具结果附带 `timed_out` / `do_not_reask` 指引，并要求改用文字提问结束本轮；此后本轮的交互提问一律暂停（`prompts_paused`），直到用户作答或中途发消息（视为回到键盘前，解除暂停并允许重问未答之题，已答之题仍复用）。凭据类（`secret=true`）提问不参与记账，永远重新收集。超时异常细化为 `UserSelectionTimeoutError` / `UserSelectionAbortedError`（仍继承 `RuntimeError`，安全门禁行为不变）。渗透测试资产同步收紧：Phase 0 先复用已声明范围（`.tmp/pentest/engagement.md` + `PENTEST_SCOPE` 项目记忆，每轮注入故不受上下文压缩影响）再决定是否提问，只问一个环境 tier 问题（`field_key="pentest_scope_tier"`，入口点/类型/排除项改为默认值陈述），生产授权独立成第二个问题且不再重问环境；recon 技能中原先语义含糊的「Environment Confirmation」改为只记录不提问
- **知识图谱 Java 解析率修复** - 缓存版本升至 2，全量作废旧图强制重解析（此前解析器修复不会重跑未变更文件，稀疏结果永久残留）；失败条目不再复用 SHA 缓存、重建即自动愈合。Java 符号覆盖补齐注解（`@interface`）、模块声明（`module-info.java`）、嵌套类型 `implements Outer.Inner`（此前拆成两个错误目标）与链式调用（`a().b()` 此前丢失对象前缀）。构建统计新增失败原因明细、按语言覆盖率与解析率（含未跟踪源文件计数），写入 graph_report.md 与 compact map。语法文件改为随镜像烘焙：运行沙箱无网络而语言包按需下载，容器内解析此前全部静默失败；现由 `docker/ts-grammars/` 提供（`scripts/fetch_ts_grammars.py` 经镜像链拉取官方包并 sha256 校验刷新），构建期零网络依赖，缺失语法时给出可操作警告与 prefetch 补救指引；`get_grammar` 不再永久缓存失败（grammar 迟到后进程内自愈）。依赖锁定 tree-sitter-language-pack==1.14.3 与 tree-sitter>=0.26,<0.27（ABI 配对），测试补全 Java 语法覆盖且 CI 联网时不再静默跳过

- **API 容器 nginx 自愈** - 组合镜像以 nginx（8000）作为公开入口反代 uvicorn（8001）；此前 entrypoint 用 `exec uvicorn` 独占 PID 1，nginx 进程被杀死后容器保持 Up 而整个站点不可用（健康检查、cloudflared 隧道、18001 端口映射均指向 8000），公网持续 502 直至人工重启。entrypoint 改为监督循环：nginx 以 `daemon off` 作为脚本直接子进程运行，死亡时先清理被 reparent 的孤儿 worker（它们仍占着 8000/8003 监听）再自动重启；uvicorn 死亡则退出容器，交由 compose `restart: unless-stopped` 重建；`wait -n` 同时回收退出子进程，不再堆积僵尸

## [1.4.0] - 2026-08-31

### 新增

- **发布管理成为项目布局顶层页签** - 发布管理从独立的 `/settings/publishes` 页面提升为项目布局第三个顶层页签「发布」（与会话、工作区并列）：页签按当前项目过滤发布列表，创建弹窗锁定当前项目（不再有项目选择器），API 密钥仅按当前发布拉取，切换项目自动重载；新建发布按钮仅项目经理可见（API 仍强制 403）；`/settings/publishes` 重定向到当前项目的发布页签（无项目时回 `/app`）
- **会话 / 工作区双页签** - 顶部导航「知识」「脚本」页签合并为「工作区」：左侧展示 `PROJECTS_ROOT/{slug}/` 下全部文件与脚本（懒加载树，`.git`/`.tmp` 跳过），Markdown 可查看与编辑保存（`knowledge/*.md` 保存后自动重索引），自定义脚本与 Playwright 测试可一键运行并实时流式查看日志。新增 `GET/PUT /api/projects/{pid}/workspace/{files,file}`（`resolve_within` 防路径逃逸）；旧 `/knowledge`、`/scripts` 路由重定向到 `/workspace`
- **仓库知识图谱构建脚本** - 新增 `scripts/build_kg.py` 命令行入口，可在应用外对任意仓库独立构建代码知识图谱

### 移除

- **「版本更新」功能** - 删除侧栏「版本更新」按钮与发布说明弹窗（`WhatsNewDialog`）、`whatsNew` i18n 文案与样式、以及 `tour` store 的 `lastSeenVersion` / `dismissWhatsNew` 状态；后端 `user_preferences.last_seen_version` 字段与 PATCH 入参一并移除（迁移 `c5d7e9f1a234`）。引导导览与 `onboarding_completed` 状态保留

### 修复

- **知识编辑保留 frontmatter** - 工作区编辑知识条目保存时，将正文修改合并回文件原有 frontmatter（DB 索引与 primary-file 两个写路径），无 frontmatter 的文件保持原样直写，不再丢 YAML 头
- **工作区脚本运行与知识交叉链接修复** - 脚本运行结束终态在重新挂载 / 导航后正确恢复；Playwright 规格运行重新提供每项目 `APP_BASE_URL` 输入（localStorage 记忆）并注入运行环境，不再恒为空 body；知识 `[[slug]]` 交叉引用改经知识 API 解析，全局框架文件与未索引文件可正常加载与保存（此前 404）
- **仓库知识图谱静默输出全 0** - 构建只索引已提交（tracked）文件；仓库里存在未提交源码（如未 `git commit` 的 `.java`）时 `files=0 sym=0 edges=0` 且无任何告警，与空仓库无法区分。现默认在 summary 上报 `warning`（未跟踪文件计数 + 样例 + hint），并新增 `include_untracked`（`build_kg.py --include-untracked` / `repo_graph build` 参数）按需将未提交文件并入图谱；缓存记录构建模式，两种模式切换不会复用陈旧 manifest
- **GitHub Actions 四项失败** - Anthropic SDK 1.x 的 HTTP 传输迁移到 httpx2，传入 `httpx.AsyncClient` 抛 `TypeError`；按已安装 SDK 版本探测选择匹配传输（httpx2，回退 httpx）。`paths.resolve_within` 在所有主机拒绝盘符 / UNC 前缀（POSIX 上 `C:\evil` 只是相对目录，逃逸请求此前回 404 而非 400）。publish 迁移 Boolean 列默认值改用 `sa.true()`（Postgres 拒绝 `DEFAULT 1`，sqlite / mysql / mssql 仍为 1）。token_tests bearer 模式也解析 Atlassian 邮箱以擦除错误回显，缺邮箱仍返回 `""` 而非误报未配置
- **PostgreSQL CI 挂起（死锁与 IPv6 超时）** - 知识 PUT 在响应前未提交事务，随后 reindex 对同一行 `knowledge_metadata` 的 UPDATE 被行锁阻塞而等待响应 → 死锁，api-postgres job 挂 40 分钟；改为先提交再响应。publish key 哈希用例插入假 `publish_id`（FK 指向 `agent_publishes`），SQLite 未启用 FK 检查放行、PostgreSQL 拒绝；改为创建父发布并用其真实 id。CI 的 PostgreSQL 服务 URL 改用 `127.0.0.1`：asyncpg 优先把 localhost 解析为 IPv6 `::1`，容器只监听 IPv4，每次连接都等完 IPv6 超时才回退

## [1.3.0] - 2026-08-29

### 新增

- **智能体即服务（Agent-as-a-Service）** - 把智能体和所在项目资源发布成 **API**（`POST /api/p/{publish_id}/stream`，SSE 流式，`thread_id` 钉会话保多轮上下文）或 **系统内嵌页面**（`/p/{publish_id}`，SSO 登录后按访问者签发绑定（发布, 访问者）的 HMAC 查看令牌，以发布者身份执行）。两种形态都运行在**发布者绑定**的模型配置上。发布管理在 `/settings/publishes`：创建 / 开关页面与 API 入口 / API 密钥（SHA-256 哈希 + 4 位 hint，明文只展示一次，可吊销）/ 删除。共享发布会话归发布者所有，查看者不可见发布者的其他会话与记忆；`page_enabled=false` 与页面对外一律 404；进程级信号量限并发（超限 429），同会话并发 409。无头执行内核抽取为 `services/agent_turn.py`（WS 变薄壳，`ToolContext.interactive=False` 下确认型工具返回可读错误而非静默放行）
- **仓库知识图谱支持 Java** - 代码图新增 Java 语言解析（tree-sitter java grammar）：类 / 接口 / 枚举 / 方法符号，跨文件 import（含 static import）解析、继承（extends / implements）与构造调用边；语言按源根（src/main/java、src、仓库根）解析包路径。符号索引按语言隔离，避免 Java 与 TS 同名符号（如 Greeter）互相干扰
- **会话按最近活跃排序** - `conversations.updated_at` 自初始 schema 即存在但从未写入；现于每条消息持久化时更新，会话列表与 /latest 按 `COALESCE(updated_at, created_at) DESC` 排序——回到旧会话即重新置顶
- **中断任务续跑而非重跑** - 中断的运行不再提供 rerun 按钮：启动清扫把硬杀运行的 streaming_snapshot 物化为 interrupted 辅助消息（历史 + 下一轮智能体上下文），将陈旧 running agent_tasks 落为 failed；活跃计划上下文携带逐任务结果摘要，续跑轮可区分已完成与待重做的工作；`stream_end` 在部分输出落库后不再保留快照
- **超限请求降级而非硬失败** - 把字节量跟踪接入压缩决策并新增最小破坏降级链：超过网关字节上限的请求（CJK JSON 转义、base64 图片）自行收缩而非以不透明 413 失败
- **工作流驱动智能体恢复可见计划** - pentest-expert 的系统提示令其走固定工作流阶段而非调用 `plan_task`，整项目请求从不发出 `task_plan_created`，UI 无计划卡片；现将工作流阶段物化为显式任务计划后再按工作流文件执行

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
