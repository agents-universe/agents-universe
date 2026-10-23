"""An external cancellation must not close event_db under a live persist.

run_turn's except BaseException drains agent_task, forward_task and the
shielded persists (bounded) before closing its sessions — close() under a
live writer interleaves commits/rollbacks on the same greenlet and silently
drops the in-flight write. The leaky path pinned here: a cancel landing at
the forward-drain wait kills forward_task via fut_waiter propagation while
the injection persist (shielded, no CancelledError handler around its await)
is still running on event_db. The test asserts the persist finishes BEFORE
any session close and the injected message lands in history.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from api.main import app
from api.models.conversation import Conversation, Message
from api.models.user import UserModelConfig
from api.services import agent_turn as agent_turn_mod
from api.services.token_vault import encrypt


@pytest.fixture(scope="session", autouse=True)
def _app_state_registries():
    """Populate app.state the way the lifespan would (tests never run it)."""
    from api.paths import AGENTS_DIR, WORKFLOWS_DIR
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


@pytest.fixture
def agent_spy(monkeypatch):
    """Replace Agent.run with a spy whose behavior is set per test."""
    state: dict = {"behavior": None}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            behavior = state["behavior"]
            if behavior is not None:
                await behavior(kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


@pytest.fixture(autouse=True)
async def _clean_model_configs(db):
    """The suite shares one DB file — drop rows from previous tests."""
    from sqlalchemy import delete

    await db.execute(delete(UserModelConfig).where(UserModelConfig.user_id == "test-user"))
    await db.commit()


async def test_external_cancel_drains_injection_persist_before_session_close(
    agent_spy, db, make_project, monkeypatch
):
    """Cancel while the shielded injection persist is mid-flight: the drain
    must let that persist finish before closing event_db, or the injected
    message is silently dropped from history."""
    events: list = []
    persist_started = asyncio.Event()
    allow_persist = asyncio.Event()

    # Gate the injection persist: the cancel lands while it is "mid-commit".
    # The turn's own user-message persist shares this helper — the injection
    # path is the one carrying a pre-generated message_id. The persist's
    # first argument IS the turn's event_db session.
    real_persist = agent_turn_mod._prepare_and_persist_user_message
    event_db: list = []

    async def _gated_persist(*args, **kwargs):
        if kwargs.get("message_id") != inj_mid:
            return await real_persist(*args, **kwargs)
        event_db.append(args[0])
        persist_started.set()
        await allow_persist.wait()
        result = await real_persist(*args, **kwargs)
        events.append("persist_done")
        return result

    monkeypatch.setattr(agent_turn_mod, "_prepare_and_persist_user_message", _gated_persist)

    # Record session closes so the test can pin persist-before-close ordering.
    # run_turn and its helpers import the factory lazily from api.database,
    # so the patch goes on the source module.
    import api.database as db_mod

    orig_factory = db_mod.AsyncSessionLocal

    def _recording_factory():
        sess = orig_factory()
        orig_close = sess.close

        async def _close():
            events.append(("close", sess))
            await orig_close()

        sess.close = _close
        return sess

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", _recording_factory)

    project = await make_project()
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    cid = str(conv.conversation_id)
    db.add(UserModelConfig(
        user_id="test-user",
        provider="openai",
        model_id="gpt-5.6-luna",
        encrypted_key=encrypt("sk-test-key-1234", "test-user"),
        key_hint="...1234",
        url_mode="base_url",
        complexity_tier=None,
        sort_order=0,
    ))
    await db.commit()

    inj_mid = str(uuid.uuid4())

    async def _behavior(kwargs):
        # A mid-run injection as the agent's last action: forward_task parks
        # on the shielded injection persist while run_turn waits on the
        # forward drain — cancelling then kills forward_task (fut_waiter
        # propagation) but leaves the shielded persist running on event_db.
        await kwargs["session"].emit(
            "user_message_injected", message_id=inj_mid, content="mid-run note"
        )

    agent_spy["behavior"] = _behavior

    task = asyncio.create_task(agent_turn_mod.run_turn(
        cid, SimpleNamespace(app=app),
        {"type": "message", "content": "hi"}, "test-user",
    ))
    await asyncio.wait_for(persist_started.wait(), timeout=15)
    task.cancel()
    # Yield so a draining (or, pre-fix, closing) run_turn runs before the
    # persist is released — the assertion pins whichever happened first.
    await asyncio.sleep(0.05)
    allow_persist.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=30)

    # Pin that the persist completed before event_db's close.
    close_at = next(
        i for i, e in enumerate(events) if isinstance(e, tuple) and e[1] is event_db[0]
    )
    assert events.index("persist_done") < close_at

    msgs = (
        await db.execute(
            select(Message).where(Message.conversation_id == cid, Message.role == "user")
        )
    ).scalars().all()
    assert [m.content for m in msgs] == ["hi", "mid-run note"]
    assert any(m.message_id == inj_mid for m in msgs)
