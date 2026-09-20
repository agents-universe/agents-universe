"""The delegate_agent / list_agents tools.

Two contracts are worth pinning here.

*The loop must never see an exception.* A delegation that blew up is a failed
subtask, not a failed turn: the model has to get a result dict it can read and
act on, whatever the API layer or the child agent did.

*The API stays lazy.* agent-core has no dependency on the web service, so these
tools reach ``run_delegated_turn`` through the same deferred import the
scheduler tool uses, and degrade to a refusal when it is unavailable — an
agent-core-only run (CLI, tests, an embedded agent) still has the tool in its
registry.

The API side is stubbed through ``sys.modules``, exactly as
``test_scheduler_tool.py`` does it.
"""
from __future__ import annotations

import sys
import types

import pytest

from agent_core.tools.agent_delegate import AgentDelegateTool, ListAgentsTool
from agent_core.tools.base import ToolContext

BRIEF = "Review PR #12 and report the blocking issues."
REASON = "I have no repository access."


def _frozen(**overrides):
    """A stand-in for api.services.delegation.DelegationContext."""
    fields = {"chain": ("parent",), "policy": (), "parent_session": None, "holds_gate": False}
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


@pytest.fixture
def context(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "agents").mkdir(parents=True)
    framework_dir = tmp_path / "framework"
    (framework_dir / "agents").mkdir(parents=True)
    ctx = ToolContext(
        project_id="p1",
        project_fs_path=str(project_dir),
        conversation_id="c1",
        user_id="u1",
        framework_root=str(framework_dir),
    )
    ctx.delegation = _frozen()
    return ctx


@pytest.fixture
def api(monkeypatch):
    """Install a stub api.services.delegation; record what the tool asked for."""
    calls: list[dict] = []
    state: dict = {"result": {"status": "ok", "agent": "tech-lead", "summary": "done"}, "error": None}

    async def run_delegated_turn(context, *, agent_slug, brief, reason):
        calls.append({
            "context": context, "agent_slug": agent_slug, "brief": brief, "reason": reason,
        })
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    monkeypatch.setitem(sys.modules, "api.services.delegation", types.SimpleNamespace(
        run_delegated_turn=run_delegated_turn,
    ))
    return types.SimpleNamespace(calls=calls, state=state)


