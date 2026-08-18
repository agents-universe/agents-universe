"""Regression: run_script acquires its concurrency slot BEFORE any DB work.

The old order ran db.execute + authorize_project before awaiting the slot
semaphore — with 3 long scripts running, every queued request pinned a pool
connection while waiting, which could exhaust the shared pool (size 10 /
overflow 20) and stall unrelated endpoints.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from api.models.script import AutomationScript, ScriptRun
from api.routers import scripts as scripts_router


async def _noop_execute(*args, **kwargs):
    pass


@pytest.mark.asyncio
async def test_run_script_blocks_before_db_when_slots_full(client, monkeypatch):
    """A full slot gate must block even a request for a nonexistent script —
    proving acquire() runs before the script lookup (404/DB path)."""
    gate = asyncio.Semaphore(0)  # no free slots
    monkeypatch.setattr(scripts_router, "_script_slot_guard", lambda: gate)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.post("/api/scripts/nope/run"), timeout=0.2)

    # Once a slot frees, the same request proceeds to the normal 404 — and
    # releases the slot again (a leaked token would silently shrink the gate).
    gate.release()
    resp = await client.post("/api/scripts/nope/run")
    assert resp.status_code == 404
    assert gate.locked() is False


@pytest.mark.asyncio
async def test_run_script_releases_slot_when_task_done(client, db, make_project, monkeypatch):
    """The slot is held until the spawned task finishes, then released."""
    project = await make_project("slot-release")
    script = AutomationScript(
        project_id=str(project.project_id),
        name="echo",
        script_type="python",
        content="print(1)",
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)

    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(scripts_router, "_script_slot_guard", lambda: gate)
    monkeypatch.setattr(scripts_router, "_execute_script", _noop_execute)

    resp = await client.post(f"/api/scripts/{script.script_id}/run")
    assert resp.status_code == 200

    # The done_callback releases on the loop's next tick; poll briefly.
    for _ in range(50):
        if not gate.locked():
            break
        await asyncio.sleep(0.01)
    assert gate.locked() is False


@pytest.mark.asyncio
async def test_run_script_error_path_marks_failed_and_releases_slot(client, db, make_project, monkeypatch):
    """A failure after the run row is committed must persist a failed state
    (never a phantom pending run) and release the slot (3 leaked slots would
    block every later script request forever)."""
    project = await make_project("slot-error")
    script = AutomationScript(
        project_id=str(project.project_id),
        name="boom",
        script_type="python",
        content="print(1)",
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)
    script_id = str(script.script_id)

    import api.paths as paths

    async def _workspace_gone(*args, **kwargs):
        raise RuntimeError("workspace gone")

    monkeypatch.setattr(paths, "resolve_project_fs_path", _workspace_gone)

    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(scripts_router, "_script_slot_guard", lambda: gate)

    # ASGITransport raises unhandled app exceptions instead of returning 500.
    with pytest.raises(RuntimeError, match="workspace gone"):
        await client.post(f"/api/scripts/{script_id}/run")
    assert gate.locked() is False  # slot released on the error path

    from api.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        result = await s.execute(
            select(ScriptRun).where(ScriptRun.script_id == script_id)
        )
        run = result.scalar_one()
        assert run.status == "failed"
