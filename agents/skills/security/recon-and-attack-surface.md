---
slug: "security/recon-and-attack-surface"
description: "Map the project attack surface - knowledge-first inventory, toolchain capability detection, and per-channel target reachability - producing the engagement attack surface map"
type: "guidance"
triggers:
  - "attack surface mapping"
  - "reconnaissance"
  - "capability detection"
  - "target reachability"
  - "攻击面测绘"
  - "信息收集"
  - "工具探测"
tools:
  - knowledge_rw
  - filesystem
  - git_repo
  - shell
---

# Skill: Recon and Attack Surface Mapping

Phase 1 of the full-project pentest. Everything downstream targets what this skill produces: a module-by-entry-point inventory plus the operational facts (tool availability, channel reachability) that decide how each test runs.

## Knowledge-First Inventory

`knowledge_rw(operation="list")`, then read in this order (skip a source only when it does not exist for the project):

1. `environment` - environment targets (`APP_URL_DEV/INT/UAT/PRD`), `AGENT_DEFAULT_LANGUAGE`.
2. `system-architecture` / `technical-stack` - components, frameworks, data stores.
3. `api-map` / `kong-map` - product-owned API inventory and gateway routes.
4. `page-map` - UI routes and feature mapping.
5. `permission-matrix` / `role-matrix` - roles and their authorized surfaces.
6. `login-and-user-switch` - auth entry points, verified accounts.
7. `test-patterns` - known fragile areas worth extra attention.

Fall back to the product repo (`git_repo`, `repo_graph`) only when knowledge is absent or stale; write what you learn back in Phase 6.

Produce the **attack surface inventory** table - one row per entry point:

| Module | Entry point | Type (UI/API/job) | Auth required | Notes |
|--------|------------|-------------------|---------------|-------|

## Toolchain Capability Detection

Probe each tool once; record availability in the engagement notes (`.tmp/pentest/engagement.md`):

```bash
HOME=.tmp/pentest/home python3 -m sqlmap.sqlmap --version
HOME=.tmp/pentest/home /opt/semgrep-venv/bin/semgrep --version
HOME=.tmp/pentest/home python3 -m bandit --version
HOME=.tmp/pentest/home python3 -m pip_audit --version
HOME=.tmp/pentest/home python3 -m detect_secrets --version
HOME=.tmp/pentest/home python3 -m sslyze --help
HOME=.tmp/pentest/home python3 -m dirsearch --version
HOME=.tmp/pentest/home python3 -m wafw00f.main -V
```

`--version` support varies (sslyze has none; wafw00f uses `-V`) - the probes above are the verified forms; `-h` is the generic fallback. A failing probe marks the tool unavailable for the engagement - see `security/pentest-toolchain.md` for degradation.

## Target Reachability Matrix

The HTTP-capable tools enforce different reachability rules. Choose the channel per target BEFORE testing:

| Target form | browser_playwright | api_request / web_fetch | code_executor (httpx) | shell tools |
|-------------|-------------------|-------------------------|----------------------|-------------|
| Hostname URL, standard port | OK | OK | OK | OK |
| Hostname URL, non-standard port | OK by default | OK by default | OK | OK |
| Literal private/loopback IP (e.g. `10.x.x.x`) | BLOCKED | BLOCKED | OK | OK |

The literal-IP block is an always-on SSRF guard independent of `SSRF_ENABLED`; the port allowlist follows the `SSRF_ENABLED` gate (off by default). `code_executor` Python runs with unrestricted sockets by default (`SANDBOX_NETWORK` policy) - it is the universal fallback channel. When `SANDBOX_NETWORK=localhost|none`, all external dynamic testing is blocked: report the limitation instead of retrying.

Practical rule: prefer DNS names from `environment` knowledge over literal IPs so the standard channels stay usable; route literal-IP targets through `code_executor` httpx PoCs.

## Environment Confirmation

Confirm the target tier against the Phase 0 scope declaration - the default is dev/test. Record: target URLs actually used, the tool versions from capability detection, and any environment-specific caveats (shared test data, rate limiters, WAF). This feeds the report's Scope & Methodology sections verbatim.

## Output

- Attack surface inventory table (goes into the report's Attack Surface section).
- Capability + reachability matrix (engagement notes; summarized in the report's Methodology).
- Discovered additional surfaces (from dirsearch/wafw00f when run in this phase) merged back into the inventory.

Every entry point must appear in the inventory or be listed out-of-scope with a reason - no silent gaps.
