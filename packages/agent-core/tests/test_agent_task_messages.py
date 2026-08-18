"""Regression tests for task-mode tool-call message history."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.providers.base import Message


def _plan_call(call_id: str = "call_plan") -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "plan_task",
                "arguments": json.dumps({"goal": "Test", "tasks": []}),
            },
        }],
    )


def _make_agent() -> Agent:
    """Minimal Agent for static-method / emit-path tests."""
    config = AgentConfig(slug="test", description="test", system_prompt="You are a test agent.")
    tool_ctx = MagicMock()
    return Agent(
        config=config,
        credentials={},
        tier_models={"cfg1": {"provider": "openai", "model": "test-model"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
    )


def test_task_messages_acknowledge_plan_tool_call_before_task_prompt():
    messages = [Message(role="user", content="Implement the feature"), _plan_call()]

    task_messages = Agent._build_task_messages(messages, "call_plan", "Update the API")

    assert [message.role for message in task_messages] == ["user", "assistant", "tool", "user"]
    assert task_messages[-2].name == "plan_task"
    assert task_messages[-2].tool_call_id == "call_plan"
    assert json.loads(task_messages[-2].content)["status"] == "accepted"
    assert task_messages[-1].content == "Execute this task: Update the API"


def test_task_message_history_has_no_pending_plan_tool_call():
    task_messages = Agent._build_task_messages(
        [Message(role="user", content="Implement the feature"), _plan_call("call_123")],
        "call_123",
        "Update the API",
    )

    history, pending_tool_ids = Agent._history_tool_call_summary(task_messages)

    assert history == "user -> assistant[plan_task] -> tool[plan_task] -> user"
    assert pending_tool_ids == []


def test_task_message_history_preserves_other_pending_tool_calls():
    messages = [
        Message(role="user", content="Implement the feature"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                _plan_call("call_plan").tool_calls[0],
                {
                    "id": "call_other",
                    "type": "function",
                    "function": {"name": "filesystem", "arguments": "{}"},
                },
            ],
        ),
    ]

    _, pending_tool_ids = Agent._history_tool_call_summary(
        Agent._build_task_messages(messages, "call_plan", "Update the API")
    )

    assert pending_tool_ids == ["call_other"]


def _tool_message(call_id: str | None = None) -> Message:
    return Message(
        role="tool",
        content="{}",
        tool_call_id=call_id,
        name="some_tool",
    )


def test_drop_orphan_tool_messages_keeps_well_formed_history():
    messages = [
        Message(role="user", content="hi"),
        _plan_call("call_a"),
        _tool_message("call_a"),
        Message(role="user", content="next"),
        _plan_call("call_b"),
        _tool_message("call_b"),
    ]
    cleaned = Agent._drop_orphan_tool_messages(messages)
    assert cleaned == messages


def test_drop_orphan_tool_messages_removes_unpaired_tool_results():
    # Interrupted/compressed histories can lead with a tool message whose
    # assistant tool_calls partner was lost — providers reject that with 400.
    messages = [
        _tool_message("call_lost"),          # orphan: no preceding assistant call
        Message(role="user", content="hi"),
        _plan_call("call_a"),
        _tool_message("call_a"),             # paired — kept
        _tool_message("call_again"),         # duplicate of call_a's id without new assistant call
    ]
    cleaned = Agent._drop_orphan_tool_messages(messages)
    assert [m.role for m in cleaned] == ["user", "assistant", "tool"]
    assert cleaned[-1].tool_call_id == "call_a"


def test_drop_orphan_tool_messages_removes_tool_without_call_id():
    # A tool message without tool_call_id can't be matched to any call. Once
    # dropped, the assistant tool_calls message above it dangles (call_a has
    # no tool result) — providers reject that with a 400, so it must go too.
    messages = [
        _plan_call("call_a"),
        _tool_message(),  # no tool_call_id — invalid for the API
    ]
    cleaned = Agent._drop_orphan_tool_messages(messages)
    assert cleaned == []


# ---------------------------------------------------------------------------
# _merge_tool_args — concatenation wins when it already parses as one document
# ---------------------------------------------------------------------------


def test_merge_tool_args_concats_when_combined_parses():
    # OpenAI-style byte-split fragments: concatenation is the correct merge.
    assert Agent._merge_tool_args('{"a":', '1}') == '{"a":1}'


def test_merge_tool_args_dict_merges_gemini_style_chunks():
    # Gemini-style per-chunk dicts: concatenation would yield {..}{..}.
    merged = Agent._merge_tool_args('{"a": 1}', '{"a": 1, "b": 2}')
    assert json.loads(merged) == {"a": 1, "b": 2}


def test_merge_tool_args_prefers_concat_on_json_boundary_split():
    # If a provider splits exactly on a boundary so both fragments parse as
    # dicts, concatenation must NOT be replaced by a dict merge (which would
    # change the argument semantics) — but the combined text is not a single
    # JSON document either, so the fallback concat keeps the bytes intact.
    result = Agent._merge_tool_args('{"a":1},', '"b":2}')
    assert result == '{"a":1},"b":2}'


def test_merge_tool_args_empty_delta_keeps_existing():
    assert Agent._merge_tool_args('{"a":1}', "") == '{"a":1}'
    assert Agent._merge_tool_args("", '{"a":1}') == '{"a":1}'


def test_merge_tool_args_caps_result_length():
    from agent_core.agent import _MAX_TOOL_ARGS_CHARS

    result = Agent._merge_tool_args("x" * _MAX_TOOL_ARGS_CHARS, "y" * 100)
    assert len(result) == _MAX_TOOL_ARGS_CHARS


# ---------------------------------------------------------------------------
# _emit_task_stream_end — no stream_end without an opened stream
# ---------------------------------------------------------------------------


class _RecordingSession:
    def __init__(self):
        self.tokens_used = 0
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, **data):
        self.emitted.append((event_type, data))


@pytest.mark.asyncio
async def test_emit_task_stream_end_skips_when_no_message_id():
    """A task that crashed before _run_task_loop opened its stream has no
    message to close — emitting stream_end with message_id=None would hand
    the frontend a dangling message id."""
    agent = _make_agent()
    session = _RecordingSession()
    # A plain object truly lacks the attribute (MagicMock would auto-create it).
    task_ctx = type("TaskCtx", (), {})()
    await agent._emit_task_stream_end(session, task_ctx, "t1")
    assert session.emitted == []


@pytest.mark.asyncio
async def test_emit_task_stream_end_emits_with_message_id():
    agent = _make_agent()
    session = _RecordingSession()
    task_ctx = MagicMock()
    task_ctx.current_message_id = "m1"
    await agent._emit_task_stream_end(session, task_ctx, "t1")
    assert [t for t, _ in session.emitted] == ["stream_end"]
    assert session.emitted[0][1]["message_id"] == "m1"
    assert session.emitted[0][1]["task_id"] == "t1"


# ---------------------------------------------------------------------------
# Gemini continuation-chunk attribution
# ---------------------------------------------------------------------------


def test_continuation_target_index_matches_by_key_overlap():
    from agent_core.providers.google_gemini import _continuation_target_index

    # Two parallel calls: call 0 accumulated {"a"}, call 1 accumulated {"x"}.
    seen = {0: {"a"}, 1: {"x"}}
    # Continuation extending call 0's args must NOT land on the most recent call.
    assert _continuation_target_index(seen, {"a": 1, "b": 2}, fallback=1) == 0
    assert _continuation_target_index(seen, {"x": 1, "y": 2}, fallback=1) == 1


def test_continuation_target_index_falls_back_to_last_call_without_overlap():
    from agent_core.providers.google_gemini import _continuation_target_index

    assert _continuation_target_index({0: {"a"}, 1: {"x"}}, {"z": 1}, fallback=1) == 1
    assert _continuation_target_index({}, {"z": 1}, fallback=0) == 0


@pytest.mark.asyncio
async def test_task_loop_summary_accumulates_text_across_iterations():
    """A multi-iteration task (tool call then final answer) must return the
    concatenation of ALL iterations' text — the task summary previously only
    carried the last iteration (often a bare 'done')."""
    from unittest.mock import AsyncMock

    from agent_core.providers.base import StopReason, StreamChunk
    from agent_core.session import ConversationSession
    from agent_core.tools.base import ToolContext

    agent = _make_agent()
    calls = {"n": 0}

    class _TwoTurnProvider:
        model_name = "test-model"

        async def stream(self, messages, tool_defs=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                yield StreamChunk(
                    delta="step one text ",
                    stop_reason=StopReason.END_TURN,
                    tool_call_delta={"index": 0, "id": "call_1",
                                     "function": {"name": "echo", "arguments": "{}"}},
                )
            else:
                yield StreamChunk(delta="final summary", stop_reason=StopReason.END_TURN)

    echo = AsyncMock(return_value={"ok": True})
    agent._tools = {"echo": echo}

    session = ConversationSession("c1", "p1", "u1")
    ctx = ToolContext(
        project_id="p1", project_fs_path="/tmp/p1", conversation_id="c1",
        user_id="u1", db_session=None,
    )
    messages = [Message(role="user", content="do it")]

    result = await agent._run_task_loop(
        messages, [], _TwoTurnProvider(), session,
        task_id="t1", turn=1, task_tool_ctx=ctx,
    )

    assert calls["n"] == 2
    echo.execute.assert_awaited_once()
    assert result == "step one text final summary"
