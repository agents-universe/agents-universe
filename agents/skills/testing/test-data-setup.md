---
slug: "testing/test-data-setup"
description: "Reuse, discover, verify, and write back test-data creation recipes so the next card does not rediscover them"
triggers:
  - "造测试数据"
  - "创建测试数据"
  - "准备测试数据"
  - "造一条测试"
  - "创建一条测试"
  - "准备一条测试"
  - "造数"
  - "test data"
  - "data setup"
  - "seed data"
---

# Skill: Test Data Setup

The slow part of test automation is rarely the test — it is figuring out how to create a record
that already has the right account, company, tenant, and upstream state. That knowledge is
discoverable once per project and reusable forever; this skill is the loop that makes it so.

Read this skill before designing or executing any case that depends on data it does not create
through the UI in the course of the test.

## The Loop

1. **Look up, don't rediscover.** Read the Recipe Index via
   `knowledge_rw(operation="read", slug="skills/test-data-setup")` — knowledge slugs are paths under
   the project `knowledge/` directory without `.md`. If the need is indexed, execute the recorded
   recipe as-is — do not re-verify the channel or re-probe the endpoint. `dataSetup.recipeRef` in
   the test design points at the recipe.
2. **Discover once, cheapest channel first.** For an unindexed need, check `technical/kong-map` /
   `technical/api-map` for an existing entry and take exactly one probe per channel, in this order:
   - `test-support-api` — an API built for creating test state. Fastest; look for it in `technical/api-map`.
   - `product-api` — the product's own API via `api_request` (`endpoint_key` from `technical/kong-map`).
   - `ui` — drive the real screen when no API path exists.
   - `db-fallback` — the self-adapt DB service (`integration/self-adapt-db-access`), justified in the design.
3. **Verify with one real read.** A GET on the created object proves it exists — never verify by
   re-driving the UI, and never treat "the POST returned 200" as verification that the record
   landed where the test will look for it.
4. **Write the recipe back immediately** — before generating the spec, not at the end of the task.
   Use `knowledge_rw(operation="write", slug="skills/test-data-setup", ...)` for a file that already
   exists (read it first, then merge), or create the file when the project predates the template.
   Record: purpose, channel, endpoint, required fields and constraints, preconditions, the verify
   read, the bulk shape, the idempotency key, cleanup, verification date and environment, source.
   A verified creation path is cross-requirement reusable, so the Knowledge Write Eligibility gate
   (`knowledge/knowledge-manager`) accepts it. Append a `system/history` entry too.
5. **Stop after two dead channels.** Record the need under `## Blocked / Known Gaps` with the reason
   and report the case as data-blocked. The record is the value — it stops the next card from
   burning the same search.

## Bulk Creation

When a case needs many records, the cost is the *number of calls*, not the endpoint:

- Prefer one seed script over N tool calls: `shell(command="python ...", env_refs={...}, timeout_seconds=300)`
  is the supported path. Keep the payload in `tests/fixtures/` so it can be re-run, and pass secrets
  through `env_refs` — never inline in the script, never in the fixture file.
- A loop of single `api_request` calls is the slow path; use it only for one or two records.
- Only fall back to clicking the UI when no API path exists. If that is the only path, say so in the
  recipe so the next case knows the cost before choosing it.

## Where Each Part Is Recorded

| Learned | Goes to |
|---|---|
| How to create entity X (endpoint, payload, preconditions, verify read) | `skills/test-data-setup` recipe |
| An account/role/company needed by a case | `technical/login-and-user-switch` Verified Accounts (non-secret metadata only) |
| A need that could not be solved | `skills/test-data-setup` `## Blocked / Known Gaps` |
| A one-off fixture with no reuse value | `tests/fixtures/` only — do not write it to knowledge |

Never write credentials, tokens, or customer data into a recipe: reference the `secret_ref` /
`env_refs` key instead. Never write project-specific passwords into knowledge at all.

## Guardrails

- Data creation never bypasses the black-box rule: seed through supported APIs or the real UI, and
  never by running the product repo's own test suite or fixtures.
- Remote deletion stays prohibited (Quality Assurance Core Principle 1) except for approved
  non-production test targets. Prefer idempotent create-or-reuse over delete-and-recreate, and
  record the cleanup rule when deletion is genuinely part of the scenario.
- Seeded data must not invalidate evidence: if a case's evidence depends on the record it created,
  the recipe's verify read is part of the run, not a one-time setup step.
