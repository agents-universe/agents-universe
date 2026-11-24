---
slug: "performance/perf-load-script-writer"
description:
  zh-CN: "按 script_writer 通道契约生成 Python（asyncio+httpx）API 压测脚本——≤240s 有界执行、信号量限流、p50/p95/p99 与错误率采集、AGENT_SECRET_* 密钥注入、判定块最后打印进 stdout tail"
  en-US: "Generate bounded Python (asyncio+httpx) API stress scripts under the script_writer channel contract - <=240s execution, semaphore throttling, p50/p95/p99 and error-rate collection, AGENT_SECRET_* injection, verdict block printed last into the stdout tail"
type: "guidance"
triggers:
  - "write load script"
  - "stress script"
  - "generate stress test"
  - "写压测脚本"
  - "生成压测脚本"
  - "压力测试脚本"
tools:
  - script_writer
  - code_executor
  - filesystem
  - secret_vault
---

# Skill: Perf Load Script Writer

Phase 3-4: turn the Phase 1 scenario design into a runnable Python API stress script and execute it through `script_writer`. This skill owns the **channel contract** - the script must fit the executor or the run dies with no result.

## Channel Contract (verified - do not fight it)

| Fact | Consequence for your script |
|---|---|
| Executor hard-caps at 300 s and kills the process tree | Every profile runs **≤ 240 s wall** (ramp + steady + cooldown + margin); encode `DEADLINE_S = 240` and enforce it with a global deadline |
| Past the poll window `run` returns `status:"running"`; **there is no fetch-by-run-id** - a second `run` starts a NEW run | The run must terminate on its own with the verdict already printed; never re-`run` to "poll" |
| `stdout_tail` / `stderr_tail` = 4000 chars each | Print a **compact verdict block LAST** (≤ ~2 KB); everything verbose goes to a workspace JSON file instead |
| Secrets resolve to `AGENT_SECRET_*` env (project → user vault fallback), redacted from persisted logs | Read `os.environ["AGENT_SECRET_..."]` only; never print, never log, never embed |
| Python sandbox: no subprocess, file I/O confined to the project workspace, `sys.executable` | asyncio + httpx only; results JSON under `performance/results/` is in-workspace and allowed |
| `httpx` already installed | No dependency work; one `AsyncClient` with a connection pool |
| Global slot pool (default 3 concurrent runs, shared with human/scheduled runs) | Expect queueing; do not launch parallel load runs |

## Script Structure (author per run - this is guidance, not a verbatim block)

```
CONFIG = {
  BASE_URL, RAMP_S, STEADY_S, COOLDOWN_S, MAX_VUS, ENDPOINTS, DEADLINE_S=240,
  SLA per endpoint (p95_ms, max_error_rate)
}
```

1. **Virtual users as asyncio tasks** - a ramp scheduler starts VU tasks on a monotonic schedule (`time.monotonic()`), each bounded by `asyncio.Semaphore(MAX_VUS)`; steady phase loops requests until its window closes.
2. **Per request** - monotonic latency, status-code bucket (2xx/4xx/5xx/timeout), error class; headers auth from `AGENT_SECRET_*` env; payloads from the scenario design's data setup (self-created test data only).
3. **Global deadline** - `asyncio.wait_for` / loop exit at `start + DEADLINE_S`; on overrun, stop issuing requests, flush what was collected, still print the verdict.
4. **Metrics** - nearest-rank percentiles p50/p95/p99/max per endpoint plus aggregate; `error_rate = (5xx + 4xx + timeouts) / total`; `rps = ok_count / elapsed`.
5. **Persistence** - `json.dump` the full series + per-endpoint summary to `performance/results/{slug}-{YYYYMMDD}.json` (workspace-relative); this is the report's evidence.
6. **Verdict LAST on stdout** - one compact block that survives the 4000-char tail:
   ```
   VERDICT {json summary: profile, elapsed, per-endpoint {p50,p95,p99,max,rps,err_rate,sla_met}}
   ```
   Followed by a small human-readable table. Nothing important printed before it may matter - the tail keeps the END of stdout.
7. **Failure honesty** - unreachable host, 100% errors, deadline hit: still print `VERDICT` with the failure state; the report needs the number, not a traceback.

## Prototype vs Measured Run

- Reachability probe (does auth work, is the path right): a ~5 s single-shot check may use `code_executor(language="python")` (30 s ceiling) first - cheap iteration.
- **The measured run always goes through `script_writer`** - only its channel produces the persisted run row, artifacts, and tail the report cites.

## Iteration Loop

```
script_writer(operation="create", name="load-<slug>-<scenario>", script_type="python",
              description="...", content="<python>", secrets=["perf:<service>:username", ...])
script_writer(operation="run", script_id="<ID>")
→ read result: status + stdout_tail + stderr_tail
  - completed: parse VERDICT; fix data/auth issues via update, rerun
  - failed: fix script via operation="update", rerun
  - status:"running": report as tool-limited (run exceeded the poll window) - do NOT re-run
```

Cap iterations at **3 update+rerun cycles**; after that report the blocker (auth? endpoint? ceiling?) instead of looping. Runs are visible on the Script Executor page for human inspection.

## When the User Asks for Longer Than 240 s

Explain the executor bound and offer **segmented runs**: same script, shifted windows (e.g. 4 × 180 s segments), each a separate `run`, aggregated in the report as one timeline. Never propose changing `SCRIPT_TIMEOUT` - it guards every human, agent, and scheduled script run.

## Secrets Discipline

- Declare keys at create/update time (`secrets=[...]`); collection (if missing) belongs to `perf-scope-guard`.
- In the script: `token = os.environ.get("AGENT_SECRET_PERF_...")`; missing key → print a VERDICT with `auth: missing_secret` and exit - never a traceback containing env.
- No secret value ever appears in `content` comments, description, results JSON, or chat.

---

*Adapted in part from addyosmani/agent-skills@e997ac5 (reviewed 2026-11-24); rewritten to framework conventions - no upstream code retained.*
