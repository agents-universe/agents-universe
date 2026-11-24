---
slug: "promotion-engagement"
description:
  zh-CN: "推广交付全流程——知识优先简报、渠道策略与文案、单次活动审批门禁、多渠道自动发布与读回验证、内容日历、复盘报告与知识回写"
  en-US: "Full promotion engagement - knowledge-first brief, channel strategy and copy, single campaign-approval gate, multi-channel publish with read-back verification, content calendar, campaign report and knowledge writeback"
triggers:
  - "推广"
  - "多渠道推广"
  - "发布公告"
  - "内容日历"
  - "promotion"
  - "launch campaign"
  - "content calendar"
  - "announce the release"
tools:
  - user_confirm
  - memory_rw
  - knowledge_rw
  - filesystem
  - web_search
  - web_fetch
  - skill_source
  - api_request
  - email
  - git_repo
  - github
  - browser_playwright
  - scheduler
  - secret_vault
  - deliver_file
  - chart_renderer
---

# Promotion Engagement

The promotion expert's default workflow for whole-engagement requests (「为这个项目做一次多渠道推广」「announce the release to every channel」「制定下个月的内容日历」). Deliverables: `promotion/campaign-{YYYYMMDD}/` (plan + brief + copy) + published channel posts with read-back evidence + `promotion/promo-report-{YYYYMMDD}.md`.

Files under `workflows/` stay English regardless of `AGENT_DEFAULT_LANGUAGE`; plan/copy/report bodies follow `AGENT_DEFAULT_LANGUAGE` unless the user overrides.

## 1. Relationship to Other Workflows

- This workflow owns the engagement: brief -> strategy -> copy -> **approval gate** -> publish -> calendar -> report -> knowledge writeback.
- `knowledge-ingestion` governs knowledge writeback; Phase 7 applies its eligibility gate with `agents/skills/knowledge/knowledge-manager.md` fill-first rules.
- `integration/custom-api-onboarding` governs adding a missing webhook/API channel configuration — Phase 2 cites it whenever a channel is blocked on config.
- Performance/QA workflows are unrelated artifacts; never merge their reports into the promotion report.

## 2. Phase 1 - Brief and Knowledge-First

1. Infer the active project; read `AGENT_DEFAULT_LANGUAGE` from `environment` knowledge.
2. Knowledge inventory before strategy: `domain/brand-guidelines`, `domain/audience-profile`, `domain/campaign-history` when present, plus general project context (release notes, feature docs).
3. Clarify only what knowledge cannot answer: objective, target release/campaign, must-cover channels. One round — never a questionnaire.
4. Create working dirs: `promotion/campaign-{YYYYMMDD}/` with `copy/`.

## 3. Phase 2 - Research and Channel Strategy

Per `agents/skills/marketing/channel-strategy.md`:

1. Audience fit ranking; optional read-only research via `web_search`/`web_fetch` (competitor presence, community norms) per `agents/skills/interaction/web-search-privacy.md`.
2. Optional market consultation per `agents/skills/marketing/market-reference.md` (`skill_source` → `marketing-skills`, untrusted reference, 只参考不落地).
3. Readiness checklist per channel (git remote, Pages path, email server, custom-api catalog + `secret_ref`, MCP declarations, browser login).
4. First launch caps at 2–4 channels; blocked channels recorded with configuration path, never silently dropped.
5. Output: `promotion/campaign-{YYYYMMDD}/plan.md` with one matrix row per channel.

## 4. Phase 3 - Plan and Copy Drafts

Per `agents/skills/marketing/copy-adaptation.md`:

1. `brief.md` first — core message + facts, every fact sourced from project knowledge.
2. One variant per selected channel under `copy/`, adaptation table applied, do-not-say list honored.
3. Consistency pass across variants (same message, same facts, one CTA each).
4. **No outbound calls in this phase** — drafts are files, publishing waits.

## 5. Phase 4 - Campaign Approval and Secret Readiness (the gate)

Per `agents/skills/marketing/campaign-launch.md` §1–3:

1. Present the COMPLETE plan in ONE dialog (`field_key="promotion_campaign_approval"`): channels, copy summaries, targets, calendar entries, blockers.
2. Reuse first (prompt ledger / existing `PROMOTION_CAMPAIGN`); `revise` → fix + re-present (same key); `cancel` → stop, zero writes; `do_not_reask`/`timed_out`/`dismissed` → state the plan in plain chat text and end the turn.
3. On approval, persist `PROMOTION_CAMPAIGN` (`memory_rw`, `memory_type="project_setting"`, `domain="marketing"`) — it is the record scheduled runs re-check.
4. Secret readiness: `secret_vault list` → collect missing keys via secure prompts (plaintext never in chat) → decline marks channel blocked.

