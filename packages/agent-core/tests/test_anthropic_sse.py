"""Anthropic gateway SSE parsing — the spec allows both "data: <payload>"
and "data:<payload>"; the no-space form must not silently empty the stream.
Also covers the thinking/signature channels and the thinking-rejection check
that drives the retry-without-thinking fallback."""
import httpx
import pytest

from agent_core.providers.anthropic_claude import (
    AnthropicClaudeProvider,
    _thinking_rejected,
)
from agent_core.providers.base import StopReason, StreamChunk


class _FakeSSEResponse:
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _provider() -> AnthropicClaudeProvider:
    return AnthropicClaudeProvider.__new__(AnthropicClaudeProvider)


@pytest.mark.asyncio
async def test_parse_sse_accepts_data_without_space():
    p = _provider()
    resp = _FakeSSEResponse([
        'data:{"type":"message_start","message":{"usage":{"input_tokens":5}}}',
        'data:{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}',
        'data:{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}',
        'data: [DONE]',
    ])

    chunks = [c async for c in p._parse_sse(resp)]

    texts = [c.delta for c in chunks if c.delta]
    assert texts == ["hello"]
    assert any(c.stop_reason == StopReason.END_TURN for c in chunks)


@pytest.mark.asyncio
async def test_parse_sse_thinking_and_signature_deltas():
    p = _provider()
    resp = _FakeSSEResponse([
        'data:{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"let me"}}',
        'data:{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":" think"}}',
        'data:{"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig-abc"}}',
        'data:{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}',
        'data: [DONE]',
    ])

    chunks = [c async for c in p._parse_sse(resp)]

    thinking = [c.thinking for c in chunks if c.thinking]
    signatures = [c.thinking_signature for c in chunks if c.thinking_signature]
    assert thinking == ["let me", " think"]
    assert signatures == ["sig-abc"]
    assert [c.delta for c in chunks if c.delta] == ["answer"]
    # Thinking text must never bleed into the content channel.
    assert all(c.delta != c.thinking for c in chunks if c.thinking)


def _http_status_error(message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gateway.example/model/x/invoke")
    response = httpx.Response(400, request=request, text=message)
    return httpx.HTTPStatusError(message, request=request, response=response)


def test_thinking_rejected_detects_param_errors():
    # Gateway 400 body mentioning the field → retry without thinking.
    assert _thinking_rejected(
        _http_status_error('{"error":{"message":"unknown parameter: thinking"}}')
    )
    # Unrelated 400 must NOT disable thinking (only the text decides).
    assert not _thinking_rejected(
        _http_status_error('{"error":{"message":"max_tokens must be <= 64000"}}')
    )


def test_completion_to_chunks_replays_thinking_before_text():
    from agent_core.providers.anthropic_claude import AnthropicClaudeProvider as P
    from agent_core.providers.base import CompletionResult, Message

    result = CompletionResult(
        message=Message(
            role="assistant",
            content="final",
            thinking_blocks=[
                {"type": "thinking", "thinking": "my chain", "signature": "sig-9"},
            ],
        ),
        usage={"prompt_tokens": 1, "completion_tokens": 2},
        model="claude-sonnet-4-6",
        finish_reason="stop",
        stop_reason=StopReason.END_TURN,
    )
    chunks = P._completion_to_chunks(result)
    assert [c.thinking for c in chunks if c.thinking] == ["my chain"]
    assert [c.thinking_signature for c in chunks if c.thinking_signature] == ["sig-9"]
    assert [c.delta for c in chunks if c.delta] == ["final"]
    # Thinking arrives first so the agent loop opens the block before content.
    assert isinstance(chunks[0], StreamChunk) and chunks[0].thinking == "my chain"
