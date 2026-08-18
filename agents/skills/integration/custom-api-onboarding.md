---
slug: "integration/custom-api-onboarding"
description: "End-to-end onboarding of a NEW third-party REST system into the project's integrations/custom-api catalog: collect config, capture secrets securely, write the catalog entry, verify with api_request, and optionally import an OpenAPI/Swagger spec"
type: "guidance"
triggers:
  - "新系统接入"
  - "接入新系统"
  - "新增集成"
  - "onboard"
  - "导入 openapi"
  - "导入 swagger"
tools:
  - api_request
  - knowledge_rw
  - user_confirm
  - secret_vault
  - memory_rw
  - web_fetch
  - filesystem
---

# Skill: Custom API Onboarding

## When to Use

- The user asks to integrate a **new** third-party system (new `integration_key`).
- The catalog (`integrations/custom-api`) has no entry for the system.
- The user asks to import an OpenAPI / Swagger spec into the project.
- A consumer failed with "endpoint not found in catalog" and the gap needs onboarding.

**NOT for** calling an existing catalog entry — use `integration/custom-api-consumer`.

## First Reads (always, in order)

1. `knowledge_rw(operation="list", category="integrations")` — see what exists.
2. `knowledge_rw(operation="read", slug="integrations/custom-api")` — avoid duplicate `integration_key`s; preserve existing entries when rewriting.
3. `secret_vault(operation="list")` — see which user-scope keys exist.

## Catalog Entry Format

Every integration is one YAML fenced block appended to `integrations/custom-api.md`:

```yaml
integration_key: <unique-kebab-case-key>
display_name: <Human-readable name>
environments:
  dev:
    base_url: https://<service>-dev.example.com
    allowed_hosts:
      - <service>-dev.example.com
  uat:
    base_url: https://<service>-uat.example.com
    allowed_hosts:
      - <service>-uat.example.com
auth:
  type: bearer | api_key_header | basic | cookie | custom_header | body_field | none
  secret_ref: third_party:<integration_key>:{environment}
  header_name: X-API-Key  # only for api_key_header / custom_header
defaults:
  timeout_seconds: 30
  max_response_chars: 20000
endpoints:
  <endpoint_key>:
    method: GET
    path: /api/v1/<resource>/{resource_id}
    description: <What this endpoint does>
    side_effect: false   # true = write/state-changing; forces user confirmation even for GET
    response_json_path: <dot-notation, e.g. data.items — NOT $.data>
```

Rules: `endpoint_key` short kebab-case; `response_json_path` plain dot notation (`data.items`, never `$.data`); side-effect endpoints (POST/PUT/PATCH/DELETE or state changes) MUST set `side_effect: true`; `{environment}` in `secret_ref` is substituted server-side with the selected environment.

## Step 1 — Collect Non-Secret Config

Collect per-environment base URLs, allowed hosts, and auth type one field per turn with `user_confirm`:

```
user_confirm(
  kind="text",
  question="What is the DEV base URL for the <name> API?",
  field_key="<ik>:dev:base_url"
)
```

Missing non-secret config may also be saved as a project-scoped personal memory (`memory_rw(operation="save", memory_type="project_setting", ...)`) so future sessions do not re-ask.

## Step 2 — Capture Secrets (Never Plaintext in Chat)

Secrets are captured ONLY through secure prompts — plaintext never reaches the LLM or the conversation history. Never echo values into knowledge/memory/chat; if the user pastes a token in plain text, ask them to use the secure dialog and do NOT repeat it back.

- **Project scope** (default for custom integrations):

```
user_confirm(
  question="API token required for <name> (DEV environment)",
  secret=true,
  service_key="third_party:<integration_key>:dev",
  environment="dev",
  save_to_project_secrets=true
)
```

- **User vault scope** (personal credentials): `secret_vault(operation="save", service_key="<key>", display_name="<name>")`.

## Step 3 — Write the Catalog Entry

1. Preserve existing entries; append the new YAML block.
2. Add a row to the integration summary table at the top of the file.
3. Write with `knowledge_rw` (full-file rewrite, triggers reindex automatically):

```
knowledge_rw(
  operation="write",
  slug="integrations/custom-api",
  content=<full file content>,
  change_summary="Add integration <integration_key>"
)
```

## Step 4 — Verify Connectivity

Call the cheapest endpoint with `response_mode="status"`:

```
api_request(
  integration_key="<integration_key>",
  endpoint_key="<cheapest-endpoint>",
  method="GET",
  response_mode="status",
  environment="dev"
)
```

- 2xx → add `verified: true` + date to the YAML block and rewrite.
- Non-2xx → keep `verified: false`, record the observed error next to the entry, troubleshoot (see the consumer skill's failure table).
- Missing secrets are auto-prompted via a secure dialog — do not ask for plaintext tokens.

## Step 5 — Report

Summarize: `integration_key`, environments, endpoints, **verified vs inferred**, secret storage location (scope), next steps. Never include secret values.

## Optional: OpenAPI / Swagger Import

1. **Get the spec**: public via `web_fetch(url=..., max_chars=50000)`; private/authenticated → ask the user to upload the file, read it with `filesystem`; never stash credentials in `web_fetch`.
2. **Convert `paths` → endpoints**: each path+operation becomes an endpoint (method, path, description, `required_params` from parameters; the first response-schema key is a `response_json_path` hint only — verify before asserting).
3. **Import as `verified: false`** — imported specs are unverified by definition.
4. **Write detail knowledge per the mandatory two-level API structure in `agents/skills/knowledge/knowledge-manager.md` ("API Documentation: Mandatory Two-Level Structure")**: create `technical/api/{service-slug}.md` with `knowledge_level: detail` and `parent: "technical/api-map"`, and keep the levels in sync: `api-map.md` frontmatter `children` lists the detail slug, body has `[[technical/api/{service-slug}]]`.

## Failure Handling

- Catalog write rejected (invalid slug/format) → fix and retry.
- 404/405 → wrong environment base_url in the entry; fix and re-verify.
- 401/403 → wrong secret_ref scope or environment tag; re-capture the secret for the right env.
- Host blocked by URL-safety check → `allowed_hosts` missing for that environment; fix the entry.
- Same root cause failing 2+ times → stop and ask the user; never fabricate endpoints or response fields.
