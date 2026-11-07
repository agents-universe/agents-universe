"""The ``## Available Agents`` roster section in the system prompt.

An agent that declares ``delegate_agent`` must see who exists without spending
a ``list_agents`` call first. The section is a per-turn filesystem snapshot
with the same source and policy/chain filtering as the tool, so the snapshot
never advertises a slug a later ``delegate_agent`` call would refuse.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_core.agent import Agent, AgentConfig
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.base import ToolContext

_AGENT_MD = """---
slug: "{slug}"
display_name: "{name}"
description: "{desc}"
category: "{category}"
---

Body.
"""


def _write_agent(dir_path: Path, slug: str, *, category: str = "agile-development") -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{slug}.agent.md").write_text(
        _AGENT_MD.format(slug=slug, name=slug, desc=f"{slug} does things", category=category),
        encoding="utf-8",
    )


def _framework(tmp_path: Path) -> Path:
    root = tmp_path / "framework"
    (root / "agents").mkdir(parents=True, exist_ok=True)
    return root


def _workspace(tmp_path: Path, slug: str = "proj-a") -> Path:
    ws = tmp_path / slug
    (ws / "agents").mkdir(parents=True, exist_ok=True)
    return ws


def _make_agent(
    tmp_path: Path,
    tools: list[str],
    *,
    slug: str = "test-agent",
    framework: Path | None = None,
    workspace: Path | None = None,
    delegation: object | None = None,
) -> Agent:
    framework = framework if framework is not None else _framework(tmp_path)
    workspace = workspace if workspace is not None else _workspace(tmp_path)
    config = AgentConfig(
        slug=slug,
        description="test",
        system_prompt="You are a test agent.",
        tools=tools,
    )
    tool_context = ToolContext(
        project_id="p1",
        project_fs_path=str(workspace),
        conversation_id="c1",
        user_id="u1",
        framework_root=str(framework),
    )
    tool_context.delegation = delegation
    return Agent(
        config=config,
        credentials={},
        tier_models={},
        skill_registry=SkillRegistry(),
        tool_context=tool_context,
    )


def _seed_framework(framework: Path) -> None:
    _write_agent(framework / "agents", "tech-lead")
    _write_agent(framework / "agents", "quality-assurance", category="platform-assistant")


# --- the section itself ------------------------------------------------------


def test_delegate_declaring_agent_gets_roster_section(tmp_path):
    framework = _framework(tmp_path)
    _seed_framework(framework)
    agent = _make_agent(tmp_path, ["delegate_agent", "list_agents"], framework=framework)

    prompt = agent._get_static_prompt()

    assert "## Available Agents" in prompt
    assert "- `tech-lead` —" in prompt
    assert "- `quality-assurance` —" in prompt
    # The tools section (which advertises delegate_agent) comes first.
    assert prompt.index("## Available Tools & Behaviors") < prompt.index("## Available Agents")


def test_section_absent_without_delegate_agent_declaration(tmp_path):
    """The condition is the frontmatter declaration — an agent that never
    asked for delegate_agent must not pay for (or see) a roster."""
    framework = _framework(tmp_path)
    _seed_framework(framework)
    agent = _make_agent(tmp_path, ["filesystem"], framework=framework)

    assert "## Available Agents" not in agent._get_static_prompt()


def test_empty_roster_has_no_heading(tmp_path):
    framework = _framework(tmp_path)  # empty agents/ dir
    agent = _make_agent(tmp_path, ["delegate_agent"], framework=framework)

    assert "## Available Agents" not in agent._get_static_prompt()


# --- filtering: same rules as the tool --------------------------------------


def test_policy_allowlist_restricts_roster(tmp_path):
    framework = _framework(tmp_path)
    _seed_framework(framework)
    delegation = SimpleNamespace(chain=(), policy=("tech-lead",))
    agent = _make_agent(
        tmp_path, ["delegate_agent"], framework=framework, delegation=delegation
    )

    prompt = agent._get_static_prompt()

    assert "- `tech-lead` —" in prompt
    assert "quality-assurance" not in prompt.split("## Available Agents", 1)[1]


def test_self_and_chain_are_excluded(tmp_path):
    """Root runs have chain=(self,); a nested run's chain holds every ancestor.
    Neither self nor an ancestor may appear — delegating up the chain is
    refused at execute time, so advertising it would only earn refusals."""
    framework = _framework(tmp_path)
    _seed_framework(framework)
    _write_agent(framework / "agents", "orchestrator")
    delegation = SimpleNamespace(chain=("orchestrator", "tech-lead"), policy=())
    agent = _make_agent(
        tmp_path,
        ["delegate_agent"],
        slug="orchestrator",
        framework=framework,
        delegation=delegation,
    )

    roster = agent._get_static_prompt().split("## Available Agents", 1)[1]

    assert "orchestrator" not in roster
    assert "- `tech-lead` —" not in roster
    assert "- `quality-assurance` —" in roster


# --- freshness: the snapshot rebuilds with the static prompt -----------------


def test_roster_picks_up_agents_created_after_first_build(tmp_path):
    """Project agents are created at runtime without a restart (customization
    expert writes files). The static cache lives one turn; invalidation (or
    the next user message constructing a fresh Agent) must rebuild the roster."""
    framework = _framework(tmp_path)
    workspace = _workspace(tmp_path)
    _seed_framework(framework)
    agent = _make_agent(
        tmp_path, ["delegate_agent"], framework=framework, workspace=workspace
    )

    assert "proj-a--helper" not in agent._get_static_prompt()

    _write_agent(workspace / "agents", "proj-a--helper", category="platform-assistant")
    # Without invalidation the cached snapshot would still be served.
    assert "proj-a--helper" not in agent._get_static_prompt()

    agent._invalidate_static_cache()
    prompt = agent._get_static_prompt()

    assert "- `proj-a--helper` —" in prompt