def _write_agent(dir_path, slug: str, *, display_name: str, description: str, category: str) -> None:
    (dir_path / f"{slug}.agent.md").write_text(
        "---\n"
        f"slug: {slug}\n"
        f"display_name: {display_name}\n"
        f"description: {description}\n"
        f"category: {category}\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )


class TestDelegateAgent:
    async def test_passes_the_request_through_and_returns_the_result_unchanged(self, context, api):
        api.state["result"] = {
            "status": "timeout", "agent": "tech-lead", "agent_name": "Tech Lead",
            "summary": "partial", "message_id": "m1", "tokens_used": 7,
            "error": "ran past its limit",
        }

        result = await AgentDelegateTool().execute(
            {"agent": "tech-lead", "brief": BRIEF, "reason": REASON}, context,
        )

        assert result == api.state["result"]
        assert api.calls == [{
            "context": context, "agent_slug": "tech-lead", "brief": BRIEF, "reason": REASON,
        }]

    @pytest.mark.parametrize("status", ["error", "aborted", "refused"])
    async def test_terminal_statuses_reach_the_model_intact(self, context, api, status):
        api.state["result"] = {"status": status, "agent": "tech-lead", "error": "why"}

        result = await AgentDelegateTool().execute(
            {"agent": "tech-lead", "brief": BRIEF, "reason": REASON}, context,
        )

        assert result["status"] == status
        assert result["error"] == "why"

    async def test_a_crash_becomes_a_result_instead_of_an_exception(self, context, api):
        """The tool is the agent loop's boundary: nothing may escape it."""
        api.state["error"] = RuntimeError("connection reset")

        result = await AgentDelegateTool().execute(
            {"agent": "tech-lead", "brief": BRIEF, "reason": REASON}, context,
        )

        assert result["status"] == "error"
        assert result["agent"] == "tech-lead"
        assert "RuntimeError" in result["error"]
        assert "connection reset" in result["error"]

    async def test_refuses_when_the_api_layer_is_absent(self, context, monkeypatch):
        """`None` in sys.modules is Python's own "this import must fail"."""
        monkeypatch.setitem(sys.modules, "api.services.delegation", None)

        result = await AgentDelegateTool().execute(
            {"agent": "tech-lead", "brief": BRIEF, "reason": REASON}, context,
        )

        assert result["status"] == "refused"
        assert "unavailable" in result["error"].lower()

    @pytest.mark.parametrize("missing", ["agent", "brief", "reason"])
    async def test_requires_all_three_arguments(self, context, api, missing):
        params = {"agent": "tech-lead", "brief": BRIEF, "reason": REASON}
        params[missing] = "  "

        result = await AgentDelegateTool().execute(params, context)

        assert result["status"] == "refused"
        assert missing in result["error"]
        assert api.calls == [], "an incomplete call still reached the API"


class TestListAgents:
    async def test_lists_project_and_framework_agents(self, context):
        _write_agent(
            _project_agents(context), "proj--reviewer",
            display_name="Reviewer", description="reviews code", category="quality",
        )
        _write_agent(
            _framework_agents(context), "tech-lead",
            display_name="Tech Lead", description="owns git", category="agile-development",
        )

        result = await ListAgentsTool().execute({}, context)

        assert {a["slug"] for a in result["agents"]} == {"proj--reviewer", "tech-lead"}
        assert result["total"] == 2
        # Useful to the model, and JSON-encodable.
        assert result["agents"][0]["display_name"]
        assert isinstance(result["agents"][0]["skills"], list)

    async def test_query_filters_on_the_text_the_model_would_search(self, context):
        agents_dir = _project_agents(context)
        _write_agent(agents_dir, "proj--reviewer", display_name="Reviewer",
                     description="reviews pull requests", category="quality")
        _write_agent(agents_dir, "proj--writer", display_name="Writer",
                     description="writes release notes", category="quality")

        result = await ListAgentsTool().execute({"query": "release"}, context)

        assert [a["slug"] for a in result["agents"]] == ["proj--writer"]

    async def test_category_filters_exactly(self, context):
        agents_dir = _project_agents(context)
        _write_agent(agents_dir, "proj--reviewer", display_name="Reviewer",
                     description="d", category="quality")
        _write_agent(agents_dir, "proj--writer", display_name="Writer",
                     description="d", category="content")

        result = await ListAgentsTool().execute({"category": "content"}, context)

        assert [a["slug"] for a in result["agents"]] == ["proj--writer"]

    async def test_the_calling_agent_and_its_chain_are_hidden(self, context):
        """Offering an agent the subtask it is already handling invites a loop."""
        agents_dir = _project_agents(context)
        for slug in ("proj--caller", "proj--ancestor", "proj--other"):
            _write_agent(agents_dir, slug, display_name=slug, description="d", category="quality")
        context.delegation = _frozen(chain=("proj--ancestor", "proj--caller"))

        result = await ListAgentsTool().execute({}, context)

        assert [a["slug"] for a in result["agents"]] == ["proj--other"]

    async def test_the_allowlist_hides_everything_else(self, context):
        agents_dir = _project_agents(context)
        for slug in ("proj--allowed", "proj--blocked"):
            _write_agent(agents_dir, slug, display_name=slug, description="d", category="quality")
        context.delegation = _frozen(policy=("proj--allowed",))

        result = await ListAgentsTool().execute({}, context)

        assert [a["slug"] for a in result["agents"]] == ["proj--allowed"]

    async def test_an_empty_roster_still_tells_the_model_what_to_do(self, context):
        result = await ListAgentsTool().execute({}, context)

        assert result["agents"] == []
        assert "note" in result


def _project_agents(context: ToolContext):
    from pathlib import Path

    return Path(context.project_fs_path) / "agents"


def _framework_agents(context: ToolContext):
    from pathlib import Path

    return Path(context.framework_root) / "agents"


def test_registry_exposes_both_tools():
    from agent_core.tools.registry import build_tool_registry, known_static_tool_names

    names = known_static_tool_names()
    assert "delegate_agent" in names
    assert "list_agents" in names

    # plan_task is always registered; the point here is that declaring these
    # two by name resolves them.
    registry = build_tool_registry(["delegate_agent", "list_agents"])
    assert {"delegate_agent", "list_agents"} <= set(registry)
    assert isinstance(registry["delegate_agent"], AgentDelegateTool)
    assert isinstance(registry["list_agents"], ListAgentsTool)
