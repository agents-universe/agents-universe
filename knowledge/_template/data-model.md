---
category: technical
slug: technical/data-model
tags: [data, data-model, tables]
template_words: 45
title: Data Model
---

# Data Model

Core tables and their relationships used by analysis work.

## Core Tables

| Table | Layer (ODS / DWD / DWS / ADS) | Granularity | Description |
|-------|-------------------------------|-------------|-------------|
| | | | |

## Fact-Dimension Relationships

```mermaid
erDiagram
    FACT_TABLE ||--o{ DIM_TABLE : "fk"
```

## Data Quality Rules

| Table | Rule (uniqueness / null / reconciliation threshold) | Severity | Owner |
|-------|------------------------------------------------------|----------|-------|
| | | | |

## Related Knowledge

- [[technical/data-source-map]]
- [[technical/data-pipelines]]
- [[domain/metric-catalog]]
