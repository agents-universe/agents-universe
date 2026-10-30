"""Public workflows appear in every agent's Available Workflows section.

Same semantics as public skills: a ``public: true`` workflow in the (overlay)
registry is listed for agents whose frontmatter never mentions it, and a slug
already declared by the agent is not duplicated.
"""
from __future__ import annotations

from agent_core.agent import Agent, AgentConfig
from agent_core.skills.loader import SkillDefinition
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.base import ToolContext


def _workflow(slug: str, *, public: bool = False) -> SkillDefinition:
    return SkillDefinition(
        slug=slug,
        skill_type="guidance",
        description=f"{slug} steps",
        public=public,
        body="STEPS BODY",
    )


def _registry(*workflows: SkillDefinition) -> SkillRegistry:
    reg = SkillRegistry()
    for wf in workflows:
        reg._skills[wf.slug] = wf
    return reg


def _make_agent(config_workflows: list[str], registry: SkillRegistry) -> Agent:
    config = AgentConfig(
        slug="test-agent",
        description="test",
        system_prompt="You are a test agent.",
        tools=[],
        workflows=config_workflows,
    )
    tool_context = ToolContext(
        project_id="p1",
        project_fs_path="C:/projects/p1",
        conversation_id="c1",
        user_id="u1",
    )
    return Agent(
        config=config,
        credentials={},
        tier_models={},
        skill_registry=SkillRegistry(),
        tool_context=tool_context,
        workflow_registry=registry,
    )


def test_public_workflow_listed_without_frontmatter():
    agent = _make_agent([], _registry(_workflow("knowledge-ingestion", public=True)))
    prompt = agent._build_system_prompt()
    assert "## Available Workflows" in prompt
    assert "- **knowledge-ingestion**: knowledge-ingestion steps" in prompt


def test_public_workflow_not_duplicated_when_declared():
    agent = _make_agent(
        ["knowledge-ingestion"],
        _registry(_workflow("knowledge-ingestion", public=True)),
    )
    prompt = agent._build_system_prompt()
    assert prompt.count("**knowledge-ingestion**") == 1


def test_non_public_workflow_stays_frontmatter_only():
    agent = _make_agent(
        ["declared-only"],
        _registry(
            _workflow("declared-only"),
            _workflow("private-one"),
        ),
    )
    prompt = agent._build_system_prompt()
    assert "**declared-only**" in prompt
    assert "private-one" not in prompt


def test_no_section_without_workflows():
    agent = _make_agent([], _registry())
    prompt = agent._build_system_prompt()
    assert "## Available Workflows" not in prompt


def test_public_workflow_appended_after_declared():
    """Agent-declared ordering stays stable; public entries append after."""
    agent = _make_agent(
        ["first", "second"],
        _registry(
            _workflow("first"),
            _workflow("second"),
            _workflow("knowledge-ingestion", public=True),
        ),
    )
    prompt = agent._build_system_prompt()
    section = prompt.split("## Available Workflows", 1)[1]
    assert section.index("**first**") < section.index("**second**") < section.index(
        "**knowledge-ingestion**"
    )
