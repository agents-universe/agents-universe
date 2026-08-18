---
slug: "testing/system-test-planner"
description: "Plan whole-system test coverage from project knowledge and produce tests/test-plan.md with a prioritized, executable case inventory"
---

# Skill: System Test Planner

Ensure `workflows/whole-system-test-planning.workflow.md` is loaded. If it is not yet loaded for the current task, read it first and follow its phase order, credential-collection rules, and plan document structure.

## When to Use

- The `whole-system-test-planning` workflow enters the `plan-system-coverage` stage.
- The user asks for a test plan covering the entire system ("design a test plan for the whole system", 「为整个系统设计测试计划」).
- A coverage baseline is needed before per-card automation work begins.

Do NOT use this skill for single Jira card test design (that is `testing/test-designer`) or for release regression design (`testing/release-regression-manager`).

## Design Principles

1. **Coverage dimensions**: `main-flow` → `boundary` → `exception` → `permission`; apply all four per module and record which dimension each case covers.
2. **No silent gaps**: every inventory item maps to at least one coverage cell or is explicitly out-of-scope with a reason.
3. **Automation-first, UI-first, DB fallback last** (inherited from `test-designer`).
4. **Prioritize by business impact**: P0 (business-critical main flows) first, then P1, then P2.
5. **Minimum sufficient set**: one clear point per case; avoid duplicate coverage of the same behavior.
6. Full execution detail goes in the plan document, not in chat prose — chat gets a concise summary.

## System Inventory Method

Inventory sources, read order, and the Git/code fallback rule follow the workflow (Step 2): `system-architecture` → `page-map` → `api-map` → `kong-map` → `permission-matrix`/`role-matrix` → `test-patterns` → `login-and-user-switch` → `environment` — skip a source only when it does not exist for the project. Produce the inventory as `module × entry points (UI page / API / job)` with the knowledge source for each entry; when knowledge is absent or stale, fall back to Git history / code reading and write findings back to knowledge afterwards (see Feedback Into Knowledge).

## Coverage Dimension Application

Apply the four dimensions per module — `main-flow`, `boundary`, `exception`, `permission` — and record the dimension on every case. Dimension definitions and role/company variant derivation are in the workflow (Step 4).

## Case ID Scheme

- Sequential `SYS-{3-digit}` per plan run: `SYS-001`, `SYS-002`, …
- Group by module in inventory order; P0 cases come first within the module.
- The module is a table column, not part of the ID — IDs are stable across plan revisions.

## Per-Case Execution Contract

Every case in the plan carries these fields (mirroring the `test-designer` JSON contract at system scale):

`id` (`SYS-001`), `title`, `module`, `type` (UI / API / DB-fallback), `coverageDimension` (main-flow / boundary / exception / permission), `priority` (P0/P1/P2), `preconditions`, `steps`, `expectedResults`, `dataSetup`, `envTarget`, `evidenceRequirements`, `source` (knowledge file or Jira reference).

## test-plan.md Document Structure

Write `tests/test-plan.md` with exactly these 10 sections in order (full section semantics are in the workflow §7):

1. Scope & Objectives
2. System Inventory
3. Coverage Matrix
4. Prioritized Case Inventory
5. Per-Case Execution Contract
6. Data Setup
7. Environment Targets
8. Evidence Requirements
9. Accounts Needed
10. Open Risks

## Account-Collection Rules

Credential collection follows the workflow (Step 5). When the skill runs standalone:

1. Check the user key vault first: `secret_vault(operation="list")` for `qa:login:username` / `qa:login:password`.
2. If missing, **ask the user to choose the storage scope** via `user_confirm(kind="selection")`: 个人密钥 (user key vault, cross-project) or 项目共享密钥 (project secrets, current project). Note that project-shared secrets are resolvable by other project members.
3. Collect one credential at a time with `user_confirm(secret=true, service_key="qa:login:username", save_to_user_tokens=true)` (personal) or `save_to_project_secrets=true` (project-shared, optionally with `environment`). `secret_vault save` covers the personal scope only.

Secret prompts return only an opaque status — never request or echo plaintext credentials in normal chat. Record only non-secret account metadata (account, role, company/tenant, use) and the chosen scope in the `login-and-user-switch.md` Verified Accounts table; passwords never enter knowledge. If the user declines, mark credential-gated cases "blocked by missing credentials" in Open Risks and continue with unauthenticated coverage.

## Feedback Into Knowledge

- New coverage patterns / reusable strategies → `test-patterns.md`.
- Verified accounts and their scope → `login-and-user-switch.md`.
- Everything new → `history.md` entry.

## Execution Notes (for later runs)

When a `SYS-xxx` case is executed on demand, login credentials are injected at runtime via:

```json
shell(
  command="npm run test:sys-001",
  cwd="tests",
  env_refs={
    "APP_USERNAME": {"scope": "user", "ref": "qa:login:username"},
    "APP_PASSWORD": {"scope": "user", "ref": "qa:login:password"}
  }
)
```

`scope` must match the storage scope chosen at collection time. Resolved values are redacted from all returned output. Non-secret values (e.g. `APP_BASE_URL`) go inline in the command prefix (`APP_BASE_URL=http://... npm run test:sys-001`) — they cannot come from server env (safe_env strips URL-suffixed keys).
