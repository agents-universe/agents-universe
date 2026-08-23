---
slug: "security/static-code-audit"
description: "White-box source audit - build audit context, run semgrep/bandit/pip-audit/detect-secrets, review injection sinks and authorization gaps, and emit the dynamic verification target list"
type: "guidance"
triggers:
  - "static code audit"
  - "SAST"
  - "source review"
  - "静态代码审计"
  - "代码审计"
tools:
  - git_repo
  - repo_graph
  - filesystem
  - shell
  - knowledge_rw
---

# Skill: Static Code Audit

Phase 2 of the full-project pentest - the primary engine of the white-box methodology. Source availability is the advantage: find WHERE to look, then hand each candidate to dynamic verification. Static candidates are NOT findings until dynamically confirmed (see `security/finding-validation.md`).

## Audit Context Building

Understand the codebase before hunting bugs in it:

1. Tech stack and framework from knowledge (`system-architecture` / `technical-stack`); entry-point conventions follow from it (controllers, route files, middleware registration).
2. Entry-point map: `repo_graph` (callers/symbols) from each HTTP entry point inward; `git_repo(operation="log"/"search")` for recent high-churn areas (new code carries new risk).
3. Auth middleware coverage: which route groups sit behind which guard, and which routes bypass it entirely (public routes, webhooks, internal jobs).
4. Data flow hotspots: where request input is read (params, body, headers) and where it reaches sinks (queries, commands, file paths, templates).

## Tool Scans

Run per `security/pentest-toolchain.md` conventions against the product source checkout:

- `semgrep` / `bandit` - SAST candidates. Triage output by confidence; cluster duplicates of the same sink into one candidate.
- `pip-audit` - dependency CVEs on the product's real runtime requirements.
- `detect-secrets` - hardcoded secrets; triage test fixtures and dummies out.

Record scans that could not run (tool unavailable) as `tool-limited` - they narrow the manual review scope instead of disappearing.

## Manual Review Focus

Tool rules miss what pattern review catches. Check per category:

1. **Injection sinks** - string-concatenated/f-string SQL (vs. parameterized), shell command construction, path joins with request input, template rendering with raw strings, deserialization of request-controlled data (`pickle`, `yaml.load`, arbitrary `json` class hydration).
2. **Authorization gaps** - object-level access control (does changing an ID in the URL reach another tenant's record?), role checks on sensitive operations, service-to-service endpoints without auth, webhook signature verification.
3. **Crypto misuse** - weak hashes for passwords (unsalted MD5/SHA1), static keys/IVs, ECB mode, tokens without expiry/signature.
4. **Insecure defaults** - fail-open exception paths around auth checks, debug endpoints left enabled, verbose errors leaking internals, CORS `*` with credentials.
5. **Secrets** - committed keys/tokens/passwords in config, `.env` files, and git history (`git_repo(operation="log"/"show")` on config paths).
6. **Framework-specific sharp edges** - ORM raw-query escapes, template auto-escape disabled, mass-assignment on user-supplied field sets.

## Dynamic Verification Target List (Output)

The bridge to Phase 3. Emit as a table in `.tmp/pentest/targets.md` (and carry it into the workflow state):

| ID | Candidate | Sink location | Entry point | HTTP parameter | Suggested channel | Priority |
|----|-----------|---------------|-------------|----------------|-------------------|----------|

Rules:

- Every candidate MUST map to a reachable entry point; a sink behind an unreachable path is noted in the report as defense-in-depth context, not queued.
- Priority: authenticated-reachable + high-impact sinks first (P0), public-reachable medium-impact next, the rest P2.
- Dependency CVEs and hardcoded secrets do not need dynamic confirmation in the same way - they go to finding validation directly with reachability evidence instead of exploitation.
- Cap the list at what Phase 3 can actually verify within timeout budgets; the remainder is reported as `unverified-static-candidates` in Open Risks.
