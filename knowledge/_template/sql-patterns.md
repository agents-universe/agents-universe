---
category: skills
slug: skills/sql-patterns
tags: [sql, patterns, query]
template_words: 55
title: SQL Pattern Library
---

# SQL Pattern Library

## Dialect & Engine

- Engine / dialect:
- Version-specific quirks:

## Naming & Formatting Conventions

- Table / column naming:
- CTE-first style, explicit aliases, no `SELECT *`:

## Reusable Snippets

### Retention

```sql
-- cohort retention skeleton
```

### Funnel

```sql
-- funnel step conversion skeleton
```

### Sessionization

```sql
-- session splitting skeleton
```

## Performance Notes

- Partition pruning: always filter on partition keys
- Avoid cross joins; pre-aggregate before joining large tables

## Related Knowledge

- [[technical/data-model]]
- [[domain/metric-catalog]]
