---
slug: "performance/perf-test-designer"
description:
  zh-CN: "基于项目知识设计 API 性能测试场景——端点清单、场景矩阵（smoke/baseline/spike/soak-lite，全部 ≤240s）、每端点 SLA 分位目标与数据准备，输出 performance/scenario-design-{date}.md"
  en-US: "Design API performance test scenarios from project knowledge - endpoint inventory, scenario matrix (smoke/baseline/spike/soak-lite, all <=240s), per-endpoint SLA percentile targets and data setup, writing performance/scenario-design-{date}.md"
type: "guidance"
triggers:
  - "design load test"
  - "test scenarios"
  - "performance test plan"
  - "设计压测场景"
  - "性能测试场景"
  - "负载测试设计"
tools:
  - filesystem
  - knowledge_rw
---

# Skill: Perf Test Designer

Phase 1 deliverable: `performance/scenario-design-{YYYYMMDD}.md` in the project workspace - the scenario matrix and SLA contract that Phase 2 writes back to Jira/Confluence and Phase 3 turns into a runnable script. Design only; this skill generates no load.

## Inputs (knowledge-first)

1. `knowledge_rw(operation="list")` - inventory what exists. The files that matter most: `technical/api-map` (endpoint list, methods, auth), `technical/kong-map` (gateways, rate-limit config), `environment` (tier URLs, `AGENT_DEFAULT_LANGUAGE`), domain context files. Files without `knowledge_level: detail` are already in context; read `detail` ones on demand via `knowledge_rw`.
2. If the request is anchored on a Jira card, read it first per `agents/skills/integration/jira-analyzer.md` - the card's acceptance criteria often carry the SLA. If knowledge points at a Confluence requirements page, read it per `agents/skills/integration/confluence-reader.md`.
3. Credential needs surface early: which scenarios require auth, and whether `perf:{service}:*` keys already exist (`secret_vault(operation="list")`) - collection itself belongs to Phase 0.

## Scenario Matrix

Write the matrix into the design doc. Every row is a **bounded profile** - the hard rule:

> **ramp + steady + cooldown ≤ 240 s.** The executor kills scripts at 300 s and there is no fetch-after-timeout; a profile that needs longer must be split into segments and aggregated in the report.

| Scenario | Pattern | Max VUs | Ramp | Steady | Budget |
|---|---|---|---|---|---|
| smoke | linear | 5-10 | 10s | 20s | ≤60s |
| baseline | step ramp | ≤50 | 30s | 120s | ≤180s |
| spike | burst | ≤100 | 5s | 30s | ≤90s |
| soak-lite | constant | ≤20 | 10s | 200s | ≤240s |

Rules:

- Concurrency ceiling = `min(declared ceiling_vus, 100)`; the Phase 0 declaration always wins when it is lower.
- Pick only the scenarios the engagement needs - a first engagement usually runs smoke + baseline; spike and soak-lite are opt-in.
- One primary endpoint family per run unless the user explicitly asks for mixed traffic; mixed profiles document the traffic split.

## SLA & Targets

Per endpoint, propose targets and get them confirmed (or taken from the Jira/Confluence requirement):

- **p95 latency** - the headline number. Default proposal: 500 ms for typical internal APIs, 200 ms for hot read paths; mark every default as *proposal* until the user or the requirement card confirms it.
- **Error rate** - ≤ 1% steady-state, 0% tolerated for 5xx on smoke.
- **Throughput** - rps floor at baseline concurrency, derived from the requirement or left as "observe, no floor" when unknown.

Rules of measurement discipline:

- **Percentiles, never averages** - p50/p95/p99/max per endpoint; an average hides the tail that users feel.
- **Baseline before claims** - a "slow" verdict needs a baseline run under the same profile; re-measure the same way you measured (same script, same data set, same window) or the numbers are not comparable.
- **Change one variable at a time** - when comparing profiles, only the scenario changes between runs.
- **Beat the noise, not the mean** - a delta inside run-to-run variance is not a finding; say "within noise" instead.

## Data Setup

- Resolve each scenario's data needs (order ids, session tokens, search terms) against `agents/skills/testing/test-data-setup.md` recipes: create-once before the run, reuse across segments, **never** clean up shared environments mid-engagement.
- Test data is self-created and disposable; production data is never a load fixture.

## Design Doc Structure

`performance/scenario-design-{YYYYMMDD}.md`:

1. **Scope reference** - tier, declared targets, ceilings (copy from `.tmp/performance/engagement.md`).
2. **Endpoint inventory** - method, path, auth, payload summary, data dependency.
3. **Scenario matrix** - the table above, filtered to chosen scenarios.
4. **SLA & targets** - per endpoint, with confirmed-vs-proposal status.
5. **Data setup** - recipes and creation steps.
6. **Open questions** - anything unconfirmed, so Phase 2 can ask before writing cards.

## Explicit Non-Goals

- No UI/browser performance (no Playwright, no Core Web Vitals) - API only.
- No k6/locust/artillery/JMeter - the channel is Python asyncio+httpx via `script_writer`.
- No unbounded or open-ended soak - anything longer than 240 s is split into segments.

---

*Adapted in part from addyosmani/agent-skills@e997ac5 (reviewed 2026-11-24); rewritten to framework conventions - no upstream code retained.*
