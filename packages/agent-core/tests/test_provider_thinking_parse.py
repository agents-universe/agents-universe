"""Reasoning-channel parsing for OpenAI-compatible and Gemini providers.

Parse-only for OpenAI (the request never asks for reasoning); Gemini also
gains the include_thoughts request config. Thought parts must never leak
into the content channel."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.providers.base import StreamChunk


# ── OpenAI / Azure: reasoning & reasoning_content → StreamChunk.thinking ──


def _openai_stream(chunks):
    """Build what `client.chat.completions.create(stream=True)` async-iterates."""

    class _Stream:
        def __init__(self):
            self._chunks = chunks

        def __aiter__(self):
            async def _gen():
                for c in self._chunks:
                    yield c
            return _gen()

        async def close(self):
            pass

    return _Stream()


def _choice(delta) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


async def _collect_openai_stream(provider, chunks):
    from agent_core.providers.base import Message

    provider._client = MagicMock()

    # create() is awaited in stream(); a MagicMock return needs __await__.
    async def _create(**kw):
        return _openai_stream(chunks)
    provider._client.chat.completions.create = MagicMock(side_effect=_create)
    return [c async for c in provider.stream([Message(role="user", content="hi")])]


def _make_openai():
    from agent_core.providers.openai import OpenAIProvider

    p = OpenAIProvider.__new__(OpenAIProvider)
    p._model = "gpt-5.6-luna"
    return p


@pytest.mark.asyncio
async def test_openai_reasoning_content_parses_to_thinking():
    p = _make_openai()
    chunks = await _collect_openai_stream(p, [
        _choice(SimpleNamespace(
            content=None, tool_calls=None,
            reasoning=None, reasoning_content="hmm ",
        )),
        _choice(SimpleNamespace(
            content=None, tool_calls=None,
            reasoning="deeper", reasoning_content=None,
        )),
        _choice(SimpleNamespace(content="answer", tool_calls=None,
                                reasoning=None, reasoning_content=None)),
    ])
    thinking = [c.thinking for c in chunks if c.thinking]
    assert thinking == ["hmm ", "deeper"]
    assert [c.delta for c in chunks if c.delta] == ["answer"]


def test_azure_stream_parses_reasoning_too():
    """The Azure path duplicates the same delta parse — pin the source so a
    future refactor can't silently drop one of the two branches."""
    import inspect

    from agent_core.providers.openai import AzureOpenAIProvider

    src = inspect.getsource(AzureOpenAIProvider.stream)
    assert 'getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)' in src
    assert "StreamChunk(thinking=" in src


# ── Gemini: thought parts → StreamChunk.thinking, never content ──────────


def test_gemini_thought_part_routes_to_thinking():
    """The stream loop checks part.thought BEFORE the content branch —
    assert via source (the loop needs a live genai stream) plus the helper's
    existence: a thought part mis-labeled as text was the original bug."""
    import inspect

    from agent_core.providers.google_gemini import GoogleGeminiProvider

    src = inspect.getsource(GoogleGeminiProvider.stream)
    thought_idx = src.find('getattr(part, "thought", False)')
    content_branch = src.find("fc = part.function_call")
    assert thought_idx != -1
    # Thought check precedes the function-call/content handling.
    assert thought_idx < content_branch
    assert "StreamChunk(thinking=" in src


def test_gemini_request_config_adds_include_thoughts():
    from agent_core.providers.google_gemini import GoogleGeminiProvider

    p = GoogleGeminiProvider.__new__(GoogleGeminiProvider)
    p._model_name = "gemini-2.5-pro"
    p._thinking_enabled = True
    p._supports = getattr(p, "_supports", None)

    cfg = p._request_config(None, [], 4096, 0.0, thinking=True)
    assert cfg.thinking_config is not None
    assert cfg.thinking_config.include_thoughts is True

    # Off switch → no config (the API then omits thought parts anyway).
    p._thinking_enabled = False
    cfg = p._request_config(None, [], 4096, 0.0, thinking=True)
    assert cfg.thinking_config is None


def test_gemini_unsupported_model_skips_thinking_config():
    from agent_core.providers.google_gemini import GoogleGeminiProvider

    p = GoogleGeminiProvider.__new__(GoogleGeminiProvider)
    p._model_name = "gemini-1.5-pro"
    p._thinking_enabled = True

    cfg = p._request_config(None, [], 4096, 0.0, thinking=True)
    assert cfg.thinking_config is None
