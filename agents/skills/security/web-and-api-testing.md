---
slug: "security/web-and-api-testing"
description: "OWASP-driven dynamic testing through real UI and API - channel selection by target reachability, browser_playwright UI flows, api_request security matrix, code_executor httpx PoCs, non-destructive proofs"
type: "guidance"
triggers:
  - "dynamic testing"
  - "web application security testing"
  - "API security testing"
  - "动态测试"
  - "Web安全测试"
  - "接口安全测试"
tools:
  - browser_playwright
  - api_request
  - code_executor
  - shell
  - user_confirm
---

# Skill: Web and API Security Testing

Phase 3 of the full-project pentest. Inputs: the attack surface inventory (Phase 1) and the dynamic verification target list (Phase 2). Every active command follows `security/pentest-scope-guard.md` - OPSEC tag, rate limits, evidence files.

## Channel Selection

Per the reachability matrix in `security/recon-and-attack-surface.md`:

- **`api_request`** - API-level testing of hostname targets. Secret injection via `secret_ref`/`secret_refs` + `secret_scope` keeps credentials out of the conversation. Confirmation prompts stay enabled for this agent - accept them as the per-request authorization gate.
- **`browser_playwright`** - UI flows, rendered-page checks, screenshots. No secret injection exists on this tool: never `fill` a password field with a vault credential (the value would enter the conversation). Use the script mode below for authenticated UI tests.
- **`code_executor` (python, httpx)** - universal channel: literal-IP targets, non-standard ports, custom PoC scripts (differential responses, bounded timing checks, header/cookie manipulation). 30 s ceiling - keep scripts single-purpose.
- **Authenticated UI script mode** - write a Playwright Python script to `.tmp/pentest/`, run it via `shell(env_refs={"PT_USERNAME": ..., "PT_PASSWORD": ...})`; the script reads credentials from env, logs in, performs the check, writes screenshots to `security/evidence/`. Same pattern as QA-generated test scripts.
- **sqlmap** - per `security/pentest-toolchain.md`, targeted at code-identified injection points only.

## OWASP Top 10 Test Matrix

| Class | How to test | Primary channel |
|-------|-------------|-----------------|
| A01 Broken Access Control | Two-account differential: low-priv creds against high-priv resources; object-ID substitution on every ID-bearing endpoint; unauthenticated access to auth-required routes | api_request |
| A02 Cryptographic Failures | TLS config (sslyze); sensitive data in responses/caches/localStorage; weak token construction from code review | sslyze + code review |
| A03 Injection | sqlmap on queued candidates; manual payloads for command/path/template injection via httpx PoCs | sqlmap + code_executor |
| A04 Insecure Design | Business-flow abuse from permission-matrix knowledge (privilege-dependent flows, step-skipping) | api_request |
| A05 Security Misconfiguration | dirsearch for exposed paths (backups, admin panels, dotfiles, swagger in prod); default credentials; verbose errors | dirsearch + browser_playwright |
| A06 Vulnerable Components | pip-audit on runtime deps + reachability triage from code | pip-audit |
| A07 Identification & Auth Failures | Login flow behavior (error specificity, rate limiting, lockout), session token lifecycle (expiry, rotation, logout invalidation), password policy from code | api_request + browser_playwright |
| A08 Software & Data Integrity | Unsigned update/deploy paths, unverified deserialization of external input | code review + code_executor |
| A09 Logging & Monitoring Failures | Whether security-relevant events (failed logins, authz denials) emit audit logs - from code review; negative testing on log injection | code review |
| A10 SSRF | URL/host parameters fed to server-side fetches: point at a controlled marker endpoint or an invalid literal-IP with a distinctive response | code_executor |

The matrix is the baseline, not a checkbox ritual - the target list's candidates (specific sinks) come first; matrix classes with no candidate and no inventory surface get a one-line "no exposure identified" note.

## Non-Destructive PoC Rules

1. **Marker strings, not real data** - `<xss-poc-7f3a>`, `PT_MARKER_20260823`; never another user's data as payload.
2. **Read-only enumeration** - prove reachability with counts, schema names, or one masked sample row; never bulk-dump real records.
3. **Writes only as self-created markers** - create/delete only records the test itself created, only on non-production tiers.
4. **Bounded time-based proofs** - sleep payloads capped at 5 s, single request, with a control request to subtract baseline latency.
5. **Stop at proof** - once the finding is demonstrated, no privilege chaining, no lateral movement, no persistence.

## Authenticated Testing

1. Obtain pentest accounts per `security/pentest-scope-guard.md` (high-privilege + low-privilege).
2. Login via `api_request` with `secret_ref` - the response session token/cookie is test material in-context and may be reused in later requests.
3. Run the access-control differential: same request paths under both roles plus unauthenticated; record the triple of responses as evidence.
4. Rate limiting: cap loops (<= 20 iterations for lockout tests), sleep between batches, honor 429/Retry-After.

## Evidence

Every finding candidate produced here carries: request/response pair (method, URL, headers minus secrets, body), the tool channel used, and the OPSEC tag it ran under - saved under `security/evidence/`. Candidates then proceed to `security/finding-validation.md` before entering the report.
