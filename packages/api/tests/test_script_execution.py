"""Executor-level tests: real sandboxed script runs and Playwright phases."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from api.models.script import AutomationScript, ScriptRun
from api.routers import scripts as scripts_router


@pytest.mark.asyncio
async def test_execute_script_streams_output_and_completes(client, db, make_project):
    """A real python run through the streaming executor: output lands in the
    run row (stdout and stderr interleaved in arrival order) and the run
    reaches a terminal state."""
    project = await make_project("exec-python")
    script = AutomationScript(
        project_id=str(project.project_id),
        name="hello",
        script_type="python",
        content="import sys\nprint('hello line')\nprint('warn line', file=sys.stderr)\n",
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)
    run = ScriptRun(script_id=script.script_id, status="pending")
    db.add(run)
    await db.commit()
    run_id = str(run.run_id)

    from api.paths import PROJECTS_ROOT

    await scripts_router._execute_script(
        run_id, script.content, "python", "test-user",
        str(PROJECTS_ROOT / project.slug),
    )

    # Fresh session: the fixture session's identity map still holds the
    # pre-run instance (select() does not refresh it by default).
    from api.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        refreshed = (await s.execute(
            select(ScriptRun).where(ScriptRun.run_id == run_id)
        )).scalar_one()
    assert refreshed.status == "completed"
    assert refreshed.exit_code == 0
    assert "hello line" in (refreshed.stdout_log or "")
    assert "warn line" in (refreshed.stdout_log or "")


@pytest.mark.asyncio
async def test_execute_playwright_reports_phase_progress(client, db, make_project, monkeypatch):
    """The Playwright executor appends phase markers so the WS log view shows
    progress during the long silent phases; the test command's exit code
    decides the final status."""
    project = await make_project("exec-pw")
    from api.paths import PROJECTS_ROOT

    tests_dir = PROJECTS_ROOT / project.slug / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "package.json").write_text("{}", encoding="utf-8")

    script = AutomationScript(
        project_id=str(project.project_id),
        name="__playwright__",
        script_type="playwright",
        content="",
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)
    run = ScriptRun(script_id=script.script_id, status="pending")
    db.add(run)
    await db.commit()
    run_id = str(run.run_id)

    async def fake_deps(*args, **kwargs):
        return None

    async def fake_stream(db_, run_, log_acc, cmd, cwd, env, timeout):
        log_acc.append(f"ran {' '.join(cmd)}\n")
        return 0

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)
    monkeypatch.setattr(scripts_router, "_stream_subprocess", fake_stream)

    await scripts_router._execute_playwright(
        run_id, "some-issue", {"APP_BASE_URL": "http://x"}, "test-user",
        str(PROJECTS_ROOT / project.slug),
    )

    from api.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        refreshed = (await s.execute(
            select(ScriptRun).where(ScriptRun.run_id == run_id)
        )).scalar_one()
    assert refreshed.status == "completed"
    assert refreshed.exit_code == 0
    log = refreshed.stdout_log or ""
    # Phase markers arrive in order - the log is append-only for the WS diff.
    markers = ["Preparing test dependencies", "Dependencies ready", "Verifying browser", "Browser ready", "Running tests"]
    positions = [log.index(m) for m in markers]
    assert positions == sorted(positions)
    # The test command targets the spec (npm run test:{slug} when the
    # package.json has it, npm test -- generated/{slug}.spec.ts otherwise).
    assert "generated/some-issue.spec.ts" in log
