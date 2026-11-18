"""Kimi models pin temperature server-side — the field must stay off the wire.

Moonshot's model parameter reference fixes temperature for kimi-k2.5+ (k3 /
k2.7-code: 1.0; k2.6: 1.0 or 0.6 non-thinking) and rejects any explicitly
passed value with 400 "invalid temperature: only 1 is allowed for this model".
The agent never passes a temperature, so the provider default 0.0 was sent
with every request and the first message of an openai-type Kimi config failed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.providers.base import Message
from agent_core.providers.openai import AzureOpenAIProvider, OpenAIProvider


def _provider(model: str, cls=OpenAIProvider) -> OpenAIProvider:
    p = cls.__new__(cls)  # skip __init__ (no client)
    p._model = model
    p._reasoning_effort = None
    return p


# ─── _fixed_sampling: model-name matching ────────────────────────────────


@pytest.mark.parametrize("model", ["kimi-k3", "Kimi-K3", "kimi-k2.6", "kimi-k2.5", "kimi-k2.7-code", "kimi-k2.10"])
def test_fixed_sampling_matches_kimi_k25_and_later(model: str):
    assert _provider(model)._fixed_sampling() is True


@pytest.mark.parametrize(
    "model",
    # k2 and earlier keep temperature; unrelated models never match.
    ["kimi-k2", "kimi-k2.4", "kimi-k1", "gpt-4o", "glm-5.3", "my-kimi-k3-gateway"],
)
def test_fixed_sampling_skips_other_models(model: str):
    assert _provider(model)._fixed_sampling() is False


def test_fixed_sampling_version_compare_is_tuple_not_float():
    # "2.10" > "2.5" as versions; float() would rank 2.10 below 2.5.
    assert _provider("kimi-k2.10")._fixed_sampling() is True
    assert _provider("kimi-k2.4")._fixed_sampling() is False


# ─── wire shape: temperature omitted only for fixed-sampling models ──────


class _EmptyStream:
    def __aiter__(self):
        async def gen():
            return
            yield  # pragma: no cover
        return gen()

    async def close(self):
        pass


class _CaptureCompletions:
    def __init__(self, response=None):
        self.captured: list[dict] = []
        self._response = response

    async def create(self, **kwargs):
        self.captured.append(kwargs)
        if kwargs.get("stream"):
            return _EmptyStream()
        return self._response


def _fake_response():
    choice = SimpleNamespace(
        message=SimpleNamespace(content="ok", tool_calls=None),
        finish_reason="stop",
    )
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        model="m",
    )


def _client(completions) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["kimi-k3", "kimi-k2.6"])
async def test_stream_omits_temperature_for_fixed_sampling_models(model: str):
    p = _provider(model)
    completions = _CaptureCompletions()
    p._client = _client(completions)

    _ = [c async for c in p.stream([Message(role="user", content="hi")])]

    assert completions.captured, "stream never issued a request"
    kwargs = completions.captured[0]
    assert "temperature" not in kwargs
    assert kwargs["max_tokens"] > 0


@pytest.mark.asyncio
async def test_stream_still_sends_temperature_for_normal_models():
    p = _provider("gpt-4o")
    completions = _CaptureCompletions()
    p._client = _client(completions)

    _ = [c async for c in p.stream([Message(role="user", content="hi")], temperature=0.7)]

    assert completions.captured[0]["temperature"] == 0.7


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["kimi-k3", "kimi-k2.6"])
async def test_complete_omits_temperature_for_fixed_sampling_models(model: str):
    p = _provider(model)
    completions = _CaptureCompletions(response=_fake_response())
    p._client = _client(completions)

    await p.complete([Message(role="user", content="hi")])

    kwargs = completions.captured[0]
    assert "temperature" not in kwargs
    assert kwargs["max_tokens"] > 0


@pytest.mark.asyncio
async def test_complete_still_sends_temperature_for_normal_models():
    p = _provider("gpt-4o")
    completions = _CaptureCompletions(response=_fake_response())
    p._client = _client(completions)

    await p.complete([Message(role="user", content="hi")], temperature=0.7)

    assert completions.captured[0]["temperature"] == 0.7


# ─── Azure overrides the same four request builders ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["kimi-k3", "kimi-k2.6"])
async def test_azure_stream_omits_temperature_for_fixed_sampling_models(model: str):
    p = _provider(model, cls=AzureOpenAIProvider)
    completions = _CaptureCompletions()
    p._client = _client(completions)

    _ = [c async for c in p.stream([Message(role="user", content="hi")])]

    assert "temperature" not in completions.captured[0]


@pytest.mark.asyncio
async def test_azure_complete_omits_temperature_for_fixed_sampling_model():
    p = _provider("kimi-k3", cls=AzureOpenAIProvider)
    completions = _CaptureCompletions(response=_fake_response())
    p._client = _client(completions)

    await p.complete([Message(role="user", content="hi")])

    assert "temperature" not in completions.captured[0]
