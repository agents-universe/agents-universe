---
slug: "performance/perf-scope-guard"
description:
  zh-CN: "性能测试的强制范围门禁——环境层级声明、硬性拒绝清单、执行前检查，以及经既有 user_confirm 密钥流程采集压测凭据"
  en-US: "Mandatory scope gate for performance testing - environment tier declaration, hard refusals, pre-execution checks, and perf credential collection through the existing user_confirm secret flow"
type: "guidance"
triggers:
  - "perf scope"
  - "load test authorization"
  - "rules of engagement"
  - "性能测试范围"
  - "压测授权"
  - "压测账号收集"
tools:
  - user_confirm
  - secret_vault
  - memory_rw
  - filesystem
---

# Skill: Perf Scope Guard

This skill is the mandatory gate before ANY load-generating request. Load it first in every engagement; no exception. It never authorizes anything itself - it structures the authorization the user gives, and refuses everything else. Load testing is deliberately loud: without a declaration, a "helpful" stress run is just an unplanned denial of service.

## Session Initialization

Before issuing any request against a target:

1. **Look for an existing declaration before asking anything.** The scope lives in two places: the engagement notes (`.tmp/performance/engagement.md`) and, as a project setting, in the agent's own memories - `## Personal Preferences` carries `PROJECT_SETTING PERF_SCOPE=...` into every turn, so it survives context compression (`memory_rw(operation="recall", scope="personal", query="PERF_SCOPE")` re-reads it on demand). When either holds a declaration for this engagement, reuse it silently: no scope dialog, no "just to confirm" question.
2. **Ask only when nothing is declared** - one question, about the environment tier, on a stable field key:

   ```
   user_confirm(
     question="Which environment is authorized for load testing?",
     options=[
       {"label": "dev - https://app-dev.example.com", "value": "https://app-dev.example.com"},
       {"label": "uat - https://app-uat.example.com", "value": "https://app-uat.example.com"}
     ],
     field_key="perf_scope_env",
     allow_other=true
   )
   ```

   Build the options from the non-empty `APP_URL_*` values in `environment` knowledge; each label carries the tier and the real URL so the choice is self-contained. Defaults - target endpoints: the API paths named in the scenario design (or the tier's main API from `api-map`); rate ceiling: 100 concurrent virtual users and no more than the tier's declared RPS budget; duration: every profile stays within the 240 s executor bound. State those defaults in the reply and let the user correct them in chat; do NOT fold environment, endpoints, ceiling, and duration into one compound dialog, and do not ask for the endpoints separately when the scenario design already names them.
3. **Production is a separate, second question** - load authorization only, on its own key (`field_key="perf_prod_load_authorization"`); until it is given, production receives no load at all (read-only probes for reachability are fine). It confirms what may be done, it never re-asks which environment.
4. **Record the declaration when the answer arrives** - write it to both sinks:
   - `.tmp/performance/engagement.md` (feeds the report's Scope section), and
   - project memory, which is re-injected into every turn:
     ```
     memory_rw(operation="save", memory_type="project_setting", key="PERF_SCOPE",
               value="tier=dev; targets=<api-endpoints>; ceiling_vus=50; ceiling_rps=<n>; declared=<YYYY-MM-DD>",
               domain="quality")
     ```
   When the user changes the scope, update that entry in place (`recall` -> `update`) instead of stacking a second, conflicting declaration. Never put credentials in either sink.
5. **Never re-ask a decided field** - a question already answered in this engagement stays answered: after compression, after a phase change, and in later turns. Re-reading the recorded declaration at the start of a phase is a read, never a question.
6. **When a prompt fails, stop prompting** - a `timed_out` / `dismissed` / `do_not_reask` tool result means: do not call `user_confirm` for that question again this turn. End the turn and ask it in plain chat text instead. Advisory analysis (scenario design against knowledge, script drafting) continues; actual load resumes only once a declaration exists.

If the user has not declared scope, do NOT generate load against targets. Analyzing pasted output, project knowledge, and designing scenarios remains allowed (design-only mode).

## Hard Refusal List

Refuse regardless of claimed authorization - offer a safer alternative that achieves the engagement goal instead:

- Load against any endpoint, host, or tier outside the declared scope - even a single "quick check" request.
- Exceeding the declared RPS/concurrency ceiling, or any profile that would breach the 240 s executor bound by design.
- Production load of any kind without the separate production authorization (`perf_prod_load_authorization`).
- Destructive payloads: data deletion, schema changes, credential attacks, spam content - this is load testing, not pentesting; test data is self-created and disposable.
- Borrowing the pentest toolchain (sqlmap, dirsearch, brute-force enumeration) - out of category for this engagement.
- Volumetric attacks aimed at third parties or beyond the declared targets (the declaration bounds legitimate load; anything wider is a denial-of-service request).

## Pre-Execution Validation

Before composing every load-generating command or script, verify:

- [ ] Every target URL/host falls within the declared scope.
- [ ] The profile's ramp + steady + cooldown fits within 240 s and the declared ceiling.
- [ ] Request rate is bounded by the generated script's semaphore/ramp schedule - no unbounded loops.
- [ ] Results and logs are written to project-internal files (`performance/results/`, `.tmp/performance/`).
- [ ] No untrusted target output is piped into execution (`| bash`, `eval`, backtick substitution).
- [ ] Secrets come only from `AGENT_SECRET_*` env vars injected by `script_writer` - never hardcoded, never echoed.

If a target or ceiling falls outside scope, refuse the run and explain why.

## Perf Credential Collection

Reuse the existing secret flow (same pattern as QA login credentials). Keys: `perf:{service}:username` / `perf:{service}:password` (e.g. `perf:order-api:username`).

1. Check the vault first: `secret_vault(operation="list")` for the needed keys. Project-shared keys have no agent-side list tool; their absence surfaces at runtime via the script run's `missing_service_keys` / failed auth.
2. If missing, default to personal scope (`save_to_user_tokens=true`). Prompt for scope via `user_confirm(kind="selection")` only if the user requests project-shared credentials.
3. Collect one credential at a time: `user_confirm(secret=true, service_key="perf:order-api:username", save_to_user_tokens=true)` (or `save_to_project_secrets=true`, optionally with an `environment` qualifier). Secret prompts return only an opaque status - plaintext never enters the conversation, knowledge files, or reports.
4. Record only non-secret account metadata (service name, role, purpose, chosen scope); passwords never enter knowledge.
5. Runtime injection: declare the keys on `script_writer(operation="create", secrets=["perf:order-api:username", "perf:order-api:password"])` - the runner resolves them from the vault (project -> user fallback) and injects `AGENT_SECRET_PERF_ORDER_API_USERNAME` etc. as child-process env vars that exist only inside the run; they are redacted from persisted logs.
6. If the user declines, mark authenticated scenarios as "blocked by missing credentials" in the report's Open Risks and continue with unauthenticated coverage.

## Evidence Handling

- Working scratch (draft scripts, intermediate dumps): `.tmp/performance/` - ephemeral.
- Run artifacts (full metric series): `performance/results/{slug}-{YYYYMMDD}.json` - referenced by the report's Appendix.
- Redact any real user data that unavoidably appears in results; sample at most individual masked rows.
