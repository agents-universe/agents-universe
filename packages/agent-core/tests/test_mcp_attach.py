"""Tests for MCP tool assembly: agent.py mcp marker filtering + add_tools()."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.tools.base import Tool, ToolContext


class _DummyTool(Tool):
    """Minimal Tool for testing."""

    def __init__(self, tool_name: str):
        self._name = tool_name

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"Dummy {self._name}"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, params, context):
        return {"ok": True}


def _make_agent(tool_names: list[str]) -> Agent:
    """Create a minimal Agent without running anything."""
    config = AgentConfig(
        slug="test-agent",
        description="Test",
        system_prompt="test",
        tools=tool_names,
    )
    ctx = ToolContext(
        project_id="p1",
        project_fs_path="/tmp/test",
        conversation_id="c1",
        user_id="u1",
    )
    return Agent(
        config=config,
        credentials={},
        tier_models={},
        skill_registry=MagicMock(),
        tool_context=ctx,
    )


# ---------------------------------------------------------------------------
# mcp marker filtering
# ---------------------------------------------------------------------------


def test_mcp_markers_filtered_from_static_registry():
    """mcp and mcp:<slug> markers should not trigger 'tool not found' warnings."""
    agent = _make_agent(["shell", "mcp", "mcp:github-copilot"])
    # Static tools (shell) should be present.
    assert "shell" in agent._tools
    # MCP markers should NOT appear as tools (they're not real tool names).
    assert "mcp" not in agent._tools
    assert "mcp:github-copilot" not in agent._tools


def test_no_mcp_markers_all_static():
    """When no mcp markers are declared, all tools are static."""
    agent = _make_agent(["shell", "web_fetch"])
    assert set(agent._tools.keys()) >= {"shell", "web_fetch"}


# ---------------------------------------------------------------------------
# add_tools
# ---------------------------------------------------------------------------


def test_add_tools_injects_new():
    agent = _make_agent(["shell"])
    new_tools = {
        "mcp__server__echo": _DummyTool("mcp__server__echo"),
        "mcp__server__adder": _DummyTool("mcp__server__adder"),
    }
    agent.add_tools(new_tools)
    assert "mcp__server__echo" in agent._tools
    assert "mcp__server__adder" in agent._tools
    # Also registered in tool context.
    assert agent._tool_ctx.get_tool("mcp__server__echo") is not None


def test_add_tools_collision_skips():
    """Built-in tool names take precedence over dynamically added tools."""
    agent = _make_agent(["shell"])
    collision = {
        "shell": _DummyTool("shell"),  # collides with built-in
    }
    agent.add_tools(collision)
    # The original shell tool should still be there, not the dummy.
    tool = agent._tools["shell"]
    assert not isinstance(tool, _DummyTool)


def test_add_tools_empty():
    agent = _make_agent(["shell"])
    agent.add_tools({})
    assert "shell" in agent._tools
