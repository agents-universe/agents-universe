---
slug: "whole-system-test-planning"
description: "Design a whole-system test plan (tests/test-plan.md) from project knowledge — system inventory, risk/priority, coverage matrix, and login/account strategy with user vault credential collection"
triggers:
  - "whole-system test plan"
  - "test plan for the entire system"
  - "whole-system test design"
  - "design a test plan for the whole system"
  - "为整个系统设计测试计划"
  - "整个系统的测试方案"
tools:
  - secret_vault
  - user_confirm
  - knowledge_rw
  - shell
  - filesystem
  - test_generator
---

# Whole-System Test Planning

The QA agent's default workflow for whole-system test plan requests. Produces a design-only deliverable at `tests/test-plan.md` (project workspace root — the same `tests/` directory used for generated specs). It does NOT auto-execute the plan and does NOT enter the single-issue Jira closed loop.

## 1. Relationship to the Other Workflows

- This workflow covers **plan design only**; `workflows/automation-workflow-playbook.workflow.md` remains the default for per-card closed-loop automation, release regression, execution, and Jira writeback.
- Executing an individual `SYS-xxx` case from the plan later re-enters the playbook's single-issue flow; login then requires `shell(env_refs=...)` (see Step 5).
- Jira assets are NOT created by this workflow unless the user explicitly opts in.
- Language governance: files under `workflows/` are repository-wide framework documents and must remain in English regardless of `AGENT_DEFAULT_LANGUAGE`. The plan document body (`tests/test-plan.md`) follows `AGENT_DEFAULT_LANGUAGE` unless the user explicitly requests another language.

## 2. Step 1 — Confirm Scope and Environment

1. Infer the active project from the conversation. Only ask the user if it cannot be inferred.
2. Read `environment/environment` knowledge: environment targets (`APP_URL_DEV/INT/UAT/PRD`), `AGENT_DEFAULT_LANGUAGE`.
3. Ask the user only if a narrower scope (specific modules, environments, or risk levels) is desired; otherwise plan the whole system.

## 3. Step 2 — System Inventory from Knowledge

1. `knowledge_rw(operation="list")` to see available knowledge files.
2. Read the inventory sources in this order: `system-architecture` → `page-map` → `api-map` → `kong-map` → `permission-matrix`/`role-matrix` → `test-patterns` → `login-and-user-switch` → `environment`.
3. Produce a `module × entry points (UI page / API / job)` inventory, with the knowledge source for each entry.
4. Fall back to Git/code only when knowledge is absent or stale; write findings back to knowledge afterwards (Step 7).

## 4. Step 3 — Risk and Priority Assessment

- For each module, estimate business impact × failure likelihood → priority P0/P1/P2.
- Business-critical main flows are P0 by default.
- State the priority rationale in the plan.

## 5. Step 4 — Coverage Matrix

- Apply the four coverage dimensions per module and record the dimension on every case: `main-flow` (primary user journeys), `boundary` (edge cases, limits), `exception` (failure/error paths), `permission` (access control — minimum test set: highest-permission role, restricted role, unauthenticated). Derive role/company variants from the permission and role matrices; keep pattern details in `test-patterns.md`.
- Every inventory item must map to at least one coverage cell OR be listed as out-of-scope with a specific reason.
- State the matrix in the plan document.

## 6. Step 5 — Login and Account Strategy (Credential Collection)

1. Check the user key vault first: `secret_vault(operation="list")` for `qa:login:username` and `qa:login:password`. Project-shared keys have no agent-side list tool; their absence surfaces at runtime via `shell(env_refs=...)` returning `missing_service_keys`, or ask the user directly.
2. **Default to personal scope** (`save_to_user_tokens=true`). Only prompt for scope via `user_confirm(kind="selection")` if the user explicitly requests project-shared credentials or if the task involves setting up credentials for a team.
3. Collect one credential at a time according to the chosen scope:
   - Personal: `user_confirm(secret=true, service_key="qa:login:username", save_to_user_tokens=true)`, then the same for `qa:login:password` (`secret_vault save` is the equivalent alternative).
   - Project-shared: same but `save_to_project_secrets=true`, optionally with an `environment` qualifier (e.g. `uat`).
4. Secret prompts return only an opaque status — plaintext never enters the conversation. NEVER request or echo credentials in normal chat text.
5. Record only non-secret account metadata (account name, role, company/tenant, use) and the chosen credential scope into the project's `login-and-user-switch.md` Verified Accounts table. Passwords are never written to knowledge.
6. If the user declines to provide credentials, mark credential-gated cases as "blocked by missing credentials" in Open Risks and continue with unauthenticated coverage.

## 7. Step 6 — Produce `tests/test-plan.md`

Write the plan with the `filesystem` tool to `tests/test-plan.md` using the mandatory structure below (10 sections, in order). Case IDs follow `SYS-{3-digit}` sequential numbering.

1. **Scope & Objectives** — what the plan covers; explicit design-only note; environment scope.
2. **System Inventory** — module × entry points (UI page / API / job) with knowledge source references.
3. **Coverage Matrix** — module × dimension table; out-of-scope list with reasons.
4. **Prioritized Case Inventory** — table: ID, module, title, type (UI/API/DB-fallback), dimension, priority, brief steps.
5. **Per-Case Execution Contract** — full detail per case grouped by module: preconditions, steps, expected results, data setup, env target, evidence requirements.
6. **Data Setup** — accounts/orgs/companies/rows needed and how to create them (test-support APIs first, DB last).
7. **Environment Targets** — env names from `environment.md` mapped to case groups.
8. **Evidence Requirements** — per-case evidence type (UI screenshot+recording, API request/response JSON, output files) per `workflows/test-artifact-and-jira-conventions.workflow.md`; design-only means evidence is defined for later execution.
9. **Accounts Needed** — non-secret account metadata table + vault service keys (`qa:login:username`, `qa:login:password`) and their storage scope. NEVER values.
10. **Open Risks** — untested areas, credential gaps ("blocked by missing credentials"), flaky dependencies.

## 8. Step 7 — Knowledge Writeback and Summary

1. New coverage patterns → `test-patterns.md`; account info already recorded in Step 5 → `login-and-user-switch.md`; append a `history.md` entry.
2. Report a concise summary: coverage statistics, P0 case list, open risks, accounts still needed.

## 9. Success Criteria

- `tests/test-plan.md` exists and contains all 10 sections in order.
- Every inventory item is covered or explicitly out-of-scope with a reason.
- Case IDs are sequential `SYS-001`, `SYS-002`, …
- The Accounts Needed section references vault service keys and scope only — never plaintext values.
- Open Risks is non-empty (design-only plans always carry residual risk).

## 10. Error Handling

- Project un-inferrable → ask the user.
- Knowledge insufficient → fill gaps from Git/code, or ask the user.
- Vault keys missing → Step 5 collection flow.
- User declines credentials → mark affected cases blocked in Open Risks and continue.
- `db_session` unavailable (headless contexts) → `env_refs` reports refs missing and `secret_vault`/`user_confirm` cannot prompt; report the limitation and do not loop.

## 11. Slices

- **Plan Design** (this workflow): inventory, risk/priority, coverage matrix, login/account strategy, plan document, knowledge writeback.
- **Plan Execution** (on demand, later): per-case closed loop per `automation-workflow-playbook`; login requires `shell(command=..., env_refs={"APP_USERNAME": {"scope": "user|project", "ref": "qa:login:username"}, "APP_PASSWORD": {"scope": "user|project", "ref": "qa:login:password"}})`, with `scope` matching the scope chosen at collection time.
