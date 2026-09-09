"""Scheduled-run result delivery.

The run summary is appended to the task's conversation as an assistant message
and the open socket is nudged with ``conversation_updated``. Every failure path
(deleted / foreign / inactive / busy conversation) must stay silent — delivery
is best-effort and must never fail the run itself.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from api.models.conversation import Conversation, Message
from api.models.schedule import ScheduledTask
from api.services import scheduled_runs
from api.services.scheduled_runs import _Outcome, _Target
from api.websocket.manager import manager


async def _conversation(db, project, *, user_id: str = "test-user", status: str = "active") -> Conversation:
    conv = Conversation(project_id=str(project.project_id), user_id=user_id, status=status)
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _target(
    db, project, *, conversation_id: str | None, created_by: str = "test-user"
) -> _Target:
    task = ScheduledTask(
        project_id=str(project.project_id),
        name="nightly",
        target_type="script",
        cron_expr="0 9 * * *",
        timezone="UTC",
        created_by=created_by,
        conversation_id=conversation_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return _Target(task)


@pytest.fixture
def manager_calls(monkeypatch):
    """Record turn claim/release and outbound WS frames."""
    calls: dict[str, list] = {"claim": [], "release": [], "send": []}

    async def claim(conversation_id: str) -> bool:
        calls["claim"].append(conversation_id)
        return True

    def release(conversation_id: str) -> None:
        calls["release"].append(conversation_id)

    async def send(conversation_id: str, data: dict) -> bool:
        calls["send"].append((conversation_id, data))
        return True

    monkeypatch.setattr(manager, "claim_turn", claim)
    monkeypatch.setattr(manager, "release_turn", release)
    monkeypatch.setattr(manager, "send", send)
    return calls


async def _messages(db, conversation_id: str) -> list[Message]:
    return (
        await db.execute(
            select(Message).where(Message.conversation_id == conversation_id)
        )
    ).scalars().all()


class TestDelivery:
    async def test_appends_summary_and_nudges_the_socket(
        self, db, make_project, manager_calls
    ):
        project = await make_project()
        conv = await _conversation(db, project)
        target = await _target(db, project, conversation_id=str(conv.conversation_id))

        await scheduled_runs.deliver_result(
            target, _Outcome("completed", summary="定时任务「nightly」执行成功")
        )

        rows = await _messages(db, str(conv.conversation_id))
        assert len(rows) == 1
        assert rows[0].role == "assistant"
        assert "执行成功" in rows[0].content
        # No agent attribution: the notice belongs to the conversation itself.
        assert rows[0].agent_slug is None
        assert manager_calls["send"] == [
            (str(conv.conversation_id), {"type": "conversation_updated"})
        ]
        # The claim serializes the insert against a live user turn.
        assert manager_calls["claim"] == [str(conv.conversation_id)]
        assert manager_calls["release"] == [str(conv.conversation_id)]

    async def test_uses_the_error_text_when_there_is_no_summary(
        self, db, make_project, manager_calls
    ):
        project = await make_project()
        conv = await _conversation(db, project)
        target = await _target(db, project, conversation_id=str(conv.conversation_id))

        await scheduled_runs.deliver_result(target, _Outcome("failed", error="exit 1"))

        rows = await _messages(db, str(conv.conversation_id))
        assert [r.content for r in rows] == ["exit 1"]


class TestSilentSkips:
    async def test_no_conversation_configured(self, db, make_project, manager_calls):
        project = await make_project()
        target = await _target(db, project, conversation_id=None)

        await scheduled_runs.deliver_result(target, _Outcome("completed", summary="x"))

        assert manager_calls["claim"] == []
        assert manager_calls["send"] == []

    async def test_conversation_was_deleted(self, db, make_project, manager_calls):
        project = await make_project()
        target = await _target(db, project, conversation_id=str(uuid.uuid4()))

        await scheduled_runs.deliver_result(target, _Outcome("completed", summary="x"))

        assert manager_calls["claim"] == []
        assert manager_calls["send"] == []

    async def test_conversation_belongs_to_another_user(
        self, db, make_project, manager_calls
    ):
        project = await make_project()
        conv = await _conversation(db, project, user_id="someone-else")
        target = await _target(db, project, conversation_id=str(conv.conversation_id))

        await scheduled_runs.deliver_result(target, _Outcome("completed", summary="x"))

        assert await _messages(db, str(conv.conversation_id)) == []
        assert manager_calls["claim"] == []

    async def test_conversation_is_no_longer_active(
        self, db, make_project, manager_calls
    ):
        project = await make_project()
        conv = await _conversation(db, project, status="archived")
        target = await _target(db, project, conversation_id=str(conv.conversation_id))

        await scheduled_runs.deliver_result(target, _Outcome("completed", summary="x"))

        assert await _messages(db, str(conv.conversation_id)) == []

    async def test_conversation_stays_busy(
        self, db, make_project, monkeypatch
    ):
        project = await make_project()
        conv = await _conversation(db, project)
        target = await _target(db, project, conversation_id=str(conv.conversation_id))

        async def never_claim(_conversation_id: str) -> bool:
            return False

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(manager, "claim_turn", never_claim)
        monkeypatch.setattr(scheduled_runs.asyncio, "sleep", no_sleep)

        await scheduled_runs.deliver_result(target, _Outcome("completed", summary="x"))

        assert await _messages(db, str(conv.conversation_id)) == []

    async def test_persist_failure_still_releases_the_turn(
        self, db, make_project, monkeypatch, manager_calls
    ):
        project = await make_project()
        conv = await _conversation(db, project)
        target = await _target(db, project, conversation_id=str(conv.conversation_id))

        async def boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        import api.services.agent_turn as agent_turn

        monkeypatch.setattr(agent_turn, "_persist_assistant_message", boom)

        await scheduled_runs.deliver_result(target, _Outcome("completed", summary="x"))

        assert manager_calls["release"] == [str(conv.conversation_id)]
