"""Shared sandboxed subprocess runner for automation scripts.

Single execution path for both human-triggered runs (the API router's
/scripts/{id}/run) and agent-triggered runs (the script_writer tool): same
concurrency slot, same credential-scrubbed env, same log streaming into the
ScriptRun row. The API router imports from here and re-exports the module-level
names its tests monkeypatch.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("agents_universe.scripts")

# every POST /scripts/{id}/run spawns a sandboxed subprocess with
# no concurrency cap - any authenticated user could exhaust server memory by
# firing unlimited runs. Cap concurrent executions; requests beyond the cap
# queue for a slot instead of failing, so multi-user access never errors.
_DEFAULT_CONCURRENCY_LIMIT = 3
# Ceiling for the configured value: the cap is a memory guard, so raising it is
# a deliberate choice about the host, not a preference to be discovered by
# setting a large number.
_MAX_CONCURRENCY_LIMIT = 16
_script_semaphore: asyncio.Semaphore | None = None


def _concurrency_limit() -> int:
    """Configured run cap, defaulting to ``_DEFAULT_CONCURRENCY_LIMIT``.

    Configurable because the right number is a property of the host, and the
    one global pool is shared by human runs, agent runs and scheduled runs (a
    browser per Playwright run means a slot is not cheap). Read when the
    semaphore is created, so an unparseable value degrades to the default
    rather than disabling the cap.
    """
    raw = os.environ.get("SCRIPTS_CONCURRENCY_LIMIT", "").strip()
    if not raw:
        return _DEFAULT_CONCURRENCY_LIMIT
    try:
        limit = int(raw)
    except ValueError:
        _log.warning(
            "SCRIPTS_CONCURRENCY_LIMIT=%r is not an integer; using %d",
            raw, _DEFAULT_CONCURRENCY_LIMIT,
        )
        return _DEFAULT_CONCURRENCY_LIMIT
    if not 1 <= limit <= _MAX_CONCURRENCY_LIMIT:
        _log.warning(
            "SCRIPTS_CONCURRENCY_LIMIT=%d is outside 1..%d; clamping",
            limit, _MAX_CONCURRENCY_LIMIT,
        )
    return max(1, min(limit, _MAX_CONCURRENCY_LIMIT))

# Execution timeout for python/bash scripts (the WS poll loop outlasts it).
SCRIPT_TIMEOUT = 300

# WS log poll cadence for the stream flusher.
RUN_POLL_INTERVAL = 2

# Caps for the run log fields (UnicodeText is unbounded - the caps keep giant
# test output from bloating rows the WS view re-reads on every poll). The
# streamed log is HEAD-capped: the WS sends incremental diffs of
# stdout_log + stderr_log, so the stored log must only ever grow - a
# tail-truncation would shift the diff boundary and garble the client view.
LOG_CAP = 100_000
STDERR_CAP = 4000

# Lines kept past the log cap when a caller passes a tail buffer. A test
# runner's verdict (counts, failed case names) is the LAST thing it prints, so
# a head-capped log alone cannot answer "did it pass?".
TAIL_LINES = 200


def new_log_tail() -> deque[str]:
    """Bounded tail buffer for stream_subprocess(tail_acc=...)."""
    return deque(maxlen=TAIL_LINES)


def _bash_binary() -> str:
    """Bash executable that actually inherits the env we hand it.

    On Windows, CreateProcess (and ``shutil.which`` on a default PATH) resolve
    a bare ``bash`` to ``System32\\bash.exe`` — the WSL launcher, which starts
    the distro with its own environment and silently drops every variable
    injected for the run (AGENT_SECRET_*, AGENT_TOOL_BRIDGE/PIN/...). Scan
    PATH ourselves and skip System32 so Git Bash (or any other bash) wins;
    bare ``bash`` keeps Linux behavior unchanged.
    """
    if os.name != "nt":
        return "bash"
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    system32 = os.path.normcase(os.path.join(system_root, "System32"))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or os.path.normcase(os.path.normpath(entry)) == system32:
            continue
        for cand in ("bash.exe", "bash"):
            candidate = os.path.join(entry, cand)
            if os.path.isfile(candidate):
                return candidate
    return "bash"


def script_slot_guard() -> asyncio.Semaphore:
    """Lazily create the slot semaphore for script runs."""
    global _script_semaphore
    if _script_semaphore is None:
        limit = _concurrency_limit()
        _log.info("script run concurrency limit: %d", limit)
        _script_semaphore = asyncio.Semaphore(limit)
    return _script_semaphore


def sandbox_env() -> dict[str, str]:
    """os.environ minus credential-like keys (the same deny lists
    ToolContext.safe_env applies to LLM-generated code), with the proxy
    spellings normalized the way every other networked channel normalizes
    them (resolved URL in all four spellings, ALL_PROXY and empty
    placeholders dropped) so a script or QA run reaches the network through
    the same proxy as the shell tool."""
    import os

    from agent_core.tools.base import ToolContext, apply_proxy_env

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in ToolContext._ENV_DENY_EXACT:
            continue
        if upper.rsplit("_", 1)[-1] in ToolContext._ENV_DENY_SUFFIXES:
            continue
        if any(upper.startswith(p) for p in ToolContext._ENV_DENY_PREFIXES):
            continue
        env[key] = value
    return apply_proxy_env(env)


def persist_run_log(run, log_acc: list[str]) -> None:
    """Head-capped snapshot of the accumulated log. The WS log view polls the
    DB and sends incremental diffs, so the stored value must only grow."""
    run.stdout_log = "".join(log_acc)[:LOG_CAP]


def _redact(text: str, redact: tuple[str, ...]) -> str:
    """Replace plaintext secrets in log text with [REDACTED].

    Values shorter than 6 chars are skipped: a tiny needle would mangle
    unrelated output for no real protection (vault secrets are long)."""
    for value in redact:
        if len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    return text


async def drain_stream(
    stream, log_acc: list[str], budget: list[int],
    tail_acc: deque[str] | None = None,
    redact: tuple[str, ...] = (),
) -> None:
    """Append output lines to log_acc in arrival order, up to the log cap.

    budget is a one-element mutable counter of remaining chars; once it hits
    zero a truncation marker is emitted and further output is dropped.
    tail_acc, when given, keeps the last lines *regardless* of the cap - the
    verdict of a test run is printed last, i.e. exactly what a head-capped log
    loses first. redact holds plaintext values (injected secrets, the bridge
    PIN) that must never reach the stored log - a script echoing
    $AGENT_SECRET_* would otherwise write the credential into stdout_log."""
    while True:
        line = await stream.readline()
        if not line:
            break
        if budget[0] <= 0:
            if budget[0] == 0:
                log_acc.append("[executor] output truncated (log cap reached)\n")
                budget[0] = -1
            if tail_acc is not None:
                tail_acc.append(_redact(line.decode(errors="replace"), redact))
            continue
        text = _redact(line.decode(errors="replace"), redact)
        budget[0] -= len(text)
        if budget[0] < 0:
            # A single line can overshoot the cap, and the marker above only
            # fires on exactly zero - clamp so the truncation is never silent.
            budget[0] = 0
        log_acc.append(text)
        if tail_acc is not None:
            tail_acc.append(text)


async def stream_subprocess(
    db: AsyncSession, run, log_acc: list[str],
    cmd: list[str], cwd: str, env: dict[str, str], timeout: float,
    tail_acc: deque[str] | None = None,
    redact: tuple[str, ...] = (),
) -> int:
    """Run a subprocess and stream its output into the run row every ~2s.

    Owns the DB session for the duration - callers must not commit
    concurrently. Timeout and cancellation kill the whole process tree: a
    Playwright run spawns chromium children that plain proc.kill() would
    leave running (and writing) after the run ends. Returns the exit code;
    raises asyncio.TimeoutError on timeout and re-raises CancelledError
    after cleanup.

    tail_acc (see drain_stream) survives the log cap so the caller can parse a
    verdict out of output the stored log had to drop. redact (see drain_stream)
    scrubs plaintext secrets from everything that lands in the log."""
    from agent_core.sandbox import spawn_in_new_session, terminate_process_tree

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **spawn_in_new_session(),
    )
    budget = [LOG_CAP - sum(len(s) for s in log_acc)]
    readers = [
        asyncio.create_task(drain_stream(proc.stdout, log_acc, budget, tail_acc, redact)),
        asyncio.create_task(drain_stream(proc.stderr, log_acc, budget, tail_acc, redact)),
    ]
    # A stop event (not task cancellation) ends the flusher: cancelling it
    # mid-commit could poison the session for the terminal-state write that
    # follows in the caller.
    stop = asyncio.Event()

    async def _flush_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=RUN_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
            persist_run_log(run, log_acc)
            await db.commit()

    flusher = asyncio.create_task(_flush_loop())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        terminate_process_tree(proc)
        await proc.wait()
        raise
    finally:
        stop.set()
        await asyncio.gather(flusher, return_exceptions=True)
        # Readers finish once the exited/killed process closes its pipes.
        await asyncio.gather(*readers, return_exceptions=True)
        persist_run_log(run, log_acc)
        await db.commit()
    return proc.returncode


def _api_secret_key() -> str:
    """The vault master key, when running inside the API package."""
    try:
        from api.config import get_settings

        return get_settings().secret_key
    except Exception:  # noqa: BLE001 - standalone agent-core has no vault
        return ""


async def _resolve_secret_env(
    run, project_id: str, project_fs: str, declared: list[str],
    session_factory: Callable[[], AsyncSession],
    secret_key: str,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Decrypt the run owner's vault entries into AGENT_SECRET_* env vars.

    Owner = run.triggered_by (schedule creator for scheduled runs, clicking
    user for manual runs). Resolution follows _auth.get_secret: project_secrets
    first, user_tokens fallback. Missing keys raise ToolAuthError whose message
    carries only the key name — the failure path stores that message, never a
    value. Returns (env dict, plaintext values for log redaction).
    """
    from ..tools._auth import get_secret
    from ..tools.base import ToolContext

    owner = (run.triggered_by or "").strip()
    if not owner:
        raise RuntimeError(
            "Script declares secrets but the run has no owner (triggered_by is empty)"
        )
    if not project_id:
        raise RuntimeError("Script declares secrets but the run has no project_id")

    ctx = ToolContext(
        project_id=project_id,
        project_fs_path=project_fs,
        conversation_id=f"script-run:{run.run_id}",
        user_id=owner,
        secret_key=secret_key,
    )
    env: dict[str, str] = {}
    values: list[str] = []
    async with session_factory() as db:
        ctx.db_session = db
        for key in declared:
            value = await get_secret(ctx, key)
            # Prefix keeps an odd service_key from colliding with PATH/HOME;
            # injection happens AFTER sandbox_env() (see execute_script), so
            # the credential deny lists never see these names.
            env[f"AGENT_SECRET_{key.upper()}"] = value
            values.append(value)
    return env, tuple(values)


