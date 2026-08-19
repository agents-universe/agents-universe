"""Tests for conversation history compression edge cases."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_core.compressor import (
    MAX_OUTPUT_RESERVE,
    RECENT_TURNS_KEEP,
    compress_history,
    compression_budget,
    estimate_request_bytes,
    estimate_wire_bytes,
    force_compress_history,
    truncate_oversized_tool_messages,
)
from agent_core.providers.base import Message, ToolDefinition


def _message(role: str, content: str, call_id: str | None = None) -> Message:
    return Message(
        role=role,
        content=content,
        tool_call_id=call_id,
    )


def _assistant_with_call(idx: int) -> Message:
    """An assistant message that declares one tool call `call_{idx}`."""
    return Message(
        role="assistant",
        content=f"assistant payload {idx} " * 12,
        tool_calls=[{
            "id": f"call_{idx}",
            "type": "function",
            "function": {"name": "some_tool", "arguments": "{}"},
        }],
    )


def _tool_response(idx: int) -> Message:
    return _message("tool", f"tool result payload {idx} " * 12, f"call_{idx}")


def _mock_provider():
    provider = AsyncMock()
    provider.complete.return_value = SimpleNamespace(
        message=SimpleNamespace(content="summary of earlier conversation")
    )
    return provider


async def test_compress_slice_never_leads_with_orphan_tool_message():
    """The recent slice must not start with a tool message whose assistant
    tool_calls partner fell into the summarized early segment."""
    # 20 messages: the RECENT_TURNS_KEEP=8 slice starts at index 12.
    # Make index 11 an assistant with a tool call and index 12 its tool
    # response, so the naive slice would lead with an orphan tool message.
    messages: list[Message] = []
    for i in range(10):
        messages.append(_message("user", f"user payload {i} " * 12))
        messages.append(_assistant_with_call(i))
        messages.append(_tool_response(i))

    provider = _mock_provider()
    result = await compress_history(messages, token_budget=100, provider=provider)

    # Compression happened (summary + ack + recent tail)
    assert result[0].role == "user"
    assert "summary" in result[0].content
    assert result[1].role == "assistant"
    # The retained tail must not lead with an orphan tool message
    assert result[2].role != "tool"
    # Sanity: every retained tool message has its assistant partner present
    pending: set[str] = set()
    for m in result:
        if m.role == "assistant":
            for call in m.tool_calls or []:
                pending.add(str(call.get("id")))
        elif m.role == "tool":
            assert m.tool_call_id in pending
            pending.discard(m.tool_call_id)


async def test_compress_keeps_recent_tail_unchanged_when_cut_is_clean():
    """When the slice boundary lands on a clean exchange boundary, the recent
    tail is preserved as-is."""
    messages: list[Message] = []
    for i in range(10):
        messages.append(_message("user", f"user payload {i} " * 12))
        messages.append(_assistant_with_call(i))
        messages.append(_tool_response(i))
    # First 6 pairs fully summarized; index 18+19 (user + assistant) retained.
    # The retained tail starts with a user message — nothing to drop.
    messages.append(_message("user", "final user payload " * 12))
    messages.append(_assistant_with_call(99))

    provider = _mock_provider()
    result = await compress_history(messages, token_budget=100, provider=provider)

    tail = result[2:]
    assert tail[0].role == "user"
    assert tail[-1] is messages[-1]


async def test_compress_keeps_full_history_when_summarization_fails():
    """A failed summarization must NOT replace the early history with a
    placeholder — compression is a soft budget optimization, so the next turn
    retries with the full history intact ."""
    messages: list[Message] = []
    for i in range(10):
        messages.append(_message("user", f"user payload {i} " * 12))
        messages.append(_assistant_with_call(i))
        messages.append(_tool_response(i))

    provider = AsyncMock()
    provider.complete.side_effect = RuntimeError("transient 429")

    result = await compress_history(messages, token_budget=100, provider=provider)

    # Same list object returned: nothing compressed, nothing dropped
    assert result is messages
    assert len(result) == len(messages)


def test_compression_budget_reserves_capped_output():
    """max_tokens must not eat the whole window — the old inline formula
    (reserve max_tokens=128000 against a 128k window) made the budget always
    <= 0 and automatic compression dead for gpt-4o-class models."""
    # Regression: the old formula gave 128000 - 5000 - 128000 <= 0.
    assert compression_budget(128000, 5000, 128000) > 0
    # Input alone overflowing the window still skips (compression cannot help).
    assert compression_budget(128000, 115000, 128000) < 0
    # A small configured max_tokens is reserved exactly, not rounded up.
    assert compression_budget(128000, 5000, 4096) == 128000 - 5000 - 4096
    # The cap itself is the gpt-4o output ceiling.
    assert MAX_OUTPUT_RESERVE == 16384


def test_estimate_wire_bytes_cjk_escape():
    """CJK chars serialize as \\uXXXX (6 bytes each) — the token heuristic
    underestimates CJK payloads by 2-3x."""
    cjk = estimate_wire_bytes("中文内容测试")
    assert cjk >= 6 * 6
    assert cjk <= 6 * 6 * 1.15 + 1
    ascii_100 = estimate_wire_bytes("a" * 100)
    assert ascii_100 == int(100 * 1.15)


def test_estimate_request_bytes_counts_content_tool_outputs_and_images():
    """The estimate must see everything that hits the wire: text, base64
    image data, tool-call JSON and tool definitions."""
    big_tool = Message(role="tool", content="x" * 10000, tool_call_id="c1", name="read")
    assert estimate_request_bytes([big_tool]) >= 10000

    image = Message(role="user", content=[{"type": "image", "media_type": "image/png", "data": "A" * 100000}])
    assert estimate_request_bytes([image]) >= 100000

    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[{"id": "c2", "type": "function", "function": {"name": "f", "arguments": "a" * 50000}}],
    )
    assert estimate_request_bytes([assistant]) >= 50000

    tool_def = ToolDefinition(name="t", description="d", parameters={"type": "object"})
    assert estimate_request_bytes([], [tool_def]) > 0
    assert estimate_request_bytes([]) == 0


def test_truncate_oversized_tool_messages_caps_only_tool_messages():
    """Only tool messages are truncated; user/assistant text and the tool
    message's identity (call id, name) survive untouched."""
    user = _message("user", "u" * 1000)
    assistant = _assistant_with_call(1)
    assistant_payload = assistant.content
    tool = Message(role="tool", content="data " * 400000, tool_call_id="call_1", name="read_file")

    count = truncate_oversized_tool_messages([user, assistant, tool])

    assert count == 1
    assert user.content == "u" * 1000
    assert assistant.content == assistant_payload
    assert tool.tool_call_id == "call_1"
    assert tool.name == "read_file"
    assert tool.content.endswith("\n[... truncated ...]")
    assert len(tool.content) < 400000 * 5


