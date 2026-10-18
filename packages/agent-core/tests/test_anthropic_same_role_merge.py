"""Anthropic messages: adjacent same-role turns must coalesce.

The direct Messages API combines consecutive same-role turns, but
Bedrock-style gateways reject them with "roles must alternate between
'user' and 'assistant'". The tool-result merge alone left three reachable
adjacencies: several injections dequeued at one step boundary, an injection
consumed right after a tool-results turn, and a delegated child's persisted
reply replayed right before its parent's on the next turn. A pause_turn
continuation can also append a second assistant turn.
"""
from __future__ import annotations

from agent_core.providers.anthropic_claude import AnthropicClaudeProvider
from agent_core.providers.base import Message

SIGNED = {"type": "thinking", "thinking": "pondering", "signature": "sig-1"}


def _provider() -> AnthropicClaudeProvider:
    return AnthropicClaudeProvider.__new__(AnthropicClaudeProvider)


def test_two_queued_injections_share_one_user_turn():
    messages = [
        Message(role="user", content="question"),
        Message(role="user", content="first injection"),
        Message(role="user", content="second injection"),
    ]

    _, out = _provider()._to_anthropic_messages(messages)

    assert [entry["role"] for entry in out] == ["user"]
    assert [b["text"] for b in out[0]["content"]] == [
        "question", "first injection", "second injection",
    ]


def test_injection_after_tool_results_stays_one_user_turn():
    messages = [
        Message(role="user", content="do the work"),
        Message(
            role="assistant", content="",
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "search", "arguments": "{}"}}],
        ),
        Message(role="tool", content="found it", tool_call_id="c1", name="search"),
        Message(role="user", content="actually focus on X"),
    ]

    _, out = _provider()._to_anthropic_messages(messages)

    assert [entry["role"] for entry in out] == ["user", "assistant", "user"]
    last = out[-1]["content"]
    # tool_result leads the turn; appended text follows it.
    assert last[0] == {"type": "tool_result", "tool_use_id": "c1", "content": "found it"}
    assert last[1] == {"type": "text", "text": "actually focus on X"}


def test_delegated_reply_and_parent_reply_share_one_assistant_turn():
    messages = [
        Message(role="user", content="Q1"),
        Message(role="assistant", content="[Helper]: did the work"),
        Message(role="assistant", content="parent final answer"),
        Message(role="user", content="Q2"),
    ]

    _, out = _provider()._to_anthropic_messages(messages)

    assert [entry["role"] for entry in out] == ["user", "assistant", "user"]
    assert out[1]["content"] == [
        {"type": "text", "text": "[Helper]: did the work"},
        {"type": "text", "text": "parent final answer"},
    ]
    assert out[2] == {"role": "user", "content": "Q2"}


def test_pause_turn_continuation_merges_and_thinking_stays_first():
    messages = [
        Message(role="user", content="go"),
        Message(role="assistant", content="part one"),
        Message(
            role="assistant", content="part two",
            tool_calls=[{"id": "t1", "type": "function",
                         "function": {"name": "shell", "arguments": "{}"}}],
            thinking_blocks=[SIGNED],
        ),
    ]

    _, out = _provider()._to_anthropic_messages(messages)

    assert [entry["role"] for entry in out] == ["user", "assistant"]
    content = out[1]["content"]
    # Signed thinking blocks lead the assistant turn, tool_use closes it.
    assert content[0] == SIGNED
    assert {"type": "text", "text": "part one"} in content
    assert {"type": "text", "text": "part two"} in content
    assert content[-1] == {
        "type": "tool_use", "id": "t1", "name": "shell", "input": {},
    }


def test_alternating_history_keeps_string_content():
    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
    ]

    _, out = _provider()._to_anthropic_messages(messages)

    assert out == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