# The bridge client module ships beside every script: python imports it via
# sys.path[0] (the per-run dir); the bash CLI execs tool_call.py, which is a
# thin stdin/stdout shell over the same code.
_CLIENT_HELPER_SRC = Path(__file__).with_name("agent_script_tools.py")

_BASH_CLI = (
    "#!/usr/bin/env bash\n"
    'exec "${AGENT_TOOL_PYTHON:?}" "${AGENT_TOOL_HELPER:?}" "$@"\n'
)

_TOOL_CALL_MAIN = (
    "import sys\n"
    "from agent_script_tools import main\n"
    "sys.exit(main())\n"
)


def _write_helpers(run_dir: Path, env: dict[str, str], with_cli: bool) -> None:
    """Drop the bridge client into the run dir; with_cli also builds bin/agent-tool."""
    shutil.copyfile(_CLIENT_HELPER_SRC, run_dir / "agent_script_tools.py")
    if not with_cli:
        return
    (run_dir / "tool_call.py").write_text(_TOOL_CALL_MAIN, encoding="utf-8", newline="\n")
    bin_dir = run_dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    wrapper = bin_dir / "agent-tool"
    # LF only: a CRLF shebang makes `env` look for an executable named `bash\r`.
    wrapper.write_text(_BASH_CLI, encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)
    env["AGENT_TOOL_PYTHON"] = sys.executable
    env["AGENT_TOOL_HELPER"] = str(run_dir / "tool_call.py")
    # PATH prepend (not append): an existing agent-tool on the server PATH
    # must not shadow the per-run one. runner-generated, never validated by
    # _validate_bash — that only ever sees user content.
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"


