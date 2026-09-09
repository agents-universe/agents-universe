"""SchedulerTool tests: cron validation, target scoping, CRUD, run_now.

The tool reaches its models through lazy ``from api.models... import ...``
statements (the script_writer pattern); the agent-core test env has no ``api``
package, so the fixtures inject stub modules into ``sys.modules``.
"""
from __future__ import annotations

import sys
import types
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import DateTime, String, Unicode, UnicodeText
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from agent_core.tools.base import ToolContext
from agent_core.tools.scheduler import SchedulerTool


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _StubBase(DeclarativeBase):
    pass


class ScheduledTask(_StubBase):
    __tablename__ = "scheduled_tasks"
    schedule_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(Unicode(255))
    description: Mapped[str | None] = mapped_column(Unicode(2000), nullable=True)
    target_type: Mapped[str] = mapped_column(String(20))
    script_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    spec_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    env_json: Mapped[str | None] = mapped_column(Unicode(2000), nullable=True)
    cron_expr: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    enabled: Mapped[bool] = mapped_column(default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    notify: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduledTaskRun(_StubBase):
    __tablename__ = "scheduled_task_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    schedule_id: Mapped[str] = mapped_column(String(36))
    project_id: Mapped[str] = mapped_column(String(36))
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AutomationScript(_StubBase):
    __tablename__ = "automation_scripts"
    script_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(Unicode(255))
    script_type: Mapped[str] = mapped_column(String(50), default="python")


class Conversation(_StubBase):
    __tablename__ = "conversations"
    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="active")


