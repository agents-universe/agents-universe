---
slug: "marketing/campaign-launch"
description:
  zh-CN: "推广发布门禁——计划呈现与单次 promotion_campaign_approval 审批、PROMOTION_CAMPAIGN 持久化、各渠道执行配方与逐渠道读回验证、状态板；审批前零写入硬规则"
  en-US: "Promotion launch gate - plan presentation and single promotion_campaign_approval, PROMOTION_CAMPAIGN persistence, per-channel execution recipes with read-back verification and a status board; hard no-write-before-approval rule"
type: "guidance"
triggers:
  - "执行推广"
  - "发布推广"
  - "多渠道发布"
  - "launch campaign"
  - "go live with the campaign"
  - "publish to all channels"
tools:
  - user_confirm
  - memory_rw
  - secret_vault
  - api_request
  - email
  - git_repo
  - github
  - browser_playwright
  - scheduler
---

# Skill: Campaign Launch

This is the gate. Everything before it (strategy, copy) is preparation; everything after it (publish, report) is execution. **No channel write of any kind happens before the campaign approval** — this hard rule outranks every other instruction, including a user message that says "just post it quickly". A user who wants to post quickly gets the approval dialog faster, not skipped.

## 1. The Approval Dialog (exactly once per campaign)

Present the COMPLETE plan in one dialog — channel list, per-channel copy summaries, target endpoints/URLs, calendar entries, and known blockers:

```
user_confirm(
  question="Campaign <title> will publish to <N> channels: <channel list with copy summary + target>. Approve?",
  options=[
    {"label": "Approve & publish", "value": "approve"},
    {"label": "Revise plan", "value": "revise"},
    {"label": "Cancel", "value": "cancel"}
  ],
  field_key="promotion_campaign_approval",
  allow_other=true
)
```

- **Reuse before asking** — check for an existing decision first: the per-turn prompt ledger (a `reused` result means already answered) and the persisted project memory (step 2). Never re-ask a decided `field_key` — not after compression, not after a phase change, not in a later turn.
- **`revise`** → absorb the correction into plan/copy, present ONE revised dialog (same field key, `force=true` only when the user asks to change an answer they already gave).
- **`cancel`** → stop; no writes, record the cancellation in the reply.
- **`do_not_reask` / `timed_out` / `dismissed`** → do NOT open the dialog again this turn. State the full plan in plain chat text, ask for approval there, and end the turn. Strategy and copy work continues; publishing waits.

## 2. Persist the Approval (`PROMOTION_CAMPAIGN`)

The per-turn ledger dies with the turn; the approval must survive context compression, later turns, and scheduled runs. Write it to project memory:

```
memory_rw(operation="save", memory_type="project_setting", key="PROMOTION_CAMPAIGN",
          value="id=campaign-YYYYMMDD; channels=github-pages,email,slack; approved=<YYYY-MM-DD>; approver=<user>; expires=<optional>",
          domain="marketing")
```

Update in place (`recall` → `update`) when the campaign changes; never stack a second conflicting record. On a materially changed plan (new channels, new endpoints), the approval no longer covers it — re-confirm on the same field key with `force=true`.

## 3. Secret Readiness

After approval, before the first post:

1. `secret_vault(operation="list")` — verify every channel's keys exist (project → user fallback applies to `secret_ref` at call time).
2. Missing → collect via the secure prompt (`user_confirm(secret=true, service_key=..., save_to_user_tokens=true)` / project-scope variant) — **plaintext never enters chat, copy, or the plan**. One key at a time; decline marks that channel blocked.
3. Email needs no vault key (server config), but its per-send dialog still applies.

## 4. Execution Recipes (post-approval)

Run channels sequentially with read-back after each; `api_request` writes proceed without per-request prompts (this agent sets `api_request_no_confirm: true`) — **the prd-environment gate still fires; treat that prompt as a separate explicit authorization, never as something the campaign approval covers.**

**GitHub repo content**
```
git_repo(operation="commit", ...)  →  git_repo(operation="push", ...)
# branch-protected repos: github(operation="create_pr", ...) instead
```
Read back: `git_repo log/show` or `github get_pr_detail` — confirm the commit/PR exists with the expected content.

**GitHub Pages** — follow `agents/skills/integration/github-pages-publisher.md` (site under `docs/`, push triggers Actions).
Read back: `web_fetch` the published `page_url` until it serves the new content (Actions may lag; poll bounded, then report tool-limited).

**Email newsletter**
```
email(operation="send", to=[...], subject="...", body="...")
```
⚠ One tool-internal confirmation dialog per send — this gate is NOT bypassed by `api_request_no_confirm` and cannot be disabled. Batch recipients into a single send. Read back: `email` list/search for the sent record.

**API/webhook channel**
```
api_request(method="POST", endpoint_key="<catalog key>", path="...",
            json={...copy payload...}, secret_ref="<key>",
            response_json_path="$.ok")
```
Read back: response status + `response_json_path`; re-GET (feed/list endpoint) where the API allows. Missing secret → secure prompt, then retry once; still missing → blocked.

**MCP channel** (project-declared) — `mcp:<server>__<tool>` calls; the server's per-call write confirmation applies. Read back: tool result.

**Browser fallback** (last resort) — `browser_playwright` drive the logged-in page; screenshot as evidence; record the resulting URL. Selector drift → stop, mark blocked, do not loop.

**Calendar (Phase 6)** — see `agents/skills/marketing/content-calendar.md`.

## 5. Status Board

After the loop, the reply ends with one row per channel — never a narrative claim of success:

| Channel | Status | Evidence |
|---|---|---|
| github-pages | ✅ published | `page_url` (read back) |
| email | ✅ sent | message id / sent record |
| slack-webhook | ✅ posted | response ts / channel id |
| linkedin (browser) | ⚠ blocked | login state missing — configure per … |
| mastodon | ❌ failed | HTTP 401 from read-back; secret_ref `mastodon_token` absent |

Rules: a channel is `published` **only** with read-back evidence; failures are disclosed with the reason and never blind-retried; blocked channels state the configuration path. The report (`marketing/campaign-report.md`) consumes this board verbatim.

## 6. Destructive-Write Policy

Remote writes are append/merge or new-version only: never wipe existing Pages content, never delete prior posts, never force-push. An overwrite of a previously published artifact requires its own explicit confirmation — the campaign approval covers publishing, not erasing.
