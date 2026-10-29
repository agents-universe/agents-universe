"""The load_conversation_history tool.

The same two contracts the delegate tool pins: nothing may raise into the
agent loop (a failed history read is degraded context, not a failed turn), and
the API layer stays lazy — the query lives in ``api.services.agent_turn``,
reached through a deferred import that degrades to an error dict when the web
service is absent. The API side is stubbed through ``sys.modules``, exactly as
``test_agent_delegate_tools.py`` does it.
"""
from __future__ import annotations

import sys
import types

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.conversation_history import LoadConversationHistoryTool


def _frozen(**overrides):
    fields = {"chain": ("parent", "child"), "policy": (), "parent_session": None}
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


@pytest.fixture
def context():
    ctx = ToolContext(
        project_id="p1",
        project_fs_path=".",
        conversation_id="c1",
        user_id="u1",
    )
    ctx.db_session = object()  # any non-None sentinel; the loader is stubbed
    ctx.delegation = _frozen()
    return ctx


@pytest.fixture
def loader(monkeypatch):
    """Install a stub api.services.agent_turn; record what the tool asked for."""
    calls: list[dict] = []
    state: dict = {
        "result": {
            "messages": [{"sequence": 3, "role": "user", "agent": None, "text": "hi"}],
            "count": 1, "has_more": False, "next_before": 3,
        },
        "error": None,
    }

    async def load_history_page(db, conversation_id, **kwargs):
        calls.append({"db": db, "conversation_id": conversation_id, **kwargs})
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    monkeypatch.setitem(sys.modules, "api.services.agent_turn", types.SimpleNamespace(
        load_history_page=load_history_page,
    ))
    return types.SimpleNamespace(calls=calls, state=state)


class TestLoadConversationHistory:
    async def test_passes_params_through_and_returns_the_page(self, context, loader):
        result = await LoadConversationHistoryTool().execute(
            {"limit": 30, "before": 10, "agent": "qa"}, context,
        )

        assert result == loader.state["result"]
        assert loader.calls == [{
            "db": context.db_session,
            "conversation_id": "c1",
            # The chain always ends with the running agent — that slug decides
            # which replies count as "own" under the attribution rule.
            "viewer_agent_slug": "child",
            "limit": 30,
            "before": 10,
            "agent": "qa",
        }]

    async def test_limit_is_clamped_and_empty_strings_become_none(self, context, loader):
        await LoadConversationHistoryTool().execute(
            {"limit": 999, "before": "", "agent": "  "}, context,
        )

        call = loader.calls[0]
        assert call["limit"] == 50
        assert call["before"] is None
        assert call["agent"] is None

    async def test_defaults_apply_when_params_are_omitted(self, context, loader):
        await LoadConversationHistoryTool().execute({}, context)

        call = loader.calls[0]
        assert call["limit"] == 20
        assert call["before"] is None
        assert call["agent"] is None

    async def test_an_empty_page_gets_a_note_for_the_model(self, context, loader):
        loader.state["result"] = {
            "messages": [], "count": 0, "has_more": False, "next_before": None,
        }

        result = await LoadConversationHistoryTool().execute({}, context)

        assert result["count"] == 0
        assert "note" in result

    async def test_no_delegation_chain_means_no_viewer_filter(self, context, loader):
        context.delegation = None

        await LoadConversationHistoryTool().execute({}, context)

        assert loader.calls[0]["viewer_agent_slug"] is None

    async def test_refuses_without_a_database_session(self, context, loader):
        context.db_session = None

        result = await LoadConversationHistoryTool().execute({}, context)

        assert "error" in result
        assert loader.calls == []

    async def test_refuses_when_the_api_layer_is_absent(self, context, monkeypatch):
        """`None` in sys.modules is Python's own "this import must fail"."""
        monkeypatch.setitem(sys.modules, "api.services.agent_turn", None)

        result = await LoadConversationHistoryTool().execute({}, context)

        assert "error" in result
        assert "unavailable" in result["error"].lower()

    async def test_a_crash_becomes_an_error_instead_of_an_exception(self, context, loader):
        """The tool is the agent loop's boundary: nothing may escape it."""
        loader.state["error"] = RuntimeError("connection reset")

        result = await LoadConversationHistoryTool().execute({}, context)

        assert "RuntimeError" in result["error"]
        assert "connection reset" in result["error"]


def test_the_tool_is_not_declarable():
    """It is injected into nested turns only — the registry must not resolve it."""
    from agent_core.tools.registry import build_tool_registry, known_static_tool_names

    assert "load_conversation_history" not in known_static_tool_names()
    assert "load_conversation_history" not in build_tool_registry(["load_conversation_history"])
