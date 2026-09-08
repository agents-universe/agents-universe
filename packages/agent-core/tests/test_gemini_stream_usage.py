"""Gemini streaming: the terminal chunk carries usage but no parts.

``if not candidate.content.parts: continue`` used to skip the usage capture
that sat after it, so every stream ending on a parts-less terminal chunk
(usage_metadata + finish_reason only) reported usage=None and the turn's
token counts were lost.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.providers.base import Message, StopReason
from agent_core.providers.google_gemini import GoogleGeminiProvider


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


def _chunk(parts=None, finish_reason=None, usage=None):
    content = SimpleNamespace(parts=parts) if parts is not None else None
    candidate = SimpleNamespace(content=content, finish_reason=finish_reason)
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)


def _text_part(text: str):
    return SimpleNamespace(function_call=None, text=text)


def _provider_with_chunks(monkeypatch, chunks) -> GoogleGeminiProvider:
    provider = GoogleGeminiProvider(api_key="k", model="gemini-2.0-flash")

    async def _generate_content_stream(**_kwargs):
        return FakeStream(chunks)

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(
            generate_content_stream=_generate_content_stream,
        )),
    )
    monkeypatch.setattr(provider, "_client", lambda: fake_client)
    return provider


@pytest.mark.asyncio
async def test_usage_from_parts_less_terminal_chunk_is_kept(monkeypatch):
    chunks = [
        _chunk(parts=[_text_part("hi")], usage=None),
        # Terminal chunk: finish_reason + usage, NO parts.
        _chunk(parts=None, finish_reason=SimpleNamespace(name="STOP"),
               usage=SimpleNamespace(prompt_token_count=11, candidates_token_count=7)),
    ]
    provider = _provider_with_chunks(monkeypatch, chunks)

    out = [c async for c in provider.stream([Message(role="user", content="hi")])]

    final = out[-1]
    assert final.stop_reason is StopReason.END_TURN
    assert final.usage == {"prompt_tokens": 11, "completion_tokens": 7}


@pytest.mark.asyncio
async def test_usage_from_candidate_less_chunk_is_kept(monkeypatch):
    """Some responses deliver usage on a chunk with no candidates at all."""
    chunks = [
        _chunk(parts=[_text_part("hi")]),
        SimpleNamespace(candidates=[], usage_metadata=SimpleNamespace(
            prompt_token_count=3, candidates_token_count=2,
        )),
    ]
    provider = _provider_with_chunks(monkeypatch, chunks)

    out = [c async for c in provider.stream([Message(role="user", content="hi")])]

    assert out[-1].usage == {"prompt_tokens": 3, "completion_tokens": 2}
