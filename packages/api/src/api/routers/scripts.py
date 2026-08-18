"""Automation script management router."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.project import Project
from api.models.script import AutomationScript, ScriptRun

router = APIRouter(prefix="/api")

# every POST /scripts/{id}/run spawns a sandboxed subprocess with
# no concurrency cap — any authenticated user could exhaust server memory by
# firing unlimited runs. Cap concurrent executions; requests beyond the cap
# queue for a slot instead of failing, so multi-user access never errors.
_SCRIPT_CONCURRENCY_LIMIT = 3
_script_semaphore: asyncio.Semaphore | None = None


def _script_slot_guard() -> asyncio.Semaphore:
    """Lazily create the slot semaphore for script runs."""
    global _script_semaphore
    if _script_semaphore is None:
        _script_semaphore = asyncio.Semaphore(_SCRIPT_CONCURRENCY_LIMIT)
    return _script_semaphore


class ScriptCreate(BaseModel):
    # Caps mirror the Unicode column widths — MSSQL DataError (500) on
    # overflow while SQLite silently accepts . content is
    # UnicodeText (unbounded), no cap needed.
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    script_type: Literal["python", "bash"] = "python"
    content: str


@router.get("/projects/{project_id}/scripts")
async def list_scripts(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    result = await db.execute(
        select(AutomationScript).where(AutomationScript.project_id == project_id).order_by(AutomationScript.created_at.desc())
    )
    scripts = result.scalars().all()
    return [
        {
            "script_id": str(s.script_id),
            "name": s.name,
            "script_type": s.script_type,
            "description": s.description,
        }
        for s in scripts
    ]


@router.post("/projects/{project_id}/scripts")
async def create_script(
    project_id: str,
    body: ScriptCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    script = AutomationScript(
        project_id=project_id,
        name=body.name,
        description=body.description,
        script_type=body.script_type,
        content=body.content,
    )
    db.add(script)
    await db.commit()
    return {"script_id": str(script.script_id), "name": script.name}


@router.post("/scripts/{script_id}/run")
async def run_script(
    script_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    # Acquire the slot BEFORE any DB work: the request-scoped session checks
    # out a pool connection on its first query, and queued waiters holding
    # checked-out connections can exhaust the shared pool (size 10 / overflow
    # 20) for every other endpoint while up to 3 long scripts run. Waiting
    # happens on the semaphore alone, no connection pinned.
    sem = _script_slot_guard()
    await sem.acquire()
    run = None
    try:
        result = await db.execute(select(AutomationScript).where(AutomationScript.script_id == script_id))
        script = result.scalar_one_or_none()
        if not script:
            raise HTTPException(status_code=404, detail="Script not found")
        # The route has no project_id; derive it from the script before authorizing.
        project = await authorize_project(str(script.project_id), db, current_user)

        # commit()/resolve_project_fs_path can throw after acquire() but before
        # the task exists — release() is only wired to the task's done_callback,
        # so an error here leaks a slot permanently (3 leaks = every later request
        # blocks forever). Release on the failure path.
        run = ScriptRun(
            script_id=script.script_id,
            triggered_by=current_user.user_id,
            status="pending",
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        await db.commit()

        # this raw PROJECTS_ROOT/slug join broke child projects (whose
        # workspace lives under PROJECTS_ROOT/{parent.slug}/projects/{child.slug})
        # — scripts ran against a nonexistent root-level dir. Use the canonical
        # resolver instead.
        from api.paths import resolve_project_fs_path
        project_fs = await resolve_project_fs_path(str(script.project_id), db)
    except BaseException:
        # run may not exist yet (404 / authorize failure) — only mark failed
        # when a row was committed; a leftover pending row would block
        # project deletion forever (PROJECT_HAS_RUNNING_WORK).
        if run is not None:
            # run_id is captured BEFORE the rollback: rollback expires every
            # ORM instance, and touching their attributes afterwards raises
            # MissingGreenlet under the async driver.
            run_id = str(run.run_id)
            try:
                await db.rollback()
                run.status = "failed"
                run.stderr_log = "Failed to resolve the project workspace"
                run.exit_code = -1
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
            except Exception:
                # Best-effort only — never mask the original error.
                logging.getLogger("agents_universe.scripts").exception("Could not mark script run %s as failed", run_id)
        sem.release()
        raise

    task = asyncio.create_task(
        _execute_script(
            str(run.run_id), script.content, script.script_type,
            current_user.user_id, project_fs,
        )
    )
    task.add_done_callback(_on_script_task_done)
    # Release the slot when the run finishes (success, failure, or cancel).
    task.add_done_callback(lambda t: sem.release())
    return {"run_id": str(run.run_id), "status": "pending"}


def _on_script_task_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        import logging
        logging.getLogger("agents_universe.scripts").error("Script execution failed: %s", exc, exc_info=exc)


async def _execute_script(
    run_id: str, content: str, script_type: str, triggered_by: str = "", project_fs: str = "",
) -> None:
    """Run a script sandboxed the same way agent-core's code_executor runs code.

    this previously executed user content with `sys.executable`/`bash`
    and no sandbox — any authenticated user could read the server's .env,
    decrypt the token vault, or reach internal services. Now:
    - the subprocess env strips credential-like keys (SECRET_KEY, DB
      connection strings, provider tokens — the framework's safe_env lists);
    - python scripts run under the sitecustomize audit-hook guard
      (python_guard_env, strict=True also blocks subprocess), with the script
      file and cwd inside the project workspace so the guard covers them;
    - bash scripts are line-validated for path escapes and rejected if they
      use the framework's blocked commands (curl/wget/node/python3/ssh/...),
      which would otherwise reach the API itself, Redis, or the metadata
      endpoint.
    """
    from api.database import AsyncSessionLocal
    import sys

    async with AsyncSessionLocal() as db:
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

        proc = None
        script_path = None
        try:
            import os
            import shlex

            from agent_core.sandbox import python_guard_env
            from agent_core.tools.base import ToolContext
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

            # Strip credential-like env keys (same deny lists the framework
            # uses for LLM-generated code) so a script cannot read SECRET_KEY,
            # DB passwords, or provider tokens from the environment.
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

            if script_type == "python":
                cmd = [sys.executable, str(script_path)]
                # Arms the sitecustomize audit hook: file reads/writes confined
                # to the project root; strict=True also blocks subprocess (a
                # child python could otherwise read outside the guard).
                env.update(python_guard_env(project_root, strict=True))
            else:
                cmd = ["bash", str(script_path)]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_root),
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

            run.stdout_log = stdout.decode(errors="replace")[:8000]
            run.stderr_log = stderr.decode(errors="replace")[:4000]
            run.exit_code = proc.returncode
            run.status = "completed" if proc.returncode == 0 else "failed"
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            run.status = "failed"
            run.stderr_log = "Execution timed out (300s)"
            run.exit_code = -1
        except asyncio.CancelledError:
            # The run row would stay at "running" forever (CancelledError is a
            # BaseException, bypassing `except Exception` and the tail commit),
            # blocking project deletion with a phantom running job. Persist a
            # terminal state, then keep propagating the cancellation.
            if proc is not None:
                proc.kill()
                await proc.wait()
            run.status = "failed"
            run.stderr_log = "Execution cancelled"
            run.exit_code = -1
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            raise
        except Exception as e:
            run.status = "failed"
            run.stderr_log = str(e)[:4000]
            run.exit_code = -1
        finally:
            if script_path is not None:
                try:
                    script_path.unlink()
                except OSError:
                    pass

        run.completed_at = datetime.now(timezone.utc)
        await db.commit()


@router.get("/scripts/{script_id}/runs")
async def list_runs(
    script_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    script_result = await db.execute(
        select(AutomationScript).where(AutomationScript.script_id == script_id)
    )
    script = script_result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    await authorize_project(str(script.project_id), db, current_user)

    result = await db.execute(
        select(ScriptRun)
        .where(ScriptRun.script_id == script.script_id)
        .order_by(ScriptRun.created_at.desc())
        .limit(20)
    )
    runs = result.scalars().all()
    return [
        {
            "run_id": str(r.run_id),
            "status": r.status,
            "exit_code": r.exit_code,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in runs
    ]


@router.websocket("/ws/script-runs/{run_id}")
async def script_run_ws(run_id: str, ws: WebSocket):
    from api.config import get_settings
    from api.services.redis_client import _get_pool, get_session as get_redis_session
    settings = get_settings()
    session_id = ws.cookies.get(settings.auth_cookie_name)
    if not session_id:
        await ws.close(code=4001)
        return
    try:
        redis = _get_pool()
        session_data = await get_redis_session(redis, session_id)
    except Exception:
        await ws.close(code=4001)
        return
    if not session_data:
        await ws.close(code=4001)
        return

    from api.database import AsyncSessionLocal

    # Authenticate the run through its complete ownership chain before
    # accepting the socket. A valid Redis session alone must not expose a run
    # from another user's project.
    user_id = session_data.get("user_id")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ScriptRun)
            .join(AutomationScript, AutomationScript.script_id == ScriptRun.script_id)
            .join(Project, Project.project_id == AutomationScript.project_id)
            .where(
                ScriptRun.run_id == run_id,
                ScriptRun.triggered_by == user_id,
                Project.is_active == True,  # noqa: E712
            )
        )
        if result.scalar_one_or_none() is None:
            await ws.close(code=4003)
            return

    await ws.accept()

    try:
        last_log_len = 0
        # Poll must outlast the execution timeout: _execute_script allows up
        # to 300s of subprocess runtime, so 60 × 2s would silently close the
        # socket while a long script is still running (client never sees the
        # final "done" event). Poll for 330s to cover 300s + margin.
        for _ in range(165):
            not_found = False
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ScriptRun)
                    .join(AutomationScript, AutomationScript.script_id == ScriptRun.script_id)
                    .join(Project, Project.project_id == AutomationScript.project_id)
                    .where(
                        ScriptRun.run_id == run_id,
                        Project.is_active == True,  # noqa: E712
                    )
                )
                run = result.scalar_one_or_none()
                if not run:
                    not_found = True
                else:
                    run_status = run.status
                    run_stdout = run.stdout_log or ""
                    run_stderr = run.stderr_log or ""
                    run_exit_code = run.exit_code

            if not_found:
                await ws.send_json({"type": "error", "message": "Run not found"})
                break

            log = run_stdout + run_stderr
            if len(log) > last_log_len:
                new_chunk = log[last_log_len:]
                await ws.send_json({"type": "log", "log": new_chunk})
                last_log_len = len(log)

            if run_status in ("completed", "failed"):
                await ws.send_json({"type": "done", "status": run_status, "exit_code": run_exit_code})
                break

            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    finally:
        await ws.close()
