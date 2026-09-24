"""get_provider must refuse header-unsafe API keys, and providers must be
able to scrub their own key out of exception text.

h11 rejects an illegal header value by echoing it back ("Illegal header value
b'...'") — for an API key that echo IS the credential, and it would surface in
stream error messages, compression logs and task_failed rows. The redaction
helper matches the raw value AND the bytes-repr escaped form that tracebacks
quote (same rule as the token/api-key routers' ``_redact_key``).
"""
from __future__ import annotations

import pytest

from agent_core.providers.registry import get_provider
from agent_core.tools._http import redact_secret


def test_get_provider_refuses_control_char_key_without_echoing():
    bad = "sk-live-with-newline\n"
    with pytest.raises(ValueError) as excinfo:
        get_provider("anthropic", {"api_key": bad, "model": "claude-haiku-4-5"})
    msg = str(excinfo.value)
    assert "control character" in msg
    # The refusal names the header problem, never the key itself.
    assert "sk-live" not in msg


def test_get_provider_refuses_non_ascii_key_without_echoing():
    bad = "sk-café-key"
    with pytest.raises(ValueError) as excinfo:
        get_provider("openai", {"api_key": bad, "model": "gpt-4o"})
    msg = str(excinfo.value)
    assert "non-ASCII" in msg
    assert "café" not in msg


def test_get_provider_accepts_clean_key_and_exposes_it_for_scrubbing():
    provider = get_provider("openai", {"api_key": "sk-clean-123", "model": "gpt-4o"})
    assert provider.secret_values() == ["sk-clean-123"]
    scrubbed = provider.scrub("Authorization: Bearer sk-clean-123")
    assert scrubbed == "Authorization: Bearer [REDACTED]"
    # Unrelated text passes through untouched.
    assert provider.scrub("all fine") == "all fine"


def test_provider_scrub_matches_the_escaped_bytes_form():
    """h11 quotes the value via bytes repr — scrub must match that form too.

    The provider is built directly (bypassing get_provider's validation) to
    model an already-stored bad key reaching the scrub path.
    """
    from agent_core.providers.openai import OpenAIProvider

    key = "sk-pasted-newline\n"
    provider = OpenAIProvider(api_key=key)
    escaped = repr(key.encode("utf-8"))[2:-1]
    text = f"Illegal header value b'Bearer {escaped}'"
    assert key not in text  # the message holds only the escaped form

    scrubbed = provider.scrub(text)
    assert "sk-pasted-newline" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_redact_secret_masks_raw_and_escaped_forms():
    key = "abc\n"
    escaped = repr(key.encode("utf-8"))[2:-1]
    assert escaped == "abc\\n"  # bytes-repr form, as h11/tracebacks quote it

    # Raw form: the exact value, trailing newline included in the match.
    assert redact_secret("x abc\ny", key) == "x [REDACTED]y"
    # Escaped form: what a traceback/h11 message holds instead.
    assert redact_secret(f"Illegal header value b'{escaped}'", key) == (
        "Illegal header value b'[REDACTED]'"
    )
    # Plain secrets and no-op inputs.
    assert redact_secret("x sk-clean y", "sk-clean") == "x [REDACTED] y"
    assert redact_secret("no secrets here", key) == "no secrets here"
    assert redact_secret("text", None) == "text"
    assert redact_secret("", key) == ""
