---
slug: "performance/perf-scenario-writeback"
description:
  zh-CN: "把性能场景设计回写到 Jira 或 Confluence——按可用性二选一、[AI perf] 卡片先搜后建、wiki 语法正文、写后读回验证，重跑用更新不重复建"
  en-US: "Write the performance scenario design back to Jira or Confluence - pick one by availability, search-before-create for [AI perf] cards, wiki-syntax bodies, read-back verification, update instead of duplicating on re-runs"
type: "guidance"
triggers:
  - "write scenarios to jira"
  - "publish test plan confluence"
  - "scenario writeback"
  - "场景写入jira"
  - "回写confluence"
  - "压测场景卡片"
tools:
  - jira
  - confluence
  - filesystem
---

# Skill: Perf Scenario Writeback

Phase 2: publish the Phase 1 scenario design (`performance/scenario-design-{YYYYMMDD}.md`) into the team's tracking system. **Jira OR Confluence - exactly one primary target per engagement**, chosen below; the local design doc is always the source of truth either way.

## Availability Decision

1. If the user named a target (an issue key to attach to, a Confluence space) - use it.
2. Else probe Jira cheaply: `jira(operation="search", jql="project = <key> ORDER BY updated DESC", max_results=1)` (or a `get_issue` when a key is known). Configured and reachable → **Jira**.
3. Else probe Confluence: `confluence(operation="search", cql="type = page", max_pages=1)` → **Confluence**.
4. Neither → write only the local design doc, state the blocker plainly, and point at Settings → Integrations (`ATLASSIAN_BASE_URL` / `CONFLUENCE_BASE_URL` + tokens). Offer to retry once configured. Never fabricate a "written" status.

## Jira Path

Conventions follow `workflows/test-artifact-and-jira-conventions.workflow.md` (evidence naming, result-first comments) with perf-specific card rules:

- **Card per target, prefix `[AI perf]`**: `jira(operation="create_issue", project_key=..., summary="[AI perf][<TARGET-KEY>] <scenario title>", description=<wiki>, issue_type="Task", labels=["AIPerf"])`. Use `create_issue`, **never `create_test_issue`** - that dedup pool belongs to `[AI test]` QA cards.
- **Search before create**: `jql='labels = "AIPerf" AND summary ~ "<TARGET-KEY>"'` - if a card exists, `update_description` / `add_comment` it instead of creating a second one. One card per target per engagement.
- **Link to the requirement** when one exists: `jira(operation="link_issues", from_key="<card>", to_key="<requirement>", link_type=<JIRA_TEST_LINK_TYPE, default "Tests">)`.
- **Body is Jira wiki syntax** (the tool converts comments from Markdown, but write descriptions directly in wiki markup): `h2.` headings, `*` bullets, `||` table headers. Content: scope tier + ceilings, the scenario matrix table, the SLA table (confirmed vs proposal), pointers to `performance/scenario-design-{YYYYMMDD}.md` and the script name - **not** raw JSON or full logs.
- **Result writeback** after execution: one `add_comment` per run, result-first ("Business Summary: SLA met / missed - p95 Xms vs target Yms"), then the Minimal Execution Contract (profile, window, evidence pointers). Markdown in comments is auto-converted.
- **Read back exactly what you wrote**: `get_issue` for the description, `get_comment` (by returned id) for comments. If the read-back does not match intent, fix and re-read - never report success from the write call alone.

## Confluence Path

- **Create**: `confluence(operation="create_page", space_key=..., title="[AI perf] <scenario title> - <YYYYMMDD>", body=<wiki or storage XHTML>, body_format="wiki", parent_page_id=<optional>)`. The tool has **no Markdown auto-conversion** - write wiki markup (or simple storage XHTML), not Markdown.
- **Read back**: `confluence(operation="get_pages", page_ids=[<returned id>], include_body=true)` - verify the body rendered (unclosed markup, mojibake).
- **Re-runs update in place**: `update_page` with the bumped version flow; **never delete pages** - history is part of the audit trail.
- Same content rules as Jira: matrix + SLA + local pointers; no raw logs, no secrets.

## Content Rules (both paths)

1. Scope section copies tier/ceilings from `.tmp/performance/engagement.md` - the published card/page must match the declaration exactly.
2. SLA rows are labeled `confirmed` or `proposal` - never present proposals as agreed targets.
3. Evidence pointers are workspace-relative paths; file contents (payloads with tokens, result JSON) stay out.
4. No secrets, no real user data, no corporate identifiers (sample URLs use the example.com family).
5. Language: follow `AGENT_DEFAULT_LANGUAGE` for body prose; card titles keep the `[AI perf]` prefix verbatim for searchability.
