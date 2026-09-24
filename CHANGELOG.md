# Changelog

本项目的所有重要变更都会记录在此文件，格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复

- **更新日志 `system/history` 默认不再进入上下文** - 该文件在 Tier-1 被 `knowledge_role: log` 跳过不假，但索引行仍落进 Tier-2 的 `deferred_entries`，每次对话的「Available Detail Knowledge」提示表都带着它；更糟的是 knowledge-manager 的范文只教正文格式，智能体重写时丢掉 frontmatter 后整个更新日志正文会当普通主文件灌进静态区。现 loader 增加 slug 级兜底（`system/history` 无论 frontmatter 如何都进不了任一上下文层），Tier-1 扫描到的 log 文件（含超大文件路径）一并从 deferred 表剔除，`update_context_file` 按 slug+role 双判守门，`knowledge_rw list` 对该 slug 显示 `log` 状态而非误导性的 `unindexed`；knowledge-manager 范文补回 frontmatter 并在层级概念、参考表写明规则，tool-reference 同步。显式 `knowledge_rw read` / `load` 不受影响

## [1.5.0] - 2026-09-24

### 新增

- **思维链流式展示与轮次阶段状态** - 四家提供商的 reasoning 通道（OpenAI `reasoning`/`reasoning_content`、Gemini thought、Anthropic `thinking_delta`）统一解析为 `StreamChunk.thinking`，以 `thinking_delta`/`thinking_end` 推给前端；Anthropic 侧首次真正请求 extended thinking（`AGENT_EXTENDED_THINKING` 默认开，4.6+/5 系用 adaptive + `display:"summarized"`——它们的默认是 omit，不请求就一条也流不出来；haiku 系才给 budget_tokens；网关对 thinking 字段回 400 时按实例记住并回退，不会热循环），签名 thinking 块只在同轮工具循环内内存往返、绝不落库（历史压缩会让签名失效成必然 400）。循环同时发出 `turn_status` 阶段（waiting_model / thinking / responding / running_tool / compressing / degrading），静默期每 5s 心跳重发当前阶段（读共享状态镜像而非包装事件 await，避免把异步生成器关掉）。API 把 thinking 持久化到 `messages.thinking`（超 20k 保留头 12k + 尾 8k），重连 `sync` 回放 thinking 与 phase；委派白名单放行 `turn_status`（父轮阻塞时子轮的阶段是唯一可见性），但 thinking 不进白名单——子轮的推理不能污染父轮气泡。前端新增 ThinkingBlock（live 展开、`thinking_end` 或首个内容 delta 立即自动折叠、历史默认折叠两行截断，渲染为纯 `<pre>` 不走 markdown——CoT 里全是裸花括号与 HTML 片段），状态行改为阶段感知（并行工具具名前两个 + 计数、TTFT/思考/压缩/降级优先于工具数启发式、准备中的工具也计数）；Gemini 的 thought 此前会当正文渲染，现已从内容通道剥离
- **逐模型思考开关与 OpenAI reasoning_effort** - `user_model_configs` 新增两个可空覆盖并贯通到各提供商构造器：`thinking_enabled` 三态（null = 沿用 `AGENT_EXTENDED_THINKING` 即旧行为；anthropic/gemini 生效，openai/azure 接收后忽略——那边 CoT 是解析-only）、`reasoning_effort`（minimal|low|medium|high，仅在四个 OpenAI 请求构造器的 `_is_reasoning_model()` 分支内下发，gpt-4o 类与网关自定义名永远看不到该参数；Azure 走显式参数而非 `**_kwargs`，不会被静默吞掉）。设置 → AI 模型新增两个下拉（分别只在对应提供商展示，minimal 仅对 gpt-5* 放开），更新时以 `model_fields_set` 让显式 null 真正清空覆盖；全 NULL 时请求体与功能上线前逐字节一致，无数据回填
- **智能体间委派（delegate_agent）** - 缺能力的智能体此前只能在文案里"请 @某人"——PR 评审推给 tech-lead、demo 工作流三处交接全是散文、没有机制，提问方也永远看不到答案。现在同一轮内完成：`list_agents` 展示项目花名册，`delegate_agent` 以嵌套 `run_turn` 运行目标智能体，父轮阻塞在工具调用上、拿到结果后自己写最终答复，会话从不切换（那是 @-mention 的语义）。难点是会话级状态只有一份：嵌套轮不 claim、不 register_session、不消费父轮 pending 注入、不 drop_uploads、不 release_turn、不写 conversation_runs、不做任务清扫（`agent_turn.py` 每处显式 `nested` 守卫，漏掉 release_turn 就会把父轮的锁交给第二个 socket）；事件走**失败即丢弃**的 `DelegationTransport` 白名单，`stream_delta`/`stream_end`/`abort_ack`/`task_plan_created`/`token_update` 一律丢弃，否则子轮会提前冻结父轮气泡、清掉父轮弹窗、替换任务面板、双计 token，只有 `user_selection_*`、图片/文件与知识事件放行（子轮仍可请用户确认 push/merge）。子轮回复落为自己的 `messages` 行（沿用 `agent_slug` 归属徽标），下一轮历史前缀 `[display_name]:`；委派期间用户打的字由父轮在下一个步进边界消费、对子轮不可见。边界：`AGENT_DELEGATION_ENABLED` 开关、深度默认 2（0 关闭）、超时默认 1800s（经嵌套轮私有取消事件接入）、链内已有的 slug 拒绝、跨项目 slug 拒绝并记日志、会话级锁经 `holds_gate` 继承（否则孙轮会死锁在祖先持有的锁上）。委派有意放权（窄智能体借宽智能体），子智能体自身的确认门照旧生效；此前靠散文承诺交接的智能体定义现已声明真正可用的工具。UI 上委派渲染为 "→ Delegated to X" 卡片（原因、任务说明、结果）而非原始 JSON
- **定时任务（Scheduled Tasks）** - 项目布局新增第四个顶层页签「定时任务」：按 5 字段 cron（时区可配）定时触发**自动化脚本 / Playwright 测试 / 智能体提示词**三类目标，卡片显示人类可读周期、下次运行、上次状态与启停开关，运行历史逐条可查；脚本类运行点开即接现有实时日志流（`/ws/script-runs/{run_id}`）。每次运行的结果摘要可投递到指定会话（投递后向该会话推送 `conversation_updated`，已打开的页面即时刷新），用户可继续在会话里追问。Tech Lead 与 Quality Assurance 新增 `scheduler` 工具与 `generation/scheduled-task-writer` 技能，对话里一句话即可创建、修改、启停、立即运行任务。调度为进程内 asyncio 循环（单副本假设），以 `next_run_at` 比较交换抢占到期任务；服务停机错过的周期只排下一个未来时间点不补跑，启动清扫把遗留 `running` 运行落为 `failed`。cron 解析器落在 agent-core（`agent_core.scheduling.cron`，无新依赖），脚本 / Playwright 与人工运行共用全局 3 槽，agent 轮次并发上限 2 且以 `interactive=False` 无头执行
- **智能客服项目分类与智能体** - 新增 `customer-service` 项目分类（8 个知识条目模板：`domain/faq` FAQ、`domain/service-policies` 服务政策、`domain/escalation-rules` 转人工规则、`skills/support-scripts` 话术库 + 背景/历史/第三方 API 与 MCP 集成），新建项目即获客服知识骨架；新增全局智能体「智能客服」（严格依据项目知识作答、答不出明确转人工、经自定义 API 与 MCP 只读查询业务系统）与 2 个客服技能（`support/customer-reply` 应答规范、`support/escalation` 转人工判断）；智能体选择器新增「智能客服」分组

