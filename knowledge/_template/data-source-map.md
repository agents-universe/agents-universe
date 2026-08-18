---
category: technical
slug: technical/data-source-map
tags: [data, data-source, inventory]
template_words: 60
title: Data Source Map
---

# Data Source Map

Inventory of all data sources this project reads from or writes to.

**Security rules:**
- Never store plaintext credentials here or in any knowledge/memory.
- Connection credentials (accounts, passwords, tokens) go to `project_secrets`; reference them only via `secret_ref` values.
- Access is performed through tools that resolve `secret_ref` server-side (e.g. `api_request`); never paste secrets into SQL, scripts, or chat.

## Source Inventory

| Source | Type (DB / Warehouse / File / API) | Environment | Access (secret_ref) | Owner | Refresh Frequency |
|--------|------------------------------------|-------------|---------------------|-------|-------------------|
| | | | | | |

## Access Notes

- Network / whitelist / VPN requirements:
- Read-only vs read-write boundaries:
- Sensitive data (PII) handling rules:

## Related Knowledge

- [[technical/data-model]]
- [[technical/data-pipelines]]
- [[integrations/custom-api]]
