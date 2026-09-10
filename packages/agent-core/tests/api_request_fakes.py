"""httpx stand-ins shared by the api_request tests.

api_request streams its response, so the fake has to satisfy the async
context-manager + ``aiter_bytes`` protocol rather than just return a response.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

from agent_core.tools.base import ToolContext


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        # Streaming reads consume self.text — default it from the payload so
        # fake responses without explicit text still carry a body.
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {"content-type": "application/json"}
        self.encoding = "utf-8"

    def json(self):
        return self._payload

    # httpx stream protocol — api_request reads the body streamingly now.
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self.text.encode(self.encoding or "utf-8")


class FakeSession:
    """Records request_user_selection calls; returns the configured result."""

    def __init__(self, result="allow"):
        self.result = result
        self.calls = []

    async def request_user_selection(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _mock_stream_response(http, response):
    """Configure an AsyncMock *http* so `async with http.stream(...) as r`
    yields *response* and the call is recorded on http.stream."""
    stream = Mock()
    stream.return_value = AsyncMock()
    stream.return_value.__aenter__.return_value = response
    stream.return_value.__aexit__.return_value = False
    http.stream = stream
    return http


def make_context(http=None, session=None, **kwargs) -> ToolContext:
    """ToolContext with api_request defaults; extra fields pass through."""
    kwargs.setdefault("project_fs_path", "/tmp/proj")
    kwargs.setdefault("db_session", None)
    return ToolContext(
        project_id="proj",
        conversation_id="conv",
        user_id="user-1",
        http_client=http,
        session=session,
        **kwargs,
    )
