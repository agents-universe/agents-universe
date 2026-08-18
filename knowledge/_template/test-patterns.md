---
category: skills
slug: skills/test-patterns
tags: [testing, patterns, qa]
template_words: 93
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

## Test Dimensions

- Main-flow regression
- Config-driven branch regression
- Permission/role regression
- Export/template regression
- External-system collaboration regression
