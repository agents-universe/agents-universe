"""Tests for conversation history compression edge cases."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_core.compressor import (
    MAX_OUTPUT_RESERVE,
    RECENT_TURNS_KEEP,
    SUMMARY_INPUT_MAX_CHARS,
    SUMMARY_MARKER,
    build_summary_pair,
    compress_history,
    compression_budget,
    demote_image_messages,
    estimate_request_bytes,
    estimate_wire_bytes,
    force_compress_history,
    format_early_history,
    request_byte_breakdown,
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


async def test_compress_history_force_overrides_token_threshold():
    """force=True summarizes a many-message history even when tokens are under
    the threshold — the byte-over-limit case the token heuristic misses."""
    messages: list[Message] = []
    for i in range(12):
        messages.append(_message("user", f"short {i}"))
        messages.append(_message("assistant", f"reply {i}"))

    provider = _mock_provider()
    result = await compress_history(messages, token_budget=10_000_000, provider=provider, force=True)

    # Summarization happened despite the huge token budget.
    assert result[0].role == "user"
    assert "summary" in result[0].content
    assert result[1].role == "assistant"
    assert len(result) < len(messages)

    # force=False with the same budget is a no-op.
    untouched = await compress_history(messages, token_budget=10_000_000, provider=provider, force=False)
    assert untouched is messages


def test_demote_image_messages_keeps_other_parts():
    """Only image parts become text refs; text parts survive unchanged."""
    msg = Message(role="user", content=[
        {"type": "text", "text": "hello"},
        {"type": "image", "media_type": "image/png", "data": "A" * 1000},
    ])
    count = demote_image_messages([msg])

    assert count == 1
    assert msg.content[0] == {"type": "text", "text": "hello"}
    assert msg.content[1]["type"] == "text"
    assert "image" in msg.content[1]["text"].lower()
    assert estimate_request_bytes([msg]) < 1000


def test_request_byte_breakdown_attributes_buckets():
    """The over-limit error must name what dominates the body."""
    system = Message(role="system", content="汉" * 1000)
    text = Message(role="user", content="hello")
    image = Message(role="user", content=[{"type": "image", "media_type": "image/png", "data": "B" * 5000}])
    tool = ToolDefinition(name="t", description="d", parameters={"type": "object"})

    bd = request_byte_breakdown([system, text, image], [tool])

    assert bd["system"] > 0
    assert bd["images"] >= 5000
    assert bd["text"] > 0
    assert bd["tools"] > 0


# ---------------------------------------------------------------------------
# Summarization input cap (auto path) and summary marker
# ---------------------------------------------------------------------------


def test_format_early_history_caps_total_input():
    """Over the cap the TAIL is kept and the dropped head is marked - the
    summarization call must stay bounded so it can beat the timeout."""
    early = [Message(role="user", content=f"msg {i} " + "x" * 500) for i in range(30)]
    out = format_early_history(early, max_chars=2000)

    assert "omitted from the summary input" in out
    assert "msg 29" in out          # most recent line survives
    assert "msg 0 " not in out      # oldest dropped
    assert len(out) < 2000 + 600    # marker + kept tail, roughly bounded


def test_format_early_history_always_keeps_last_line():
    """A cap smaller than any single line still keeps the last line - an
    empty summary input would disable compression entirely."""
    early = [Message(role="user", content="x" * 900) for _ in range(5)]
    out = format_early_history(early, max_chars=500)

    assert out.count("USER:") == 1
    assert "xxx" in out
    assert "4 earlier messages omitted" in out


def test_format_early_history_uncapped_keeps_all():
    early = [Message(role="user", content=f"m{i}") for i in range(5)]
    out = format_early_history(early)
    assert out == "\n".join(f"USER: m{i}" for i in range(5))


async def test_compress_history_bounds_summary_input():
    """compress_history passes the cap through: the provider receives at most
    SUMMARY_INPUT_MAX_CHARS of history text, not the full early segment."""
    messages: list[Message] = []
    for i in range(60):
        messages.append(Message(role="user", content=f"user payload {i} " * 300))
        messages.append(Message(role="assistant", content=f"assistant payload {i} " * 300))

    provider = _mock_provider()
    result = await compress_history(messages, token_budget=100, provider=provider, force=True)

    assert result[0].content.startswith(SUMMARY_MARKER)
    sent = provider.complete.call_args.args[0]
    user_text = sent[1].content
    assert len(user_text) < SUMMARY_INPUT_MAX_CHARS + 1000
    assert "payload 55" in user_text    # newest early line survives the cap
    assert "omitted from the summary" in user_text


def test_build_summary_pair_uses_marker():
    pair = build_summary_pair("sum text")
    assert pair[0].content.startswith(SUMMARY_MARKER)
    assert pair[0].role == "user"


# ---------------------------------------------------------------------------
# Estimate fast paths
# ---------------------------------------------------------------------------


def test_estimate_wire_bytes_ascii_fast_path():
    """ASCII text skips the CJK scan and only applies the escape factor."""
    assert estimate_wire_bytes("hello world") == int(11 * 1.15)
    assert estimate_wire_bytes("你好") == int((2 * 6 + 0) * 1.15)
    assert estimate_wire_bytes("abc你好def") == int((2 * 6 + 6) * 1.15)


def test_tool_calls_wire_bytes_close_to_dump_estimate():
    """Per-part estimation stays within the old json.dumps-based margin."""
    import json as _json

    from agent_core.compressor import _tool_calls_wire_bytes

    tool_calls = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "some_tool", "arguments": '{"query": "' + "你好" * 50 + '"}'},
    }]
    old = estimate_wire_bytes(_json.dumps(tool_calls, ensure_ascii=True, default=str))
    new = _tool_calls_wire_bytes(tool_calls)
    assert abs(new - old) / old < 0.2


def test_estimate_request_bytes_tool_wire_bytes_matches_full_estimate():
    """The tool_wire_bytes fast path is equivalent to estimating tools inline."""
    tools = [ToolDefinition(name="t1", description="d1", parameters={"type": "object"})]
    msgs = [_message("user", "hello"), _assistant_with_call(1)]

    full = estimate_request_bytes(msgs, tools)
    tools_only = estimate_request_bytes([], tools)
    assert tools_only > 0
    assert estimate_request_bytes(msgs) + tools_only == full
    assert estimate_request_bytes(msgs, tools, tool_wire_bytes=tools_only) == full
