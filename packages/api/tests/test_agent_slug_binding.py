"""Client-supplied agent slugs: injection sanitation + scheduled binding scope.

1. The orphan-injection persist writes the raw injection `agent_id` into
   messages.agent_slug (String(100)) without resolving it — a non-string or
   oversized value could DataError the persist on strict dialects (silently
   losing a message the client was already told was queued).
2. _resolve_conversation must mirror routers/conversations.create_conversation:
   a project-scoped agent row is valid only within its own project.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from api.models.agent import Agent
from api.models.conversation import Conversation
from api.models.project import Project
from api.models.schedule import ScheduledTask
from api.services.agent_turn import _sanitize_agent_slug
from api.services.scheduled_runs import _Target, _resolve_conversation


class TestSanitizeAgentSlug:
    def test_non_string_becomes_none(self):
        assert _sanitize_agent_slug(None) is None
        assert _sanitize_agent_slug(123) is None
        assert _sanitize_agent_slug(["a"]) is None

    def test_blank_becomes_none(self):
        assert _sanitize_agent_slug("") is None
        assert _sanitize_agent_slug("   ") is None

    def test_valid_slug_passes_through(self):
        assert _sanitize_agent_slug("tech-lead") == "tech-lead"

    def test_long_value_truncated_to_column_width(self):
        assert len(_sanitize_agent_slug("x" * 500)) == 100


async def _agent_task(db, project_id: str, agent_slug: str) -> ScheduledTask:
    task = ScheduledTask(
        project_id=project_id, name="nightly", target_type="agent",
        agent_slug=agent_slug, prompt="daily digest",
        cron_expr="0 9 * * *", timezone="UTC", created_by="test-user",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _bound_agent_id(db, conversation_id) -> str | None:
    conv = (
        await db.execute(
            select(Conversation).where(Conversation.conversation_id == conversation_id)
        )
    ).scalar_one()
    return str(conv.agent_id) if conv.agent_id else None


class TestResolveConversationAgentBinding:
    async def test_global_agent_is_bound(self, db, make_project):
        project = await make_project()
        agent = Agent(slug="analyst", display_name="分析师", project_id=None)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        target = _Target(await _agent_task(db, str(project.project_id), "analyst"))
        conv_id = await _resolve_conversation(db, target)

        assert await _bound_agent_id(db, conv_id) == str(agent.agent_id)

    async def test_own_project_agent_is_bound(self, db, make_project):
        project = await make_project()
        agent = Agent(
            slug="local-agent", display_name="本项目", project_id=str(project.project_id),
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        target = _Target(await _agent_task(db, str(project.project_id), "local-agent"))
        conv_id = await _resolve_conversation(db, target)

        assert await _bound_agent_id(db, conv_id) == str(agent.agent_id)

    async def test_foreign_project_agent_left_unbound(self, db, make_project):
        project = await make_project()
        other = Project(
            slug="other-p", display_name="Other",
            created_by="test-user", visibility="public",
        )
        db.add(other)
        await db.commit()
        await db.refresh(other)
        agent = Agent(
            slug="foreign-agent", display_name="外项目", project_id=str(other.project_id),
        )
        db.add(agent)
        await db.commit()

        target = _Target(await _agent_task(db, str(project.project_id), "foreign-agent"))
        conv_id = await _resolve_conversation(db, target)

        # Unbound, not bound to another project's row — that row's FK would
        # then ride into this conversation and break the foreign deletion.
        assert await _bound_agent_id(db, conv_id) is None


class TestResolveConversationBindsTargetSnapshot:
    async def test_auto_created_conversation_id_binds_back_to_target(self, db, make_project):
        """_resolve_conversation creates the first conversation and writes the
        id back to the ScheduledTask row — it must bind the SAME id onto this
        run's _Target snapshot, or deliver_result's `if not
        target.conversation_id` early-return skips the first fire's
        conversation_updated nudge."""
        project = await make_project()
        target = _Target(await _agent_task(db, str(project.project_id), None))
        assert target.conversation_id is None

        conv_id = await _resolve_conversation(db, target)

        assert target.conversation_id == conv_id

    async def test_existing_conversation_is_returned_untouched(self, db, make_project):
        project = await make_project()
        task = await _agent_task(db, str(project.project_id), None)
        task.conversation_id = "already-bound"
        await db.commit()

        target = _Target(task)
        conv_id = await _resolve_conversation(db, target)

        assert conv_id == "already-bound"
        assert target.conversation_id == "already-bound"
