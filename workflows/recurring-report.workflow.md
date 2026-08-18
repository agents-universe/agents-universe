---
slug: "recurring-report"
description: "Generate a periodic report or dashboard from its definition in the analysis-scenarios knowledge file — metrics, queries, rendering, delivery, and drift detection"
triggers:
  - "weekly report"
  - "monthly report"
  - "run the report"
  - "生成周报"
  - "生成月报"
  - "例行报告"
tools:
  - knowledge_rw
  - shell
  - code_executor
  - filesystem
  - chart_renderer
  - user_confirm
---

# Workflow: Recurring Report

## Goal

Produce a named recurring report or dashboard exactly per its definition row in `[[domain/analysis-scenarios]]` Recurring Reports & Dashboards (Frequency, Audience, Core Metrics, Output Format) — consistent across runs, never improvised.

## Inputs Required

- Report name (or frequency, e.g. "weekly").
- Target period.

## Steps

### Step 1 — Load the definition

Match the report's row in `[[domain/analysis-scenarios]]`. Not found → ask via `user_confirm` whether to define it first (name, frequency, audience, core metrics, output format). Never improvise a recurring report.

### Step 2 — Resolve the metrics

Each Core Metric → formula from `[[domain/metric-catalog]]`. If a formula has drifted from what previous runs used (check prior SQL in `[[skills/sql-patterns]]` or past outputs), flag it via `user_confirm` and record the ruling in Definition Conflicts & Rulings.

### Step 3 — Query

Via `agents/skills/analysis/sql-crafter.md`, reusing `[[skills/sql-patterns]]` snippets. Use **one data window for all metrics**; record window and filters.

### Step 4 — Render

By the definition's Output Format:

- `dashboard` → self-contained HTML via `agents/skills/analysis/report-writer.md` at `reports/<name>/<period>.html`.
- `report` → Markdown report contract.
- `export` → CSV/Excel via `code_executor` into the project workspace.
- Charts via `agents/skills/analysis/dataviz.md`; `chart_renderer` only for flow/concept diagrams.

### Step 5 — Deliver

Chat: conclusion-first highlights matched to the Audience + links. State the artifact path. If the source data was stale (pipeline delay per `[[technical/data-pipelines]]`), label the report **preliminary** with the reason.

### Step 6 — Log

Routine runs are NOT appended to the Ad-hoc log. Do update the Recurring row if metrics/format/audience changed, and always append `history.md`.

## Success Criteria

- All Core Metrics present, formulas aligned to the catalog, one consistent period.
- Artifact at a stable, dated path.
- Any definition drift was surfaced and ruled on, not silently absorbed.

## Error Handling

- **One metric fails** → deliver a partial report with the gap called out; never silently drop a metric.
- **Stale source** → preliminary label + reason; offer to re-run after the pipeline recovers.
- **Missing secret** → collect via `agents/skills/interaction/user-confirm.md`, retry.
