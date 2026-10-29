"""Isolated context for delegated turns (AGENT_DELEGATION_ISOLATED_CONTEXT).

The child of a delegated turn starts with NO conversation history — only its
own system prompt, the brief and its knowledge — mirroring the fresh-context
sub-agent model: the brief is supposed to be self-contained, and preloading
the history the parent just read duplicated tokens at every nesting level.
The child can page history back in via the injected ``load_conversation_history``
tool (nested turns only, not declarable, never visible to root turns).

The flag is the escape hatch: ``AGENT_DELEGATION_ISOLATED_CONTEXT=false``
restores the legacy behavior exactly — preloaded history, no tool, the legacy
G8 note with its trailing [name]-prefixed-history sentence.

Attribution, the gate, depth/cycle checks and token accounting are untouched
and pinned by the sibling delegation test files.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from api.config import get_settings
from api.main import app
from api.models.conversation import Conversation, Message
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.routers.agents import _sync_project_agents
from api.services.agent_turn import load_history_page
from api.services.delegation import DelegationContext, run_delegated_turn
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
def agent_spy(monkeypatch):
    """Record each ``Agent.run`` call's kwargs AND the agent's tool set."""
    state: dict = {"behavior": None, "calls": []}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["calls"].append({**kwargs, "_tools": set(self._tools)})
            if state["behavior"] is not None:
                await state["behavior"](state["calls"][-1])

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


async def _setup(db, make_project, tag: str):
    project = await make_project(tag)
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

    child_slug = f"{project.slug}--helper"
    (Path(project_fs(project)) / "agents" / f"{child_slug}.agent.md").write_text(
        "---\n"
        f"slug: {child_slug}\n"
        "display_name: Helper Bot\n"
        "description: test agent\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    await _sync_project_agents(db, project)

    # Prior conversation content the isolated child must NOT receive.
    for seq, role, content, slug in (
        (1, "user", "earlier question", None),
        (2, "assistant", "earlier answer", "isoqa--parent"),
    ):
        db.add(Message(
            conversation_id=conv.conversation_id, role=role, content=content,
            agent_slug=slug, sequence_num=seq,
        ))
    await db.commit()
    return project, conv, child_slug


def _ctx(session, project, conv):
    return SimpleNamespace(
        delegation=DelegationContext(
            chain=("isoqa--parent",), parent_session=session, actor_user_id="test-user",
        ),
        app=app,
        project_fs_path=project_fs(project),
        conversation_id=conv.conversation_id,
        interactive=True,
    )


async def _run_two_turns(agent_spy, conv, project, child_slug):
    """Turn 1: parent delegates. Turn 2: a plain root turn (regression guard)."""
    seen: dict = {}
    child_msg_id = str(uuid.uuid4())

    async def _behavior(call):
        session = call["session"]
        content = call["user_message"]
        if content.startswith("CHILD:"):
            seen["child_call"] = call
            await session.emit("stream_delta", delta="child reply")
            await session.emit("stream_end", message_id=child_msg_id, total_tokens=4)
            return
        if not seen.get("delegated"):
            seen["delegated"] = True
            seen["parent_call"] = call
            seen["result"] = await run_delegated_turn(
                _ctx(session, project, conv),
                agent_slug=child_slug, brief="CHILD: do the thing", reason="no capability",
            )
            await session.emit("stream_delta", delta="parent final")
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=2)
            return
        seen["root2_call"] = call
        await session.emit("stream_delta", delta="ok")
        await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)

    agent_spy["behavior"] = _behavior
    await _handle_message(conv.conversation_id, SimpleNamespace(app=app), {"type": "message", "content": "hi"}, "test-user")
    manager.release_turn(conv.conversation_id)
    await _handle_message(conv.conversation_id, SimpleNamespace(app=app), {"type": "message", "content": "next"}, "test-user")
    manager.release_turn(conv.conversation_id)
    return seen


class TestIsolatedChildContext:
    async def test_child_starts_without_history_and_gets_the_tool(self, agent_spy, db, make_project):
        project, conv, child_slug = await _setup(db, make_project, "isocx")
        seen = await _run_two_turns(agent_spy, conv, project, child_slug)

        child = seen["child_call"]
        # No history preloaded — the seeded rows stay out of the child's window.
        assert child["history"] == []
        # The system note tells the child how to recover context on demand.
        assert "WITHOUT the conversation history" in child["user_message"]
        assert "load_conversation_history" in child["user_message"]
        # The injected tool is in the child's registry...
        assert "load_conversation_history" in child["_tools"]
        # ...and it still honors its own frontmatter allowlist otherwise.
        assert "filesystem" in child["_tools"]
        assert seen["result"]["status"] == "ok"

    async def test_root_turns_keep_history_and_never_see_the_tool(self, agent_spy, db, make_project):
        project, conv, child_slug = await _setup(db, make_project, "isort")
        seen = await _run_two_turns(agent_spy, conv, project, child_slug)

        # The parent (delegating root turn) reads the seeded history as usual —
        # its own user message is persisted only after the history load.
        assert [m.content for m in seen["parent_call"]["history"] if m.role == "user"] \
            == ["earlier question"]
        assert "load_conversation_history" not in seen["parent_call"]["_tools"]
        # The second root turn is unchanged too (its history now also has the
        # turn-1 user message, which was persisted after turn 1's load).
        assert [m.content for m in seen["root2_call"]["history"] if m.role == "user"] \
            == ["earlier question", "hi"]
        assert "load_conversation_history" not in seen["root2_call"]["_tools"]

    async def test_flag_off_restores_legacy_behavior(self, agent_spy, db, make_project, monkeypatch):
        monkeypatch.setattr(get_settings(), "agent_delegation_isolated_context", False)
        project, conv, child_slug = await _setup(db, make_project, "isole")
        seen = await _run_two_turns(agent_spy, conv, project, child_slug)

        child = seen["child_call"]
        # Preloaded history, with the attribution prefix rule intact. By
        # delegation time the turn-1 user message is already persisted, so the
        # legacy child sees it too.
        assert [m.content for m in child["history"] if m.role == "user"] == ["earlier question", "hi"]
        assert "[isoqa--parent]:\nearlier answer" in [
            m.content for m in child["history"] if m.role == "assistant"
        ]
        # No tool injected; the legacy note keeps its [name]-prefixed sentence.
        assert "load_conversation_history" not in child["_tools"]
        assert "WITHOUT" not in child["user_message"]
        assert "prefixed with [name]" in child["user_message"]


class TestLoadHistoryPage:
    async def _seed(self, db, make_project, tag: str):
        # The test DB is session-scoped — project slugs must be unique per test.
        project = await make_project(tag)
        conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        rows = (
            (1, "user", "q1", None),
            (2, "assistant", "mine", "viewer"),
            (3, "assistant", "theirs", "other"),
            (4, "tool", "tool noise", None),
            (5, "assistant", "", "other"),          # empty — filtered out
            (6, "user", "q2", None),
        )
        for seq, role, content, slug in rows:
            db.add(Message(
                conversation_id=conv.conversation_id, role=role, content=content,
                agent_slug=slug, sequence_num=seq,
            ))
        await db.commit()
        return conv

    async def test_attribution_prefixes_only_other_agents(self, db, make_project):
        conv = await self._seed(db, make_project, "isopg1")

        page = await load_history_page(db, conv.conversation_id, viewer_agent_slug="viewer")

        by_seq = {m["sequence"]: m for m in page["messages"]}
        assert by_seq[1]["role"] == "user" and by_seq[1]["text"] == "q1"
        # The viewer's own reply stays bare; another agent's gets the marker
        # (an unknown slug falls back to the slug itself, as in _load_history).
        assert by_seq[2]["text"] == "mine"
        assert by_seq[3]["text"] == "[other]:\ntheirs"
        # Tool rows and empty content never surface.
        assert 4 not in by_seq and 5 not in by_seq
        assert page["has_more"] is False and page["next_before"] == 1

    async def test_cursor_pagination_walks_backwards(self, db, make_project):
        conv = await self._seed(db, make_project, "isopg2")

        newest = await load_history_page(db, conv.conversation_id, limit=2)
        # Newest page: q2 (6) and theirs (3) — oldest-first order within the page.
        assert [m["sequence"] for m in newest["messages"]] == [3, 6]
        assert newest["has_more"] is True
        assert newest["next_before"] == 3

        older = await load_history_page(
            db, conv.conversation_id, limit=2, before=newest["next_before"],
        )
        assert [m["sequence"] for m in older["messages"]] == [1, 2]
        assert older["has_more"] is False
        assert older["next_before"] == 1

    async def test_agent_filter_and_empty_results(self, db, make_project):
        conv = await self._seed(db, make_project, "isopg3")

        only_other = await load_history_page(
            db, conv.conversation_id, agent="other", viewer_agent_slug="viewer",
        )
        assert [m["sequence"] for m in only_other["messages"]] == [3]

        past_start = await load_history_page(db, conv.conversation_id, before=1)
        assert past_start == {
            "messages": [], "count": 0, "has_more": False, "next_before": None,
        }

    async def test_long_text_is_truncated_head_and_tail(self, db, make_project):
        project = await make_project("isotr")
        conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        long_text = "HEAD" + "x" * 5000 + "TAIL"
        db.add(Message(
            conversation_id=conv.conversation_id, role="user", content=long_text,
            agent_slug=None, sequence_num=1,
        ))
        await db.commit()

        page = await load_history_page(
            db, conv.conversation_id, max_chars_per_message=200,
        )

        text = page["messages"][0]["text"]
        assert text.startswith("HEAD")
        assert text.endswith("TAIL")
        assert len(text) < len(long_text)
        assert "…" in text

    async def test_total_cap_marks_more_history_available(self, db, make_project):
        project = await make_project("isocap")
        conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        for seq in range(1, 6):
            db.add(Message(
                conversation_id=conv.conversation_id, role="user",
                content=f"m{seq} " + "y" * 300, agent_slug=None, sequence_num=seq,
            ))
        await db.commit()

        page = await load_history_page(
            db, conv.conversation_id, limit=5, max_total_chars=400,
        )

        # The cap keeps the NEWEST contiguous segment, and the caller can page.
        assert page["has_more"] is True
        assert page["messages"][-1]["sequence"] == 5
        assert page["next_before"] == page["messages"][0]["sequence"]


def project_fs(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)
