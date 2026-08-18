---
category: integrations
slug: integrations/custom-api
tags: [integration, api, third-party]
title: Custom Third-Party API Integrations
---

# Custom Third-Party API Integrations

Defines how agents call customer-owned/third-party APIs via the `api_request` tool.

**Security rules:**
- Never store plaintext secrets here or in any knowledge/memory — use only `secret_ref` values.
- Secrets are stored as project secrets, resolved server-side by the tool.
- Never pass auth values in `headers` or `json_body`.

## Integration Catalog

| integration_key | Name | Environments | Auth Type | Secret Ref Pattern | Owner |
|---|---|---|---|---|---|
| (to be filled during project onboarding) | | | | | |

## Integration Template

Copy this block per third-party system:

```yaml
integration_key: <unique-key>
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
  type: bearer | api_key_header | basic | cookie | custom_header | none
  secret_ref: third_party:<integration_key>:{environment}
  header_name: X-API-Key  # only for api_key_header / custom_header

defaults:
  timeout_seconds: 30
  max_response_chars: 20000
  require_confirmation_for_write: true

endpoints:
  <endpoint_key>:
    method: GET
    path: /api/v1/<resource>/{resource_id}
    description: <What this endpoint does>
    side_effect: false
    required_params:
      - resource_id
    response_json_path: data
```

`response_json_path` uses plain dot notation (`data`, `data.items`) — the `$.` JSONPath prefix is not supported. Set `side_effect: true` on state-changing endpoints; it forces user confirmation even for GET.

## Usage Rules

1. **Authenticated APIs: use `api_request`**, never `web_fetch`.
2. **Prefer `endpoint_key`** from the catalog; `api_request` resolves it **server-side from this file** — method, path, per-environment base_url, allowed_hosts, response_json_path, auth defaults applied automatically. Use a raw `path` only when the catalog lacks the endpoint.
3. **Never pass auth in headers/body** — the tool injects it server-side from `secret_ref`.
4. **Missing non-secret config** (base URL, allowed hosts) — ask via `user_confirm(kind="text")`, save to project-scoped personal memory via `memory_rw`.
5. **Missing secrets** — let `api_request` handle it: it triggers a secure prompt and saves to project secrets automatically.
6. **Write operations** — confirm with the user before POST/PUT/PATCH/DELETE, especially in production.
7. **Response handling** — use `response_json_path` to extract only needed data, minimizing context usage.
