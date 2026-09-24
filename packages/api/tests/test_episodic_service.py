"""Episodic summary must never leak the model API key it decrypts.

The decrypted key goes into an HTTP header; h11 rejects an illegal value by
echoing it back ("Illegal header value b'...'") — that echo IS the credential,
and both failure paths (the refusal and the generic HTTP error) must keep it
out of the logs.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from api.services.episodic_service import _call_llm_for_summary
from api.services.token_vault import encrypt

USER_ID = "episodic-test-user"
BAD_KEY = "sk-episodic-newline\n"
ESCAPED = repr(BAD_KEY.encode("utf-8"))[2:-1]


class _ProtocolErrorAsyncClient:
    """Raises the h11 echo of an illegal header value, as httpx would."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {ESCAPED}'")

    async def __aexit__(self, *a):
        return False


CLEAN_KEY = "sk-episodic-clean-echo"


class _LeakyAsyncClient:
    """Transport error that quotes the key back (SDK auth errors do this)."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise RuntimeError(f"upstream rejected Authorization: Bearer {CLEAN_KEY}")


def _db_with_config() -> AsyncMock:
    """Fake db whose UserModelConfig query returns one row with an encrypted key."""
    row = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o-mini",
        base_url=None,
        encrypted_key=encrypt(BAD_KEY, USER_ID),
        user_id=USER_ID,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result
    return db


async def test_summary_refuses_header_unsafe_key(monkeypatch, caplog):
    """The pasted-newline key must be refused before the request — pre-fix it
    reached h11, whose echo was logged verbatim via exc_info."""
    monkeypatch.setattr("httpx.AsyncClient", _ProtocolErrorAsyncClient)

    with caplog.at_level(logging.DEBUG):
        result = await _call_llm_for_summary("some transcript", USER_ID, _db_with_config())

    assert result is None
    # Actionable warning naming the problem, no secret anywhere.
    assert "control character" in caplog.text
    assert BAD_KEY not in caplog.text
    assert ESCAPED not in caplog.text


async def test_summary_http_error_log_scrubs_the_key(monkeypatch, caplog):
    """The generic except-branch logs the formatted traceback scrubbed instead
    of exc_info — a transport error quoting the key must not reach the log.
    The key here is header-safe so it passes validation and actually reaches
    the request (the refusal path is covered by the test above)."""
    row = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o-mini",
        base_url=None,
        encrypted_key=encrypt(CLEAN_KEY, USER_ID),
        user_id=USER_ID,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result

    monkeypatch.setattr("httpx.AsyncClient", _LeakyAsyncClient)

    with caplog.at_level(logging.DEBUG):
        out = await _call_llm_for_summary("some transcript", USER_ID, db)

    assert out is None
    assert CLEAN_KEY not in caplog.text
    assert "[REDACTED]" in caplog.text


async def test_summary_accepts_a_clean_key(monkeypatch, caplog):
    """Sanity: a header-safe key passes validation and reaches the request."""
    from unittest.mock import Mock

    row = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o-mini",
        base_url=None,
        encrypted_key=encrypt("sk-clean-key", USER_ID),
        user_id=USER_ID,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result

    class _OkAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"message": {"content": '{"summary": "ok"}'}}]
            }
            return resp

    monkeypatch.setattr("httpx.AsyncClient", _OkAsyncClient)

    with caplog.at_level(logging.DEBUG):
        out = await _call_llm_for_summary("transcript", USER_ID, db)

    assert out == {"summary": "ok"}
    assert "control character" not in caplog.text


# --- anthropic dialect: gateway vs official host vs full_url -----------------
# The runtime provider (anthropic_claude), the connectivity test
# (model_configs._do_test) and the token test (tokens.py) all treat a custom
# anthropic base_url as a Bedrock-compatible gateway: Bearer auth and
# /model/{model}/invoke. The episodic summary must speak the same dialect or
# every summary against a corporate gateway fails.


def _anthropic_row(base_url: str | None, url_mode: str = "base_url") -> AsyncMock:
    row = SimpleNamespace(
        provider="anthropic",
        model_id="claude-haiku-4-5-20251001",
        base_url=base_url,
        url_mode=url_mode,
        encrypted_key=encrypt(CLEAN_KEY, USER_ID),
        user_id=USER_ID,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result
    return db


def _capture_client(captured: list, body: dict | None = None):
    if body is None:
        body = {"content": [{"type": "text", "text": '{"summary": "ok"}'}]}

    class _CaptureAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, **k):
            captured.append((url, headers, json))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = body
            return resp

    return _CaptureAsyncClient


async def test_summary_anthropic_gateway_posts_invoke_with_bearer(monkeypatch):
    """Custom base_url → Bedrock-style gateway: {base}/model/{model}/invoke +
    Bearer. Pre-fix it posted {base}/v1/messages with x-api-key and every
    summary against a gateway 404'd/401'd."""
    captured: list = []
    monkeypatch.setattr("httpx.AsyncClient", _capture_client(captured))

    out = await _call_llm_for_summary(
        "transcript", USER_ID, _anthropic_row("https://gateway.example.com")
    )

    assert out == {"summary": "ok"}
    url, headers, payload = captured[0]
    assert url == "https://gateway.example.com/model/claude-haiku-4-5-20251001/invoke"
    assert headers["Authorization"] == f"Bearer {CLEAN_KEY}"
    assert "x-api-key" not in headers
    assert payload["anthropic_version"] == "bedrock-2023-05-31"
    # The gateway takes the model from the URL path (runtime _gateway_payload
    # omits the key too).
    assert "model" not in payload


async def test_summary_anthropic_official_host_posts_v1_messages(monkeypatch):
    """No custom base_url → official host keeps /v1/messages + x-api-key."""
    captured: list = []
    monkeypatch.setattr("httpx.AsyncClient", _capture_client(captured))

    out = await _call_llm_for_summary("transcript", USER_ID, _anthropic_row(None))

    assert out == {"summary": "ok"}
    url, headers, payload = captured[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == CLEAN_KEY
    assert "Authorization" not in headers
    assert payload["model"] == "claude-haiku-4-5-20251001"


async def test_summary_anthropic_full_url_posts_base_as_is(monkeypatch):
    """url_mode=full_url → POST the base URL as-is with Bearer, like the
    runtime's _gateway_complete."""
    captured: list = []
    monkeypatch.setattr("httpx.AsyncClient", _capture_client(captured))

    out = await _call_llm_for_summary(
        "transcript",
        USER_ID,
        _anthropic_row("https://gateway.example.com/bedrock", url_mode="full_url"),
    )

    assert out == {"summary": "ok"}
    url, headers, payload = captured[0]
    assert url == "https://gateway.example.com/bedrock"
    assert headers["Authorization"] == f"Bearer {CLEAN_KEY}"
    assert "x-api-key" not in headers
    assert payload["anthropic_version"] == "bedrock-2023-05-31"


