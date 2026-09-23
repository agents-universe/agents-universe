"""Matched skill bodies must survive system-prompt rebuilds mid-turn.

run() used to append matched skills to a local `system` string — the moment a
plan_task pipeline called _mark_prompt_dirty and the prompt was rebuilt, the
skill body evaporated. The match is now stored on the instance
(`_active_skills`) and rendered inside the dynamic prompt section, so every
rebuild keeps it. It resets at the start of each run() (per-turn semantics).
"""
from __future__ import annotations

from agent_core.agent import Agent, AgentConfig
from agent_core.skills.loader import SkillDefinition
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.base import ToolContext


def _registry_with_data_setup() -> SkillRegistry:
    reg = SkillRegistry()
    reg._skills["testing/test-data-setup"] = SkillDefinition(
        slug="testing/test-data-setup",
        skill_type="guidance",
        description="data setup recipes",
        triggers=["造测试数据", "test data"],
        body="RECIPE LOOP BODY: one probe per channel, write back immediately.",
    )
    return reg


def _make_agent() -> Agent:
    config = AgentConfig(
        slug="test-agent",
        description="test",
        system_prompt="You are a test agent.",
        tools=[],
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
        skill_registry=_registry_with_data_setup(),
        tool_context=tool_context,
    )


def test_fresh_agent_has_no_active_skills():
    agent = _make_agent()
    assert agent._active_skills == []


def test_active_skills_rendered_in_dynamic_prompt():
    agent = _make_agent()
    agent._active_skills = agent._skill_registry.matching_triggers("帮我造测试数据")
    assert agent._active_skills

    dynamic = agent._build_dynamic_prompt()
    assert "## Active Skill: testing/test-data-setup" in dynamic
    assert "RECIPE LOOP BODY" in dynamic


def test_active_skills_survive_prompt_rebuild():
    """The plan_task regression: _mark_prompt_dirty → rebuild must keep the body."""
    agent = _make_agent()
    agent._active_skills = agent._skill_registry.matching_triggers("帮我造测试数据")
    first = agent._build_system_prompt()
    assert "RECIPE LOOP BODY" in first

    # Mid-turn rebuild — what the task loop does after each knowledge write.
    agent._mark_prompt_dirty()
    second = agent._build_system_prompt()

    assert "## Active Skill: testing/test-data-setup" in second
    assert "RECIPE LOOP BODY" in second


def test_active_skills_reset_between_runs():
    """Per-turn semantics: run() reassigns _active_skills from the new message
    (a non-matching message clears the previous match)."""
    agent = _make_agent()
    agent._active_skills = agent._skill_registry.matching_triggers("帮我造测试数据")
    assert agent._active_skills

    # Emulate run()'s per-turn reassignment for a message with no matches.
    agent._active_skills = agent._skill_registry.matching_triggers("hello there")
    assert agent._active_skills == []
    assert "RECIPE LOOP BODY" not in agent._build_dynamic_prompt()


def test_no_active_skills_prompt_unchanged():
    agent = _make_agent()
    dynamic = agent._build_dynamic_prompt()
    assert "Active Skill" not in dynamic
