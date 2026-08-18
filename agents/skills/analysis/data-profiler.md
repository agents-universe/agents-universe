---
slug: "analysis/data-profiler"
description: "Profile any dataset before analysis — schema, distributions, nulls, duplicates, outliers — with sample-then-scale discipline and data-quality rule cross-checks"
type: "guidance"
triggers:
  - "profile this data"
  - "data quality check"
  - "EDA"
  - "数据画像"
  - "数据质量"
  - "探索性分析"
tools:
  - code_executor
  - shell
  - knowledge_rw
  - user_confirm
---

# Skill: Data Profiler

Profile every dataset **before** drawing conclusions from it. The output is a profile report plus a caveat list that must travel with any downstream analysis or report.

## Trigger Conditions

- Immediately after any ingestion (via `analysis/sql-crafter` or `analysis/local-file-analyst`).
- Explicit requests: data quality check, EDA, "看看数据长什么样".

## Rule 1 — Sample-Then-Scale

- **Database tables**: profile with SQL aggregates through `analysis/sql-crafter` — `COUNT(*)`, `COUNT(DISTINCT col)`, `SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END)`, `MIN`/`MAX`, percentile approximations. Never pull a full table into the sandbox.
- **DataFrames**: profile in `code_executor`:

```python
import pandas as pd
print(df.shape)
print(df.dtypes)
print(df.describe(include="all").to_string())
print("null %:\n", (df.isna().mean() * 100).round(2))
print("duplicate rows:", df.duplicated().sum())
```

## Rule 2 — Standard Profile Contract

- Per column: dtype, null %, unique %, min/max/mean/median (numeric) or top-5 values with counts (categorical).
- Dataset level: row count, full-row duplicates, time range of date/timestamp columns.

## Rule 3 — Outliers

Use IQR fences (Q1 − 1.5·IQR, Q3 + 1.5·IQR) or |z| > 3. Report counts and a few example values. **Never silently drop outliers** — state how they were handled.

## Rule 4 — Data-Quality Cross-Check

Compare findings against the Data Quality Rules table in `[[technical/data-model]]` (uniqueness / null / reconciliation rules). Report every violation with its severity from that table, and carry it as a caveat in any analysis built on this data.

## Rule 5 — Freshness

Always state the data window. For warehouse tables, check `[[technical/data-pipelines]]` Job Inventory schedules before trusting "latest" data — a table whose job failed yesterday is stale, say so.

## Output Requirements

1. Structured profile table in chat (compact — no raw dumps).
2. Explicit **caveat list**: every quality issue, freshness concern, or assumption. This list is mandatory input to `analysis/report-writer`.
3. New durable DQ findings: propose adding them to `[[technical/data-model]]` Data Quality Rules via `user_confirm` before writing.
