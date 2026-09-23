"""reasoning_effort threading: constructor storage and request-kwargs gating.

The effort must reach reasoning-model request bodies and MUST NOT leak into
non-reasoning requests — chat-completions on gpt-4o-class models and on
OpenAI-compatible gateways (vLLM/Ollama model names) 400 on the unknown
parameter.
"""
from __future__ import annotations

from agent_core.providers.openai import AzureOpenAIProvider, OpenAIProvider
from agent_core.providers.registry import get_provider

_AZURE = {"api_key": "x", "endpoint": "https://example.openai.azure.com"}


def test_constructor_stores_effort():
    p = OpenAIProvider(api_key="x", model="gpt-5", reasoning_effort="high")
    assert p._reasoning_effort == "high"
    assert p._reasoning_kwargs() == {"reasoning_effort": "high"}


def test_omitted_effort_defaults_to_none_and_empty_kwargs():
    p = OpenAIProvider(api_key="x", model="gpt-5")
    assert p._reasoning_effort is None
    assert p._reasoning_kwargs() == {}


def test_reasoning_models_get_effort():
    for model in ("gpt-5", "gpt-5-mini", "o4-mini", "o3"):
        p = OpenAIProvider(api_key="x", model=model, reasoning_effort="medium")
        assert p._reasoning_kwargs() == {"reasoning_effort": "medium"}, model


def test_non_reasoning_models_never_get_effort():
    # Even with the effort set, non-reasoning and gateway-style names
    # routed through provider "openai" must send an empty kwargs dict.
    for model in ("gpt-4o", "gpt-4.1", "glm-5.3", "qwen3-32b"):
        p = OpenAIProvider(api_key="x", model=model, reasoning_effort="high")
        assert p._reasoning_kwargs() == {}, model


def test_azure_stores_effort_as_explicit_param():
    """AzureOpenAIProvider takes **_kwargs — without explicit params the
    effort would be swallowed silently, leaving Azure configs env-only."""
    az = AzureOpenAIProvider(**_AZURE, model="gpt-5", reasoning_effort="low")
    assert az._reasoning_effort == "low"
    assert az._reasoning_kwargs() == {"reasoning_effort": "low"}
    # thinking_enabled must also be an explicit param (accepted, unused:
    # OpenAI CoT is parse-only — there is no request-side switch).
    az2 = AzureOpenAIProvider(**_AZURE, model="gpt-4o", thinking_enabled=False, reasoning_effort="high")
    assert az2._reasoning_effort == "high"


def test_openai_accepts_thinking_flag_without_storing_request_param():
    # Construction must not TypeError; there is no request-side thinking
    # param to build for OpenAI (CoT arrives parse-only from gateways).
    p = OpenAIProvider(api_key="x", model="gpt-4o", thinking_enabled=False)
    assert p._reasoning_kwargs() == {}


def test_registry_passthrough():
    p = get_provider("openai", {"api_key": "x", "model": "gpt-5", "reasoning_effort": "high"})
    assert p._reasoning_kwargs() == {"reasoning_effort": "high"}
    az = get_provider("azure_openai", {**_AZURE, "model": "gpt-5", "reasoning_effort": "minimal"})
    assert az._reasoning_kwargs() == {"reasoning_effort": "minimal"}
