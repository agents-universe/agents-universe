---
slug: "interaction/user-confirm"
description: "Framework guidance for presenting the user with a selection dialog, text prompt, or secure secret input"
type: "guidance"
triggers: []
tools:
  - user_confirm
  - memory_rw
---

# Skill: User Confirm

## When to Use

Use when the user must make a choice, provide a value, or confirm before proceeding:

- Multiple candidates from an external system and you need the user to pick one
- A value is ambiguous and the correct option is not clear from context
- You are about to perform a write action with lasting effects and want explicit approval
- The user's intent maps to more than one interpretation
- A required non-secret project config is missing (e.g. issue tracker project key, source control host, API base URL)
- A secret/token is needed for a third-party integration

## Prompt Modes

### Selection (default)

```
user_confirm(
  question="Which issue tracker project does this work relate to?",
  options=[
    {"label": "PLATFORM – Platform Services", "value": "PLATFORM"},
    {"label": "MOBILE – Mobile App", "value": "MOBILE"}
  ],
  field_key="issue_tracker_project_key"
)
```

### Free-text input

```
user_confirm(
  kind="text",
  question="What is the API gateway base URL for the DEV environment?",
  field_key="API_GATEWAY_BASE_URL_DEV"
)
```

### Secure secret input

```
user_confirm(
  question="API token required for CRM (UAT environment)",
  secret=true,
  service_key="third_party:crm:uat",
  environment="uat",
  save_to_project_secrets=true
)
```

Personal credentials (e.g. QA login username/password) can be saved to the user's
personal key vault instead — cross-project, per-user:

```
user_confirm(
  question="Login username required for the system under test",
  secret=true,
  service_key="qa:login:username",
  save_to_user_tokens=true
)
```

Secret mode returns only an opaque status — plaintext is never visible to the agent.

### Choosing the storage scope

When collecting a credential, ask the user to choose the storage scope first
(`kind="selection"`) when the choice is not obvious:

| Choice | Table | Scope | Used for |
|---|---|---|---|
| 个人密钥 / user key vault | `user_tokens` | per-user, cross-project | Personal credentials used across projects |
| 项目共享密钥 / project secrets | `project_secrets` | per-project, shared with members | Team-shared credentials for one project |

`save_to_user_tokens` and `save_to_project_secrets` are mutually exclusive; exactly
one must be set when `secret=true`. Project-shared secrets are resolvable by other
project members — state that when offering the choice.

## Collecting Missing Configuration

When a required non-secret value is missing:

1. Call `user_confirm(kind="text", ...)` to collect the value.
2. Use the returned value immediately in the current task.
3. Save it as a project-scoped personal memory so future sessions don't ask again:

```
memory_rw(
  operation="save",
  memory_type="project_setting",
  key="JIRA_PROJECT_KEY",
  value="PLATFORM",
  domain="issue-tracker"
)
```

**Do NOT write customer/project-specific config into knowledge files or framework templates.** Only shared, durable project facts (API maps, permission matrices, business rules) belong in project knowledge.

## Secret Handling

For secrets (tokens, passwords, API keys, cookies, private keys):

- Use `user_confirm(secret=true, service_key="...", save_to_project_secrets=true)` or let the integration tool (api_request, kong, jira, etc.) handle secure collection automatically.
- For personal credentials, use `save_to_user_tokens=true` instead — see "Choosing the storage scope" above.
- Secrets are saved to the chosen scope and resolved server-side by tools via `secret_ref` (project) or `secret_ref` with `secret_scope="user"` (user vault). Runtime test injection uses `shell(env_refs=...)` with a matching `scope`.
- Never store secrets in personal memory, project knowledge, or messages.
- Never ask users to paste tokens into regular text prompts.

## Information Storage Rules

| Info type | Where to store |
|---|---|
| Non-secret project/customer config (base URLs, project keys, system names) | project-scoped personal memory via `memory_rw(memory_type="project_setting")` |
| Shared stable project facts (API map, business rules, architecture) | project knowledge via `knowledge_rw` |
| Secrets (tokens, passwords, API keys) | project secrets via secure prompt |
| Personal credentials | user token vault (user vault) — or project secrets if the user chooses project-shared |

## Notes

- You can call `user_confirm` multiple times in sequence for multi-field flows
- If the options list is empty with kind="selection", it renders as free-text input with "Other"
- The `allow_other` option lets the user type a custom value not in your list
- For batch onboarding, ask questions one at a time to keep the flow conversational
