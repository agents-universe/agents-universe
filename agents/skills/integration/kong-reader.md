---
slug: "integration/kong-reader"
description: "Call Kong / OpenAPI endpoints based on the project base and kong-map knowledge, automatically resolve the Kong key from user tokens by environment, and send it in the x-api-key header"
---

# Skill: Kong Reader

## Trigger Conditions

- The user asks to inspect a project's Kong / OpenAPI / api-docs.
- The user asks to build an endpoint path from the project base.
- The user provides one or more full Kong URLs and expects the agent to read or reuse them directly.
- The current task needs to supplement the project's API-doc entry point, service catalog, or live response examples.

## Required Knowledge

Read first: `knowledge/kong-map.md`, `knowledge/login-and-user-switch.md`. If the project files do not exist, fall back to `knowledge/_template/kong-map.md` and `knowledge/_template/login-and-user-switch.md`.

## Execution Steps

1. Full Kong URL → normalize immediately into project base / relative path / environment hint; do not ask the user to rewrite it.
2. Multiple full URLs as variants (e.g. AFC / LC) → candidate route variants of the same capability unless evidence shows they are unrelated.
3. Project base priority: user-specified `--base` > base from the provided URL > `KONG_BASE_URL_{ENV}` in the `environment/environment` config block.
4. Access the endpoint with the `kong` tool:

```json
kong(operation="request", path="<relative-path>", method="GET", env="dev")
kong(operation="request", path="<relative-path>", method="POST", body={...}, env="uat")
```

The `kong` tool auto-resolves the base URL from `KONG_BASE_URL_{ENV}` in the project's `environment/environment` config block (`KONG_BASE_URL_DEV` / `KONG_BASE_URL_UAT` / `KONG_BASE_URL_INT`); pass `base_url` only to override.

5. Non-`GET` → set `method` and include `body` as a JSON object.
6. Token auto-selected by env: `env="dev"` → `kong:dev`, `env="uat"` → `kong:uat`, `env="int"` → `kong:int`.
7. Business login state or company / tenant switching needed → establish that context per `login-and-user-switch.md`.
8. Route variants in the provided URLs (e.g. `/kong/api/variant-a/tables` vs `/kong/api/variant-b/tables`) → write both into `kong-map.md` with the observed difference; do not collapse into one guessed path.

## Output Requirements

- Prefer structured JSON or Markdown; record request URL, method, status code, key response fields.
- Input was full URLs → record the normalized relative paths into `kong-map.md` for reuse without the host.
- Kong endpoint maps to a downstream business endpoint → record that mapping too (e.g. `POST /closed-contracts`).
- Gateway layer → distinguish the gateway request body from the downstream business body; do not assume the downstream DTO works as the Kong body.
- Write back to `kong-map.md` or `history.md`; never write the token into knowledge.

## Error Handling

- Missing token for the environment → the `kong` tool auto-prompts via a secure input dialog; the token is encrypted and saved as a project secret (`kong:{env}`), never passing through the LLM, usable by any project member via the tool.
- Missing Kong base URL → tell the user to set `KONG_BASE_URL_DEV` / `KONG_BASE_URL_UAT` / `KONG_BASE_URL_INT` in the project's `environment/environment` config block.
- Non-2xx response → preserve the original `status` and `body`; do not guess at the meaning. 404/405/400 during data setup → trigger the agent's API Failure Recovery (refresh spec from `api-map.md` `Page:`, update knowledge, retry).
- Wrong base/path concatenation → first check whether the relative path in knowledge starts with `/`, then check the Kong Gateway table in environment knowledge.

## Credentials

```yaml
credentials:
  - service_key: kong:dev
    environment: dev
    scope: project
    required: true
    description: Kong Dev API token (stored as project secret)
  - service_key: kong:uat
    environment: uat
    scope: project
    required: false
    description: Kong UAT API token (stored as project secret)
  - service_key: kong:int
    environment: int
    scope: project
    required: false
    description: Kong INT API token (stored as project secret)
```

Tokens are managed via the Memory tab → 项目密钥 panel, or interactively when first needed.
