"""Anthropic gateway SSE parsing — the spec allows both "data: <payload>"
and "data:<payload>"; the no-space form must not silently empty the stream."""
import pytest

from agent_core.providers.anthropic_claude import AnthropicClaudeProvider
from agent_core.providers.base import StopReason


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
