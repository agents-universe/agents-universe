---
category: environment
slug: environment/environment
tags: [environment, integration, deployment]
title: Project Environment
---

# Project Environment

Runtime config; tools/skills resolve values from the `env` block below at execution time. No separate `config.json`.

```env
# ── Agent ──
AGENT_DEFAULT_LANGUAGE=en

# ── Integration Keys ──
JIRA_PROJECT_KEY=
CONFLUENCE_SPACE_KEY=
CONFLUENCE_ROOT_PAGE_ID=
GIT_REPOSITORY=

# ── System Names ──
JIRA_NAME=
CONFLUENCE_NAME=
GIT_NAME=

# ── Deployment URLs ──
APP_URL_DEV=
APP_URL_INT=
APP_URL_UAT=
APP_URL_PRD=

# ── Kong Gateway ──
# Base URL; kong tool appends relative API paths.
# Token selection: env=dev → kong:dev, env=uat → kong:uat, env=int → kong:int
KONG_BASE_URL_DEV=
KONG_BASE_URL_UAT=
KONG_BASE_URL_INT=

# ── Git Integration ──
# GIT_REPOSITORY is the default repo path (e.g. org/repo-name).
# Git token and base URL are per-user config: Settings -> Integrations.
GIT_REPOSITORY=
GIT_COMMIT_SEARCH_MODE=branch

# ── Atlassian / Jira ──
JIRA_TEST_ISSUE_TYPE=Test
JIRA_TEST_LINK_TYPE=is tested by

# ── Third-Party Integrations ──
# Non-secret integration config (base URLs, system names) → project-scoped
# personal memory via memory_rw(memory_type="project_setting").
# See integrations/custom-api.md for the catalog.
```

## Project Secrets

API tokens/secrets are NOT stored here. Managed as encrypted project secrets via the Memory tab → 项目密钥 panel, or provided interactively when a tool first needs them.

Secret token resolution order:
1. `project_secrets` table (project-scoped, shared across project members)
2. `user_tokens` table (legacy per-user fallback)
3. Interactive secure prompt (if tool supports it)

QA automation login credentials (APP_USERNAME / APP_PASSWORD) are resolved at
runtime from the key vault via shell `env_refs` — service keys
`qa:login:username` / `qa:login:password`, `scope="user"` reads user_tokens,
`scope="project"` reads project_secrets with user_tokens fallback. Never stored
in knowledge.

## Related Knowledge

- [[domain/context]]
- [[technical/technical-stack]]
- [[technical/kong-map]]
