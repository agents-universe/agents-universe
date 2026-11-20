"""Running-turn counts for sidebar badges (REST + user WS + debounce)."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.models.agent import Agent
from api.models.conversation import Conversation, Message
from api.models.conversation_run import ConversationRun
from api.models.project_member import ProjectMember
from api.services import running_counts as rc
from api.websocket.manager import manager


async def _agent(db, slug: str) -> Agent:
    agent = Agent(slug=slug, display_name=slug, project_id=None)
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _conv(db, project, *, user_id="test-user", agent=None, source=None):
    conv = Conversation(
        user_id=user_id,
        project_id=project.project_id,
        agent_id=agent.agent_id if agent else None,
        source=source,
        title="t",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _run(db, conv, *, status="running", agent_slug=None):
    msg = None
    if agent_slug is not None:
        msg = Message(
            conversation_id=conv.conversation_id,
            role="user",
            content="hi",
            sequence_num=1,
            agent_slug=agent_slug,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
    run = ConversationRun(
        conversation_id=conv.conversation_id,
        user_message_id=msg.message_id if msg else None,
        status=status,
    )
    db.add(run)
    await db.commit()
    return run


async def _counts(user_id="test-user"):
    return await rc.compute_running_counts(user_id)


@pytest.mark.asyncio
async def test_running_counts_requires_auth(monkeypatch):
    from api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_bypass_enabled", False)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/running-counts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_groups_by_project_and_agent(db, make_project):
    p1 = await make_project(slug=f"rc-p1-{uuid.uuid4().hex[:6]}")
    p2 = await make_project(slug=f"rc-p2-{uuid.uuid4().hex[:6]}")
    p_excl = await make_project(slug=f"rc-px-{uuid.uuid4().hex[:6]}")
    a1 = await _agent(db, f"rc-agent-a1-{uuid.uuid4().hex[:6]}")
    a2 = await _agent(db, f"rc-agent-a2-{uuid.uuid4().hex[:6]}")

    c1 = await _conv(db, p1, agent=a1)
    c2 = await _conv(db, p1, agent=a2)
    c3 = await _conv(db, p2, agent=a1)
    await _run(db, c1)
    await _run(db, c2)
    await _run(db, c3)
    # Terminal run in p1 must not count.
    done = await _conv(db, p1, agent=a1)
    await _run(db, done, status="completed")
    # Another user's running turn in p_excl must not count.
    foreign = await _conv(db, p_excl, agent=a1, user_id="someone-else")
    await _run(db, foreign)

    counts = await _counts()
    assert counts["by_project"][p1.project_id] == 2
    assert counts["by_project"][p2.project_id] == 1
    assert p_excl.project_id not in counts["by_project"]
    assert counts["by_agent"][a1.slug] == 2
    assert counts["by_agent"][a2.slug] == 1
    # Non-running noise from other tests must not leak into our keys.
    assert counts["by_project"][p1.project_id] >= 2


@pytest.mark.asyncio
async def test_agent_attribution_prefers_user_message_slug(db, make_project):
    p = await make_project(slug=f"rc-mention-{uuid.uuid4().hex[:6]}")
    default = await _agent(db, f"rc-default-{uuid.uuid4().hex[:6]}")
    conv = await _conv(db, p, agent=default)
    # @-mention turn: user message carries the mentioned slug.
    await _run(db, conv, agent_slug="mentioned-agent")

    counts = await _counts()
    assert counts["by_project"][p.project_id] >= 1
    assert counts["by_agent"]["mentioned-agent"] >= 1
    assert counts["by_agent"].get(default.slug, 0) == 0 or default.slug not in counts["by_agent"]


@pytest.mark.asyncio
async def test_falls_back_to_conversation_agent_when_message_missing(db, make_project):
    p = await make_project(slug=f"rc-fb-{uuid.uuid4().hex[:6]}")
    default = await _agent(db, f"rc-fb-agent-{uuid.uuid4().hex[:6]}")
    conv = await _conv(db, p, agent=default)
    await _run(db, conv)  # no user message at all

    counts = await _counts()
    assert counts["by_agent"].get(default.slug, 0) >= 1
    assert counts["by_project"][p.project_id] >= 1


@pytest.mark.asyncio
async def test_private_project_visibility(db, make_project):
    p = await make_project(
        slug=f"rc-priv-{uuid.uuid4().hex[:6]}",
        created_by="other-owner",
        visibility="private",
    )
    conv = await _conv(db, p)
    await _run(db, conv)

    counts = await _counts()
    assert p.project_id not in counts["by_project"]

    db.add(
        ProjectMember(
            project_id=p.project_id, user_id="test-user", added_by="other-owner"
        )
    )
    await db.commit()

    counts = await _counts()
    assert counts["by_project"][p.project_id] >= 1


@pytest.mark.asyncio
async def test_includes_in_memory_session_without_run_row(db, make_project):
    p = await make_project(slug=f"rc-mem-{uuid.uuid4().hex[:6]}")
    conv = await _conv(db, p)
    manager._sessions[conv.conversation_id] = SimpleNamespace(
        user_id="test-user", project_id=p.project_id
    )
    try:
        counts = await _counts()
        assert counts["by_project"][p.project_id] >= 1
    finally:
        manager._sessions.pop(conv.conversation_id, None)


@pytest.mark.asyncio
async def test_excludes_publish_source_conversations(db, make_project):
    p = await make_project(slug=f"rc-pub-{uuid.uuid4().hex[:6]}")
    conv = await _conv(db, p, source="publish")
    await _run(db, conv)

    counts = await _counts()
    assert p.project_id not in counts["by_project"]


def test_user_ws_sends_initial_running_counts():
    # No `with TestClient(app)`: the app lifespan (scheduler, redis) is not
    # available under ASGI tests (same constraint as test_script_run_ws).
    client = TestClient(app)
    with client.websocket_connect("/ws/user") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "running_counts"
        assert "by_project" in frame and "by_agent" in frame
        # ping/pong round-trip
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.asyncio
async def test_notify_broadcasts_debounced(db, make_project, monkeypatch):
    monkeypatch.setattr(rc, "_DEBOUNCE_S", 0)
    p = await make_project(slug=f"rc-notify-{uuid.uuid4().hex[:6]}")
    conv = await _conv(db, p)
    await _run(db, conv)

    class _FakeWs:
        def __init__(self):
            self.frames = []

        async def send_text(self, text):
            self.frames.append(text)

    fake = _FakeWs()
    key = "user:test-user"
    manager._connections.setdefault(key, set()).add(fake)
    try:
        task = await rc.notify_conversation_running_changed(conv.conversation_id)
        assert task is not None
        await task
        assert fake.frames, "expected a running_counts frame"
        import json

        payload = json.loads(fake.frames[-1])
        assert payload["type"] == "running_counts"
        assert payload["by_project"].get(p.project_id, 0) >= 1
    finally:
        manager._connections.get(key, set()).discard(fake)
        if not manager._connections.get(key):
            manager._connections.pop(key, None)


@pytest.mark.asyncio
async def test_notify_unknown_conversation_is_noop():
    task = await rc.notify_conversation_running_changed("no-such-conversation")
    assert task is None
    # No stray debounce timer scheduled for any user.
    await asyncio.sleep(0.01)
    assert not rc._pending