async def test_force_compress_history_outcomes():
    """Byte guard outcomes: ok (untouched), truncated (shrunk), over_limit."""
    small = [_message("user", "hello"), _message("assistant", "hi")]
    assert force_compress_history(small, byte_limit=100_000) == "ok"
    # Under the limit the messages are untouched.
    assert [m.content for m in small] == ["hello", "hi"]

    # Oversized tool outputs shrink below the limit via in-place truncation —
    # and no provider is involved (force_compress_history takes none, so the
    # hot path is deterministic; a real LLM call could never be awaited here).
    fat = [_message("user", "u" * 100), Message(role="tool", content="data " * 400000, tool_call_id="c1")]
    assert force_compress_history(fat, byte_limit=100_000) == "truncated"
    assert estimate_request_bytes(fat) <= 100_000

    # A system-prompt-dominated payload cannot be helped by tool truncation.
    system = [Message(role="system", content="汉" * 100000), _message("user", "u")]
    assert force_compress_history(system, byte_limit=100_000) == "over_limit"


async def test_compress_history_short_branch_still_truncates():
    """<= RECENT_TURNS_KEEP messages: in-place tool truncation, no LLM call."""
    messages = [
        _message("user", "u" * 100),
        _assistant_with_call(1),
        Message(role="tool", content="data " * 400000, tool_call_id="call_1"),
    ]
    provider = AsyncMock()

    result = await compress_history(messages, token_budget=100, provider=provider)

    assert result is messages
    assert messages[2].content.endswith("\n[... truncated ...]")
    provider.complete.assert_not_awaited()
