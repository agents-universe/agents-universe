---
slug: "integration/self-adapt-db-access"
description: "Use and register the self-adapt DB access service as the last fallback test data source through existing Kong routes, and highlight those Jira steps in red"
---

# Skill: Self-Adapt DB Access Service

Ensure `workflows/test-artifact-and-jira-conventions.workflow.md` is loaded. If the current task is a Jira test workflow, ensure `workflows/automation-workflow-playbook.workflow.md` is also loaded.

## Trigger Conditions

- The user provides or confirms a Kong path such as `/kong/api/{accessName}/tables`.
- The user provides full variant URLs such as `https://.../kong/api/<variant-a>/tables` and `https://.../kong/api/<variant-b>/tables`.
- The current story has no stable UI entry and no stable system-owned API for the target assertion.
- The user explicitly asks to use the self-adapt DB access service.

## What This Service Is

Per project knowledge:

- Spring Boot service that scans configured datasources and exposes dynamic CRUD APIs; multiple named datasources.
- Access-name-aware surface `/api/{accessName}/...`; table discovery `GET /api/{accessName}/tables`; datasource discovery `GET /api/datasources`.
- Dynamic CRUD examples: `GET /api/{accessName}/{tableName}` and `GET /api/{accessName}/{tableName}/{id}`.
- In the current gateway usage, consume it through the existing Kong path.
- Discovery routes `GET /kong/api/<variant-a>/tables` / `GET /kong/api/<variant-b>/tables` only reveal which tables have generated APIs; after discovery, register every returned table's generated CRUD routes in `kong-map.md`.

## Source Priority Rule

Verification path order, always:

1. UI or UI+integration when the behavior is stably observable through the product.
2. The system's own business API when there is no reliable UI path.
3. The self-adapt DB access service only when both UI and system-owned API are unavailable or insufficient.

Do not jump to it just because it is convenient.

## Kong Route Rule

Fetch discovery results with the `kong` tool (use the actual paths configured in your project's `kong-map.md`):

```json
kong(operation="request", path="/kong/api/<variant-a>/tables", method="GET")
kong(operation="request", path="/kong/api/<variant-b>/tables", method="GET")
```

The `kong` tool handles authentication automatically. Do not open, search, quote, print, or summarize credential values.

Then use the returned table lists to register dynamic CRUD routes by calling Kong for each table:

- `GET /kong/api/{accessName}/{tableName}`
- `GET /kong/api/{accessName}/{tableName}/{id}`
- `POST /kong/api/{accessName}/{tableName}`
- `PUT /kong/api/{accessName}/{tableName}/{id}`
- `DELETE /kong/api/{accessName}/{tableName}/{id}`

Full Kong URLs instead of relative paths → automatically:

1. Normalize into project base + relative path.
2. Write the normalized variants into `kong-map.md`.
3. Reuse them via the `kong` tool: `kong(operation="request", path="...", method="GET", env="dev")`.

Do not ask the user to manually convert the URLs into `kong-map` format first.

## Variant Rule

The target story may differ between named variants (e.g. `<variant-a>` / `<variant-b>`); do not assume the Kong path encodes that through `accessName`.

Preferred order:

1. Use the real gateway path or full URL first as configured in `kong-map.md`.
2. Check whether the distinction is carried by path, params, headers, returned table set, or downstream business meaning.
3. Record only the observed distinction; keep concrete variants in `kong-map.md` when the live route exposes them.
4. Do not fabricate variant segments the live route does not expose.

Explicit user-provided variants override older knowledge. Actual variant names are project-specific — read them from `kong-map.md` or environment knowledge, never a hardcoded list.

## Jira Marking Rule

If a test design, Jira test-card description, or execution writeback uses the self-adapt DB access service, the affected steps must be marked in red.

Wrap the affected lines in Jira wiki red-color markup yourself when writing bodies via
`jira(operation="create_test_issue"|"update_description"|"add_comment"|"create_test_cycle", ...)`:

```text
{color:red}[SELF-ADAPT-DB] Query /kong/api/tables through Kong because no stable UI or product API is available.{color}
```

There is no automatic conversion; the `{color:red}...{color}` markup must be included in the body text.

## Output Requirements

- Whenever used, state why UI and product API were insufficient.
- Record the gateway path, table name, and table purpose when known (business meaning / why it is the right fallback).
- Variant differences relevant → record the actual distinguishing factor, not an assumed `accessName`.
- Full variant URLs provided → persist both normalized routes into `kong-map.md` automatically.
- Write Kong-backed fallback knowledge into `kong-map.md`, not `api-map.md`.
- Do not stop after `.../tables` — discovery routes are not the full generated API surface.
- Write to `api-map.md` only when the same business fact also needs a product-owned API inventory entry.
- Do not write secrets (Kong Admin tokens, gateway keys) into knowledge or Jira.
