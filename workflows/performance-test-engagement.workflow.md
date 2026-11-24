---
slug: "performance-test-engagement"
description:
  zh-CN: "性能测试交付全流程——范围授权门禁、知识驱动场景设计、Jira/Confluence 回写、Python 压测脚本生成与有界执行、MD+PDF 报告交付与知识回写"
  en-US: "Full performance-test engagement - scope/authorization gate, knowledge-driven scenario design, Jira/Confluence writeback, bounded Python stress-script generation and execution, MD+PDF report delivery, and knowledge writeback"
triggers:
  - "performance test"
  - "load test"
  - "stress test"
  - "性能测试"
  - "压测"
  - "负载测试"
tools:
  - user_confirm
  - memory_rw
  - knowledge_rw
  - filesystem
  - script_writer
  - code_executor
  - jira
  - confluence
  - deliver_file
  - chart_renderer
---

# Performance Test Engagement

The performance test expert's default workflow for whole-engagement requests (「对订单接口做一次压测」「run a load test on the login API」「性能测试并出报告」). Deliverable: `performance/perf-report-{YYYYMMDD}.md` + `performance/perf-report-{YYYYMMDD}.pdf` + `performance/results/` in the project workspace. Channel: **Python asyncio+httpx API load scripts** executed through `script_writer` - no browser, no external load tools.

Files under `workflows/` remain English regardless of `AGENT_DEFAULT_LANGUAGE`; the report body follows `AGENT_DEFAULT_LANGUAGE` unless the user overrides.

## 1. Relationship to Other Workflows

- This workflow owns the full engagement: scope -> design -> writeback -> script -> execution -> report -> knowledge writeback.
- `test-artifact-and-jira-conventions` still governs Jira evidence/writeback conventions - Phase 2 follows its §0/§0.5/§3 rules with the `[AI perf]` card variant from `agents/skills/performance/perf-scenario-writeback.md`.
- `knowledge-ingestion` governs knowledge writeback; Phase 6 applies its eligibility gate.
- QA's `whole-system-test-planning` is a UI/functional test plan (writes `tests/test-plan.md`) and is a different artifact - do not merge the two documents.

## 2. Phase 0 - Scope and Authorization

Per `agents/skills/performance/perf-scope-guard.md`:

1. Infer the active project; read `environment` knowledge for tier URLs and `AGENT_DEFAULT_LANGUAGE`.
2. Check for an existing scope declaration FIRST - `.tmp/performance/engagement.md` and the `PERF_SCOPE` project memory. Reuse it; only when nothing is declared ask ONE question (the environment tier, `field_key="perf_scope_env"`), and record the answer to both sinks. Endpoint/ceiling/duration defaults are stated, not asked (correctable in chat). Production load needs separate explicit confirmation (`field_key="perf_prod_load_authorization"`), never a second environment question; no load on production until given.
3. Collect perf credentials if authenticated scenarios are in scope (`perf:{service}:username/password`); user decline -> mark blocked, continue unauthenticated.
4. Create working dirs: `.tmp/performance/` (scratch), `performance/results/` (metric series). Record the declaration in `.tmp/performance/engagement.md`.

## 3. Phase 1 - Scenario Design

Per `agents/skills/performance/perf-test-designer.md`:

1. Knowledge-first inventory (`environment` -> `technical/api-map` -> `technical/kong-map` -> domain context); read `detail`-level files on demand.
2. Endpoint inventory with auth and data dependencies; anchor on a Jira/Confluence requirement when the request names one (`integration/jira-analyzer` / `integration/confluence-reader`).
3. Scenario matrix filtered to the engagement's needs (smoke/baseline always; spike/soak-lite opt-in), every profile ≤ 240 s, concurrency = min(declared, 100).
4. Per-endpoint SLA targets (confirmed vs proposal), data setup via `testing/test-data-setup` recipes.
5. Output: `performance/scenario-design-{YYYYMMDD}.md`.

## 4. Phase 2 - Writeback (Jira OR Confluence)

Per `agents/skills/performance/perf-scenario-writeback.md`:

1. Availability decision: user-named target > Jira probe > Confluence probe > local-only fallback with blocker stated.
2. Jira: one `[AI perf]` card per target (`create_issue`, not `create_test_issue`), search-before-create on the `AIPerf` label, link to the requirement, wiki-syntax body, read-back verify. Confluence: `create_page(body_format="wiki")`, read-back verify, `update_page` on re-runs (never delete).
3. Design-only engagements stop after this phase.

