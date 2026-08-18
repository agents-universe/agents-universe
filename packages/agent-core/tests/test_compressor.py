"""Tests for conversation history compression edge cases."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_core.compressor import RECENT_TURNS_KEEP, compress_history
from agent_core.providers.base import Message


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
