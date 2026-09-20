"""What a delegated turn is allowed to put on the wire.

A child turn and its parent share one WebSocket, and a forwarded frame carries
nothing but ``{"type": ..., **data}`` — the browser cannot tell which agent
emitted it. So the child's transport is fail-closed: an unlisted event would
freeze the parent's bubble (``stream_end``), wipe its streaming state and
pending dialogs (``abort_ack``), replace its task panel (``task_plan_created``)
or double count its tokens (``token_update``).

The integration half drives a real parent turn plus a real delegated child and
records every frame that reaches the connection manager; the unit half pins the
capture/status contract the delegator reads its result from.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.agent import Agent
from api.main import app
from api.models.conversation import Conversation
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.services.delegation import DelegationContext, DelegationTransport, run_delegated_turn
from api.services.token_vault import encrypt
from api.websocket.handlers import _handle_message
from api.websocket.manager import manager


@pytest.fixture(scope="session", autouse=True)
def _app_state_registries():
    from agent_core.knowledge.cache import KnowledgeCache
    from agent_core.skills.registry import SkillRegistry
    from agent_core.workflows import WorkflowRegistry

    app.state.knowledge_cache = KnowledgeCache()
    skill_registry = SkillRegistry()
    skill_registry.load_dir(
        str(AGENTS_DIR / "skills"),
        mixin_dir=str(AGENTS_DIR / "skills" / "_mixins"),
    )
    app.state.skill_registry = skill_registry
    workflow_registry = WorkflowRegistry()
    workflow_registry.load_dir(str(WORKFLOWS_DIR))
    app.state.workflow_registry = workflow_registry
    yield


@pytest.fixture(autouse=True)
async def _clean_model_configs(db):
    from sqlalchemy import delete

    await db.execute(delete(UserModelConfig).where(UserModelConfig.user_id == "test-user"))
    await db.commit()


@pytest.fixture
def frames(monkeypatch):
    """Record every frame that reaches the connection manager."""
    seen: list[dict] = []

    async def _send(conversation_id: str, data: dict) -> bool:
        seen.append(data)
        return True

    monkeypatch.setattr(manager, "send", _send)
    return seen


class _Recorder:
    """Stand-in for the real transport, so a unit test can inspect forwarding."""

    def __init__(self) -> None:
        self.received: list[dict] = []

    async def send(self, conversation_id: str, data: dict) -> bool:
        self.received.append(data)
        return True


# ── the transport contract ────────────────────────────────────────────────


async def test_transport_drops_everything_that_is_not_whitelisted():
    inner = _Recorder()
    transport = DelegationTransport(inner)

    for event in (
        {"type": "stream_delta", "delta": "hello "},
        {"type": "stream_end", "message_id": "m1", "total_tokens": 11},
        {"type": "abort_ack"},
        {"type": "error", "message": "boom"},
        {"type": "task_plan_created", "tasks": [{"task_id": "t1"}]},
        {"type": "token_update", "used": 5, "budget": 100},
        {"type": "model_selected", "model": "gpt"},
        {"type": "knowledge_loaded", "files": ["a.md"]},
    ):
        assert await transport.send("c1", event) is True

    assert inner.received == []
    # ...while the result the delegator needs was still captured.
    assert transport.summary == "hello"
    assert transport.message_id == "m1"
    assert transport.tokens_used == 11
    assert transport.error == "boom"
    assert transport.status == "error"


async def test_transport_forwards_the_whitelist():
    inner = _Recorder()
    transport = DelegationTransport(inner)

    for event in (
        {"type": "user_selection_required", "prompt_id": "p1"},
        {"type": "user_selection_cancelled", "prompt_id": "p1", "reason": "timeout"},
        {"type": "image_output", "images": [{"url": "u"}]},
        {"type": "file_output", "files": [{"name": "n"}]},
        {"type": "knowledge_updated", "slug": "s"},
        {"type": "knowledge_dynamic_load", "slug": "s"},
        {"type": "knowledge_dynamic_unload", "slugs": ["s"]},
    ):
        await transport.send("c1", event)

    assert [e["type"] for e in inner.received] == [
        "user_selection_required",
        "user_selection_cancelled",
        "image_output",
        "file_output",
        "knowledge_updated",
        "knowledge_dynamic_load",
        "knowledge_dynamic_unload",
    ]


async def test_transport_summary_is_bounded():
    inner = _Recorder()
    transport = DelegationTransport(inner)
    await transport.send("c1", {"type": "stream_delta", "delta": "x" * 10_000})

    assert len(transport.summary) < 10_000
    assert transport.summary.startswith("x")
    assert "..." in transport.summary


@pytest.mark.parametrize(
    "events,expected",
    [
        ([], "ok"),
        ([{"type": "stream_end", "message_id": "m", "total_tokens": 1}], "ok"),
        ([{"type": "stream_end", "message_id": "m", "stop_reason": "aborted"}], "aborted"),
        ([{"type": "stream_end", "message_id": "m", "stop_reason": "interrupted"}], "aborted"),
        ([{"type": "error", "message": "nope"}], "error"),
    ],
)
async def test_transport_status(events, expected):
    transport = DelegationTransport(_Recorder())
    for event in events:
        await transport.send("c1", event)
    assert transport.status == expected


# ── end to end: what the socket actually carries ──────────────────────────


async def test_child_events_do_not_reach_the_parent_socket(frames, db, make_project, monkeypatch):
    """A chatty child leaks nothing; the parent's own stream is untouched."""
    project = await make_project("filterp")
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    db.add(UserModelConfig(
        user_id="test-user", provider="openai", model_id="gpt-5.6-luna",
        encrypted_key=encrypt("sk-test-key-1234", "test-user"), key_hint="...1234",
        url_mode="base_url", complexity_tier=None, sort_order=0,
    ))
    await db.commit()

    child_slug = "filterp--helper"
    (Path(db_project_fs(project)) / "agents" / f"{child_slug}.agent.md").write_text(
        "---\n"
        f"slug: {child_slug}\n"
        f"display_name: {child_slug}\n"
        "description: test agent\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            session = kwargs["session"]
            if kwargs["user_message"].startswith("CHILD:"):
                await session.emit("stream_delta", delta="child partial")
                await session.emit("task_plan_created", tasks=[{"task_id": "t-child"}])
                await session.emit("token_update", used=999, budget=1000)
                await session.emit("user_selection_required", prompt_id="p-child", field_key="f")
                await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=5)
                return
            ctx = SimpleNamespace(
                delegation=DelegationContext(
                    chain=("filterp--parent",), parent_session=session, actor_user_id="test-user",
                ),
                app=app,
                project_fs_path=db_project_fs(project),
                conversation_id=conv.conversation_id,
                interactive=True,
            )
            await run_delegated_turn(ctx, agent_slug=child_slug, brief="CHILD: go", reason="r")
            await session.emit("stream_delta", delta="parent text")
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=2)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    await _handle_message(conv.conversation_id, SimpleNamespace(app=app), {"type": "message", "content": "hi"}, "test-user")

    types = [f["type"] for f in frames]
    # The parent's stream is intact and its own events are NOT swallowed.
    assert "parent text" in "".join(f.get("delta", "") for f in frames)
    # The child's dialog is the one exception — the user must see it.
    assert [f["prompt_id"] for f in frames if f["type"] == "user_selection_required"] == ["p-child"]
    # Nothing else of the child's, and no task/token clobber.
    assert "task_plan_created" not in types
    assert "token_update" not in types
    assert "child partial" not in "".join(f.get("delta", "") for f in frames)
    # One stream_end for the whole conversation — the parent's.
    assert types.count("stream_end") == 1
    # The delegation lifecycle is visible to the client.
    assert "delegate_started" in types
    assert "delegate_finished" in types
    assert "conversation_updated" in types
    finished = next(f for f in frames if f["type"] == "delegate_finished")
    assert finished["agent"] == child_slug
    assert finished["status"] == "ok"
    assert finished["tokens_used"] == 5


def db_project_fs(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)
