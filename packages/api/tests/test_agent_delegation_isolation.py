"""Project isolation for delegation (CLAUDE.md #6).

Delegation resolves an agent by slug *inside the calling conversation's
project*, so isolation has to hold on two levels: a project's roster must not
advertise another project's agents, and passing such a slug directly — a
hand-written tool call, or a model working from a stale conversation — must be
refused outright. Neither is a filter applied after the fact: the definition
lookup is rooted in the project's workspace, so another project's agent simply
is not reachable, and the refusal is logged because a cross-project attempt is
a bug signal rather than a normal outcome.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core.agent import Agent
from agent_core.delegation import list_delegation_candidates
from api.main import app
from api.models.conversation import Conversation
from api.paths import AGENTS_DIR, PROJECTS_ROOT, WORKFLOWS_DIR
from api.services.delegation import DelegationContext, run_delegated_turn


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


@pytest.fixture
def agent_spy(monkeypatch):
    state: dict = {"calls": []}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["calls"].append(kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


@pytest.fixture
def parent_session():
    class _S:
        async def emit(self, *a, **kw):
            pass

    return _S()


def _write_agent(project, slug: str, display_name: str) -> None:
    fs = PROJECTS_ROOT / project.slug
    (fs / "agents").mkdir(parents=True, exist_ok=True)
    (fs / "agents" / f"{slug}.agent.md").write_text(
        "---\n"
        f"slug: {slug}\n"
        f"display_name: {display_name}\n"
        "description: test agent\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )


def _ctx(session, project, conv) -> SimpleNamespace:
    return SimpleNamespace(
        delegation=DelegationContext(
            chain=("iso--parent",), parent_session=session, actor_user_id="test-user",
        ),
        app=app,
        project_fs_path=str(PROJECTS_ROOT / project.slug),
        conversation_id=conv.conversation_id,
        interactive=True,
    )


def _slugs(entries: list[Any]) -> set[str]:
    return {e.slug for e in entries}


@pytest.fixture
async def two_projects(db, make_project):
    """Project A and project B, each with one agent of its own.

    Slugs are unique per test: project slugs are unique in the DB, so a fixed
    pair would collide on the second test that asks for one.
    """
    tag = uuid.uuid4().hex[:8]
    proj_a = await make_project(f"isoa-{tag}")
    proj_b = await make_project(f"isob-{tag}")
    a_slug, b_slug = f"{proj_a.slug}--helper", f"{proj_b.slug}--helper"
    _write_agent(proj_a, a_slug, "A Helper")
    _write_agent(proj_b, b_slug, "B Helper")
    return SimpleNamespace(a=proj_a, b=proj_b, a_slug=a_slug, b_slug=b_slug)


async def _conversation_in(db, project):
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def test_roster_of_one_project_never_lists_the_other(two_projects):
    projs = two_projects

    a_slugs = _slugs(list_delegation_candidates(
        str(PROJECTS_ROOT / projs.a.slug), str(PROJECTS_ROOT),
    ))
    b_slugs = _slugs(list_delegation_candidates(
        str(PROJECTS_ROOT / projs.b.slug), str(PROJECTS_ROOT),
    ))

    assert projs.a_slug in a_slugs
    assert projs.b_slug not in a_slugs
    assert projs.b_slug in b_slugs
    assert projs.a_slug not in b_slugs


async def test_a_project_agent_that_keeps_the_other_project_name_is_unreachable(
    db, two_projects, agent_spy, parent_session, caplog
):
    """The slug is self-describing, so a stray one is refused, not resolved."""
    projs = two_projects
    conv = await _conversation_in(db, projs.a)

    with caplog.at_level(logging.WARNING, logger="agents_universe.ws"):
        result = await run_delegated_turn(
            _ctx(parent_session, projs.a, conv),
            agent_slug=projs.b_slug, brief="b", reason="r",
        )

    assert agent_spy["calls"] == [], "a cross-project delegation still ran a turn"
    assert result["status"] == "refused"
    assert any(
        projs.b_slug in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


async def test_a_global_agent_is_reachable_from_any_project(
    db, two_projects, parent_session
):
    """Isolation bounds project agents; the framework-wide roster is shared."""
    projs = two_projects
    conv = await _conversation_in(db, projs.a)

    global_slug = next(
        (
            p.stem.removesuffix(".agent")
            for p in sorted(AGENTS_DIR.glob("*.agent.md"))
            if "--" not in p.stem
        ),
        None,
    )
    assert global_slug, "no global agent definitions found"

    framework_root = str(AGENTS_DIR.parent)
    assert global_slug in _slugs(list_delegation_candidates(
        str(PROJECTS_ROOT / projs.a.slug), framework_root,
    ))
    # A resolved definition is what "reachable" means here: actually running the
    # turn is covered by the other delegation tests, and would need a model
    # config this test is not about.
    from api.services.delegation import _resolve_definition

    assert _resolve_definition(_ctx(parent_session, projs.a, conv), global_slug)