async def test_summary_legacy_gemini_key_defaults_to_a_gemini_model(monkeypatch):
    """A pre-migration google_gemini key with no user_tier_models row must get
    a gemini default model — pre-fix the table returned "gpt-4o-mini" (an
    OpenAI id) and the summary request 404'd on the Gemini API."""
    # A developer .env may set a system-default model, which would shadow the
    # legacy path this test exercises.
    monkeypatch.setattr(
        "api.config.get_settings",
        lambda: SimpleNamespace(
            system_default_model_id="",
            system_default_base_url="",
            system_default_api_key="",
            llm_ssl_verify=True,
        ),
    )
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = None
    key_result = MagicMock()
    key_result.scalar_one_or_none.return_value = SimpleNamespace(
        provider="google_gemini",
        encrypted_value=encrypt(CLEAN_KEY, USER_ID),
    )
    tier_result = MagicMock()
    tier_result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.side_effect = [cfg_result, key_result, tier_result]

    captured: list = []
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _capture_client(
            captured,
            body={
                "candidates": [
                    {"content": {"parts": [{"text": '{\"summary\": \"ok\"}'}]}}
                ]
            },
        ),
    )

    out = await _call_llm_for_summary("transcript", USER_ID, db)

    assert out == {"summary": "ok"}
    url = captured[0][0]
    assert "gemini-2.5-flash" in url
    assert "gpt-4o-mini" not in url
