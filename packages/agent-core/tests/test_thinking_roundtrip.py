"""Signed thinking blocks round-trip through `_to_anthropic_messages`.

Same-turn tool loops must replay the signature (or the API 400s); unsigned
blocks and non-assistant roles must never leak into the replay."""
from __future__ import annotations

from agent_core.providers.anthropic_claude import AnthropicClaudeProvider
from agent_core.providers.base import Message


def _provider() -> AnthropicClaudeProvider:
    return AnthropicClaudeProvider.__new__(AnthropicClaudeProvider)


SIGNED = {"type": "thinking", "thinking": "pondering", "signature": "sig-1"}
UNSIGNED = {"type": "thinking", "thinking": "display only"}


def test_tool_call_assistant_replays_signed_blocks_first():
    msg = Message(
        role="assistant",
        content="call it",
        tool_calls=[{"id": "t1", "type": "function",
                     "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}}],
        thinking_blocks=[SIGNED, UNSIGNED],
    )
    _, out = _provider()._to_anthropic_messages([msg])
    content = out[0]["content"]
    # thinking first, then text, then tool_use — the API requires thinking
    # blocks before any tool_use in the replayed assistant turn.
    assert content[0] == SIGNED
    assert UNSIGNED not in content
    assert content[1] == {"type": "text", "text": "call it"}
    assert content[2]["type"] == "tool_use"


def test_plain_assistant_with_blocks_becomes_block_list():
    msg = Message(
        role="assistant",
        content="answer",
        tool_calls=None,
        thinking_blocks=[SIGNED],
    )
    _, out = _provider()._to_anthropic_messages([msg])
    assert out[0]["content"] == [SIGNED, {"type": "text", "text": "answer"}]


def test_plain_assistant_without_blocks_keeps_string_content():
    msg = Message(role="assistant", content="answer", tool_calls=None)
    _, out = _provider()._to_anthropic_messages([msg])
    assert out[0]["content"] == "answer"


def test_unsigned_blocks_are_never_replayed():
    """Signatures are memory-only; an unsigned block sent back would 400."""
    msg = Message(
        role="assistant",
        content="x",
        tool_calls=[{"id": "t1", "type": "function",
                     "function": {"name": "shell", "arguments": "{}"}}],
        thinking_blocks=[UNSIGNED],
    )
    _, out = _provider()._to_anthropic_messages([msg])
    assert UNSIGNED not in out[0]["content"]


def test_system_and_user_untouched():
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]
    system, out = _provider()._to_anthropic_messages(msgs)
    assert system == "sys"
    assert out == [{"role": "user", "content": "hi"}]
