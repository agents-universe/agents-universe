"""A failed connectivity test must not print the submitted key into the log.

Both test endpoints logged ``traceback.format_exc()`` verbatim at ERROR level.
SDK and httpx messages echo the credential they rejected ("Incorrect API key
provided: sk-..."), so the traceback carried the key in full.
"""
from __future__ import annotations

import logging

import httpx
import openai
import pytest

_KEY = "sk-logme-1234567890"


async def test_api_key_test_traceback_log_redacts_key(client, caplog, monkeypatch):
    resp = await client.put("/api/api-keys/openai", json={"value": _KEY})
    assert resp.status_code == 200

    class _FakeOpenAIError(Exception):
        def __str__(self):
            return f"Incorrect API key provided: {_KEY}."

    class _FakeModels:
        async def list(self):
            raise _FakeOpenAIError("bad key")

    class _FakeAsyncOpenAI:
        models = _FakeModels()

        def __init__(self, *args, **kwargs):
            pass

    # The endpoint imports AsyncOpenAI inside the request, so patch the SDK
    # attribute it will look up.
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeAsyncOpenAI)

    with caplog.at_level(logging.ERROR, logger="api.routers.api_keys"):
        resp = await client.post("/api/api-keys/openai/test")

    assert resp.status_code == 200
    assert _KEY not in caplog.text, caplog.text
    assert "[REDACTED]" in caplog.text  # the traceback itself is still logged


async def test_token_test_traceback_log_redacts_key(client, caplog, monkeypatch):
    resp = await client.put("/api/tokens/anthropic", json={"value": _KEY})
    assert resp.status_code == 200

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise RuntimeError(f"Incorrect API key provided: {_KEY}.")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    with caplog.at_level(logging.ERROR, logger="api.routers.tokens"):
        resp = await client.post("/api/tokens/anthropic/test")

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert _KEY not in caplog.text, caplog.text
    assert "[REDACTED]" in caplog.text


async def test_shared_token_tests_traceback_log_redacts_key(
    client, caplog, capsys, monkeypatch
):
    """The shared git/jira/confluence helper must redact before logging too.

    ``test_service_token`` used to ``traceback.print_exc()`` — writing the
    raw credential-bearing message to stderr, past the log pipeline, while
    both sibling endpoints (api_keys / tokens) already logged the redacted
    traceback.
    """
    key = "atlassian-logme-1234567890"
    resp = await client.put(
        "/api/tokens/jira",
        json={"value": key, "base_url": "https://example.atlassian.net"},
    )
    assert resp.status_code == 200
    resp = await client.put("/api/tokens/jira:email", json={"value": "logme@example.com"})
    assert resp.status_code == 200

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError(f"Bad credentials: token {key} is invalid")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    with caplog.at_level(logging.ERROR, logger="api.services.token_tests"):
        resp = await client.post("/api/tokens/jira/test")

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert key not in caplog.text, caplog.text
    assert "[REDACTED]" in caplog.text  # the traceback itself is still logged
    # print_exc() would have put the raw key on stderr, bypassing caplog.
    assert key not in capsys.readouterr().err