- **提交卫生检查：身份邮箱 + URL 主机白名单** - 约定 14「No corporate identifiers」此前只能人工自查，现由 `scripts/check_commit_hygiene.py` 自动执行。pre-commit 新增两道钩子：`commit-identity` 拒绝 author/committer 邮箱命中禁用子串的提交（读 `git var`，即 git 真正会用的身份，含环境变量覆盖）；`url-policy` 要求内容里的 URL 主机属于保留的 `example` 家族 / 测试域（`.test`/`.invalid`/`localhost`）、回环与文档地址段，或显式白名单里的公开域名，其余（未知名主机 ≈ 疑似内网或公司域名）一律拒绝并给出文件:行号。策略集中在新增的 `.commit-hygiene.toml`：禁用邮箱子串、公开域名白名单、内网后缀黑名单（`.corp`/`.internal` 等）、云元数据 IP 豁免、生成文件跳过，以及 `hygiene:allow-url` 行内豁免（安全夹具与引用敌对主机的文档用）。CI 新增 `hygiene` job，用同一脚本检查推送/PR 范围（新分支或强推回退全历史）与全部跟踪文件——服务端 clone（平台智能体的提交）装不到本地钩子，靠它兜底。随迁现有 15 处占位符与夹具主机（`your-company.com`、`your-org.atlassian.net`、`proxy.corp`、`evil.com`、`h.com`）改为 `example.com` 家族；`LICENSE`、`package-lock.json` 与约 40 处 SSRF 安全夹具（数字型回环、云元数据 IP、compose 服务名）无需改动

