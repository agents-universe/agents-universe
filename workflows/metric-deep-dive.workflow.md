---
slug: "metric-deep-dive"
description: "Investigate a metric anomaly end-to-end — lock the definition, exclude data-quality causes, compare periods, drill down dimensions, quantify contributions"
triggers:
  - "why did the metric change"
  - "metric dropped"
  - "deep dive"
  - "指标为什么下降"
  - "异动归因"
  - "下钻分析"
tools:
  - knowledge_rw
  - shell
  - code_executor
  - user_confirm
  - chart_renderer
---

# Workflow: Metric Deep-Dive

## Goal

Produce a ranked, quantified root-cause list for one metric's movement — every cause carries a number, and unexplained change is stated as an explicit residual.

## Inputs Required

- Metric name (must exist in or be added to `[[domain/metric-catalog]]`).
- Anomaly period.
- Baseline period (default: the immediately preceding period).
- Optional: dimension hints, suspected causes.

## Steps

### Step 1 — Lock the definition

Restate the formula verbatim from `[[domain/metric-catalog]]`. Undefined or conflicting → resolve via `user_confirm` and record the ruling in Definition Conflicts & Rulings. Do not proceed on an undefined metric.

### Step 2 — Data sanity first

1. Check pipeline freshness/failures in `[[technical/data-pipelines]]` (Backfill & Failure Handling).
2. Check DQ rules in `[[technical/data-model]]`; run targeted `analysis/data-profiler` checks.
3. If the data is broken, that IS the root cause — report it and stop.

### Step 3 — Quantify the change

Via `agents/skills/analysis/sql-crafter.md`: compute the metric for anomaly vs baseline periods. State absolute and relative change and both data windows.

### Step 4 — Drill down dimensions

Follow `agents/skills/analysis/metric-investigator.md`: iterate the catalog's Dimensions, rank segments by change magnitude, and separate segment mix shift from within-segment change (Simpson's paradox guard).

### Step 5 — Attribute contributions

Decompose the total change into per-segment contributions; verify they reconcile with the total. Render the decomposition via `agents/skills/analysis/dataviz.md` (waterfall or ranked bar).

### Step 6 — Report and persist

1. Report per `agents/skills/analysis/report-writer.md` — ranked causes with quantified contributions, evidence, residual.
2. Append to `[[domain/analysis-scenarios]]` Ad-hoc / Thematic Analysis Log; reusable segment SQL → `[[skills/sql-patterns]]`; log writes in `history.md`.

## Success Criteria

- Every claimed cause has a quantified contribution and evidence.
- The residual (unexplained change) is stated explicitly.
- Any definition conflict encountered was ruled on and recorded.

## Error Handling

- **Metric undefined** → offer to add it to the catalog (`user_confirm`) before continuing.
- **Insufficient dimension coverage** → state the gap explicitly; never invent causes.
- **Missing secret** → collect via `agents/skills/interaction/user-confirm.md`, retry.
