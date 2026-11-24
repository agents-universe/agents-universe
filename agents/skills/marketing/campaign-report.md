---
slug: "marketing/campaign-report"
description:
  zh-CN: "推广复盘报告——结构化活动总结、逐渠道状态板与真实度量（不可得标 tool-limited）、失败与阻塞、下一步，遵循 report-writer 规范并做知识回写"
  en-US: "Promotion campaign report - structured campaign summary, per-channel status board with honestly measured metrics (tool-limited when unavailable), failures/blockers, next actions, following report-writer conventions with knowledge writeback"
type: "guidance"
triggers:
  - "推广报告"
  - "活动复盘"
  - "campaign report"
  - "promotion report"
  - "campaign retrospective"
tools:
  - filesystem
  - chart_renderer
  - deliver_file
  - knowledge_rw
  - memory_rw
  - web_search
---

# Skill: Campaign Report

The report closes the engagement. It consumes the launch status board verbatim and measures only what was actually measured — promotion reports rot fastest when they flatter.

## 1. Report Structure

`promotion/promo-report-{YYYYMMDD}.md`, sections in order (body follows `AGENT_DEFAULT_LANGUAGE`, conventions per `agents/skills/analysis/report-writer.md`):

1. **Executive Summary** — campaign id, dates, objective, headline outcome (published X/Y channels, blocked Z).
2. **Campaign & Audience** — brief summary, target audience, channel rationale (from `plan.md`).
3. **Channel Status** — the launch status board verbatim: channel, status, read-back evidence/URL. Approved copy files referenced per channel.
4. **Copy Variants** — one line per variant: channel, copy path, CTA; notable adaptation decisions.
5. **Engagement Metrics** — *only numbers actually retrieved* (API responses, analytics endpoints, email send stats). Every metric cites its source call/file. **Unavailable metrics are labeled `tool-limited` with the reason** (no analytics API configured, platform does not expose the number) — never estimated, never benchmarked from memory.
6. **Failures & Blockers** — per channel: what failed, root cause, configuration path that would unblock it.
7. **Calendar Outcomes** — scheduled fires that ran, blocked, or were cancelled (from calendar + `scheduler` run history).
8. **Lessons & Next Actions** — concrete, carry-forward items: channels worth repeating, copy that resonated (with evidence), blockers to fix before next campaign.
9. **Appendix** — approval record (`PROMOTION_CAMPAIGN` summary, no secrets), plan/copy file index.

Charts (channel outcome breakdown, funnel when multi-stage metrics exist): call `chart_renderer` first — the rendered PNG displays automatically; never paste Mermaid source or embed image markdown in the reply.

## 2. Evidence Rules

- Published = read-back evidence exists (URL, message id, response ts); the report never upgrades a `blocked`/`failed` row.
- Metrics: cited or `tool-limited`. A number without a source does not enter section 5.
- No secrets, no credential material, no raw token values — statuses only.
- Sample/example URLs use the example.com family (convention 14).

## 3. Delivery

```
deliver_file(file_path="promotion/promo-report-{YYYYMMDD}.md")
```

Summarize the headline + status board in chat; the file carries the detail.

## 4. Knowledge Writeback

After delivery, per `workflows/knowledge-ingestion.workflow.md` eligibility and the fill-first rule in `agents/skills/knowledge/knowledge-manager.md`:

- **Reusable** (goes to project knowledge): channel playbook learnings → `domain/campaign-history` (created ad hoc if absent: what ran, what worked, what broke, configuration paths), brand/voice corrections → `domain/brand-guidelines`.
- **Not reusable** (stays in the report): campaign-specific metrics, one-off copy, transient dates.
- Append a `system/history` entry: campaign id, date, report path, headline outcome (`knowledge_role: log` — on-demand only).
