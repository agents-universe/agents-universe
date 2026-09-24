"""The seam between "declared in frontmatter" and "the subtask actually ran".

Every other delegation test starts partway down the path: the guard tests patch
``Agent`` out, the tool tests stub ``api.services.delegation``, the roster tests
only read the filesystem. None of them proves the ends are connected — that an
agent whose frontmatter lists ``delegate_agent`` really gets that tool, and that
calling it reaches the real ``run_delegated_turn``.

That chain fails *quietly* when it breaks, which is why it needs its own test:
``delegate_agent`` is an optional module and the branch that consumes it is
``getattr``-driven. A dropped registry key or a frontmatter line lost in an edit
leaves every other test green — the agent simply never delegates. The registry
does log a warning (``Agent declares tools not found in registry``), but nothing
reaches the conversation, so from the user's side the capability is just gone.

The last test pins the same seam for the shipped definitions, because the
framework's own prose now promises hand-offs ("QA delegates PR review to
tech-lead", the demo workflow delegates to two agents) that only work if those
frontmatter lists stay intact.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from agent_core.session import ConversationSession
from agent_core.tools.agent_delegate import AgentDelegateTool, ListAgentsTool
from agent_core.tools.registry import build_tool_registry
from api.main import app
from api.models.conversation import Conversation, Message
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.routers.agents import _sync_project_agents
from api.services.delegation import DelegationContext
from api.services.token_vault import encrypt


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
    state: dict = {"behavior": None, "calls": []}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["calls"].append(kwargs)
            if state["behavior"] is not None:
                await state["behavior"](kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


def project_fs(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)


def _declared_tools(path: Path) -> list[str]:
    """The tools: list a definition declares, as ``Agent`` reads it."""
    from agent_core.agent import AgentConfig

    return list(AgentConfig.from_file(str(path)).tools)


def test_a_frontmatter_tools_list_is_what_gates_the_two_tools(tmp_path):
    """Allowlist semantics: no declaration, no tool — and no crash either."""
    declaring = build_tool_registry(["filesystem", "delegate_agent", "list_agents"])
    assert isinstance(declaring["delegate_agent"], AgentDelegateTool)
    assert isinstance(declaring["list_agents"], ListAgentsTool)

    silent = build_tool_registry(["filesystem"])
    assert "delegate_agent" not in silent
    assert "list_agents" not in silent
    # plan_task rides along with every list; the delegation pair must not.
    assert "plan_task" in silent


def test_a_shipped_agent_that_promises_a_handoff_can_delegate():
    """Prose in the framework's own agents must match their frontmatter.

    These four are named by shipped instructions — QA hands PR review to
    tech-lead, the demo workflow delegates to tech-lead and quality-assurance,
    and the customer-service pipeline delegates a lookup — so losing the tool
    from any of them turns documented behavior into a silent no-op.
    """
    for slug in ("project-owner", "quality-assurance", "tech-lead", "customer-service"):
        path = AGENTS_DIR / f"{slug}.agent.md"
        registry = build_tool_registry(_declared_tools(path))
        assert "delegate_agent" in registry, f"{slug} is told to delegate but cannot"
        assert "list_agents" in registry, f"{slug} cannot discover who to delegate to"


async def test_the_tool_call_lands_in_a_real_delegated_turn(
    agent_spy, db, make_project
):
    """A parent calls the tool it was given by name; the child really runs."""
    project = await make_project("toolpath")
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

    parent_slug = f"{project.slug}--parent"
    child_slug = f"{project.slug}--helper"
    agents_dir = Path(project_fs(project)) / "agents"
    # The parent declares the pair, exactly as a shipped definition does; the
    # child deliberately declares only filesystem, so a child that tried to
    # delegate onward would find no tool to do it with.
    (agents_dir / f"{parent_slug}.agent.md").write_text(
        "---\n"
        f"slug: {parent_slug}\n"
        "display_name: Parent\n"
        "description: test agent\n"
        "tools: [filesystem, delegate_agent, list_agents]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    (agents_dir / f"{child_slug}.agent.md").write_text(
        "---\n"
        f"slug: {child_slug}\n"
        "display_name: Helper Bot\n"
        "description: test agent\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    await _sync_project_agents(db, project)

    # The tool instance the parent would actually hold, from its own list.
    registry = build_tool_registry(_declared_tools(agents_dir / f"{parent_slug}.agent.md"))
    tool = registry["delegate_agent"]

    # A real session, not a stub: `delegate_agent` publishes its progress
    # events through the parent's session, and that session is also the prompt
    # sink a child's confirmation dialog registers on (G3). A stub here would
    # pass this test and then fail the first time a child asked a question.
    parent_session = ConversationSession(conv.conversation_id, project.project_id, "test-user")
    context = SimpleNamespace(
        delegation=DelegationContext(
            chain=(parent_slug,), parent_session=parent_session, actor_user_id="test-user",
        ),
        app=app,
        project_fs_path=project_fs(project),
        conversation_id=conv.conversation_id,
        interactive=True,
    )

    child_msg_id = str(uuid.uuid4())

    async def _behavior(kwargs):
        # No delegation from the child: it has no such tool, and this test is
        # about the parent's call reaching the service.
        await kwargs["session"].emit("stream_delta", delta="child reply")
        await kwargs["session"].emit("stream_end", message_id=child_msg_id, total_tokens=3)

    agent_spy["behavior"] = _behavior

    result = await tool.execute(
        {"agent": child_slug, "brief": "do the thing", "reason": "missing tool"},
        context,
    )

    assert result["status"] == "ok", result
    assert result["agent"] == child_slug
    assert result["agent_name"] == "Helper Bot"
    assert result["summary"] == "child reply"
    assert result["message_id"] == child_msg_id
    # The delegation tool card renders output.duration_ms — a completed run
    # must carry it or the card silently drops the duration line.
    assert isinstance(result.get("duration_ms"), int)
    assert result["duration_ms"] >= 0
    # The list tool from the same registry sees the child and not the parent.
    listed = registry["list_agents"].execute(
        {}, SimpleNamespace(
            project_fs_path=project_fs(project),
            framework_root=str(AGENTS_DIR.parent),
            delegation=context.delegation,
        )
    )
    slugs = [a["slug"] for a in (await listed)["agents"]]
    assert child_slug in slugs
    assert parent_slug not in slugs

    # It ran the real turn: the child's reply is a persisted message under the
    # child's slug, not a value the tool made up.
    rows = (await db.execute(
        select(Message).where(Message.conversation_id == conv.conversation_id)
    )).scalars().all()
    child_row = next(r for r in rows if r.message_id == child_msg_id)
    assert child_row.agent_slug == child_slug
    assert child_row.content == "child reply"

    # Progress events travel on the parent's session — the route the client
    # actually sees, since a child's own session is never registered.
    events: list[tuple[str, dict]] = []
    while not parent_session._event_queue.empty():
        event = parent_session._event_queue.get_nowait()
        if event is not None:
            events.append((event.type, event.data))
    kinds = [t for t, _ in events]
    assert "delegate_started" in kinds, kinds
    assert "delegate_finished" in kinds, kinds
    started = dict(events)[ "delegate_started"]
    assert started["agent"] == child_slug
    assert started["reason"] == "missing tool"
    assert started["depth"] == 1
