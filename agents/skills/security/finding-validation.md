---
slug: "security/finding-validation"
description: "Validation gate before reporting - mandatory reproduction, false-positive elimination, CVSS 3.1 scoring, CWE mapping, and annotated evidence capture"
type: "guidance"
triggers:
  - "validate finding"
  - "false positive check"
  - "CVSS scoring"
  - "发现验证"
  - "误报检查"
  - "漏洞定级"
tools:
  - browser_playwright
  - image_annotator
  - focus_template
  - code_executor
  - filesystem
---

# Skill: Finding Validation

Phase 4 gate: a candidate becomes a finding only by passing this skill. A finding that cannot be reproduced is not a finding - it is a note in Open Risks at most. No exceptions for severity.

## Validation Gate

Every finding must have all four before it enters the report:

1. **Reproduction** - the PoC re-run from a clean state with the documented steps; same outcome.
2. **FP elimination** - the checklist below, answered with evidence, not assumption.
3. **Scoring** - severity + CVSS 3.1 vector + CWE class assigned.
4. **Evidence** - annotated screenshot or request/response pair filed under `security/evidence/`.

## False-Positive Elimination

For each candidate, eliminate the classic false positives:

- **Sanitized upstream?** - trace the parameter path in source (Phase 2 context): validator/ORM parameterization/encoder before the sink.
- **Framework default protection?** - template auto-escaping, CSRF middleware, type coercion that defuses the payload class.
- **Environment-specific?** - dev-only debug flag, test double, feature branch - does the deployed tier being tested actually exhibit it?
- **Precondition real?** - the vulnerable path requires a role/token/state the attacker cannot actually obtain?
- **Tool artifact?** - scanner false positive pattern (semgrep confidence, sqlmap heuristic match): confirm with an independent manual request.

A candidate failing elimination is dropped with a one-line reason recorded in the working notes - it stays out of the report, and the pattern is worth a knowledge-writeback if it will recur.

## Severity Scoring

CVSS 3.1 base score from AV/AC/PR/UI/S/C/I/A; record the full vector:

| Rating | CVSS | Meaning | Remediation SLA |
|--------|------|---------|-----------------|
| Critical | 9.0-10.0 | Immediate exploitation likely; direct path to sensitive data or full compromise | Immediate |
| High | 7.0-8.9 | Feasible with minimal complexity; significant exposure | 30 days |
| Medium | 4.0-6.9 | Requires specific conditions; moderate impact | 90 days |
| Low | 0.1-3.9 | Limited impact or heavy prerequisites | Regular maintenance |
| Informational | 0.0 | Best-practice recommendation | Backlog |

White-box calibration: score what an attacker WITH source access demonstrated, and note in the finding whether an external no-source attacker could realistically reach it (the report's methodology disclosure covers the stance).

## CWE Mapping

Map to the CWE class (not the specific instance) for the finding header - common ones: CWE-79 (XSS), CWE-89 (SQLi), CWE-22 (path traversal), CWE-78 (OS command injection), CWE-284/CWE-639 (access control / IDOR), CWE-798 (hardcoded credentials), CWE-327 (broken crypto), CWE-1104 (vulnerable dependency), CWE-918 (SSRF), CWE-613 (session expiry). CVE numbers for dependency findings come from the pip-audit output.

## Evidence Capture

- **UI findings** - `browser_playwright` screenshot (or script-mode screenshot), then annotate: `focus_template(image_path=..., count=N, title="<finding id> key evidence")`, or `image_annotator` with explicit focus areas. No placeholder labels.
- **API findings** - request/response pair as JSON/text files; mask any secret values and real user data.
- **Tool findings** - reference the saved log file path plus the decisive excerpt; do not inline kilobytes of raw scan output into the report.
- File naming per `security/pentest-scope-guard.md`: `{finding-id}_{kind}_{YYYYMMDD_HHMMSS}.{ext}` under `security/evidence/`.

## Batch Discipline

Validate candidates in priority order from the target list; if validation cannot run (target down, credentials missing), the candidate is reported as `pending-validation` in Open Risks - never promoted to a finding on static evidence alone. Dependency CVEs and hardcoded secrets validate differently: reachability-in-code + exploitability note replaces dynamic reproduction.
