"""Gemini client construction must pass a finite request timeout.

The SDK's HttpOptions.timeout defaults to None, which the httpx path
(timeout=None → no timeout) and the aiohttp path (ClientTimeout(total=None))
both read as infinite — a blackholed endpoint would hang the turn forever.
The openai/anthropic providers set phased 300s ceilings; Gemini needs the
same parity (HttpOptions.timeout is milliseconds).
"""
from __future__ import annotations

from agent_core.providers.google_gemini import GoogleGeminiProvider


def test_client_construction_passes_finite_timeout(monkeypatch):
    import google.genai as genai_module

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(genai_module, "Client", _FakeClient)

    provider = GoogleGeminiProvider(api_key="k", model="gemini-2.5-flash")
    provider._client()

    http_options = captured["http_options"]
    assert http_options.timeout is not None
    assert 0 < http_options.timeout <= 600_000  # milliseconds, sane ceiling
