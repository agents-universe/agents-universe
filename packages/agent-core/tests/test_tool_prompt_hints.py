"""Tests for the '## Available Tools & Behaviors' system prompt section."""
from __future__ import annotations

from agent_core.agent import Agent, AgentConfig
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.base import ToolContext
from agent_core.tools.registry import (
    _CORE_TOOLS,
    _OPTIONAL_TOOL_MODULES,
    _load_optional,
    build_tool_registry,
)


def _make_agent(tool_names: list[str]) -> Agent:
    config = AgentConfig(
        slug="test-agent",
        description="test",
        system_prompt="You are a test agent.",
        tools=tool_names,
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
    )


def test_static_prompt_contains_tools_section():
    agent = _make_agent(["filesystem", "shell"])
    prompt = agent._get_static_prompt()

    assert "## Available Tools & Behaviors" in prompt
    assert prompt.index("## Interaction Style") < prompt.index("## Available Tools & Behaviors")
    assert "- **filesystem** —" in prompt
    assert "- **shell** —" in prompt
    assert "Behavior rules:" in prompt


def test_tools_section_lists_only_enabled_tools():
    agent = _make_agent(["filesystem", "knowledge_rw"])
    prompt = agent._get_static_prompt()

    section = prompt.split("## Available Tools & Behaviors", 1)[1]
    assert "- **filesystem** —" in section
    assert "- **knowledge_rw** —" in section
    assert "- **shell** —" not in section
    assert "- **sql_query** —" not in section


def test_tools_section_uses_prompt_hint_not_description():
    agent = _make_agent(["filesystem"])
    prompt = agent._get_static_prompt()
    tool = agent._tools["filesystem"]

    assert f"- **filesystem** — {tool.prompt_hint}" in prompt


def test_every_registered_tool_has_non_empty_prompt_hint():
    registry = build_tool_registry()
    for name, tool in registry.items():
        assert tool.prompt_hint.strip(), f"Tool {name!r} has an empty prompt_hint"


def test_plan_task_always_injected_even_when_tools_omit_it():
    # plan_task is framework-level behavior (the agent loop intercepts it into
    # task mode), not an ordinary tool an agent definition may forget. An
    # explicit tools: list that omits it must still get it — otherwise
    # workflow-driven agents silently lose the visible plan card.
    registry = build_tool_registry(["shell", "filesystem"])
    assert "plan_task" in registry
    assert set(registry) >= {"shell", "filesystem", "plan_task"}
    # And an empty explicit list (agent that lists no tools) still plans.
    registry = build_tool_registry([])
    assert "plan_task" in registry


def test_allowlist_is_exact_plan_task_is_the_only_exemption():
    # plan_task is exempt from an explicit tools: list; every OTHER core tool
    # must honor the declaration. A tool omitted from the list (here memory_rw
    # and knowledge_rw) must NOT be injected — the system prompt promises the
    # agent "exactly the tools listed below", and memory_rw's secret-rejection
    # guard is bypassable, so an undeclared memory_rw is a silent credential
    # leak vector on agents whose definitions never asked for it.
    registry = build_tool_registry(["shell", "filesystem"])
    assert set(registry) == {"shell", "filesystem", "plan_task"}
    assert "memory_rw" not in registry
    assert "knowledge_rw" not in registry
    # Declaring it makes it available again.
    registry = build_tool_registry(["shell", "memory_rw"])
    assert "memory_rw" in registry
    # The agent's prompt section must not advertise undeclared tools.
    agent = _make_agent(["shell"])
    section = agent._get_static_prompt().split("## Available Tools & Behaviors", 1)[1]
    assert "- **shell** —" in section
    assert "- **memory_rw** —" not in section
    assert "- **filesystem** —" not in section


def test_every_declared_tool_class_loads_with_hint():
    classes = list(_CORE_TOOLS)
    for module_name in _OPTIONAL_TOOL_MODULES:
        cls = _load_optional(module_name)
        assert cls is not None, f"Optional tool {module_name!r} failed to load"
        classes.append(cls)
    for cls in classes:
        assert cls.prompt_hint.strip(), f"Tool class {cls.__name__} has an empty prompt_hint"