## 5. Phase 3 - Script Generation

Per `agents/skills/performance/perf-load-script-writer.md`:

1. Author the asyncio+httpx script for the chosen scenario(s): bounded by `DEADLINE_S=240`, semaphore-limited VUs, monotonic timing, nearest-rank percentiles, results JSON to `performance/results/{slug}-{YYYYMMDD}.json`, compact `VERDICT` block printed last.
2. `script_writer(operation="create", script_type="python", secrets=[...])` with declared `perf:{service}:*` keys; prototype auth/paths via a short `code_executor` probe if needed.

## 6. Phase 4 - Execution

1. `script_writer(operation="run", script_id=...)`; parse `stdout_tail` for the `VERDICT` block.
2. Failed run -> `operation="update"` fix -> rerun; cap at 3 update+rerun cycles, then report the blocker.
3. `status:"running"` -> the run exceeded the poll window: report tool-limited, do NOT re-run to poll.
4. Longer-than-240s requests -> segmented runs (same script, shifted windows), each a separate run, aggregated for the report.

## 7. Phase 5 - Report

Per `agents/skills/performance/perf-report-writer.md`:

1. Write `performance/perf-report-{YYYYMMDD}.md` (9 sections; every metric traced to `performance/results/*.json`; executor limits disclosed; SLA verdict unambiguous).
2. Convert to the consulting-grade PDF per `agents/skills/performance/perf-report-pdf.md` (Converter Script via `code_executor`; clear every `WARN:`; pypdf verification inside the script).
3. Deliver both files via `deliver_file` (two explicit calls), summarize in chat.

## 8. Phase 6 - Knowledge Writeback

1. Reusable perf knowledge -> existing project knowledge files only when they cover the topic (fill-first rule per `workflows/knowledge-ingestion.workflow.md` and `agents/skills/knowledge/knowledge-manager.md`): recurring endpoint quirks, capacity observations, proven load patterns - engagement-specific numbers stay in the report.
2. Append `history.md` entry: engagement date, scope tier, report path, headline verdict.

## 9. Success Criteria

- Scope declared exactly once per engagement; `PERF_SCOPE` persisted; production load only after its separate authorization.
- Scenario design exists with every profile ≤ 240 s and SLA rows labeled confirmed/proposal.
- Writeback read-back verified (Jira `get_issue`/`get_comment` or Confluence `get_pages`), or the local-only fallback is explicitly reported.
- The load run terminated on its own (< 240 s) with the `VERDICT` block inside `stdout_tail`; metrics persisted to `performance/results/*.json`.
- `performance/perf-report-{YYYYMMDD}.md` exists with all 9 sections in order; `.pdf` exists, pypdf-verified (headings + `p95` extractable, no `WARN:`), both delivered via `deliver_file` with URLs quoted verbatim.
- Every Results number traces to a run artifact; no estimated metrics anywhere.

## 10. Error Handling

- Scope not declared -> Phase 0 asks once; never generate load ahead of authorization.
- Scope prompt timed out, was dismissed, or the tool result says `do_not_reask` / `timed_out` -> do NOT open the dialog again this turn. Ask in plain chat text, keep design work running, and stop execution phases until the declaration exists.
- Neither Jira nor Confluence reachable -> local design doc only, state the integration blocker and configuration path; proceed with script/report phases.
- Run failed / auth missing -> fix via `update` (≤ 3 cycles) or mark the scenario blocked by missing credentials; never loop silently.
- Run returned `status:"running"` -> tool-limited disclosure; the report's Appendix records it. Never re-run as a poll.
- Target unreachable from the executor -> VERDICT with the failure state already captured (or a failed run recorded); report environment-blocked with evidence.

## 11. Slices

- **Full engagement** (this workflow): Phases 0-6.
- **Design-only**: Phases 0-2, stop (scenario design delivered, no script, no report).
- **Writeback-only**: Phase 0 (re-check declaration), Phase 2 against an existing `performance/scenario-design-*.md`.
- **Script-only**: Phase 0 (re-check declaration), Phases 3-4 against an existing design; report optional.
- **Report-only re-run**: Phase 0 (re-check), Phase 5 from persisted `performance/results/*.json` + run logs.
