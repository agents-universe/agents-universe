"""Kong tool: a stored x-api-key token must never leak through transport errors."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_core.tools.kong import KongTool


class _Ctx:
    user_id = "u1"
    project_id = "proj-1"
    secret_key = "test-secret-key"
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
