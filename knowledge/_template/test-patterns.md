---
category: skills
slug: skills/test-patterns
tags: [testing, patterns, qa]
template_words: 223
title: Test Pattern Library
---

# Test Pattern Library

## File Naming

`{issue-key}-{concise-english-description}.spec.ts` (e.g. `dm-3825-inventory-sync.spec.ts`)

Whole-system plans: fixed name `tests/test-plan.md`, case IDs `SYS-{3-digit}` (see `testing/system-test-planner`).

## Validation Patterns

- Layered: UI flow / service collaboration / foundational capability
- State transitions: verify intermediate states + operation history, not just final state
- Permission: menu visibility + route interception + API status code
- Exports: UI entry + API result + file destination

## Data Setup

- Prefer `test-support` APIs for data creation and state driving
- Record account, company, org scope, env preconditions explicitly
- Per-entity creation recipes (endpoint, required fields, verify read, bulk shape, cleanup) live in `test-data-setup.md` — look there before probing a channel, and write a verified recipe back immediately

## Run Shape

- One sign-in per run: `tests/auth.setup.ts` signs in once and every spec inherits that session, so cases must not log in themselves. A case about signing in, switching user, or checking permissions runs signed out deliberately.
- Cases in one spec file run in parallel: a case may not depend on another case's effects. Merge a genuine continuation into one case; mark state-sharing cases as serial instead of leaving the dependency implicit.
- Tunables live on the config: `PW_SERIAL=1` (one at a time), `PW_WORKERS=n` (browser count), `PW_RETRIES=1` (retry; off by default so the recorded video matches the failure that was reported).

## Test Dimensions

- Main-flow regression
- Config-driven branch regression
- Permission/role regression
- Export/template regression
- External-system collaboration regression