- **智能体收藏按「用户 + 项目」隔离，并按项目分类自带默认收藏** - 此前侧边栏的智能体收藏是 localStorage 里的一个全局数组：同一浏览器上所有项目共用一份，换账号登录也共用；而收藏按 slug 存，`{项目slug}--{名字}` 形式的项目专属智能体一切到别的项目就查不到，看起来像「收藏丢了」。现按 `agents-universe:agentFav:{userId}:{projectId}` 分区，用户之间、项目之间互不可见。同时每个项目按其分类自带默认收藏——`software`：Product Owner / Tech Lead / QA；`data-analysis`：数据分析专家 / 办公助手；`customer-service`：智能客服 / 办公助手；`docs`：办公助手 / 项目定制专家；`other`：项目定制专家——新建项目的侧边栏不再空空如也。默认与用户改动的关系是「分类默认 − 已移除 + 已添加」，只持久化两个增量数组，因此以后调整默认名单对已有项目同样生效（用户手动增删过的项除外）。选择器里默认项带「默认」标记，底部新增「恢复默认收藏」；顺带修好分组——`data-analysis` 与 `office-docs` 的智能体此前落在「未分类/未知」里。旧的全量收藏 key 直接删除、不迁移，升级后侧边栏即为分类默认列表；项目收藏（侧边栏项目列表）仍是浏览器级全局列表，语义不变。默认名单暂以 `utils/agentFavorites.ts` 的前端常量维护，与 `knowledge/categories.yaml` 的分类 slug 一一对应