@pytest.fixture
async def sched_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(_StubBase.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    monkeypatch.setitem(sys.modules, "api.models.schedule", types.SimpleNamespace(
        ScheduledTask=ScheduledTask, ScheduledTaskRun=ScheduledTaskRun,
    ))
    monkeypatch.setitem(sys.modules, "api.models.script", types.SimpleNamespace(
        AutomationScript=AutomationScript,
    ))
    monkeypatch.setitem(sys.modules, "api.models.conversation", types.SimpleNamespace(
        Conversation=Conversation,
    ))
    monkeypatch.setitem(sys.modules, "api.services.agent_sync", types.SimpleNamespace(
        resolve_agent_definition_path=lambda slug, fs: f"/agents/{slug}.agent.md"
        if slug == "tech-lead" else None,
    ))

    project_dir = tmp_path / "project"
    (project_dir / "tests" / "generated").mkdir(parents=True)

    async with factory() as session:
        context = ToolContext(
            project_id="p1",
            project_fs_path=str(project_dir),
            conversation_id="c1",
            user_id="u1",
            db_session=session,
            db_session_factory=factory,
        )
        yield SchedulerTool(), context, session, project_dir
    await engine.dispose()


async def _create(tool, context, **overrides):
    params = {
        "operation": "create",
        "name": "nightly-report",
        "target_type": "script",
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
    }
    params.update(overrides)
    return await tool.execute(params, context)


async def _add_script(session, project_id: str = "p1", script_type: str = "python") -> AutomationScript:
    row = AutomationScript(project_id=project_id, name="s", script_type=script_type)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


class TestCreate:
    async def test_creates_script_task_with_next_run(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)

        result = await _create(tool, context, script_id=str(script.script_id))
        assert result["success"] is True
        assert result["target_type"] == "script"
        assert result["enabled"] is True
        # 09:00 Asia/Shanghai is 01:00 UTC.
        assert datetime.fromisoformat(result["next_run_at"]).hour == 1
        assert result["schedule"] == "at 09:00 every day"

    async def test_rejects_invalid_cron(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        result = await _create(tool, context, script_id=str(script.script_id), cron_expr="0 9 * *")
        assert "cron" in result["error"].lower()

    async def test_rejects_unknown_timezone(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        result = await _create(tool, context, script_id=str(script.script_id), timezone="Mars/Olympus")
        assert "timezone" in result["error"].lower()

    async def test_rejects_script_from_another_project(self, sched_env):
        tool, context, session, _ = sched_env
        foreign = await _add_script(session, project_id="p2")
        result = await _create(tool, context, script_id=str(foreign.script_id))
        assert "not found" in result["error"].lower()

    async def test_rejects_playwright_anchor_as_script(self, sched_env):
        tool, context, session, _ = sched_env
        anchor = await _add_script(session, script_type="playwright")
        result = await _create(tool, context, script_id=str(anchor.script_id))
        assert "playwright" in result["error"].lower()

    async def test_rejects_missing_spec_file(self, sched_env):
        tool, context, session, _ = sched_env
        result = await _create(tool, context, target_type="playwright", spec_slug="login-flow")
        assert "not found" in result["error"].lower()

    async def test_accepts_existing_spec_file(self, sched_env):
        tool, context, session, project_dir = sched_env
        (project_dir / "tests" / "generated" / "login-flow.spec.ts").write_text("x")
        result = await _create(tool, context, target_type="playwright", spec_slug="login-flow")
        assert result["success"] is True

    async def test_agent_target_requires_known_slug_and_prompt(self, sched_env):
        tool, context, _, _ = sched_env
        assert "agent" in (
            await _create(tool, context, target_type="agent", agent_slug="tech-lead")
        )["error"].lower()
        assert "no agent definition" in (
            await _create(tool, context, target_type="agent", agent_slug="ghost", prompt="hi")
        )["error"].lower()
        result = await _create(tool, context, target_type="agent", agent_slug="tech-lead", prompt="daily digest")
        assert result["success"] is True

    async def test_rejects_credential_shaped_prompt(self, sched_env):
        tool, context, _, _ = sched_env
        result = await _create(
            tool, context, target_type="agent", agent_slug="tech-lead",
            prompt="use the api_key sk-abc123 to call the service",
        )
        assert "credential" in result["error"].lower()

    async def test_rejects_foreign_conversation(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        conv = Conversation(project_id="p1", user_id="someone-else")
        session.add(conv)
        await session.commit()
        result = await _create(
            tool, context, script_id=str(script.script_id),
            conversation_id=str(conv.conversation_id),
        )
        assert "conversation" in result["error"].lower()

    async def test_disabled_task_has_no_next_run(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        result = await _create(tool, context, script_id=str(script.script_id), enabled=False)
        assert result["next_run_at"] is None


class TestListGetUpdate:
    async def test_list_scopes_to_project(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        await _create(tool, context, script_id=str(script.script_id))
        session.add(ScheduledTask(
            project_id="p2", name="other", target_type="script",
            cron_expr="0 9 * * *", timezone="UTC", created_by="u1",
        ))
        await session.commit()

        result = await tool.execute({"operation": "list"}, context)
        assert result["count"] == 1
        assert result["tasks"][0]["name"] == "nightly-report"

    async def test_get_rejects_foreign_task(self, sched_env):
        tool, context, session, _ = sched_env
        foreign = ScheduledTask(
            project_id="p2", name="other", target_type="script",
            cron_expr="0 9 * * *", timezone="UTC", created_by="u1",
        )
        session.add(foreign)
        await session.commit()
        result = await tool.execute(
            {"operation": "get", "schedule_id": str(foreign.schedule_id)}, context
        )
        assert "not found" in result["error"].lower()

    async def test_update_changes_cron_and_recomputes(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        created = await _create(tool, context, script_id=str(script.script_id))

        result = await tool.execute({
            "operation": "update",
            "schedule_id": created["schedule_id"],
            "cron_expr": "*/30 * * * *",
        }, context)
        assert result["success"] is True
        assert result["cron_expr"] == "*/30 * * * *"
        assert result["next_run_at"] != created["next_run_at"]

    async def test_enable_disable_round_trip(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        created = await _create(tool, context, script_id=str(script.script_id))

        disabled = await tool.execute(
            {"operation": "disable", "schedule_id": created["schedule_id"]}, context
        )
        assert disabled["enabled"] is False
        assert disabled["next_run_at"] is None

        enabled = await tool.execute(
            {"operation": "enable", "schedule_id": created["schedule_id"]}, context
        )
        assert enabled["enabled"] is True
        assert enabled["next_run_at"] is not None

    async def test_delete_removes_task(self, sched_env):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        created = await _create(tool, context, script_id=str(script.script_id))
        result = await tool.execute(
            {"operation": "delete", "schedule_id": created["schedule_id"]}, context
        )
        assert result["success"] is True
        assert (
            await tool.execute(
                {"operation": "get", "schedule_id": created["schedule_id"]}, context
            )
        )["error"]


class TestRunNow:
    async def test_run_now_uses_shared_launch_path(self, sched_env, monkeypatch):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        created = await _create(tool, context, script_id=str(script.script_id))

        launched: list[tuple[object, str, str]] = []

        async def fake_spawn(app, schedule_id, *, trigger):
            launched.append((app, schedule_id, trigger))
            return "run-9"

        monkeypatch.setitem(sys.modules, "api.services.scheduled_runs", types.SimpleNamespace(
            spawn_run=fake_spawn,
            ScheduleBusy=type("ScheduleBusy", (RuntimeError,), {}),
        ))
        result = await tool.execute(
            {"operation": "run_now", "schedule_id": created["schedule_id"]}, context
        )
        assert result["success"] is True
        assert result["run_id"] == "run-9"
        assert launched == [(context.app, created["schedule_id"], "manual")]

    async def test_run_now_reports_busy(self, sched_env, monkeypatch):
        tool, context, session, _ = sched_env
        script = await _add_script(session)
        created = await _create(tool, context, script_id=str(script.script_id))

        class ScheduleBusy(RuntimeError):
            pass

        async def fake_spawn(app, schedule_id, *, trigger):
            raise ScheduleBusy(schedule_id)

        monkeypatch.setitem(sys.modules, "api.services.scheduled_runs", types.SimpleNamespace(
            spawn_run=fake_spawn, ScheduleBusy=ScheduleBusy,
        ))
        result = await tool.execute(
            {"operation": "run_now", "schedule_id": created["schedule_id"]}, context
        )
        assert "in progress" in result["error"].lower()


def test_registry_exposes_scheduler_tool():
    from agent_core.tools.registry import build_tool_registry

    registry = build_tool_registry(["scheduler"])
    assert "scheduler" in registry
