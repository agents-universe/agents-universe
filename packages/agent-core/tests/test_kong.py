"""Kong tool: a stored x-api-key token must never leak through transport errors."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_core.tools.kong import KongTool


class _Ctx:
    user_id = "u1"
    project_id = "proj-1"
    secret_key = "test-secret-key"
    current_task_id = None
    integration_settings: dict[str, str] = {}

    def cfg(self, key, default=None):
        return default


@pytest.mark.asyncio
async def test_token_with_control_char_is_refused_without_echoing(caplog):
    """A stored Kong token with a pasted trailing newline must be refused
    before headers are built. h11 echoes an illegal value back ("Illegal
    header value b'...'") — the echo IS the credential, and execute()'s
    except Exception returns `{e}` raw to the LLM and the log."""
    bad = "kong-with-newline\n"

    tool = KongTool()
    with patch.object(tool, "_resolve_token", AsyncMock(return_value=bad)):
        result = await tool.execute(
            {"operation": "request", "path": "/status", "base_url": "https://kong.example.com"},
            _Ctx(),
        )

    assert "error" in result
    assert "kong:dev" in result["error"]
    assert "control character" in result["error"]
    assert "kong-with-newline" not in result["error"]
    assert "kong-with-newline" not in caplog.text


@pytest.mark.asyncio
async def test_prompt_failure_reports_missing_token_instead_of_raising():
    """Timeout/abort/headless errors from the interactive prompt all subclass
    RuntimeError; they must not escape execute() and replace the actionable
    'token not configured' result."""

    class _FailingSession:
        async def request_user_selection(self, **kwargs):
            raise RuntimeError("prompt timed out")

    class _PromptCtx(_Ctx):
        session = _FailingSession()

    tool = KongTool()
    with patch(
        "agent_core.tools.kong.get_secret_optional", AsyncMock(return_value=None)
    ):
        result = await tool.execute(
            {"operation": "request", "path": "/status", "base_url": "https://kong.example.com"},
            _PromptCtx(),
        )

    assert "error" in result
    assert "kong:dev" in result["error"]


@pytest.mark.asyncio
async def test_transport_error_log_redacts_query_values(caplog):
    """httpx exception messages quote the full request URL, including the
    query string — logging the raw exception leaked every query value."""

    class _ExplodingHTTP:
        def stream(self, *args, **kwargs):
            raise RuntimeError(
                "connect failed for https://kong.example.com/orders?apiKey=supersecret"
            )

    tool = KongTool()
    with patch.object(tool, "_resolve_token", AsyncMock(return_value="tok")), \
         patch("agent_core.tools.kong.ensure_http_client", return_value=_ExplodingHTTP()):
        result = await tool.execute(
            {
                "operation": "request",
                "path": "/orders",
                "base_url": "https://kong.example.com",
                "query_params": {"apiKey": "supersecret"},
            },
            _Ctx(),
        )

    assert "error" in result
    assert "supersecret" not in result["error"]
    assert "supersecret" not in caplog.text
    assert "REDACTED" in result["error"]
