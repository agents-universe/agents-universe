"""Malformed planner/user_confirm args must produce a readable tool error.

Both tools declare object schemas, but models still emit a bare string or a
list of strings. The old code called ``.get()`` on those entries, which raised
AttributeError and reached the model as the opaque "plan_task failed: 'str'
object has no attribute 'get'" — or, for user_confirm, let a list of bare
strings through to a dialog that renders blank buttons.
"""
from __future__ import annotations

import pytest

from agent_core.agent import Agent
from agent_core.session import ConversationSession
from agent_core.tools.base import ToolContext
from agent_core.tools.planner import PlannerTool
from agent_core.tools.user_confirm import UserConfirmTool


def _ctx(**kwargs) -> ToolContext:
    return ToolContext(
        project_id="p", project_fs_path=".", conversation_id="c", user_id="u", **kwargs
    )


class _FakeSession:
    """Session double: only the prompt call itself is faked.

    Prompt-ledger and pause state delegate to a real session, so the double
    cannot drift from the interface UserConfirmTool relies on.
    """

    def __init__(self, value: str = "chosen"):
        self.value = value
        self.calls: list[dict] = []
        self._state = ConversationSession("c", "p", "u")

    async def request_user_selection(self, **kwargs):
        self.calls.append(kwargs)
        return self.value

    def __getattr__(self, name):
        return getattr(self._state, name)


@pytest.mark.asyncio
async def test_planner_rejects_list_of_strings():
    result = await PlannerTool().execute({"tasks": ["build it"]}, _ctx())

    assert "error" in result
    assert "object" in result["error"]


@pytest.mark.asyncio
async def test_planner_rejects_string_tasks():
    result = await PlannerTool().execute({"tasks": "build it"}, _ctx())

    assert "error" in result


def test_normalize_task_plan_rejects_non_dict_entry():
    with pytest.raises(ValueError, match="task"):
        Agent._normalize_task_plan(["build it"])


def test_normalize_task_plan_rejects_non_list():
    with pytest.raises(ValueError, match="task"):
        Agent._normalize_task_plan("build it")


@pytest.mark.asyncio
async def test_user_confirm_rejects_non_dict_options():
    session = _FakeSession()
    result = await UserConfirmTool().execute(
        {"question": "Pick", "options": ["a", "b"]}, _ctx(session=session)
    )

    assert "error" in result
    assert session.calls == []  # never reached the dialog


@pytest.mark.asyncio
async def test_user_confirm_rejects_option_without_label():
    session = _FakeSession()
    result = await UserConfirmTool().execute(
        {"question": "Pick", "options": [{"value": "a"}]}, _ctx(session=session)
    )

    assert "error" in result
    assert session.calls == []


@pytest.mark.asyncio
async def test_user_confirm_still_accepts_valid_options():
    session = _FakeSession("a")
    result = await UserConfirmTool().execute(
        {"question": "Pick", "options": [{"label": "A", "value": "a"}]},
        _ctx(session=session),
    )

    assert result["selected_value"] == "a"
    assert session.calls[0]["options"] == [{"label": "A", "value": "a"}]
