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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# every POST /scripts/{id}/run spawns a sandboxed subprocess with
# no concurrency cap - any authenticated user could exhaust server memory by
# firing unlimited runs. Cap concurrent executions; requests beyond the cap
# queue for a slot instead of failing, so multi-user access never errors.
CONCURRENCY_LIMIT = 3
_script_semaphore: asyncio.Semaphore | None = None

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


def script_slot_guard() -> asyncio.Semaphore:
    """Lazily create the slot semaphore for script runs."""
    global _script_semaphore
    if _script_semaphore is None:
        _script_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    return _script_semaphore


def sandbox_env() -> dict[str, str]:
    """os.environ minus credential-like keys (the same deny lists
    ToolContext.safe_env applies to LLM-generated code)."""
    import os

    from agent_core.tools.base import ToolContext

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
    return env


def persist_run_log(run, log_acc: list[str]) -> None:
    """Head-capped snapshot of the accumulated log. The WS log view polls the
    DB and sends incremental diffs, so the stored value must only grow."""
    run.stdout_log = "".join(log_acc)[:LOG_CAP]


async def drain_stream(stream, log_acc: list[str], budget: list[int]) -> None:
    """Append output lines to log_acc in arrival order, up to the log cap.

    budget is a one-element mutable counter of remaining chars; once it hits
    zero a truncation marker is emitted and further output is dropped."""
    while True:
        line = await stream.readline()
        if not line:
            break
        if budget[0] <= 0:
            if budget[0] == 0:
                log_acc.append("[executor] output truncated (log cap reached)\n")
                budget[0] = -1
            continue
        text = line.decode(errors="replace")
        budget[0] -= len(text)
        log_acc.append(text)


async def stream_subprocess(
    db: AsyncSession, run, log_acc: list[str],
    cmd: list[str], cwd: str, env: dict[str, str], timeout: float,
) -> int:
    """Run a subprocess and stream its output into the run row every ~2s.

    Owns the DB session for the duration - callers must not commit
    concurrently. Timeout and cancellation kill the whole process tree: a
    Playwright run spawns chromium children that plain proc.kill() would
    leave running (and writing) after the run ends. Returns the exit code;
    raises asyncio.TimeoutError on timeout and re-raises CancelledError
    after cleanup."""
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
        asyncio.create_task(drain_stream(proc.stdout, log_acc, budget)),
        asyncio.create_task(drain_stream(proc.stderr, log_acc, budget)),
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


async def execute_script(
    run_id: str, content: str, script_type: str, triggered_by: str = "",
    project_fs: str = "", session_factory: Callable[[], AsyncSession] | None = None,
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

    The session factory is parameterized so callers with their own DB wiring
    (the script_writer tool passes the conversation's session factory) reuse
    it; the default falls back to the API's AsyncSessionLocal.
    """
    try:
        from api.models.script import ScriptRun
    except ImportError:
        logging.getLogger("agents_universe.scripts").warning(
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
        log_acc: list[str] = []
        try:
            import shlex

            from agent_core.sandbox import python_guard_env
            from agent_core.tools.code_executor import CodeExecutorTool

            project_root = Path(project_fs)
            # Script file lives inside the project (.tmp/work/scripts/) so the
            # Python file-access guard can read it; cwd is the project root.
            work_dir = project_root / ".tmp" / "work" / "scripts"
            work_dir.mkdir(parents=True, exist_ok=True)
            ext = ".py" if script_type == "python" else ".sh"
            script_path = work_dir / f"{run_id}{ext}"
            script_path.write_text(content, encoding="utf-8")

            if script_type == "bash":
                # Same gates as code_executor's bash mode: blocked external
                # commands (curl/wget/node/python3/ssh/...) then line-level
                # path-escape validation.
                unescaped = content
                try:
                    lexer = shlex.shlex(content, posix=True)
                    lexer.whitespace_split = True
                    unescaped = " ".join(list(lexer))
                except ValueError:
                    pass
                if CodeExecutorTool._BLOCKED_BASH.search(unescaped):
                    raise RuntimeError(
                        "Script uses a blocked command (curl/wget/node/python3/ssh/...). "
                        "Server-side scripts are confined to in-project automation."
                    )
                reason = CodeExecutorTool._validate_bash(content, project_root)
                if reason:
                    raise RuntimeError(reason)

            env = sandbox_env()

            if script_type == "python":
                cmd = [sys.executable, str(script_path)]
                # Arms the sitecustomize audit hook: file reads/writes confined
                # to the project root; strict=True also blocks subprocess (a
                # child python could otherwise read outside the guard).
                env.update(python_guard_env(project_root, strict=True))
            else:
                cmd = ["bash", str(script_path)]

            exit_code = await stream_subprocess(
                db, run, log_acc, cmd, str(project_root), env, SCRIPT_TIMEOUT,
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
            run.status = "failed"
            run.stderr_log = str(e)[:STDERR_CAP]
            run.exit_code = -1
        finally:
            if script_path is not None:
                try:
                    script_path.unlink()
                except OSError:
                    pass

        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
