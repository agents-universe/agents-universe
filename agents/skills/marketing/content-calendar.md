---
slug: "marketing/content-calendar"
description:
  zh-CN: "推广内容日历——排期产物 promotion/calendar-*.md、scheduler agent 任务创建（cron/时区/提示词契约）、审批再校验规则（PROMOTION_CAMPAIGN 缺失即停）"
  en-US: "Promotion content calendar - calendar artifact promotion/calendar-*.md, scheduler agent-task creation (cron/timezone/prompt contract), approval recheck rule (stop when PROMOTION_CAMPAIGN is missing)"
type: "guidance"
triggers:
  - "内容日历"
  - "推广排期"
  - "定时发布推广"
  - "content calendar"
  - "schedule promotion posts"
  - "promotion cadence"
tools:
  - scheduler
  - filesystem
  - memory_rw
  - user_confirm
  - knowledge_rw
---

# Skill: Content Calendar

A campaign is a moment; a calendar is a cadence. This skill turns the campaign's remaining or follow-up copy into dated entries and挂s them on cron schedules via `scheduler(target_type="agent", ...)` — each fire hands `promotion-expert` a prompt that advances the calendar. This skill attaches them via cron schedules.

## 1. Calendar Artifact

`promotion/calendar-{YYYYMM}.md` — one row per scheduled post:

| Date (tz) | Channel | Copy file | Stage | Schedule id | Status |
|---|---|---|---|---|---|
| 2026-12-01 09:00 Asia/Shanghai | slack-webhook | copy/calendar-tease.md | tease | `<id>` | scheduled |
| 2026-12-08 10:00 Asia/Shanghai | email-newsletter | copy/email-launch.md | launch | `<id>` | scheduled |

- Stages: `tease` → `launch` → `recap` (or the campaign's own stages).
- Timezone: default from `environment` knowledge / user preference; every row states its IANA tz explicitly.
- Every entry's copy already went through `marketing/copy-adaptation.md`; the calendar schedules approved copy, it does not invent it.

## 2. Creating Schedules

The calendar itself gets ONE user approval before any schedule is created (it commits to future automated posts — it is part of the campaign plan presented at launch, or a standalone `field_key="promotion_campaign_approval"` for calendar-only requests). Then:

```
scheduler(operation="create",
          name="promo-<campaign>-<stage>-<channel>",
          target_type="agent",
          agent_slug="promotion-expert",
          prompt="<prompt contract below>",
          cron_expr="0 9 * * 1",
          timezone="Asia/Shanghai",
          is_active=true)
```

- **Cron**: 5-field POSIX (`minute hour dom month dow`, dom/dow OR semantics) — pick the smallest granularity matching the row (one-shot dates → cron pinned to that day/month; weekly cadence → `dow` form).
- After create, record the returned schedule id in the calendar row; read back via `scheduler(operation="list")` that the next run matches the intended time.
- Update rows via `scheduler(operation="update", schedule_id=...)`; remove via `delete` when a campaign is cancelled — never leave orphan schedules posting for a dead campaign.

## 3. Scheduled Prompt Contract

The prompt handed to the agent on each fire is plaintext in the DB — **it must never contain secrets** (the tool also rejects credential-shaped prompts). Template:

```
Advance promotion campaign <campaign-id>: calendar entry
promotion/calendar-{YYYYMM}.md row "<date/channel/stage>".
Re-read promotion/campaign-<date>/plan.md + copy/<file>, verify the
PROMOTION_CAMPAIGN approval record still exists and covers this channel,
publish via the approved recipe in marketing/campaign-launch.md, read back,
append the status board row, and report the outcome. If approval is missing
or expired: publish NOTHING, stop and report.
```

The prompt references the campaign by path/id only — the firing run re-reads everything it needs from files and memory.

## 4. Headless Recheck (hard rule)

A scheduled fire runs `interactive=False`: prompts are unavailable and any write attempt that needs one fails closed. Before **any** channel write in a scheduled run:

1. `memory_rw(operation="recall", scope="personal", query="PROMOTION_CAMPAIGN")` (and the project-setting record) — the approval must exist, cover this channel, and not be cancelled.
2. **Approval missing / expired / channel not covered → publish NOTHING.** Stop, write a short report row ("blocked: no PROMOTION_CAMPAIGN approval"), and end. Never treat the schedule's existence as approval.
3. Gates that remain in headless runs: email per-send and MCP per-call dialogs **fail closed** (they return cancelled — record blocked, do not loop); prd-environment `api_request` gate likewise refuses. API channels in non-prd proceed under the recorded approval.

## 5. Maintenance

- Monthly: reconcile calendar rows against `scheduler list` — every `scheduled` row has a live schedule id, every dead row is `done`/`cancelled`.
- A campaign revocation updates `PROMOTION_CAMPAIGN` first, then pauses the schedules (`is_active=false`) — memory before machinery.
- Calendar history (what fired, what it posted) feeds `marketing/campaign-report.md`.
