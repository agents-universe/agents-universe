"""ScriptWriterTool tests: persisted script CRUD + sandboxed runs.

The tool reaches its DB models through a lazy ``from api.models.script
import ...`` (the knowledge/index.py pattern) — the agent-core test env has no
``api`` package, so these tests inject a stub module into ``sys.modules`` with
locally-defined declarative models mirroring the real columns.

Run tests execute REAL sandboxed subprocesses (python via the audit-hook
guard, bash gated by the blocked-command regex). The engine is FILE-backed
(like the API suite's test.db): the runner and the poll open their own
sessions, so every session must see the others' commits — ``:memory:`` would
give each session a private database.
"""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import DateTime, Integer, String, Unicode, UnicodeText
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from agent_core.tools.base import ToolContext
from agent_core.tools.script_writer import ScriptWriterTool


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _StubBase(DeclarativeBase):
    pass


class AutomationScript(_StubBase):
    __tablename__ = "automation_scripts"
    script_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(Unicode(255))
    description: Mapped[str | None] = mapped_column(Unicode(2000), nullable=True)
    script_type: Mapped[str] = mapped_column(String(50), default="python")
    content: Mapped[str] = mapped_column(UnicodeText)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScriptRun(_StubBase):
    __tablename__ = "script_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    script_id: Mapped[str] = mapped_column(String(36))
    triggered_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_log: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    stderr_log: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


