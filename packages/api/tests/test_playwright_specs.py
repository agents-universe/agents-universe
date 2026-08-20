"""Playwright spec discovery + run endpoint contracts."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from api.models.script import AutomationScript, ScriptRun
from api.routers import scripts as scripts_router


async def _noop_execute(*args, **kwargs):
    pass


def _write_spec(project, slug: str, title: str | None = None) -> None:
    from api.paths import PROJECTS_ROOT

    specs_dir = PROJECTS_ROOT / project.slug / "tests" / "generated"
    specs_dir.mkdir(parents=True, exist_ok=True)
    body = "import { test } from '@playwright/test';\n"
    if title:
        body += f"test.describe('{title}', () => {{}});\n"
    (specs_dir / f"{slug}.spec.ts").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_list_specs_empty(client, make_project):
    project = await make_project("pw-empty")
    resp = await client.get(f"/api/projects/{project.project_id}/playwright/specs")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_specs_discovers_and_parses_titles(client, make_project):
    project = await make_project("pw-list")
    _write_spec(project, "alpha-1", "Alpha login flow")
    _write_spec(project, "beta-2")  # no test.describe -> title falls back to slug
    _write_spec(project, "not_a_slug")  # underscore fails the slug regex -> skipped
    resp = await client.get(f"/api/projects/{project.project_id}/playwright/specs")
    assert resp.status_code == 200
    specs = resp.json()
    assert [s["slug"] for s in specs] == ["alpha-1", "beta-2"]
    assert specs[0]["title"] == "Alpha login flow"
    assert specs[0]["file"] == "tests/generated/alpha-1.spec.ts"
    assert specs[1]["title"] == "beta-2"


@pytest.mark.asyncio
async def test_run_rejects_invalid_slug(client, make_project):
    project = await make_project("pw-slug")
    resp = await client.post(f"/api/projects/{project.project_id}/playwright/specs/UPPER/run")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_rejects_bad_env_without_side_effects(client, db, make_project):
    """Non-APP_* keys are refused before any slot/anchor work happens."""
    project = await make_project("pw-env")
    resp = await client.post(
        f"/api/projects/{project.project_id}/playwright/specs/some-issue/run",
        json={"env": {"FOO": "bar"}},
    )
    assert resp.status_code == 422

    result = await db.execute(
        select(AutomationScript).where(
            AutomationScript.project_id == str(project.project_id),
            AutomationScript.script_type == "playwright",
        )
    )
    assert result.scalars().first() is None


@pytest.mark.asyncio
async def test_run_missing_spec_marks_failed_run(client, db, make_project, monkeypatch):
    project = await make_project("pw-missing")
    monkeypatch.setattr(scripts_router, "_execute_playwright", _noop_execute)
    resp = await client.post(
        f"/api/projects/{project.project_id}/playwright/specs/gone-1/run"
    )
    assert resp.status_code == 404

    # The run row exists in a terminal state - a pending leftover would block
    # project deletion forever.
    result = await db.execute(
        select(ScriptRun).join(AutomationScript).where(
            AutomationScript.project_id == str(project.project_id),
        )
    )
    run = result.scalars().first()
    assert run is not None and run.status == "failed"


@pytest.mark.asyncio
async def test_run_creates_anchor_and_run(client, db, make_project, monkeypatch):
    project = await make_project("pw-run")
    _write_spec(project, "proj-456", "Login flow")
    monkeypatch.setattr(scripts_router, "_execute_playwright", _noop_execute)

    resp = await client.post(
        f"/api/projects/{project.project_id}/playwright/specs/proj-456/run",
        json={"env": {"APP_BASE_URL": "http://localhost:3000"}},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    anchor_result = await db.execute(
        select(AutomationScript).where(
            AutomationScript.project_id == str(project.project_id),
            AutomationScript.script_type == "playwright",
        )
    )
    anchor = anchor_result.scalars().first()
    assert anchor is not None

    run_result = await db.execute(select(ScriptRun).where(ScriptRun.run_id == run_id))
    run = run_result.scalar_one()
    assert run.script_id == anchor.script_id
    assert run.status == "pending"
    assert run.triggered_by == "test-user"

    # The anchor never appears in the user-visible script list.
    list_resp = await client.get(f"/api/projects/{project.project_id}/scripts")
    assert list_resp.status_code == 200
    assert all(s["script_id"] != str(anchor.script_id) for s in list_resp.json())


@pytest.mark.asyncio
async def test_run_script_rejects_playwright_anchor(client, db, make_project):
    """The internal anchor row is not runnable through the plain script endpoint."""
    project = await make_project("pw-anchor")
    anchor = AutomationScript(
        project_id=str(project.project_id),
        name="__playwright__",
        script_type="playwright",
        content="",
    )
    db.add(anchor)
    await db.commit()
    await db.refresh(anchor)

    resp = await client.post(f"/api/scripts/{anchor.script_id}/run")
    assert resp.status_code == 404