- **运行判定、产物与历史** - Playwright 运行结束后 UI 里只剩流式 stdout：通过/失败汇总、失败用例、截图、视频、HTML 报告全在磁盘上，而 Playwright 下一次运行会先清空自己的输出目录——事后看盘也只有最后一次尝试。现每次运行捕获：JSON reporter 结果（计数、逐用例状态与耗时、失败信息、总时长）归一化落到 run 行，运行日志按头 100k 字符截断（测试运行器把结论打在最后，正是这个上限曾把结论吞掉），无 JSON 报告时解析未截断的尾部并标 `partial` 供 UI 说明；附件从 Playwright 输出目录拷贝进 run 专属目录并建 manifest，把每张截图标回产生它的用例而非一个哈希；HTML 报告经 run 绑定的 HMAC 能力令牌 + CSP sandbox 提供——报告是测试产物页（spec 能回显用户输入），给它不透明源与无 cookie，而不是应用源加会话，令牌走 URL 因为这种页面无法自证；报告内 trace 查看器需要不透明源永远注册不了的 service worker，该入口直接给出说明并改为从附件区下载 trace。run 行新增 `spec_slug`（此前所有 spec 共用一个隐藏锚点脚本，行内说不清跑的是谁），工作区因此能列出某 spec 的最近运行并重开；产物按每项目 20 个 run / 512MiB 淘汰
- **文件上传、multipart api_request 与浏览器录屏** - `browser_playwright` 从 9 个操作扩到 17：upload（工作区路径 / 会话附件 / 内存 payload，以及 `via_chooser` 原生文件选择器）、select_option/check/uncheck/hover/press、显式 `record_start`/`record_stop`（页面上下文改为显式，因为录屏必须换进带 `record_video_dir` 的上下文；录像文件在 record 时就命名，下游没人能改名而这个名字正是 Jira 评审里看到的），上传结果回 `uploaded` 而非 `files`（后者会被当成面向用户的交付物）。`api_request` 支持 files + form_fields 的 multipart，并显式拒绝两种静默丢数据的组合：与 `json_body` 并用（httpx 会丢掉 JSON）、调用方自带 Content-Type（丢掉生成的 boundary），GET/HEAD 与无文件的 form_fields 同样拒绝。测试生成器输出真实 `setInputFiles` 而不再把上传步骤降级成 `waitForTimeout(1000)`；jira `add_attachment` 加 50MB 上限并映射 413；两条建页路径汇入同一处 SSRF 路由注册（此前活过重定向的旧上下文所建页面是无防护的）
- **引用并按需安装外部技能（skill_source）** - 智能体定制专家此前只能用框架自带技能拼装项目智能体，现可读取公开 git 仓库发布的技能作参考：`skill_source` 负责全程——`list_sources`（项目文件优先、框架模板兜底，既有项目无需重建工作区即可看到默认目录）、`fetch` 浅克隆到 `{project}/.tmp/skill_cache/`、`list` 枚举 SKILL.md 包或单文件且不返回正文、`read` 包在 untrusted-content 标记里返回、`install` 拷入项目。地址经单一解析器只收 owner/repo、https URL、tree/blob/raw 链接，其余（非 https 协议、scp 式、内嵌凭证、query/fragment、commit SHA）一律拒绝；克隆以 `GIT_ALLOW_PROTOCOL=https`、禁重定向、指向不存在路径的 `core.hooksPath`（克隆会触发 post-checkout，`-c` 参数到不了 git 自己派生的进程，故含环境变量拼写）执行；Git token 仅在目标主机匹配 `GIT_BASE_URL` 时注入，其余一律匿名，第三方私库不在射程内；扫描与拷贝跳过符号链接并复检收容，候选数、manifest 大小、单文件与总量均有上限。安装把上游资产挡在注册表之外：注册文件落在 `skills/imported/<name>.md`（重写 frontmatter：slug、type: guidance、空 triggers、来源 URL + commit SHA），上游原包逐字拷到 `skills/imported/_<name>/`——下划线前缀使 loader 跳过整棵子树，恰好只注册一个技能；安装需用户确认（无会话 / 非交互轮 / 超时即 fail closed），上游 `## Execution` 标题会被拆除（loader 会把这种形状变成可执行负载）。顺带修私库匿名克隆挂在凭证提示上的问题（`GIT_TERMINAL_PROMPT=0` 改为无条件）；github 新增 `search_repositories` / `search_code`
- **QA：服务端 spec 执行通道与全面提速** - 生成的 `.spec.ts` 此前经 agent 侧 shell 执行，反复撞上沙箱网络限制；现优先走平台自有通道 `POST /api/projects/{id}/playwright/specs/{slug}/run`（异步 run_id + 轮询，cwd 钉在 `tests/`、凭据安全的环境、进程树超时击杀），shell 只作兜底，`code_executor` 明令不得接 TypeScript spec（Python 通道跑不了）；`script_run_ws` 接受 AUTH_BYPASS 用户，内网/本地部署里运行日志 WebSocket 不再 403，该通道在那类部署真正可用。提速三处且证据规则不变：测试设计阶段每张卡五次 Jira 往返并为一次（`get_issue_context` 一把取 issue/comments/transitions）、PR 一次取齐 diff/reviews/comments/checks、confluence 并发、`knowledge_rw read` 收 slug 列表，且提示词不再索引系统提示里已有的知识；数据准备把验证过的创建配方沉淀进 `knowledge/_template/test-data-setup.md` 与 `testing/test-data-setup` 技能（先查、最便宜的通道探一次、一次真实读证实、立刻写回配方），`test-designer` 逐用例产出 `dataSetup`，创建方式在设计时定而非执行时现探；执行阶段脚手架整轮只登录一次（session 供全部 spec 复用）、用例默认并行（`PW_SERIAL`/`PW_WORKERS`/`PW_RETRIES` 逃生阀）、重试默认 0（重试会用通过那次的录屏盖掉失败那次、报绿），涉登录/切用户/权限的用例被识别出来故意未登录运行（session 正是它们要测的东西），配置只在摘要仍匹配本生成器写出的版本时就地升级、手改过的一律不动。导航默认 `domcontentloaded` 不再被最慢子资源拖满超时（`load`/`networkidle` 仍可逐次指定），suite 起跑前先探 `APP_BASE_URL`——任何 HTTP 状态都算可达（401 也证明 DNS/TCP/代理通），约 10s 判死而非每条用例各烧一次导航直到 540s 预算耗尽，未指定 `APP_BASE_URL` 时整段跳过
- **项目转私有即切断对外发布面** - 项目翻成 private 后，所有对外发布入口对局外人返回 403 `PROJECT_PRIVATE` 提示：API 密钥流/abort（密钥没有身份可对白名单，私有即切断全部 API 调用）、嵌入页（创建者与白名单成员仍可加载，其他登录用户看到提示；已禁用的页面保持 404）、`/session` 路径（翻转前铸的 viewer token 每次调用重新校验可见性，立即失效）。拒绝码提升为 `auth.PROJECT_PRIVATE_DENIED`，与应用内 REST 门禁共用同一码与文案，前端映射为本地化提示
- **仓库知识图谱索引 Java 21** - `record_declaration` 映射为类节点（此前 record 只剩文件级节点，record 重度的 Java 21 代码几乎无符号）；继承提取重写为一次递归类型走查，收集 extends/implements、接口 extends 与 sealed permits，同文件 sealed 子类型发 inherits 边（跨文件已由各自 implements 覆盖）并做文件内去重（permits 与子类型自己的 implements 不再双发）；补 records / sealed 层级 / 箭头 switch 带 null 分支的解析层与端到端夹具
- **知识写入优先填充既有模板** - 工作区约定注入「Knowledge write priority」，`knowledge_rw` 在新建 slug 时给出放置提示（API/Kong detail 与层级子条目豁免），knowledge-manager / knowledge-ingestion 改为先把内容映射到既有文件、再考虑建新 slug；模板在建项目时实例化一次、不再复制
- **PR 评审只读上下文，绝不执行仓库测试** - pr-review-manager 与 git-pr-manager 明令结论只依据上下文（远端 diff、提交、评审、评论、checks、Jira 卡片、本地只读代码检查），禁止跑 pytest / vitest / npm test 去「验证」PR，`get_commit_checks` 的状态是唯一执行证据

