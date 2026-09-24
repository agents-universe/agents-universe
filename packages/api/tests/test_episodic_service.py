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