## 6. Phase 5 - Multi-Channel Publish Loop

Per `agents/skills/marketing/campaign-launch.md` §4–5:

1. Sequential per channel: execute recipe → read-back verification → append status-board row. `api_request` writes run without per-request prompts (`api_request_no_confirm`); the **prd-environment gate still prompts** and is treated as separate authorization.
2. Email: one tool-internal dialog per send (never bypassed; batch recipients into one send).
3. MCP channels: per-call write confirmation applies (project-declared tools only).
4. Browser fallback only when no API path exists; screenshot evidence; selector drift → blocked, no loops.
5. Channel failure → record reason, continue remaining channels; never blind-retry, never fake success.
6. End phase with the status board (channel | status | evidence) in the reply.

## 7. Phase 6 - Content Calendar (optional)

Per `agents/skills/marketing/content-calendar.md`:

1. Only when the campaign has future/queued copy or the user asked for cadence.
2. Calendar artifact `promotion/calendar-{YYYYMM}.md`; calendar included in the Phase 4 approval (or its own approval for calendar-only requests).
3. `scheduler(operation="create", target_type="agent", agent_slug="promotion-expert", prompt="<contract>", cron_expr=..., timezone=...)` — prompts reference the campaign by path/id, **never secrets**.
4. Read back via `scheduler list`: next run matches the intended time; record schedule ids in calendar rows.

## 8. Phase 7 - Report and Knowledge Writeback

Per `agents/skills/marketing/campaign-report.md`:

1. Write `promotion/promo-report-{YYYYMMDD}.md` (9 sections; status board verbatim; metrics cited or `tool-limited`; never upgrade a blocked row).
2. `chart_renderer` for any chart (auto-displays; never paste Mermaid source).
3. `deliver_file(file_path="promotion/promo-report-{YYYYMMDD}.md")`, summarize headline + board in chat.
4. Knowledge writeback: reusable channel learnings → `domain/campaign-history` / `domain/brand-guidelines` per `workflows/knowledge-ingestion.workflow.md`; append `system/history` entry (campaign id, date, report path, headline outcome).

## 9. Success Criteria

- Campaign approval obtained exactly once (same `field_key`, reuse not re-ask); `PROMOTION_CAMPAIGN` persisted before any write.
- Zero outbound writes before approval; approved plan matches what was published (corrections announced).
- Every selected channel is either **published with read-back evidence** or **listed blocked/failed with reason + configuration path** — no silent drops, no fabricated success.
- Status board present in the reply and consumed verbatim by the report; metrics cited or labeled `tool-limited`.
- Report delivered via `deliver_file`; calendar schedules (if any) read-back verified with ids recorded.
- Knowledge writeback respects fill-first eligibility; no secrets anywhere (chat, files, prompts, memory).

## 10. Error Handling

- Approval prompt `do_not_reask`/`timed_out`/`dismissed` → do NOT reopen the dialog this turn; full plan in plain chat text, end turn, publishing waits for a recorded approval.
- Channel config missing → blocked with configuration path (`integration/custom-api-onboarding` for API channels); campaign continues on healthy channels.
- Secret declined → channel blocked, continue; recorded on the status board.
- Post read-back fails → status `failed` with evidence; one bounded retry only when a transient cause is identified, then stop; never loop.
- Scheduled run: `PROMOTION_CAMPAIGN` missing/cancelled/channel-not-covered → publish NOTHING, stop, report blocked. Headless dialog gates (email, MCP, prd) fail closed → record blocked, do not retry.
- `web_search` unavailable → strategy from project knowledge only, limitation stated.

## 11. Slices

- **Full engagement** (this workflow): Phases 1–7.
- **Strategy-only**: Phases 1–2, stop (plan.md delivered, no copy, no approval, no publish).
- **Draft-only**: Phases 1–3, stop (copy drafts awaiting user review; publishing needs Phase 4).
- **Publish-only**: Phase 4 re-check (reuse `PROMOTION_CAMPAIGN` if it covers the plan) + Phase 5 against existing approved drafts.
- **Calendar-only**: Phase 6 (own approval), Phases 1–3 skipped when copy exists.
- **Report-only re-run**: Phase 8 from persisted plan/copy/status records.
