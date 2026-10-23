"""Request-shape edge cases across the LLM providers.

Each case is a request the real API/gateway rejects or a stream the parser
mistakes for a normal end of turn. All are exercised without network calls.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_core.agent import Agent
from agent_core.compressor import _estimate_tokens_safe
from agent_core.providers.anthropic_claude import AnthropicClaudeProvider
from agent_core.providers.base import Message, StopReason
from agent_core.providers.google_gemini import GoogleGeminiProvider, _max_output_tokens
from agent_core.providers.openai import OpenAIProvider


# ─── Anthropic: temperature vs thinking ──────────────────────────────────


def _request_kwargs(provider: AnthropicClaudeProvider) -> dict:
    return provider._request_kwargs(
        [Message(role="user", content="hi")], None, 4096, 0.0,
    )


def test_anthropic_omits_temperature_when_thinking_is_sent():
    """The Messages API rejects any temperature but 1 with thinking enabled.

    The agent never passes a temperature, so the provider default 0.0 was
    sent alongside every thinking request: each call 400'd and the
    retry-without-thinking fallback silently disabled thinking.
    """
    on = AnthropicClaudeProvider(
        api_key="x", model="claude-sonnet-4-6", thinking_enabled=True,
    )
    kwargs = _request_kwargs(on)
    assert "thinking" in kwargs
    assert "temperature" not in kwargs

    off = AnthropicClaudeProvider(
        api_key="x", model="claude-sonnet-4-6", thinking_enabled=False,
    )
    kwargs_off = _request_kwargs(off)
    assert "thinking" not in kwargs_off
    assert kwargs_off["temperature"] == 0.0


def test_anthropic_gateway_payload_omits_temperature_with_thinking():
    p = AnthropicClaudeProvider(
        api_key="x", model="claude-sonnet-4-6",
        base_url="https://gateway.example.com", thinking_enabled=True,
    )
    payload = p._gateway_payload(
        [Message(role="user", content="hi")], None, max_tokens=4096, temperature=0.0,
    )
    assert "thinking" in payload
    assert "temperature" not in payload


@pytest.mark.asyncio
async def test_anthropic_stream_retry_restores_temperature():
    """The thinking-reject retry pops `thinking` from kwargs; the temperature
    omitted alongside it must come back — the complete() fallback restores it
    (with a comment saying so), and without it the retried stream request runs
    at the API default 1.0 instead of the configured 0.0."""
    p = AnthropicClaudeProvider(
        api_key="x", model="claude-sonnet-4-6", thinking_enabled=True,
    )
    captured: list[dict] = []

    class _EmptyEvents:
        def __aiter__(self):
            async def gen():
                return
                yield  # pragma: no cover
            return gen()

    class _StreamCtx:
        def __init__(self, kwargs: dict):
            self._kwargs = kwargs

        async def __aenter__(self):
            if "thinking" in self._kwargs:
                # The SDK surfaces the 400 when the stream context opens,
                # before any event — emitted stays False, retry fires.
                raise RuntimeError("400 thinking: unsupported parameter")
            return _EmptyEvents()

        async def __aexit__(self, *exc):
            return False

    class _Messages:
        def stream(self, **kwargs):
            captured.append(kwargs)
            return _StreamCtx(kwargs)

    p._client = SimpleNamespace(messages=_Messages())

    _ = [c async for c in p.stream([Message(role="user", content="hi")], temperature=0.0)]

    assert len(captured) == 2
    assert "thinking" in captured[0]
    assert "temperature" not in captured[0]
    assert "thinking" not in captured[1]
    assert captured[1].get("temperature") == 0.0


# ─── Anthropic gateway: mid-stream error event ───────────────────────────


class _FakeSSEResponse:
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


@pytest.mark.asyncio
async def test_gateway_sse_error_event_raises_instead_of_ending_turn():
    """An Anthropic-style mid-stream error must not become a normal END_TURN.

    The direct SDK raises on these events; dropping them here persisted a
    truncated reply and reported a successful turn.
    """
    p = AnthropicClaudeProvider.__new__(AnthropicClaudeProvider)
    resp = _FakeSSEResponse([
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"partial"}}',
        'data: {"type":"error","error":{"type":"overloaded_error",'
        '"message":"Overloaded"}}',
    ])
    with pytest.raises(RuntimeError, match="overloaded_error"):
        async for _chunk in p._parse_sse(resp):
            pass


# ─── Gemini: output-token clamp ──────────────────────────────────────────


def test_gemini_clamps_output_tokens_per_model():
    """Agent configs default to 128000; Gemini rejects that with 400."""
    assert _max_output_tokens("gemini-2.5-flash") == 65_535
    assert _max_output_tokens("gemini-3-pro") == 65_535
    assert _max_output_tokens("gemini-1.5-flash") == 8_192

    p = GoogleGeminiProvider(
        api_key="k", model="gemini-2.5-flash", thinking_enabled=False,
    )
    clamped = p._request_config(None, None, 128_000, 0.0)
    assert clamped.max_output_tokens == 65_535
    untouched = p._request_config(None, None, 4_096, 0.0)
    assert untouched.max_output_tokens == 4_096


# ─── Gemini: no-argument function call ───────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_complete_handles_none_function_args(monkeypatch):
    """A zero-argument function call reports args=None; dict(None) raised."""
    p = GoogleGeminiProvider(
        api_key="k", model="gemini-2.5-flash", thinking_enabled=False,
    )
    part = SimpleNamespace(
        thought=False,
        function_call=SimpleNamespace(name="ping", args=None),
        text=None,
    )
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[part]),
        finish_reason=SimpleNamespace(name="STOP"),
    )
    response = SimpleNamespace(candidates=[candidate], usage_metadata=None)

    async def _generate_content(**_kwargs):
        return response

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content)),
    )
    monkeypatch.setattr(p, "_client", lambda: fake_client)

    result = await p.complete([Message(role="user", content="hi")])
    assert result.message.tool_calls[0]["function"] == {
        "name": "ping", "arguments": "{}",
    }


def test_gemini_all_blocked_finish_reasons_report_refusal():
    blocked = (
        "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
        "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
    )
    for reason in blocked:
        assert GoogleGeminiProvider._normalize_stop(reason, False) is StopReason.REFUSAL


# ─── Gemini: thinking-rejection retry covers request-time errors ─────────


class _RaisingStream:
    def __aiter__(self):
        async def gen():
            raise ValueError("ThinkingConfig is not supported by this model")
            yield  # pragma: no cover

        return gen()


class _ChunkStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


def _text_chunk(text: str):
    part = SimpleNamespace(function_call=None, text=text, thought=False)
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[part]), finish_reason=None,
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=None)


@pytest.mark.asyncio
async def test_gemini_stream_retries_when_first_chunk_raises(monkeypatch):
    """The SDK issues the request on first iteration, not at the await.

    A ThinkingConfig rejection therefore surfaced inside the async-for,
    outside the old try, and the stream failed instead of retrying without
    thinking.
    """
    p = GoogleGeminiProvider(
        api_key="k", model="gemini-2.5-flash", thinking_enabled=True,
    )
    configs = []

    async def _generate_content_stream(**kwargs):
        configs.append(kwargs["config"])
        if kwargs["config"].thinking_config is not None:
            return _RaisingStream()
        return _ChunkStream([_text_chunk("ok")])

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(
            generate_content_stream=_generate_content_stream,
        )),
    )
    monkeypatch.setattr(p, "_client", lambda: fake_client)

    out = [c async for c in p.stream([Message(role="user", content="hi")])]

    assert [c.delta for c in out if c.delta] == ["ok"]
    assert p._thinking_enabled is False
    assert len(configs) == 2
    assert configs[1].thinking_config is None


# ─── OpenAI: usage on a chunk that still carries choices ────────────────


class _OpenAIStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()

    async def close(self):
        pass


def _openai_chunk(content: str, finish: str | None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice], usage=usage)


@pytest.mark.asyncio
async def test_openai_usage_kept_on_chunk_with_a_choice(monkeypatch):
    """LiteLLM attaches final usage to a chunk that still has an empty choice."""
    p = OpenAIProvider.__new__(OpenAIProvider)
    p._model = "gpt-4o"
    p._reasoning_effort = None
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    stream = _OpenAIStream([
        _openai_chunk("hi", None),
        _openai_chunk("", "stop", usage=usage),
    ])

    class _Completions:
        async def create(self, **_kwargs):
            return stream

    p._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions()),
    )

    out = [c async for c in p.stream([Message(role="user", content="hi")])]
    assert {"prompt_tokens": 11, "completion_tokens": 7} in [c.usage for c in out]


# ─── Agent: blank streamed arguments mean no arguments ──────────────────


def test_blank_tool_arguments_mean_empty_object():
    tc = {"function": {"name": "ping", "arguments": ""}}
    assert Agent._parse_tool_arguments(tc) == {}
    # Normalized so the next provider request carries valid JSON.
    assert tc["function"]["arguments"] == "{}"

    tc_none = {"function": {"name": "ping", "arguments": None}}
    assert Agent._parse_tool_arguments(tc_none) == {}

    tc_json = {"function": {"name": "ping", "arguments": '{"a": 1}'}}
    assert Agent._parse_tool_arguments(tc_json) == {"a": 1}

    with pytest.raises(json.JSONDecodeError):
        Agent._parse_tool_arguments({"function": {"arguments": "{"}})


def test_non_object_json_arguments_rejected():
    """A well-formed array/null/string payload must not reach args.get(...).

    The knowledge_rw/memory_rw post-execution hooks call args.get outside
    the tool try/except — a non-dict argument crashes the turn with
    AttributeError instead of the malformed-arguments tool error both loops
    already handle for JSONDecodeError/TypeError.
    """
    for payload in ("[1, 2]", "null", '"just a string"', "42"):
        tc = {"function": {"name": "knowledge_rw", "arguments": payload}}
        with pytest.raises(TypeError):
            Agent._parse_tool_arguments(tc)


# ─── Compressor: whitespace-free payloads ───────────────────────────────


def test_whitespace_free_ascii_payload_is_not_one_token():
    """Minified JSON/base64 has one "word" but thousands of real tokens."""
    blob = "a" * 40_000
    assert _estimate_tokens_safe(blob) >= 10_000
