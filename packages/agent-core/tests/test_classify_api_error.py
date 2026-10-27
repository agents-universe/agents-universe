"""classify_api_error — context-window overflow arrives as an ordinary
HTTP exception on every provider except Anthropic (which streams a stop
reason). Misclassifying a 400 as UNKNOWN keeps the raw "LLM API error"
bubble; classifying too eagerly (e.g. every 400) would mask real schema
errors, so both directions are pinned here."""
from __future__ import annotations

import httpx
import pytest

from agent_core.providers.base import StopReason, classify_api_error


class _OpenAIStyleError(Exception):
    """openai.APIStatusError shape: status_code + JSON body."""

    def __init__(self, message: str, status_code: int, body: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class _AnthropicStyleError(Exception):
    """anthropic.APIStatusError shape: status_code + .message."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _GenaiStyleError(Exception):
    """google-genai APIError shape: numeric .code + .details."""

    def __init__(self, code: int, details: str):
        super().__init__(details)
        self.code = code
        self.details = details


def _httpx_status_error(status: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gateway.example.com/v1/chat")
    response = httpx.Response(status, text=text, request=request)
    return httpx.HTTPStatusError(f"Server error '{status}'", request=request, response=response)


@pytest.mark.parametrize(
    "exc",
    [
        _OpenAIStyleError(
            "Error code: 400 - {'error': {'message': \"This model's maximum context length is "
            "8192 tokens, but your messages resulted in 9214 tokens\", "
            "'type': 'invalid_request_error', 'param': 'messages', 'code': 'context_length_exceeded'}}",
            status_code=400,
            body='{"error": {"code": "context_length_exceeded"}}',
        ),
        _AnthropicStyleError(
            "prompt is too long: 210000 tokens > 200000 maximum"
        ),
        _GenaiStyleError(
            code=400,
            details="InvalidArgument: input token count exceeds max token count: "
                    "250000 > 1048576. Please reduce the length of the input.",
        ),
        _AnthropicStyleError(
            "model_context_window_exceeded: request exceeds the model context window",
            status_code=413,
        ),
        _httpx_status_error(413, "Payload Too Large"),
        _OpenAIStyleError(
            "Context window exceeded for model gpt-x",
            status_code=400,
            body="",
        ),
    ],
)
def test_context_overflow_classifies(exc):
    assert classify_api_error(exc) is StopReason.CONTEXT_EXCEEDED


def test_httpx_413_without_markers_classifies():
    assert classify_api_error(_httpx_status_error(413, "no markers here")) is StopReason.CONTEXT_EXCEEDED


@pytest.mark.parametrize(
    "exc",
    [
        # 400 with unrelated text — schema/auth errors must stay UNKNOWN.
        _OpenAIStyleError(
            "Invalid schema for function 'search': 'properties' is required",
            status_code=400,
            body="",
        ),
        _AnthropicStyleError("temperature must be between 0 and 1"),
        _GenaiStyleError(code=400, details="API key not valid. Please pass a valid API key."),
        # Rate limits / server errors — retryable, not overflow.
        _OpenAIStyleError("Rate limit reached", status_code=429, body=""),
        _AnthropicStyleError("overloaded", status_code=529),
        # No status at all.
        RuntimeError("connection reset"),
        ValueError("bad input"),
    ],
)
def test_non_overflow_errors_stay_unknown(exc):
    assert classify_api_error(exc) is StopReason.UNKNOWN


def test_context_window_phrase_needs_overflow_verb():
    # "context window" alone (config/docs chatter) must not classify…
    assert classify_api_error(
        _AnthropicStyleError("The context window setting is documented in the console")
    ) is StopReason.UNKNOWN
    # …but paired with an overflow verb it does.
    assert classify_api_error(
        _AnthropicStyleError("Input exceeds the configured context window")
    ) is StopReason.CONTEXT_EXCEEDED
