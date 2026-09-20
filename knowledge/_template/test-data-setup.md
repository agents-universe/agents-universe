---
category: skills
slug: skills/test-data-setup
tags: [testing, data-setup, recipes, qa]
template_words: 337
title: Test Data Setup
---

# Test Data Setup

Verified recipes for creating the records test cases need. Read the Recipe Index before designing
any case that depends on pre-existing data — an indexed need is executed as-is, never rediscovered.

## Recipe Index

| ID | Data need | Channel | Cost | Verified |
|---|---|---|---|---|
| {RECIPE_ID} | {WHAT_IT_CREATES} | {CHANNEL} | {COST} | {DATE} |

Channel is one of `test-support-api` (fastest, made for this), `product-api`, `ui`, or
`db-fallback` (last resort). Cost is the rough wall-clock of one creation, so the next task can
pick a cheaper recipe for the same need when one exists.

## Recipes

### {RECIPE_ID} — {WHAT_IT_CREATES}

- Purpose: {WHICH_CASES_NEED_IT}
- Channel: {CHANNEL}
- Endpoint: {METHOD} {PATH}
- Payload: {REQUIRED_FIELDS_AND_VALUE_CONSTRAINTS}
- Preconditions: {ACCOUNT_COMPANY_TENANT_OR_UPSTREAM_RECORD}
- Verify read: {ONE_READ_THAT_PROVES_THE_RECORD_EXISTS}
- Bulk: {HOW_TO_CREATE_MANY_IN_ONE_CALL}
- Idempotency key: {WHAT_MAKES_A_RERUN_SAFE}
- Cleanup: {HOW_TO_REMOVE_OR_REUSE_IT}
- Verified: {DATE} on {ENVIRONMENT}
- Source: {JIRA_KEY_OR_KNOWLEDGE_FILE}

## Discovery Protocol

> How does a new recipe get discovered and written back?

1. Check the Recipe Index first. An indexed need is executed verbatim — no re-discovery, no
   re-verification of the channel.
2. Unindexed: one probe through the cheapest channel, in this order — test-support API (look for an
   entry in `api-map.md`) → the product's own API (`api_request`) → UI flow → self-adapt DB.
   Check `kong-map.md` / `api-map.md` for an existing entry before probing anything.
3. Verify with exactly one real read of the created object. Never verify by re-driving the UI.
4. Write the recipe back here immediately — before generating the spec, not at the end of the task —
   and append a `history.md` entry. A verified recipe is cross-requirement reusable, so the
   Knowledge Write Eligibility gate accepts it.
5. Reuse it across cards through `dataSetup.recipeRef` in the test design.
6. Budget the search: after two failed channels, record the need under Blocked below with the reason
   and report the case as data-blocked rather than looping.

## Bulk Creation

Creating many records one call at a time is the slow path — a seed script that loops (or runs
concurrently) against the same endpoint creates them in one turn. Keep the payload in
`tests/fixtures/` so it can be re-run, put secrets in `env_refs` rather than in the script, and
record the batch shape in the recipe's Bulk line. Drive the UI only when no API path exists.

## Blocked / Known Gaps

| Data need | Channels tried | Why it blocked | Workaround |
|---|---|---|---|
| {DATA_NEED} | {CHANNELS_TRIED} | {REASON} | {WORKAROUND} |

Recording a dead end here is the point: it stops the next card from burning the same search.