async def execute_script(
    run_id: str, content: str, script_type: str, triggered_by: str = "",
    project_fs: str = "", session_factory: Callable[[], AsyncSession] | None = None,
    project_id: str = "", declared_secrets: list[str] | None = None,
    declared_tools: list[str] | None = None,
) -> None:
    """Run a script sandboxed the same way agent-core's code_executor runs code.

    this previously executed user content with `sys.executable`/`bash`
    and no sandbox - any authenticated user could read the server's .env,
    decrypt the token vault, or reach internal services. Now:
    - the subprocess env strips credential-like keys (SECRET_KEY, DB
      connection strings, provider tokens - the framework's safe_env lists);
    - python scripts run under the sitecustomize audit-hook guard
      (python_guard_env, strict=True also blocks subprocess), with the script
      file and cwd inside the project workspace so the guard covers them;
    - bash scripts are line-validated for path escapes and rejected if they
      use the framework's blocked commands (curl/wget/node/python3/ssh/...),
      which would otherwise reach the API itself, Redis, or the metadata
      endpoint.

    Two declarations widen that sandbox on purpose, both scoped to the run's
    owner (triggered_by) and both fail-closed:
    - declared_secrets: vault service_keys resolved via _auth.get_secret
      (project -> user) and injected as AGENT_SECRET_* env vars. Injected
      AFTER sandbox_env() strips credential names — that ordering is what
      lets the declared keys through while everything else stays scrubbed.
      Plaintext is redacted from the stored log and never written anywhere
      else. Owner is the schedule creator for scheduled runs, the clicking
      user for manual runs.
    - declared_tools: starts a per-run loopback ToolBridge so the script can
      call those agent tools in the service process (sandboxed subprocesses
      cannot reach them otherwise). Undeclared tools answer tool_not_allowed;
      confirmation gates run headless (interactive=False, no session) and
      fail closed. No declarations means neither bridge nor env injection —
      behavior identical to a plain run.

    The session factory is parameterized so callers with their own DB wiring
    (the script_writer tool passes the conversation's session factory) reuse
    it; the default falls back to the API's AsyncSessionLocal.
    """
    try:
        from api.models.script import ScriptRun
    except ImportError:
        _log.warning(
            "api.models.script unavailable - cannot execute run %s", run_id
        )
        return

    if session_factory is None:
        from api.database import AsyncSessionLocal
        session_factory = AsyncSessionLocal

    async with session_factory() as db:
        result = await db.execute(select(ScriptRun).where(ScriptRun.run_id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            return

        run.status = "running"
        await db.commit()

        if script_type not in ("python", "bash"):
            run.status = "failed"
            run.stderr_log = f"Unsupported script_type {script_type!r} (allowed: python, bash)"
            run.exit_code = -1
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        if not project_fs:
            run.status = "failed"
            run.stderr_log = "Script execution requires a project workspace"
            run.exit_code = -1
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        script_path = None
        run_dir: Path | None = None
        bridge = None
        log_acc: list[str] = []
        try:
            from agent_core.sandbox import python_guard_env
            from agent_core.tools.code_executor import CodeExecutorTool

            from .declarations import filter_runtime_tools, validate_declarations

            project_root = Path(project_fs)
            # Per-run directory inside the project (.tmp/work/scripts/{run_id}/)
            # so the Python file-access guard can read it; cwd is the project
            # root. Holds the script plus the bridge client files, and is
            # removed as a whole when the run ends.
            work_dir = project_root / ".tmp" / "work" / "scripts"
            work_dir.mkdir(parents=True, exist_ok=True)
            run_dir = work_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            ext = ".py" if script_type == "python" else ".sh"
            script_path = run_dir / f"main{ext}"
            # newline="\n": default translation writes CRLF on Windows, and
            # bash chokes on it (shebang becomes `bash\r`, command words gain
            # a trailing CR). Scripts are POSIX text.
            script_path.write_text(content, encoding="utf-8", newline="\n")

            if script_type == "bash":
                # Same gates as code_executor's bash mode: blocked external
                # commands (curl/wget/node/python3/ssh/...) then line-level
                # path-escape validation.
                # ANSI-C quoting ($'\x2fetc\x2fpasswd') decodes at runtime,
                # after every check below has seen only quote-stripped text:
                # the shlex unescape turns the token into literal content
                # while bash still executes the decoded path. Check the RAW
                # script, like code_executor's bash branch.
                if "$'" in content:
                    raise RuntimeError(
                        "Script uses ANSI-C quoting ($'...'), which is not "
                        "allowed: it decodes characters after validation."
                    )
                # Per-line tokenizing, like code_executor: whole-content
                # shlex raised on an apostrophe anywhere (e.g. a heredoc
                # body) and the raw fallback then missed a backslash-
                # obfuscated command (`c\url ...`), which bash unescapes
                # back to curl and executes.
                try:
                    blocked = CodeExecutorTool._find_blocked_bash(content)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Script could not be tokenized safely ({exc}); refusing to run."
                    ) from exc
                if blocked:
                    raise RuntimeError(
                        "Script uses a blocked command (curl/wget/node/python3/ssh/...). "
                        "Server-side scripts are confined to in-project automation."
                    )
                reason = CodeExecutorTool._validate_bash(content, project_root)
                if reason:
                    raise RuntimeError(reason)

            # Declarations: re-validated here (not only at save time) so a row
            # written before a rule existed still fails closed at run time.
            clean_secrets, _ = validate_declarations(declared_secrets or [], [])
            clean_tools = filter_runtime_tools(list(declared_tools or []))

            secret_env: dict[str, str] = {}
            secret_values: tuple[str, ...] = ()
            if clean_secrets:
                secret_env, secret_values = await _resolve_secret_env(
                    run, project_id, project_fs, clean_secrets,
                    session_factory, _api_secret_key(),
                )

            if clean_tools:
                from .tool_bridge import ToolBridge

                bridge = await ToolBridge.start(
                    project_id=project_id,
                    project_fs_path=project_fs,
                    user_id=(run.triggered_by or ""),
                    run_id=run_id,
                    declared_tools=clean_tools,
                    db_session_factory=session_factory,
                    secret_key=_api_secret_key(),
                )

            env = sandbox_env()
            # Injection MUST stay after sandbox_env(): it strips credential-
            # like names (…_TOKEN, …_SECRET, …_KEY) and would otherwise eat
            # exactly the keys we just decrypted. Only declared keys land here,
            # only in this run's env dict, only for this process.
            env.update(secret_env)
            if bridge is not None:
                env.update(bridge.env_vars())

            if script_type == "python":
                cmd = [sys.executable, str(script_path)]
                # Arms the sitecustomize audit hook: file reads/writes confined
                # to the project root; strict=True also blocks subprocess (a
                # child python could otherwise read outside the guard).
                # HOME must point inside the project: the guard whitelists
                # $HOME caches (~/.cache, ~/.npm, ~/.semgrep) for tooling, and
                # inheriting the server user's HOME would whitelist their real
                # caches as readable — the same reason code_executor points
                # HOME at its scratch dir.
                env["HOME"] = str(run_dir)
                env.update(python_guard_env(project_root, strict=True))
                # Helper always present so `from agent_script_tools import
                # call_tool` without a tools declaration raises the bridge's
                # own "not available" error instead of ModuleNotFoundError.
                _write_helpers(run_dir, env, with_cli=False)
            else:
                # Relative posix path (code_executor's pattern): bash treats a
                # Windows absolute path's backslashes as escapes and then fails
                # to find the script; cwd is already the project root.
                cmd = [_bash_binary(), script_path.relative_to(project_root).as_posix()]
                if bridge is not None:
                    _write_helpers(run_dir, env, with_cli=True)

            # Scrub every plaintext the child could echo — injected secrets
            # and the bridge PIN alike — before anything reaches the log.
            redact = secret_values + ((bridge.pin,) if bridge is not None else ())

            exit_code = await stream_subprocess(
                db, run, log_acc, cmd, str(project_root), env, SCRIPT_TIMEOUT,
                redact=redact,
            )
            run.exit_code = exit_code
            run.status = "completed" if exit_code == 0 else "failed"
        except asyncio.TimeoutError:
            run.status = "failed"
            run.stderr_log = f"Execution timed out ({SCRIPT_TIMEOUT}s)"
            run.exit_code = -1
        except asyncio.CancelledError:
            # The run row would stay at "running" forever (CancelledError is a
            # BaseException, bypassing `except Exception` and the tail commit),
            # blocking project deletion with a phantom running job. Persist a
            # terminal state, then keep propagating the cancellation.
            run.status = "failed"
            run.stderr_log = "Execution cancelled"
            run.exit_code = -1
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            raise
        except Exception as e:
            # A failed progress flush leaves the session pending-rollback:
            # assigning alone would make the final commit raise
            # PendingRollbackError and strand the row at "running", blocking
            # project deletion. Roll back and re-fetch on a clean session.
            await db.rollback()
            run = await db.get(ScriptRun, run_id)
            if run is None:
                return
            run.status = "failed"
            run.stderr_log = str(e)[:STDERR_CAP]
            run.exit_code = -1
        finally:
            if bridge is not None:
                try:
                    await bridge.close()
                except Exception:  # noqa: BLE001 - teardown must not mask the run result
                    _log.debug("tool bridge close failed for run %s", run_id, exc_info=True)
            if run_dir is not None:
                # Removes the script, the bridge client copies and bin/ together.
                shutil.rmtree(run_dir, ignore_errors=True)
            elif script_path is not None:
                try:
                    script_path.unlink()
                except OSError:
                    pass

        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