### 修复

- **代理抖动后会话不再永久失联** - nginx 是唯一公网入口且 15 小时内静默死了三次，每次都掐断全部 WebSocket：客户端重试三次（约 7s）就 DELETE 连接条目，而 `connect()` 只挂在会话 watch 上，用户此后每次发送都卡在「WebSocket 未连接」，直到手动切换会话；横幅却还在说「重试中」，`disconnected` 态更是连横幅都不出。现改为两段阶梯 1s/2s/4s → 8s/15s/30s、±20% 抖动、**永不终止**（条目保留，让重连有地方住），`online`/`focus`/visibilitychange 唤醒全部已知连接，`send()` 发现空闲即发起重连，非 connected 状态一律给横幅 +「立即重连」按钮，发不出去的选择确认也明说而非静默丢弃。更糟的是那条「上次运行被打断，输入继续」的横幅在撒谎：中途注入会发 `stream_end(stop_reason="interrupted")` 冻结片段输出但轮次仍在跑，API 却把它当终态结算了 run，`finish_run` 又只从 running 转出，真正的终态写入成了 no-op——生产上 16 秒的 run 挂着「interrupted」、同一会话又继续输出了一小时。现注入冻结带 `injection=True`（持久化冻结片段但留着 run），finally 尾巴在硬取消时发 `aborted`（Stop 不再记成 completed），`finish_run` 加 `replace_snapshot` 只让 stream_end 路径清掉节流快照（abort/兜底路径常常是文本仅存的一份）。另修 `json_utils` 统一走 date-safe `dumps`——工具结果里一个 datetime 曾把整行 assistant 拖垮（生产丢过带 79 个工具调用的回复）。nginx 取证：error_log 走 stderr 的 notice 级（原先指到一个死后仍 0 字节的文件），监督循环打印被收割子进程的退出码（137=SIGKILL vs 0=正常退出，这是唯一能活过进程的证据）
- **detail 知识加载跨轮持久化，父女层级闭合** - 详情级知识加载在每次上下文重建时凭空蒸发：load 事件从不落库，且加载被绑到 `plan_task`，任务一结束就卸载。现加载写 `KnowledgeLoadEvent`（不绑任务）、每轮开始从磁盘重挂、直到显式卸载才消失；`update_context_file` 增加 deferred 分支，写出的 detail 文件只更新 `deferred_entries` 的 summary/word_count，绝不漏进静态 `loaded_content` 区域。层级闭合：写/删同步父 frontmatter 的 children 列表并重索引父条目，children 操作按 memory → DB → disk 回落，Tier-1 磁盘条目携带 parent/children/depth/level/summary，无索引行的 detail 文件仍经磁盘回落浮出，重索引失败返回 `indexed:false` + warnings 而非假成功。技能正文（如 test-data-setup）经 `Agent._active_skills` 在 `plan_task` 的 prompt 重建后存活；`knowledge_level: auto` 的父条目与 detail 同样递延，索引归一到根，无效 slug 的报错讲清 ASCII slug + frontmatter title 规则
- **媒体链接输出为绝对地址并去重** - 各工具此前返回相对 `/api/media/...`，智能体在回复里引用就成了缺部署子路径（如 `/agent`）的断链；现 `ToolContext` 注入 `APP_BASE_URL`/`APP_ROOT_PATH` 由 `_media.media_url()` 构造完整地址（未设置时回退相对路径，本地/测试不变），前端 `withApi()` 原样透传绝对地址。模型自行再拼一次基址产生的 `<base><base>/api/media/...` 在三处兜底归一：agent-core 的 `normalize_media_urls()`（仅 media URL 作用域的反引用正则）、API 落库前、前端渲染前（直播 delta 与历史行都干净）；并把此前一直没提交的提示词规则（工具 URL 是完整绝对地址、原样引用、绝不前置基址/子路径）补发到 office-assistant、dataviz、demo-maker 与 office/* 技能
- **确认弹窗的超时与重连一致性** - 服务端 prompt 超时或会话中止时现在发 `user_selection_cancelled`（prompt_id + field_key + reason），并留在队列压缩保护名单里；前端按 promptId 精确移除对应弹窗，`stream_end`/`error` 再兜底清空——此前唯一清理路径是「用户答了」与 `abort_ack`，超时弹窗永久僵尸、后续每次 user_confirm 叠一层（「再对话弹窗不出现/错乱」）。同时把仍在等待的 prompt 搭现有 WS `sync` 事件回放（payload 在 emit 前登记、随 Future 的应答/超时/作废一并丢弃，重连不会复活已结案的）：切换智能体/项目、切会话、刷新页面后弹窗回来了，按 promptId 去重不叠第二层，回放的 user_confirm 工具调用照活体路径 settle（否则弹窗旁边永远转着「工具运行中」）；服务端已超时的不回放
- **发布面修复组** - 发布运行（嵌入页 `POST /api/p/{id}/session/run` 与 API 流 `POST /api/p/{id}/stream`）此前不带 agent，`run_turn` 回退到字母序第一个全局智能体而非发布时选定的那个；现三处消息构造都传 `publish.agent_slug` 作 `agent_id`。发布页会话此前按 `(project, owner, source, status)` 查找，同 owner+project 的不同发布共享一条 source='publish' 会话：历史混杂、`claim_turn` 跨发布互抢 409、abort 互相打断；`conversations` 新增 `publish_id`/`viewer_id`/`thread_id` 三列（迁移），嵌入页按 (publish, viewer) 隔离、API 流按 (publish, thread_id) 隔离，`/abort` 的 `thread_id` 改为 find-only 不再凭空建行，顺带修两处潜在 500（无范围 `scalar_one_or_none` 多行即炸、同 slug 双行 `MultipleResultsFound`）。发布管理复制链接改为绝对地址（`window.location.origin`）；`ORDER BY project_id IS NULL` 在 SQL Server 是布尔谓词语法错误（error 156），改 `CASE WHEN` 表达式并补四方言离线编译断言
- **E2E 发现的缺陷（触发词、层级日志、GLM 钳制、计划任务）** - `test-data-setup` 触发词补单条措辞（造/创建/准备一条测试）；`sync_parent_children` 异常处理器改用模块级 `_log`（原为 `NameError`）；glm* 系模型豁免 `MAX_OUTPUT_RESERVE` 钳制——服务端思考会烧光 16384 输出预算，轮次被截成零内容；计划任务向依赖方携带已完成任务的结果摘要，后继不再重复探测前驱已证实的 API；任务级超时改由 `AGENT_TASK_TIMEOUT_SECONDS`（默认 600s）配置，硬编码 300s 会砍掉慢思考模型并级联跳过依赖任务
- **shell 工具代理与 Playwright 预算对齐** - shell 的 `_build_env` 改走与 code_executor / 浏览器同一套 `proxy_env()` 归一（不再裸透传 `os.environ`、清掉未解析的 `ALL_PROXY` 与空占位，`NO_PROXY` 保留），stdout/stderr/npm 安装错误经 `redact_proxy_credentials` 擦除代理凭证（含百分号编码与解码形态）；Playwright / npm test 命令默认预算从 30s 提到 300s——工具级中途击杀在智能体看来就是连接失败，让 shell 驱动的浏览器套件显得「连不上目标」（服务端 spec 通道本就允许 540s），显式 `timeout_seconds` 仍优先；文档补 `APP_NO_PROXY` 内网绕行与 shell spec 运行的 `APP_BASE_URL` 要求
- **脚本运行日志 WebSocket 终态关闭幂等** - 发出 done/error 终态帧后 `finally` 无条件 `ws.close()`，而客户端可能已经断开（中途 WebSocketDisconnect，或收到 done 后立刻断），uvicorn 已答过 close 帧，第二次 `close()` 让 Starlette 抛 `RuntimeError("Cannot call send once a close message has been sent")`，日志面板显示 500、运行看着像没执行。现把已关闭场景（`RuntimeError`、`WebSocketDisconnect`）的 finally 关闭吞掉，其余错误照常传播；补两条回归（done 后断开、中途断开）在修复前代码上以生产栈失败
- **生命周期、密钥擦除与输入校验 bug 扫除** - claim 窗口内保住 abort 事件，Stop 不再被静默丢弃（每个运行中的轮次拥有 abort watcher）；MCP 连接中途取消会关掉已连会话与后台传输任务（session 先注册再 gather），浏览器页面按 task context 跟踪并在 cleanup 全关；`api_request`/`api_keys`/`tokens` 擦除 httpx/h11 错误文本与 traceback（含凭证的字节表示转义形态）；`_repo_paths` 把仓库收容进 `repos/<name>`，符号链接克隆无法逃出工作区；github `get_commit_checks` 把零 legacy 状态判为中性（纯 Actions 仓库可报 all_passing），API 失败或无 CI 仍 fail closed；planner/user_confirm 对非对象的 task/options 给出明确报错而非晦涩 AttributeError；知识图谱/索引/加载器、技能加载、沙箱、提供商、发布、model_tier 与 web UI 一批修缮。12 个回归测试文件均先复现再修复
- **构建与测试修复** - npm 安装留下缺失 / 悬空的 `node_modules/.bin` 垫片时（中断过的安装、node_modules 不可写）补一次 `npm rebuild` 再报错，且错误信息指向 bin-links 配置与目录可写性；每项目第二次起重跑 Playwright spec 时 anchor 去重查询取的是裸 str 却访问 `.script_id`，`AttributeError` 必崩，改为直接收集标量；审计扫除遗留的未使用 `statusFor` 让 `npm run build` 在 main 上失败（TS6133），删除；CI 上 `mimetypes` 依赖宿主 `mime.types`（Ubuntu 注册了 `.xyz`）与 50ms 绝对时间断言两条不稳定测试改为桩掉 guess_type / 断言任务间间隔；FastAPI 0.141 下 `include_router` 不再摊平路由，socket 路径守卫空转为永真，改为真实握手断言（`/api/ws/script-runs/...` 应得路由缺失的 1000 而非 handler 的 4003）；pre-commit 声明为仓库级 dev 依赖（`requirements-dev.txt`，纯 ASCII 以免 cp936 让 pip 读崩）
- **拖动文件到对话框任意位置都能加附件** - 此前只有 CodeMirror 输入框自己接了 `drop`：把文件拖到消息列表上松手，不但没有附件，浏览器还会直接打开该文件、整个会话当场丢失。现整个对话面板（消息列表 + 输入区 + 工具栏）都是放置区，拖文件进入时面板浮出虚线提示「松开鼠标即可添加附件」，松手后文件进入输入框的附件条，与回形针选择、剪贴板粘贴走同一条上传链路（`addFiles`）。拖拽判定只看 `dataTransfer.types` 里有无 `Files`，拖选中的文字仍是原生行为；拖动进入/离开按子孙元素计数，提示不会在光标划过子元素时闪烁，拖拽在面板外结束（Esc、丢到侧栏、丢出窗口）由 window 级 `drop`/`dragend` 兜底清除，不留浮层遮住对话。编辑器里原本那个 `drop` 处理已删除，改由面板统一处理——两处都接会让同一个文件上传两次

- **挂机时服务端日志不再刷屏** - 用户只要在浏览器里开着对话，服务端日志就一行接一行地刷。此前那次修复（把 middleware 里那条访问日志按路径降为 DEBUG）只砍了一层，大头其实在 `authorize_project` 依赖：项目级接口全走它（12 个 router、66 处调用），而它在进入与通过时各打一条 INFO，5 秒一次的会话树轮询于是每轮刷两行（单个打开的标签页每分钟 24 行），middleware 降不降级都拦不住。现把该依赖的成功路径降为 DEBUG，两条 `DENIED` 保持 WARNING（越权尝试仍看得见）；`list_projects` 里把整个项目清单（slug / created_by）打进 INFO 的两行同样降为 DEBUG。访问日志策略一并反转为「默认安静」：成功的常规读取（GET/HEAD/OPTIONS）走 DEBUG，只有 4xx/5xx、写操作和少数需留痕的路径（登录 / OAuth 回调 / 登出）保持 INFO——原先按具体路径列白名单的做法会随新端点腐化，每加一个轮询接口都要记得补一次；`/api/media` 图片、`/health` 健康检查、`/conversations/latest` 这些漏网的读取因此一并静音。另修 WebSocket 连接噪音：uvicorn 的 WS 协议把 `uvicorn.error` 这个 logger 传给 websockets 库，每次连接建立/断开都会在那上面打约三行 INFO，而静音 `uvicorn.access` 管不到它，前端断线后是永不放弃的重连阶梯，代理一抖动即持续刷屏；现由 `WebSocketLifecycleFilter` 把 `connection open` / `connection closed` / `[accepted]` 降为 DEBUG，握手 403 保持 INFO（那意味着鉴权坏了）。排查时 `LOG_LEVEL=DEBUG` 仍可拿回全量，nginx 侧 `access.log` 也仍是完整的请求台账。回归测试改为**走真实路由**断言「挂机轮询零 INFO」——上一版只给 middleware 挂了个假 app，看不见依赖层的刷屏，才绿着放过了这个问题

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
