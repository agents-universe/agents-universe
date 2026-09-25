"""Executor-level tests: real sandboxed script runs and Playwright phases."""
from __future__ import annotations

import json
from pathlib import Path

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

    async def fake_stream(db_, run_, log_acc, cmd, cwd, env, timeout, tail_acc=None):
        log_acc.append(f"ran {' '.join(cmd)}\n")
        return 0

    async def fake_probe(base_url):
        # The placeholder host does not resolve; reachability is covered by its
        # own tests, and this one is about the phase markers.
        return ""

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)
    monkeypatch.setattr(scripts_router, "_stream_subprocess", fake_stream)
    monkeypatch.setattr(scripts_router, "_probe_target", fake_probe)

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


def _json_report(attachment_path: Path) -> dict:
    """The slice of a Playwright JSON report the capture path reads."""
    return {
        "config": {},
        "suites": [
            {
                "title": "login-1.spec.ts",
                "file": "tests/generated/login-1.spec.ts",
                "specs": [
                    {
                        "title": "signs in",
                        "line": 4,
                        "tests": [
                            {
                                # A report test entry's verdict: Playwright's
                                # outcome enum under `status`.
                                "status": "unexpected",
                                "results": [
                                    {
                                        "duration": 340,
                                        "errors": [{"message": "Error: expected 200, got 401"}],
                                        "attachments": [
                                            {
                                                "name": "screenshot",
                                                "path": str(attachment_path),
                                                "contentType": "image/png",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "stats": {"expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0, "duration": 340},
    }


async def _seed_playwright_run(db, project, **kwargs):
    from api.paths import PROJECTS_ROOT

    project_fs = PROJECTS_ROOT / project.slug
    tests_dir = project_fs / "tests"
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
    run = ScriptRun(script_id=script.script_id, status="pending", triggered_by="test-user")
    for key, value in kwargs.items():
        setattr(run, key, value)
    db.add(run)
    await db.commit()
    return run, project_fs


async def _reload_run(run_id: str) -> ScriptRun:
    from api.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(ScriptRun).where(ScriptRun.run_id == run_id)
        )).scalar_one()


@pytest.mark.asyncio
async def test_execute_playwright_captures_result_and_artifacts(
    client, db, make_project, monkeypatch
):
    """What the run panel needs after a run: the reporter's verdict on the row,
    the attachments it wrote, and a manifest to serve them from - everything
    under a run-scoped directory, since Playwright empties its own output
    folders at the start of the next run."""
    project = await make_project("exec-pw-result")
    run, project_fs = await _seed_playwright_run(db, project, spec_slug="login-1")
    run_id = str(run.run_id)

    calls: list[tuple[list[str], dict[str, str]]] = []

    async def fake_deps(*args, **kwargs):
        return None

    async def fake_stream(db_, run_, log_acc, cmd, cwd, env, timeout, tail_acc=None):
        calls.append((list(cmd), dict(env)))
        if "--reporter" not in cmd:
            return 0
        artifacts = Path(env["PLAYWRIGHT_JSON_OUTPUT_DIR"])
        shot = artifacts / "test-results" / "login-fails" / "test-failed-1.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
        (artifacts / "results.json").write_text(
            json.dumps(_json_report(shot)), encoding="utf-8"
        )
        # The uncapped tail, which is what survives a 100k-capped log.
        tail_acc.append("1 failed\n1 passed (4.6s)\n")
        return 1

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)
    monkeypatch.setattr(scripts_router, "_stream_subprocess", fake_stream)

    await scripts_router._execute_playwright(
        run_id, "login-1", {}, "test-user", str(project_fs),
    )

    refreshed = await _reload_run(run_id)
    assert refreshed.status == "failed" and refreshed.exit_code == 1
    assert refreshed.spec_slug == "login-1"
    assert refreshed.completed_at is not None

    result = json.loads(refreshed.result_json)
    assert result["source"] == "json" and result["partial"] is False
    assert result["counts"] == {"total": 1, "passed": 0, "failed": 1, "flaky": 0, "skipped": 0}
    assert result["failed_tests"][0]["title"] == "signs in"

    # Both reporters and the attachment directory ride on the test command; the
    # JSON reporter's own destination is env-only, so it is set there instead.
    test_cmd, test_env = next(call for call in calls if "--reporter" in call[0])
    assert test_cmd[test_cmd.index("--reporter") + 1] == "list,html,json"
    artifacts = Path(test_env["PLAYWRIGHT_JSON_OUTPUT_DIR"])
    assert test_cmd[test_cmd.index("--output") + 1] == str(artifacts / "test-results")
    assert artifacts.is_relative_to(project_fs) and run_id in artifacts.parts
    assert test_env["PLAYWRIGHT_HTML_REPORT"] == str(artifacts / "html-report")
    assert test_env["PLAYWRIGHT_HTML_OPEN"] == "never"

    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["rel_path"] == "test-results/login-fails/test-failed-1.png"
    assert manifest["artifacts"][0]["test_title"] == "signs in"
    assert manifest["artifacts"][0]["kind"] == "screenshot"


@pytest.mark.asyncio
async def test_execute_playwright_falls_back_to_the_log_verdict(
    client, db, make_project, monkeypatch
):
    """No JSON report (an old runner, or a project config winning over the env
    vars): the uncapped tail still yields counts, flagged as best-effort."""
    project = await make_project("exec-pw-log-only")
    run, project_fs = await _seed_playwright_run(db, project, spec_slug="login-2")
    run_id = str(run.run_id)

    async def fake_deps(*args, **kwargs):
        return None

    async def fake_stream(db_, run_, log_acc, cmd, cwd, env, timeout, tail_acc=None):
        if "--reporter" in cmd:
            tail_acc.append("  1) login-2.spec.ts:12:3 › rejects a bad password ──\n")
            tail_acc.append("\x1b[31m1 failed\x1b[0m\n\x1b[32m2 passed\x1b[0m (4.6s)\n")
            return 1
        return 0

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)
    monkeypatch.setattr(scripts_router, "_stream_subprocess", fake_stream)

    await scripts_router._execute_playwright(
        run_id, "login-2", {}, "test-user", str(project_fs),
    )

    result = json.loads((await _reload_run(run_id)).result_json)
    assert result["source"] == "log" and result["partial"] is True
    assert result["status"] == "failed"
    assert result["counts"] == {"total": 3, "passed": 2, "failed": 1, "flaky": 0, "skipped": 0}
    assert result["duration_ms"] == 4600
    assert result["failed_tests"][0]["title"].startswith("login-2.spec.ts:12:3")


@pytest.mark.asyncio
async def test_execute_playwright_records_a_verdict_when_startup_fails(
    client, db, make_project, monkeypatch
):
    """A run that dies in phase 1 never reaches the reporter, but the row must
    still say why - an empty card was the original complaint."""
    project = await make_project("exec-pw-nodeps")
    run, project_fs = await _seed_playwright_run(db, project, spec_slug="login-3")
    run_id = str(run.run_id)

    async def fake_deps(*args, **kwargs):
        return "npm install exploded"

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)

    await scripts_router._execute_playwright(
        run_id, "login-3", {}, "test-user", str(project_fs),
    )

    refreshed = await _reload_run(run_id)
    assert refreshed.status == "failed" and refreshed.exit_code == -1
    result = json.loads(refreshed.result_json)
    assert result["status"] == "failed" and result["source"] == "log"


@pytest.mark.asyncio
async def test_execute_playwright_fails_fast_when_the_target_is_unreachable(
    client, db, make_project, monkeypatch
):
    """A dead target otherwise fails one navigation per case and burns the whole
    suite budget, and the report reads as an application bug rather than an
    unreachable environment."""
    project = await make_project("exec-pw-unreachable")
    run, project_fs = await _seed_playwright_run(db, project, spec_slug="login-4")
    run_id = str(run.run_id)

    commands: list[list[str]] = []

    async def fake_deps(*args, **kwargs):
        return None

    async def fake_stream(db_, run_, log_acc, cmd, cwd, env, timeout, tail_acc=None):
        commands.append(list(cmd))
        return 0

    probed: list[str] = []

    async def fake_probe(base_url):
        probed.append(base_url)
        return "ConnectError: [Errno 111] Connection refused"

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)
    monkeypatch.setattr(scripts_router, "_stream_subprocess", fake_stream)
    monkeypatch.setattr(scripts_router, "_probe_target", fake_probe)

    await scripts_router._execute_playwright(
        run_id, "login-4", {"APP_BASE_URL": "https://qa.example.com"},
        "test-user", str(project_fs),
    )

    refreshed = await _reload_run(run_id)
    assert refreshed.status == "failed"
    assert probed == ["https://qa.example.com"]
    # The suite never launched: only the browser preflight reached a subprocess.
    assert not any("--reporter" in cmd for cmd in commands), commands
    assert "qa.example.com" in refreshed.stdout_log
    assert "unreachable" in refreshed.stdout_log.lower()


@pytest.mark.asyncio
async def test_execute_playwright_runs_the_suite_when_the_target_answers(
    client, db, make_project, monkeypatch
):
    project = await make_project("exec-pw-reachable")
    run, project_fs = await _seed_playwright_run(db, project, spec_slug="login-5")
    run_id = str(run.run_id)

    commands: list[list[str]] = []

    async def fake_deps(*args, **kwargs):
        return None

    async def fake_stream(db_, run_, log_acc, cmd, cwd, env, timeout, tail_acc=None):
        commands.append(list(cmd))
        return 0

    async def fake_probe(base_url):
        return ""

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)
    monkeypatch.setattr(scripts_router, "_stream_subprocess", fake_stream)
    monkeypatch.setattr(scripts_router, "_probe_target", fake_probe)

    await scripts_router._execute_playwright(
        run_id, "login-5", {"APP_BASE_URL": "https://qa.example.com"},
        "test-user", str(project_fs),
    )

    assert any("--reporter" in cmd for cmd in commands), commands


@pytest.mark.asyncio
async def test_execute_playwright_skips_the_probe_without_app_base_url(
    client, db, make_project, monkeypatch
):
    """Specs may address absolute URLs; failing a run over a host the suite never
    visits would be worse than the slow failure the probe guards against."""
    project = await make_project("exec-pw-nobase")
    run, project_fs = await _seed_playwright_run(db, project, spec_slug="login-6")
    run_id = str(run.run_id)

    async def fake_deps(*args, **kwargs):
        return None

    async def fake_stream(db_, run_, log_acc, cmd, cwd, env, timeout, tail_acc=None):
        return 0

    async def fake_probe(base_url):
        raise AssertionError("probe must not run without APP_BASE_URL")

    monkeypatch.setattr("agent_core.tools.shell.ensure_node_deps", fake_deps)
    monkeypatch.setattr(scripts_router, "_stream_subprocess", fake_stream)
    monkeypatch.setattr(scripts_router, "_probe_target", fake_probe)

    await scripts_router._execute_playwright(
        run_id, "login-6", {}, "test-user", str(project_fs),
    )

    assert (await _reload_run(run_id)).status == "completed"


@pytest.mark.asyncio
async def test_startup_sweep_settles_stranded_script_runs(client, db, make_project):
    """A hard kill (SIGKILL/OOM) skips the executor's CancelledError handler,
    leaving ScriptRun rows pending/running forever — nothing else can settle
    them, and project deletion 409s on them permanently. The startup sweep
    must fail them the way the scheduler sweep settles ScheduledTaskRun."""
    from api.routers.scripts import startup_sweep

    project = await make_project("sweep-scripts")
    script = AutomationScript(
        project_id=str(project.project_id),
        name="sweep-target",
        script_type="python",
        content="pass",
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)

    stranded_pending = ScriptRun(script_id=script.script_id, status="pending")
    stranded_running = ScriptRun(script_id=script.script_id, status="running")
    finished = ScriptRun(
        script_id=script.script_id, status="completed", exit_code=0
    )
    db.add_all([stranded_pending, stranded_running, finished])
    await db.commit()

    settled = await startup_sweep(db)

    # >= 2: other tests may leave their own pending/running rows behind in the
    # shared DB; this test only owns the two rows it created.
    assert settled >= 2
    await db.refresh(stranded_pending)
    await db.refresh(stranded_running)
    await db.refresh(finished)
    for stranded in (stranded_pending, stranded_running):
        assert stranded.status == "failed"
        assert stranded.exit_code == -1
        assert "restart" in (stranded.stderr_log or "")
        assert stranded.completed_at is not None
    assert finished.status == "completed"
