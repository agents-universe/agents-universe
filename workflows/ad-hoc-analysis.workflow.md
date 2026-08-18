---
slug: "ad-hoc-analysis"
description: "Full ad-hoc data analysis pipeline — clarify the question, locate and acquire data, profile, analyze, visualize, report, and persist knowledge"
triggers:
  - "analyze this"
  - "ad hoc analysis"
  - "help me analyze"
  - "帮我分析一下"
  - "分析一个业务问题"
tools:
  - knowledge_rw
  - shell
  - code_executor
  - api_request
  - chart_renderer
  - user_confirm
  - filesystem
  - plan_task
---

# Workflow: Ad-hoc Analysis

## Goal

Turn a business question into a verified, metric-aligned answer — and leave the project knowledge base richer than before.

## Inputs Required

- The business question (required).
- Optional: data source hint, file path, time window, comparison baseline.

## Steps

### Step 1 — Clarify the question

1. Restate the question in one sentence.
2. Map every mentioned metric to `[[domain/metric-catalog]]`; quote the formula.
3. Ambiguous scope/window/segments → one `user_confirm` round with concrete options. Non-critical gaps → state `[assumption: ...]` and proceed.
4. For multi-part questions, use `plan_task` to track the sub-questions.

### Step 2 — Locate the data

1. Find the tables in `[[technical/data-model]]` and their physical source in `[[technical/data-source-map]]`.
2. Source not registered → ask the user, then hand off to `workflows/data-source-onboarding.workflow.md`.
3. File input → skip to acquisition via `analysis/local-file-analyst`.

### Step 3 — Acquire the data

- Database → `agents/skills/analysis/sql-crafter.md` (definition-first, read-only, sampled).
- Local file → `agents/skills/analysis/local-file-analyst.md`.
- HTTP API → `api_request` with `secret_ref` per `[[integrations/custom-api]]`.

### Step 4 — Profile

Run `agents/skills/analysis/data-profiler.md`. Capture the caveat list — it is mandatory input to the report.

### Step 5 — Analyze

Pick the method from `[[skills/analysis-patterns]]` Method Selection (comparative / trend / funnel / retention-cohort / attribution). Keep every number traceable to a query or computation you actually ran.

### Step 6 — Visualize

Render figures via `agents/skills/analysis/dataviz.md`; chart type per its Chart Selection Rules. Conceptual diagrams only via `chart_renderer`.

### Step 7 — Report

Follow `agents/skills/analysis/report-writer.md`: conclusion-first chat summary + optional self-contained HTML in `reports/`.

### Step 8 — Persist knowledge

1. Append the analysis to `[[domain/analysis-scenarios]]` Ad-hoc / Thematic Analysis Log.
2. Reusable SQL → `[[skills/sql-patterns]]` Reusable Snippets.
3. New source/table facts → `[[technical/data-source-map]]` / `[[technical/data-model]]`.
4. Log all writes in `history.md` per `agents/skills/knowledge/knowledge-manager.md`.

## Success Criteria

- Conclusion cites catalog definitions and states the data window.
- Caveats from profiling are present in the report.
- At least one knowledge write-back occurred (or an explicit reason why not).
- No secret appears in any message, artifact, or knowledge file.

## Error Handling

- **Missing secret** (`missing_service_keys`) → collect via `agents/skills/interaction/user-confirm.md`, retry the identical command.
- **code_executor timeout** → reduce the sample, verify the approach, then scale up.
- **CSV decode failure** → follow the encoding fallback chain in `analysis/local-file-analyst`.
- **Source unreachable** → report the Access Notes from `[[technical/data-source-map]]` (VPN / whitelist / read-only boundary) and stop — never ask for credentials in chat.
