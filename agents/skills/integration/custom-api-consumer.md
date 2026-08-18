---
slug: "integration/custom-api-consumer"
description: "Discover and call project third-party REST APIs via the api_request tool: catalog first, endpoint_key resolution, server-side auth from secret_ref, response extraction and failure handling"
type: "guidance"
triggers:
  - "第三方 api"
  - "custom api"
  - "外部系统"
  - "调用接口"
  - "接口调用"
  - "api_request"
tools:
  - api_request
  - knowledge_rw
  - user_confirm
  - secret_vault
---

# Skill: Custom API Consumer

## When to Use

ANY task that needs to call a third-party / customer-owned REST API — the cross-agent skill, auto-loaded by trigger for every agent declaring `api_request` (project-owner, tech-lead, quality-assurance, external-system-integration-expert).

## Step 1 — Catalog First

Read the catalog before calling anything:

```
knowledge_rw(operation="read", slug="integrations/custom-api")
```

It defines integrations, per-env `base_url` + `allowed_hosts`, auth, and named endpoints. Passing `endpoint_key` makes the server resolve method default, path, per-environment base_url, allowed_hosts, response_json_path, and auth defaults from it — the catalog is the source of truth.

## Step 2 — Prefer endpoint_key over Raw Path

Catalog covers the call → use `endpoint_key` (plus `integration_key`, `environment`, `method`, and payload/path_params only):

```
api_request(
  integration_key="crm",
  endpoint_key="get_customer",
  method="GET",
  environment="uat",
  path_params={"customer_id": "C-123"}
)
```

Raw `path` + explicit `base_url` + `allowed_hosts` ONLY for endpoints not in the catalog:

```
api_request(
  integration_key="crm",
  method="GET",
  path="/api/v2/leads/search",
  base_url="https://crm.example.com",
  allowed_hosts=["crm.example.com"],
  query_params={"status": "open"}
)
```

## Step 3 — Auth via secret_ref Only

- Single secret: `secret_ref` + `secret_scope` (`"project"` default → project secrets then user tokens; `"user"` → user vault only).
- Multi-secret (basic auth, custom templates): `secret_refs` with placeholder names.
- **Never** place auth values in `headers`, `query_params`, or `json_body` — the tool rejects auth-named headers and injects authentication server-side from the vault.
- Missing secret → the tool auto-prompts via a secure dialog; do NOT ask for the token in chat.

```
api_request(
  integration_key="crm",
  endpoint_key="create_lead",
  method="POST",
  secret_ref="third_party:crm:uat",
  secret_scope="project",
  json_body={"name": "Acme"}
)
```

## Step 4 — Writes and Environments

- POST/PUT/PATCH/DELETE and `prd*` environments auto-trigger a user confirmation gate — never call a write op in production without it, never bypass it.
- Catalog endpoints with `side_effect: true` are confirmed even for GET.
- Health checks: `response_mode="status"` (returns `ok` + status only).

## Step 5 — Response Handling

- `response_json_path` (dot notation, e.g. `data.items`) extracts only what you need, keeping context usage minimal.
- `max_response_chars` (default 20000, max 100000) caps the body.
- Sensitive fields (tokens, passwords, keys) are redacted by the tool — never echo them.
- `truncated: true` → body was cut; re-query with a narrower `response_json_path`, not a bigger cap.
- `endpoint_key` responses include a `catalog` block (`resolved_from_catalog`, `endpoint_key`, `resolved_environment`) telling you which environment/base_url the server picked.

## Step 6 — Failure Handling

| Symptom | Likely cause | Action |
|---|---|---|
| 404 / 405 | Wrong environment base_url or path | Re-check the catalog entry for that env |
| 401 / 403 | Secret scope/env mismatch | Secret may be tagged for another environment; re-capture via secure prompt |
| "URL safety check failed: Host ... not in allowed_hosts" | `allowed_hosts` missing for that env | Fix the catalog entry (or ask the integration expert) |
| "Endpoint ... not found in catalog" | Wrong key spelling or not onboarded | Check spelling, or onboard via `integration/custom-api-onboarding` |
| "Secret ... not available after prompt" / prompt timeout | User skipped the secure dialog | Stop, tell the user, do not retry in a loop |
| Catalog missing ("integration catalog not found") | Project has no catalog yet | Read `knowledge/_template/custom-api.md` or run onboarding |

## Cross-Agent Note

Applies to every agent with `api_request` in its frontmatter — no extra configuration needed. The external-system-integration-expert owns catalog gaps and onboarding; other agents own correct consumption.
