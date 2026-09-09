---
slug: "generation/scheduled-task-writer"
description: "Create and manage 定时任务 (cron-scheduled tasks) that run a project script, a Playwright spec, or an agent prompt on a schedule — scheduler tool"
type: "guidance"
triggers:
  - "定时任务"
  - "定时"
  - "每天"
  - "每周"
  - "每月"
  - "每小时"
  - "周期性"
  - "定时跑"
  - "自动执行"
  - "cron"
  - "schedule"
  - "scheduled task"
  - "recurring"
tools:
  - scheduler
  - script_writer
  - test_generator
  - user_confirm
---

# Skill: Scheduled Task Writer（定时任务）

用户希望某个动作**按时间自动执行**（每天 9 点、每周一、每小时……），并且能在"定时任务"页签看到运行历史和日志，结果还能回到会话里继续追问。

## When to Use

- 用户说"定时 / 每天 / 每周 / 每小时 / 周期性地跑一下 X / cron / schedule"。
- **不适用**（转交其他路径）：
  - 用户只是想现在跑一次 → 直接用 `script_writer(operation="run")` 或对应的执行路径，不要建定时任务。
  - 目标还不存在 → 先用 `[[generation/custom-script-writer]]` 落盘脚本，或让 QA 用 `[[generation/playwright-generator]]` 生成 spec，再挂定时任务。

## 三种目标怎么选

| 用户意图 | target_type | 需要的字段 |
|---|---|---|
| 定期跑数据处理 / 报告 / 清理（python/bash） | `script` | `script_id`（先 `script_writer` 创建并跑通） |
| 定期回归某个页面流程 | `playwright` | `spec_slug`（`tests/generated/{slug}.spec.ts`，QA 生成） |
| "让某智能体每天汇报 / 巡检 / 总结" | `agent` | `agent_slug` + `prompt` + `conversation_id` |

## Steps（流程）

1. **澄清三件事**（缺一不可，不要自己猜）：
   - **频率**：每天几点？每周几？每小时？——同时确认时区（默认 `Asia/Shanghai`），换算成 5 字段 cron：`分 时 日 月 周`。
   - **目标**：脚本 / Playwright spec / 智能体提示词。
   - **结果送到哪**：哪个会话（`conversation_id`，通常就是当前会话）。
2. **确保目标已存在**：脚本先 `script_writer create` + `run` 验证通过；spec 先确认文件已生成；智能体确认 slug 存在。
3. **创建**：`scheduler(operation="create", name="...", target_type="script", script_id="...", cron_expr="0 9 * * *", timezone="Asia/Shanghai", conversation_id="...")`。
4. **回显给用户**：把工具返回的 `next_run_at`（下次运行时间，按任务时区）和 `schedule`（人类可读描述）念一遍，让用户确认时间对不对。
5. **说明在哪看**：告诉用户左侧"定时任务"页签能看到任务列表、运行历史和实时日志；脚本/Playwright 任务可以点开某次运行看日志，结果摘要也会投递到指定会话。
6. **后续调整**：`update`（改 cron / 目标 / 投递会话）、`enable` / `disable`、`delete`、`run_now`（立刻跑一次，不影响排期）。

## JSON 调用示例

```json
scheduler(operation="create", name="每日数据汇总", target_type="script",
  script_id="<script_id>", cron_expr="0 9 * * *", timezone="Asia/Shanghai",
  conversation_id="<conversation_id>")
scheduler(operation="list")
scheduler(operation="get", schedule_id="<id>")
scheduler(operation="update", schedule_id="<id>", cron_expr="0 9 * * 1-5")
scheduler(operation="disable", schedule_id="<id>")
scheduler(operation="run_now", schedule_id="<id>")
scheduler(operation="delete", schedule_id="<id>")
```

## Cron 速查

| 写法 | 含义 |
|---|---|
| `0 9 * * *` | 每天 09:00 |
| `30 8 * * 1-5` | 工作日 08:30 |
| `*/15 * * * *` | 每 15 分钟 |
| `0 0 1 * *` | 每月 1 号 00:00 |
| `0 9 * * 0` | 每周日 09:00（周日 = 0，也可写 7） |

日（day-of-month）和周（day-of-week）**同时**被限定时按 POSIX 的「或」语义：`0 0 1 * 0` 表示"每月 1 号或每周日"。不写具体值就用 `*`。

## Guardrails（边界与安全）

- **提示词是明文**：`prompt` 会原样存库并每次投递进会话——**严禁**写密码、token、密钥、连接串；需要凭据时让运行期的 `api_request` / `secret_vault` 通过 `secret_ref` 取用。
- **定时执行是无人值守的**：agent 任务以无交互模式运行，`user_confirm` 类确认会失败而不是等待；提示词里不要让智能体做需要人工确认的写操作。
- **错过的周期直接跳过**：服务停机期间到点的执行不会补跑，只排下一个未来时间点。
- **脚本 / Playwright 任务**与页面手工运行共用同一个并发槽位（最多 3 个），agent 任务另有并发上限（2 个）；同一任务上一次还没结束，本次会记为"跳过"而不是叠加。
- 任务按项目隔离，只能引用**当前项目**的脚本、spec 和会话。
- 频率不要设得过高（如每分钟）除非用户明确要求——每次运行都会占用槽位并产生历史记录。
