"""Automation script management router."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.scripts.runner import (
    LOG_CAP,
    RUN_POLL_INTERVAL,
    STDERR_CAP,
    execute_script,
    persist_run_log,
    sandbox_env,
    script_slot_guard,
    stream_subprocess,
)
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.project import Project
from api.models.script import AutomationScript, ScriptRun

router = APIRouter(prefix="/api")

# Module-level aliases into agent_core.scripts.runner — the shared execution
# path used by both human runs (below) and agent runs (the script_writer
# tool). The API test suite monkeypatches these exact names on this module
# (_script_slot_guard, _execute_script, _stream_subprocess) to gate runs
# without spawning subprocesses, so the aliases must stay in sync with the
# runner's implementation.
_LOG_CAP = LOG_CAP
_STDERR_CAP = STDERR_CAP
_RUN_POLL_INTERVAL = RUN_POLL_INTERVAL
_script_slot_guard = script_slot_guard
_execute_script = execute_script
_stream_subprocess = stream_subprocess
_persist_run_log = persist_run_log
_sandbox_env = sandbox_env

# Playwright phase budgets: browser preflight and the test run itself (the
# dependency install budget lives inside agent-core's ensure_node_deps). Their
# sum bounds how long a cold run can take - the WS poll loop below must
# outlast it.
_PLAYWRIGHT_BROWSER_TIMEOUT = 180
_PLAYWRIGHT_TEST_TIMEOUT = 540

# WS log poll total window. 900s covers a cold Playwright run
# (deps 120s + browser 180s + tests 540s) plus margin; plain python/bash
# scripts cap at 300s, far below this.
_RUN_POLL_LOOPS = 450


class ScriptCreate(BaseModel):
    # Caps mirror the Unicode column widths - MSSQL DataError (500) on
    # overflow while SQLite silently accepts . content is
    # UnicodeText (unbounded), no cap needed.
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    script_type: Literal["python", "bash"] = "python"
    content: str


class PlaywrightRunCreate(BaseModel):
    # Non-secret env for a Playwright run. The generated specs read APP_BASE_URL
    # / APP_LOGIN_URL / APP_USERNAME / APP_PASSWORD; APP_* keys only, so the
    # request cannot smuggle arbitrary server-side env names. Vault-backed
    # secret injection needs a conversation context and is not offered here.
    env: dict[str, str] = Field(default_factory=dict, max_length=16)


@router.get("/projects/{project_id}/scripts")
async def list_scripts(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    result = await db.execute(
        select(AutomationScript)
        .where(AutomationScript.project_id == project_id, AutomationScript.script_type != "playwright")
        .order_by(AutomationScript.created_at.desc())
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


async def _spawn_script_run(
    db: AsyncSession,
    current_user: UserInfo,
    prepare: Callable[[AsyncSession], Awaitable[tuple[AutomationScript, Callable[[str, str], Awaitable[None]]]]],
) -> dict:
    """Shared run bootstrap for the script and Playwright-spec endpoints.

    ``prepare`` runs under the acquired slot (target lookup, authorization,
    anchor creation) and returns the owning script row plus a factory that
    builds the executor coroutine once the workspace path is known. The slot
    is acquired BEFORE any DB work: the request-scoped session checks out a
    pool connection on its first query, and queued waiters holding checked-out
    connections can exhaust the shared pool (size 10 / overflow 20) for every
    other endpoint while up to 3 long runs hold slots. Anything that throws
    between acquire() and the spawned task releases the slot; once the run
    row is committed it is marked failed too (a leftover pending row would
    block project deletion forever).
    """
    sem = _script_slot_guard()
    await sem.acquire()
    run: ScriptRun | None = None
    try:
        script, build = await prepare(db)
        run = ScriptRun(
            script_id=script.script_id,
            triggered_by=current_user.user_id,
            status="pending",
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        await db.commit()

        # resolve_project_fs_path can throw after acquire() but before the
        # task exists - release() is only wired to the task's done_callback,
        # so an error here leaks a slot permanently (3 leaks = every later
        # request blocks forever). Release on the failure path.
        from api.paths import resolve_project_fs_path
        project_fs = await resolve_project_fs_path(str(script.project_id), db)
        coro = build(str(run.run_id), project_fs)
    except BaseException as exc:
        # run may not exist yet (404 / authorize failure) - only mark failed
        # when a row was committed. run_id is captured BEFORE the rollback:
        # rollback expires every ORM instance, and touching their attributes
        # afterwards raises MissingGreenlet under the async driver.
        if run is not None:
            run_id = str(run.run_id)
            try:
                await db.rollback()
                run.status = "failed"
                run.stderr_log = f"Failed to start the run: {exc}"[:_STDERR_CAP]
                run.exit_code = -1
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
            except Exception:
                # Best-effort only - never mask the original error.
                logging.getLogger("agents_universe.scripts").exception("Could not mark script run %s as failed", run_id)
        sem.release()
        raise

    task = asyncio.create_task(coro)
    task.add_done_callback(_on_script_task_done)
    # Release the slot when the run finishes (success, failure, or cancel).
    task.add_done_callback(lambda t: sem.release())
    return {"run_id": str(run.run_id), "status": "pending"}


@router.post("/scripts/{script_id}/run")
async def run_script(
    script_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    async def prepare(db: AsyncSession):
        result = await db.execute(select(AutomationScript).where(AutomationScript.script_id == script_id))
        script = result.scalar_one_or_none()
        if not script:
            raise HTTPException(status_code=404, detail="Script not found")
        # The route has no project_id; derive it from the script before authorizing.
        await authorize_project(str(script.project_id), db, current_user)
        if script.script_type == "playwright":
            # The internal Playwright anchor is not a runnable script.
            raise HTTPException(status_code=404, detail="Playwright specs are run via the specs endpoint")

        def build(run_id: str, project_fs: str):
            return _execute_script(run_id, script.content, script.script_type, current_user.user_id, project_fs)

        return script, build

    return await _spawn_script_run(db, current_user, prepare)


# ── Playwright specs (QA-generated tests) ────────────────────────────────────

_PW_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PW_ENV_KEY_RE = re.compile(r"^APP_[A-Z0-9_]+$")
_PW_SPEC_TITLE_RE = re.compile(r"test\.describe\(\s*['\"]([^'\"]+)['\"]")
_PLAYWRIGHT_ANCHOR_NAME = "__playwright__"


def _resolve_playwright_spec(project_fs: str, slug: str) -> Path:
    """Path of tests/generated/{slug}.spec.ts; 404 when missing.

    The slug is regex-validated upstream - the resolve check is defense in
    depth against a symlinked spec escaping the workspace."""
    spec = Path(project_fs) / "tests" / "generated" / f"{slug}.spec.ts"
    try:
        ok = spec.is_file() and spec.resolve().is_relative_to(Path(project_fs).resolve())
    except OSError:
        ok = False
    if not ok:
        raise HTTPException(status_code=404, detail=f"Spec not found: tests/generated/{slug}.spec.ts")
    return spec


def _validated_playwright_env(request_env: dict[str, str]) -> dict[str, str]:
    """APP_*-prefixed keys only, values capped at 500 chars.

    Deliberately NOT re-run through the sandbox deny lists: those strip
    *URL-suffixed keys and would reject APP_BASE_URL itself. The request
    carries user-supplied values (never server secrets), so the prefix is
    the boundary."""
    validated: dict[str, str] = {}
    for key, value in request_env.items():
        if not _PW_ENV_KEY_RE.match(key):
            raise HTTPException(status_code=422, detail=f"env key {key!r} must match APP_*")
        if len(value) > 500:
            raise HTTPException(status_code=422, detail=f"env value for {key!r} must be at most 500 characters")
        validated[key] = value
    return validated


async def _get_or_create_playwright_anchor(
    db: AsyncSession, project_id: str, created_by: str,
) -> AutomationScript:
    """Hidden per-project AutomationScript row anchoring Playwright runs.

    ScriptRun.script_id is a NOT NULL FK, and both the WS auth check and the
    project deletion guard join ScriptRun -> AutomationScript -> Project. One
    anchor row per project satisfies both without a schema migration;
    list_scripts filters it out of the user-visible list."""
    rows = (await db.execute(
        select(AutomationScript)
        .where(
            AutomationScript.project_id == project_id,
            AutomationScript.script_type == "playwright",
        )
        .order_by(AutomationScript.created_at, AutomationScript.script_id)
    )).scalars().all()
    if rows:
        # Two concurrent first-runs raced the create and both committed an
        # anchor. Keep the earliest deterministically and drop the extras -
        # but only extras that never anchored a run (ScriptRun.script_id is a
        # NOT NULL FK; an anchor with runs stays load-bearing forever).
        row = rows[0]
        runnable = set((await db.execute(
            select(ScriptRun.script_id).where(ScriptRun.script_id.in_([a.script_id for a in rows]))
        )).scalars().all())
        stale = [a for a in rows[1:] if a.script_id not in runnable]
        if stale:
            for extra in stale:
                await db.delete(extra)
            await db.commit()
        return row
    row = AutomationScript(
        project_id=project_id,
        name=_PLAYWRIGHT_ANCHOR_NAME,
        description="Internal anchor for Playwright test runs",
        script_type="playwright",
        content="",
        created_by=created_by,
    )
    db.add(row)
    await db.commit()
    return row


@router.get("/projects/{project_id}/playwright/specs")
async def list_playwright_specs(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """Discover QA-generated specs in tests/generated/ (file stem = issue slug)."""
    from api.paths import resolve_project_fs_path

    project_fs = await resolve_project_fs_path(project_id, db)
    specs_dir = Path(project_fs) / "tests" / "generated"
    if not specs_dir.is_dir():
        return []
    specs = []
    for f in sorted(specs_dir.glob("*.spec.ts")):
        slug = f.name[: -len(".spec.ts")]
        if not _PW_SLUG_RE.match(slug):
            continue  # not a QA-generated name; skip rather than guess
        title = slug
        try:
            with f.open("rb") as fp:
                head = fp.read(4096).decode(errors="replace")
            m = _PW_SPEC_TITLE_RE.search(head)
            if m:
                title = m.group(1)
        except OSError:
            pass
        specs.append({"slug": slug, "file": f"tests/generated/{f.name}", "title": title})
    return specs


@router.post("/projects/{project_id}/playwright/specs/{slug}/run")
async def run_playwright_spec(
    project_id: str,
    slug: str,
    body: PlaywrightRunCreate | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    if not _PW_SLUG_RE.match(slug):
        raise HTTPException(status_code=422, detail="Invalid spec slug")
    request_env = _validated_playwright_env(body.env if body else {})

    async def prepare(db: AsyncSession):
        # Authorized inside the slot: a route-level authorize_project
        # dependency would query the DB before the slot is acquired, pinning
        # a pool connection for every queued request (the same contract
        # run_script keeps).
        await authorize_project(project_id, db, current_user)
        anchor = await _get_or_create_playwright_anchor(db, project_id, current_user.user_id)

        def build(run_id: str, project_fs: str):
            _resolve_playwright_spec(project_fs, slug)
            return _execute_playwright(run_id, slug, request_env, current_user.user_id, project_fs)

        return anchor, build

    return await _spawn_script_run(db, current_user, prepare)


def _on_script_task_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        import logging
        logging.getLogger("agents_universe.scripts").error("Script execution failed: %s", exc, exc_info=exc)


# ── Playwright executor ──────────────────────────────────────────────────────

def _playwright_test_cmd(tests_dir: Path, slug: str) -> list[str]:
    """Prefer the per-issue npm script test_generator injects
    (`test:{slug}`); fall back to passing the spec path when the entry is
    missing (hand-written specs). Both run the project's local
    @playwright/test runner - never bare `npx playwright test`, the
    `playwright` package is a different thing without the test subcommand."""
    npm = shutil.which("npm") or "npm"
    try:
        package = json.loads((tests_dir / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if isinstance(scripts, dict) and f"test:{slug}" in scripts:
            return [npm, "run", f"test:{slug}"]
    except (OSError, ValueError):
        pass
    return [npm, "test", "--", f"generated/{slug}.spec.ts"]


async def _execute_playwright(
    run_id: str, slug: str, request_env: dict[str, str],
    triggered_by: str = "", project_fs: str = "",
) -> None:
    """Run a QA-generated Playwright spec through the project's local runner.

    Node has no runtime file guard (the sitecustomize audit hook only covers
    Python interpreters), so containment is: cwd pinned to the project's
    tests/ directory, credential-like env keys stripped, explicit argv with
    no shell, and the process tree killed on timeout so chromium children
    cannot outlive the run. This matches the trust model of the agent-side
    shell tool running `npm run test:{slug}`.

    Long silent phases (a cold project's dependency install, browser
    download) append progress markers to the run's stderr so the WS log view
    shows what is happening; the subprocess output itself streams into the
    run row every couple of seconds.
    """
    from api.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ScriptRun).where(ScriptRun.run_id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            return

        run.status = "running"
        await db.commit()

        # Accumulator, not ORM reads: rollback would expire the instance and
        # reading an attribute back under the async driver raises
        # MissingGreenlet. Only assignment is used below.
        log_acc: list[str] = []

        async def progress(text: str) -> None:
            log_acc.append(f"[executor] {text}\n")
            _persist_run_log(run, log_acc)
            await db.commit()

        async def fail(message: str) -> None:
            log_acc.append(f"[executor] {message}\n")
            _persist_run_log(run, log_acc)
            run.status = "failed"
            run.exit_code = -1
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()

        try:
            tests_dir = Path(project_fs) / "tests"
            if not (tests_dir / "package.json").is_file():
                await fail("tests/package.json not found - let the QA agent generate tests first")
                return

            env = _sandbox_env()
            env.update(request_env)

            # Phase 1: dependencies. ensure_node_deps runs npm install only
            # when the local playwright/tsc shims are missing - a warm project
            # skips this in seconds.
            await progress("Preparing test dependencies (a first run installs them and can take a minute or two)...")
            from agent_core.tools.shell import ensure_node_deps
            install_error = await ensure_node_deps(
                f"npm run test:{slug}", str(tests_dir), project_fs, env,
            )
            if install_error:
                await fail(f"Dependency setup failed: {install_error}")
                return
            await progress("Dependencies ready")

            # Phase 2: browser preflight. Idempotent - downloads only what
            # PLAYWRIGHT_BROWSERS_PATH is missing, which covers projects whose
            # scaffold pins an older @playwright/test than the container's
            # chromium build. --no-install forces the local node_modules shim.
            await progress("Verifying browser binaries...")
            npx = shutil.which("npx") or "npx"
            try:
                code = await _stream_subprocess(
                    db, run, log_acc,
                    [npx, "--no-install", "playwright", "install", "chromium"],
                    str(tests_dir), env, _PLAYWRIGHT_BROWSER_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await fail(f"Browser verification timed out ({_PLAYWRIGHT_BROWSER_TIMEOUT}s)")
                return
            if code != 0:
                await fail(f"Browser verification failed (exit code {code})")
                return
            await progress("Browser ready")

            # Phase 3: the test run itself. Artifacts (screenshots, videos,
            # traces) stay in tests/test-results/ - surfacing them in the UI
            # would need run-scoped media, deliberately out of scope here.
            await progress("Running tests...")
            cmd = _playwright_test_cmd(tests_dir, slug)
            try:
                code = await _stream_subprocess(
                    db, run, log_acc,
                    cmd, str(tests_dir), env, _PLAYWRIGHT_TEST_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await fail(f"Test execution timed out ({_PLAYWRIGHT_TEST_TIMEOUT}s)")
                return
            _persist_run_log(run, log_acc)
            run.exit_code = code
            run.status = "completed" if code == 0 else "failed"
        except asyncio.CancelledError:
            # Persist a terminal state before propagating - a run left at
            # "running" blocks project deletion with a phantom job.
            run.status = "failed"
            run.stderr_log = "Execution cancelled"
            run.exit_code = -1
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            raise
        except Exception as e:
            run.status = "failed"
            run.stderr_log = str(e)[:_STDERR_CAP]
            run.exit_code = -1

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
        # Poll must outlast the execution budgets: a Playwright run allows
        # 120s deps + 180s browser preflight + 540s tests, so a shorter loop
        # would silently close the socket while the run is still going (the
        # client would never see the final "done" event). Poll for 900s.
        for _ in range(_RUN_POLL_LOOPS):
            not_found = False
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ScriptRun)
                    .join(AutomationScript, AutomationScript.script_id == ScriptRun.script_id)
                    .join(Project, Project.project_id == AutomationScript.project_id)
                    .where(
                        ScriptRun.run_id == run_id,
                        # Re-assert ownership on every poll, not just at
                        # connect: the socket outlives the run and must keep
                        # refusing a run that was re-parented mid-stream.
                        ScriptRun.triggered_by == user_id,
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

            await asyncio.sleep(_RUN_POLL_INTERVAL)
    except WebSocketDisconnect:
        pass
    finally:
        await ws.close()