@pytest.fixture
async def script_env(tmp_path, monkeypatch):
    """(tool, context, session, project_dir) with stubbed api.models.script.

    Also speeds up the run-op poll so quick script executions resolve in one
    tick instead of waiting the production 2s cadence."""
    import agent_core.tools.script_writer as sw

    monkeypatch.setattr(sw, "_POLL_INTERVAL", 0.05)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(_StubBase.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    stubs = types.SimpleNamespace(AutomationScript=AutomationScript, ScriptRun=ScriptRun)
    monkeypatch.setitem(sys.modules, "api.models.script", stubs)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    async with factory() as session:
        context = ToolContext(
            project_id="p1",
            project_fs_path=str(project_dir),
            conversation_id="c1",
            user_id="u1",
            db_session=session,
            db_session_factory=factory,
        )
        yield ScriptWriterTool(), context, session, project_dir
    await engine.dispose()


async def _create(tool, context, **overrides):
    params = {
        "operation": "create",
        "name": "daily-summary",
        "script_type": "python",
        "description": "Summarize the day",
        "content": "print('hi')",
    }
    params.update(overrides)
    return await tool.execute(params, context)


# ── create / list / get / update ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_persists_all_fields(script_env):
    tool, context, session, _ = script_env
    result = await _create(tool, context)
    assert result["success"] is True
    script_id = result["script_id"]

    from sqlalchemy import select

    row = (
        (await session.execute(
            select(AutomationScript).where(AutomationScript.script_id == script_id)
        )).scalar_one()
    )
    assert row.project_id == "p1"
    assert row.name == "daily-summary"
    assert row.script_type == "python"
    assert row.description == "Summarize the day"
    assert row.content == "print('hi')"
    assert row.created_by == "u1"


@pytest.mark.asyncio
async def test_create_rejects_invalid_input(script_env):
    tool, context, _, _ = script_env
    assert (await _create(tool, context, name=""))["error"]
    assert (await _create(tool, context, name="x" * 300))["error"]
    assert (await _create(tool, context, content=""))["error"]
    assert (await _create(tool, context, script_type="powershell"))["error"]
    assert (await _create(tool, context, description="d" * 3000))["error"]


@pytest.mark.asyncio
async def test_list_scoped_and_excludes_playwright(script_env):
    tool, context, session, _ = script_env
    await _create(tool, context, name="a")
    await _create(tool, context, name="b", script_type="bash")
    # The hidden Playwright anchor row must never surface to the agent.
    session.add(AutomationScript(
        project_id="p1", name="__playwright__", script_type="playwright", content="",
    ))
    # A script from another project must not leak in either.
    session.add(AutomationScript(
        project_id="p-other", name="other", script_type="python", content="x",
    ))
    await session.commit()

    result = await tool.execute({"operation": "list"}, context)
    assert result["count"] == 2
    names = {s["name"] for s in result["scripts"]}
    assert names == {"a", "b"}
    assert all(s["script_type"] in ("python", "bash") for s in result["scripts"])


@pytest.mark.asyncio
async def test_get_roundtrip_and_isolation(script_env):
    tool, context, _, _ = script_env
    created = await _create(tool, context, content="import os\nprint(os.getcwd())")
    script_id = created["script_id"]

    got = await tool.execute({"operation": "get", "script_id": script_id}, context)
    assert got["content"] == "import os\nprint(os.getcwd())"
    assert got["name"] == "daily-summary"

    missing = await tool.execute({"operation": "get", "script_id": "missing"}, context)
    assert "error" in missing
    other = types.SimpleNamespace(project_id="p-other", db_session=context.db_session,
                                  db_session_factory=None, project_fs_path="",
                                  conversation_id="c1", user_id="u1")
    isolated = await tool.execute({"operation": "get", "script_id": script_id}, other)
    assert "error" in isolated


@pytest.mark.asyncio
async def test_update_fields_partial_and_isolation(script_env):
    tool, context, _, _ = script_env
    created = await _create(tool, context)
    script_id = created["script_id"]

    updated = await tool.execute(
        {"operation": "update", "script_id": script_id, "content": "print('v2')", "name": "renamed"},
        context,
    )
    assert updated["success"] is True
    got = await tool.execute({"operation": "get", "script_id": script_id}, context)
    assert got["content"] == "print('v2')"
    assert got["name"] == "renamed"
    assert got["description"] == "Summarize the day"  # untouched

    from sqlalchemy import select

    row = (
        (await context.db_session.execute(
            select(AutomationScript).where(AutomationScript.script_id == script_id)
        )).scalar_one()
    )
    assert row.updated_at is not None

    assert (await tool.execute({"operation": "update", "script_id": "missing", "content": "x"}, context))["error"]
    assert (await tool.execute({"operation": "update", "script_id": script_id}, context))["error"]


# ── run ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_python_success(script_env):
    tool, context, _, project_dir = script_env
    created = await _create(
        tool, context,
        content="from pathlib import Path\nPath('out.txt').write_text('written', encoding='utf-8')\nprint('script ran ok')\n",
    )
    result = await tool.execute({"operation": "run", "script_id": created["script_id"]}, context)
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "script ran ok" in result["log_tail"]
    assert (project_dir / "out.txt").read_text(encoding="utf-8") == "written"


@pytest.mark.asyncio
async def test_run_python_popen_blocked(script_env):
    tool, context, _, _ = script_env
    created = await _create(
        tool, context,
        content="import subprocess\nsubprocess.Popen(['echo', 'hi'])\n",
    )
    result = await tool.execute({"operation": "run", "script_id": created["script_id"]}, context)
    assert result["success"] is True
    assert result["status"] == "failed"
    # The guard fires inside the child — the script crashes with its own
    # exit code (1); the traceback is streamed into the combined log
    # (stdout+stderr interleave in the run row, like the API test expects).
    assert result["exit_code"] != 0
    assert "agent-guard" in result["log_tail"]


@pytest.mark.asyncio
async def test_run_bash_curl_blocked(script_env):
    tool, context, _, _ = script_env
    created = await _create(
        tool, context, script_type="bash",
        content="curl http://example.com\n",
    )
    result = await tool.execute({"operation": "run", "script_id": created["script_id"]}, context)
    assert result["success"] is True
    assert result["status"] == "failed"
    assert "blocked command" in result["stderr_tail"]


@pytest.mark.asyncio
async def test_run_bash_path_escape(script_env):
    tool, context, _, _ = script_env
    created = await _create(
        tool, context, script_type="bash",
        content="cat ../secret.txt\n",
    )
    result = await tool.execute({"operation": "run", "script_id": created["script_id"]}, context)
    assert result["success"] is True
    assert result["status"] == "failed"
    assert result["exit_code"] == -1
    assert ".." in result["stderr_tail"]


@pytest.mark.asyncio
async def test_run_missing_script_error(script_env):
    tool, context, _, _ = script_env
    result = await tool.execute({"operation": "run", "script_id": "nope"}, context)
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_run_timeout(script_env, monkeypatch):
    import agent_core.scripts.runner as runner

    monkeypatch.setattr(runner, "SCRIPT_TIMEOUT", 1)
    tool, context, _, _ = script_env
    created = await _create(tool, context, content="import time\ntime.sleep(5)\n")
    result = await tool.execute({"operation": "run", "script_id": created["script_id"]}, context)
    assert result["success"] is True
    assert result["status"] == "failed"
    assert "timed out" in result["stderr_tail"]


@pytest.mark.asyncio
async def test_run_blocks_on_full_slot(script_env, monkeypatch):
    """A full slot gate must block the tool call until a slot frees (the same
    contract as the HTTP route: request queued, not rejected)."""
    import agent_core.tools.script_writer as sw

    tool, context, _, _ = script_env
    created = await _create(tool, context)
    gate = asyncio.Semaphore(0)
    monkeypatch.setattr(sw, "script_slot_guard", lambda: gate)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            tool.execute({"operation": "run", "script_id": created["script_id"]}, context),
            timeout=0.3,
        )

    gate.release()
    result = await tool.execute({"operation": "run", "script_id": created["script_id"]}, context)
    assert result["status"] == "completed"
    assert gate.locked() is False  # slot released by the run's done_callback
