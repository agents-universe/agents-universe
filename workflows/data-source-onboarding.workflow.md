---
slug: "data-source-onboarding"
description: "Register a new data source end-to-end — metadata, secure credential collection, connectivity test, schema discovery, and knowledge updates"
triggers:
  - "onboard a data source"
  - "connect a database"
  - "register a new data source"
  - "接入数据源"
  - "新增数据库"
  - "配置数据连接"
tools:
  - knowledge_rw
  - user_confirm
  - shell
  - secret_vault
  - api_request
  - memory_rw
---

# Workflow: Data Source Onboarding

## Goal

A fully registered, connectivity-verified data source: credentials in the vault, `[[technical/data-source-map]]` updated, zero plaintext anywhere.

## Inputs Required

- Source type: DB / Warehouse / File / API.
- Environment: dev / uat / prod.
- Owner (team or person).

## Steps

### Step 1 — Collect non-secret metadata

Via `user_confirm(kind="text", ...)` one at a time: host, port, database name, owner, refresh frequency. Persist per `agents/skills/interaction/user-confirm.md` conventions (`memory_rw(memory_type="project_setting", ...)`).

### Step 2 — Choose the secret scope

Via `user_confirm(kind="selection", ...)`:

| Choice | Storage | Scope |
|---|---|---|
| 个人密钥 / user key vault | `user_tokens` | per-user, cross-project |
| 项目共享密钥 / project secrets | `project_secrets` | per-project, shared with members |

State that project secrets are resolvable by other project members.

### Step 3 — Collect the secret

- For user scope: `secret_vault(operation="list")` first — skip collection if the key already exists.
- Then `user_confirm(secret=true, service_key="db:<name>:dsn", environment="<env>", save_to_project_secrets=true)` or `save_to_user_tokens=true` per the chosen scope. Exactly one save flag. Secret mode returns only an opaque status.

### Step 4 — Connectivity test

Database source:

```json
shell(
  command="python -c \"import os, sqlalchemy as sa; e = sa.create_engine(os.environ['DB_DSN']); print(e.connect().execute(sa.text('SELECT 1')).scalar())\"",
  env_refs={"DB_DSN": {"scope": "<project|user>", "ref": "db:<name>:dsn", "environment": "<env>"}},
  timeout_seconds=60
)
```

API source: `api_request` against its health/ping endpoint with `secret_ref`.

### Step 5 — Schema discovery (optional)

List tables via `information_schema` and propose Core Tables rows for `[[technical/data-model]]` (layer/granularity marked `[inferred]` until the user confirms).

### Step 6 — Write knowledge

1. Append a Source Inventory row to `[[technical/data-source-map]]` — the Access column contains ONLY the `secret_ref`, never a value.
2. Update Access Notes: network/VPN requirements, read-only boundary, PII rules.
3. If Step 5 ran, update `[[technical/data-model]]`.
4. Append `history.md`.

## Success Criteria

- `SELECT 1` (or API ping) succeeds through the injected secret.
- data-source-map row present with `secret_ref` only; scope and environment recorded.
- No plaintext credential in any message, file, or knowledge entry.

## Error Handling

- **Missing Python driver/library** → report exactly what is missing (shell blocks `pip install`; runtime deps live in the container image) and stop.
- **Connection failure** → walk the Access Notes checklist (VPN, IP whitelist, wrong environment, expired credential). Never retry by asking the user to paste the password into chat.
- **User declines secret save** → mark the source `pending credentials` in the Source Inventory and stop.
