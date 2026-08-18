---
category: technical
slug: technical/login-and-user-switch
tags: [login, auth, user-switch]
template_words: 150
title: Login and User Switching
---

# Login and User Switching

## Entry

- Login URL:
- Env vars: `APP_BASE_URL`, `APP_LOGIN_URL`, `APP_USERNAME`, `APP_PASSWORD`

## Vault Service Keys (QA automation credentials)

Login username/password are NEVER stored in this file or any knowledge file — they live in the key vault, injected at runtime:

- service_key `qa:login:username` — username
- service_key `qa:login:password` — password

Two storage scopes; the user chooses at collection time:

- **Personal** — user key vault (`user_tokens`), per-user, cross-project.
- **Project-shared** — project secrets (`project_secrets`), shared with project members.

QA agent checks `secret_vault(list)` first; if missing, it asks the user to choose the scope, collects via `user_confirm(secret=true, save_to_user_tokens=true)` or `save_to_project_secrets=true`, injects at runtime via `shell(env_refs=...)` with a matching `scope`. Plaintext never enters the LLM context or touches disk.

## API Login Steps

1. Create context
2. Call login API
3. Extract token/session
4. Switch company/tenant if needed

## UI Login Elements

- Login page key elements:
- User-switch control:
- Company switch entry:
- Success anchor:

## Verified Accounts

Non-secret identity metadata only (account, role, company/tenant, use); passwords are never recorded — see Vault Service Keys above.

| Account | Role | Company/Tenant | Use | Notes |
|---------|------|----------------|-----|-------|
| | | | | |
