---
slug: "analysis/sql-crafter"
description: "Author and execute metric-aligned, read-only SQL against project business databases via shell env_refs (python + sqlalchemy), or api_request for HTTP-exposed data"
type: "guidance"
triggers:
  - "query the database"
  - "write SQL"
  - "run this SQL"
  - "查一下数据"
  - "写个SQL"
  - "跑个数"
tools:
  - shell
  - api_request
  - knowledge_rw
  - user_confirm
---

# Skill: SQL Crafter

Author and run read-only SQL against the project's **business databases**. Never use `sql_query` for business data — it only reaches the platform's own application database.

## Trigger Conditions

- Any request that needs data from an external business database or warehouse.
- A workflow step says "query" or "acquire data" and the source is a database.
- Data is only reachable through an HTTP/CRUD API → use the `api_request` variant below.

## Rule 1 — Definition First

Before writing any SQL:

1. Open `[[domain/metric-catalog]]` and copy the metric's formula/definition **verbatim** into your reasoning.
2. If the metric is missing from the catalog, ask via `user_confirm` whether to add it; record the agreed definition in the Metric Dictionary (and any dispute in Definition Conflicts & Rulings) before querying.
3. If definitions conflict, follow the latest row of Definition Conflicts & Rulings.

## Rule 2 — Reuse Before Authoring

1. Check `[[skills/sql-patterns]]` for a Reusable Snippet that already solves this (retention, funnel, sessionization, …).
2. Follow its Dialect & Engine, Naming & Formatting Conventions, and Performance Notes: CTE-first, explicit aliases, no `SELECT *`, always filter on partition/date keys, pre-aggregate before joins.

## Rule 3 — Locate the Source

1. Resolve the tables you need via `[[technical/data-model]]` Core Tables (layer, granularity).
2. Find the physical source in `[[technical/data-source-map]]` Source Inventory.
3. Take the credential **only** from the row's `Access (secret_ref)` column. Never ask the user to paste a DSN or password into chat.

## Rule 4 — Execute Read-Only

Only `SELECT` / `WITH` / `EXPLAIN`. Refuse DDL/DML outright.

```json
shell(
  command="python -c \"import os, sqlalchemy as sa, pandas as pd; e = sa.create_engine(os.environ['DB_DSN']); df = pd.read_sql(sa.text('WITH daily AS (SELECT ... ) SELECT ... LIMIT 10000'), e); print(len(df)); print(df.head(20).to_csv(index=False))\"",
  env_refs={"DB_DSN": {"scope": "project", "ref": "<secret_ref from data-source-map>", "environment": "prod"}},
  timeout_seconds=120
)
```

- `scope` is `project` for project_secrets refs, `user` for user_tokens refs — match the scope recorded when the secret was saved.
- Secrets injected via `env_refs` are redacted from all output. Never `echo`, print, or interpolate them yourself.
- For HTTP-exposed data, use `api_request(..., secret_ref=...)` per `[[integrations/custom-api]]` instead.

## Rule 5 — Missing Secret

If the tool returns `missing_service_keys`:

1. Follow `agents/skills/interaction/user-confirm.md`: offer the scope choice, then collect via `user_confirm(secret=true, service_key=..., save_to_project_secrets=true)` (or `save_to_user_tokens=true`).
2. Retry the exact same command. Never work around a missing secret by asking for plaintext.

## Rule 6 — Result Discipline

- Always `LIMIT` exploratory queries (default 10000); aggregate in SQL, not in memory.
- Report the row count actually returned and, when truncated, run a `COUNT(*)` variant to state the true size.
- Show a small sample (≤ 20 rows) in chat, not the full result set.

## Output Requirements

1. The final SQL, formatted, with engine/dialect noted.
2. Row count + sample rows.
3. Data window and filters applied.
4. If the query pattern is reusable, append it to `[[skills/sql-patterns]]` Reusable Snippets and log the write in `history.md` per `agents/skills/knowledge/knowledge-manager.md`.
