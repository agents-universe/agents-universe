---
slug: "analysis/metric-investigator"
description: "Root-cause metric anomalies — confirm the definition, exclude data-quality causes, compare periods, drill down dimensions, quantify each factor's contribution"
type: "guidance"
triggers:
  - "why did the metric drop"
  - "why did X increase"
  - "root cause"
  - "指标下降"
  - "指标异动"
  - "异动分析"
  - "找一下原因"
tools:
  - shell
  - code_executor
  - knowledge_rw
  - user_confirm
---

# Skill: Metric Investigator

Investigate why a metric moved. The deliverable is a **ranked, quantified** cause list — every claimed cause carries a number.

## Trigger Conditions

- The user reports an unexpected metric movement ("GMV dropped yesterday", "转化率怎么涨了").
- The user asks for attribution of a change between periods or groups.

## Rule 1 — Lock the Definition

Restate the metric's formula **verbatim** from `[[domain/metric-catalog]]` before touching data. If the metric is undefined or definitions conflict, resolve it first via `user_confirm` and record the ruling in Definition Conflicts & Rulings. Never investigate an undefined metric.

## Rule 2 — Rule Out Data Problems First

Before any business hypothesis:

1. Pipeline freshness/failures per `[[technical/data-pipelines]]` (Backfill & Failure Handling).
2. DQ rule violations per `[[technical/data-model]]` (run `analysis/data-profiler` checks on the affected tables).

A broken pipeline or a DQ violation **is a valid root cause** — if found, report it and stop. Do not invent business explanations for broken data.

## Rule 3 — Quantify the Change

Via `analysis/sql-crafter`: compute the metric for the anomaly period and the baseline (previous period / same period last week / last year, as appropriate). State absolute and relative change, and the exact data window of both.

## Rule 4 — Dimension Drill-Down

Iterate the metric's Dimensions column from the catalog (channel, region, product, …):

- Rank segments by change magnitude, not by level.
- Guard against **Simpson's paradox**: separate "segment mix shift" (composition changed) from "within-segment change" (the segment itself moved). Report both.

## Rule 5 — Contribution Quantification

Decompose the total change into per-segment contributions (share-of-change or leave-one-out). The sum of contributions must reconcile with the total change; whatever remains unexplained is stated as an explicit residual.

## Output Requirements

1. Ranked cause list: cause → quantified contribution → evidence (query/chart).
2. Explicit residual / unresolved factors.
3. Data caveats inherited from profiling.
4. Append the investigation to `[[domain/analysis-scenarios]]` Ad-hoc / Thematic Analysis Log (Date | Topic | Question | Conclusion | Output Location), and log the write in `history.md`.
