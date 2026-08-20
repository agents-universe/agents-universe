---
slug: "generation/custom-script-writer"
description: "Design, create, run and iterate on reusable project automation scripts (python/bash) saved to the Script Executor page — script_writer tool"
type: "guidance"
triggers:
  - "脚本"
  - "自定义脚本"
  - "自动化脚本"
  - "写个脚本"
  - "写脚本"
  - "做一个脚本"
  - "script"
  - "custom script"
  - "automation script"
tools:
  - script_writer
  - code_executor
  - shell
  - user_confirm
---

# Skill: Custom Script Writer（自定义脚本）

用户要求写一个可复用、可反复运行的项目自动化脚本（python/bash），并希望它出现在"脚本执行器"页面（左侧"自定义脚本"列表），随时可以在页面上重新运行。

## When to Use

- 用户说"写个脚本 / 帮我写个脚本 / 自定义脚本 / 自动化脚本 / script"，脚本用于处理项目内数据、文件或重复性工作。
- 脚本需要保存下来反复运行，而不是一次性的临时计算。
- **不适用**（转交其他路径）：
  - Playwright 测试 → 归 QA 的 `[[generation/playwright-generator]]`（`test_generator` 工具），不要用 `script_writer` 创建。
  - 一次性临时计算 → 直接用 `code_executor` 即时执行即可，无需保存为脚本。

## Steps（流程）

1. **澄清需求**：脚本目的、输入/输出（数据源、产物位置）、用 python 还是 bash、起一个清晰的脚本名称并写一句 description 说明用途（列表页会展示）。
2. **原型验证**：先用 `code_executor` 把核心逻辑跑通（30 秒快速反馈，可临时打印/试错），确认无误后再落盘为正式脚本。
3. **创建**：`script_writer(operation="create", name="...", script_type="python", description="...", content="...")` — 脚本即保存到"脚本执行器"页面。
4. **运行验证**：`script_writer(operation="run", script_id="...")` — 走真实沙箱（300 秒超时），查看返回的 `status` / `exit_code` / `log_tail`。
5. **失败迭代**：根据 `log_tail` 定位问题 → `script_writer(operation="update", script_id="...", content="...")` 修改 → 再次 `run`，直到成功。
6. **交付确认**：告诉用户脚本已保存，位于"脚本执行器"页面左侧"自定义脚本"列表，可随时点击运行；后续可在对话中要求继续修改。

## JSON 调用示例

```json
script_writer(operation="create", name="每日数据汇总", script_type="python",
  description="汇总当日 CSV 生成 report.md", content="...")
script_writer(operation="list")
script_writer(operation="get", script_id="<id>")
script_writer(operation="update", script_id="<id>", content="...")
script_writer(operation="run", script_id="<id>")
```

## Guardrails（沙箱与安全）

- **python 脚本**：文件读写被限制在项目工作区内；`subprocess.Popen` 等子进程调用会被运行时拦截，脚本无法启动子进程；300 秒超时。
- **bash 脚本**：`curl` / `wget` / `npm` / `npx` / `node` / `python3` / `ssh` / 数据库客户端等外部命令被禁止；路径逃逸（`../` 或绝对路径读写工作区外）会被拒绝；300 秒超时。
- **严禁把密钥、token、连接串写进脚本内容或 description**：运行环境变量已剥离凭据，脚本本身也可能被读取展示——任何敏感信息都不落盘。
- 脚本按项目隔离，只能操作当前项目；保持脚本单一职责、短小可维护；description 写清用途，让执行器列表可读。
- 先原型后落盘：不要在 `script_writer` 里反复试错长脚本——先用 `code_executor` 跑通逻辑再保存，改脚本用 `update`，不要创建同名副本。
