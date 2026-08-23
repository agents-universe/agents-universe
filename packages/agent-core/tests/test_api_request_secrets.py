"""Tests for api_request secret resolution (user tokens + multi-secret auth)."""
import base64
import json

from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_core.tools.api_request import ApiRequestTool
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

def make_context(http=None, session=None) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path="/tmp/proj",
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
        http_client=http,
        session=session,
    )


async def _run(params, http, session=None):
    return await ApiRequestTool().execute(params, make_context(http=http, session=session))


def _sent_headers(http) -> dict:
    return http.stream.call_args.kwargs["headers"]


@pytest.mark.asyncio
async def test_user_scope_bearer_resolves_token():
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))
    session = FakeSession()

    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="tok-123")):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "auth_type": "bearer",
                "secret_ref": "svc:token",
                "secret_scope": "user",
            },
            http,
            session,
        )

    assert result["status"] == 200
    assert _sent_headers(http)["Authorization"] == "Bearer tok-123"
    assert session.calls == []  # no prompt needed when token exists


@pytest.mark.asyncio
async def test_string_numeric_params_coerced_to_int():
    """LLM 常把数字参数传成字符串（"30000"）——str/int 混比此前直接抛
    TypeError 让整个调用失败。字符串必须被 int() 后再夹取。"""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))

    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "max_response_chars": "30000",
                "timeout_seconds": "45",
            },
            http,
            session=FakeSession(),
        )

    assert result["status"] == 200
    assert http.stream.call_args.kwargs["timeout"] == 45.0


@pytest.mark.asyncio
async def test_string_numeric_params_fallback_on_garbage():
    """不可解析的字符串（"abc"）回退默认值，而不是 500 或 TypeError。"""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))

    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "max_response_chars": "abc",
                "timeout_seconds": None,
            },
            http,
            session=FakeSession(),
        )

    assert result["status"] == 200
    assert http.stream.call_args.kwargs["timeout"] == 30.0


@pytest.mark.asyncio
async def test_user_scope_missing_token_prompts_with_user_vault_flag():
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))
    session = FakeSession(result="secret_saved")

    with patch(
        "agent_core.tools._auth.get_token_optional",
        new=AsyncMock(side_effect=[None, "tok-after-prompt"]),
    ):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "auth_type": "bearer",
                "secret_ref": "svc:token",
                "secret_scope": "user",
            },
            http,
            session,
        )

    assert result["status"] == 200
    prompt = session.calls[0]
    assert prompt["service_key"] == "svc:token"
    assert prompt["secret"] is True
    assert prompt["save_to_user_tokens"] is True
    assert prompt["save_to_project_secrets"] is False
    assert _sent_headers(http)["Authorization"] == "Bearer tok-after-prompt"


@pytest.mark.asyncio
async def test_project_scope_missing_token_prompts_to_project_secrets():
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))
    session = FakeSession(result="secret_saved")

    with patch(
        "agent_core.tools._auth.get_secret_optional",
        new=AsyncMock(side_effect=[None, "proj-secret"]),
    ):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "auth_type": "bearer",
                "secret_ref": "svc:token",
                "secret_scope": "project",
            },
            http,
            session,
        )

    assert result["status"] == 200
    prompt = session.calls[0]
    assert prompt["save_to_project_secrets"] is True
    assert prompt["save_to_user_tokens"] is False
    assert _sent_headers(http)["Authorization"] == "Bearer proj-secret"


@pytest.mark.asyncio
async def test_basic_auth_uses_multi_secret_refs():
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))

    with patch(
        "agent_core.tools._auth.get_token_optional",
        new=AsyncMock(side_effect=["svc-user", "svc-pass"]),
    ):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "auth_type": "basic",
                "secret_refs": {"username": "svc:user", "password": "svc:pass"},
                "secret_scope": "user",
            },
            http,
        )

    assert result["status"] == 200
    expected = "Basic " + base64.b64encode(b"svc-user:svc-pass").decode()
    assert _sent_headers(http)["Authorization"] == expected


@pytest.mark.asyncio
async def test_custom_header_with_multi_secret_template():
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))

    with patch(
        "agent_core.tools._auth.get_token_optional",
        new=AsyncMock(side_effect=["u1", "p1"]),
    ):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "auth_type": "custom_header",
                "auth_header_name": "X-Service-Auth",
                "auth_prefix": "{username}:{password}",
                "secret_refs": {"username": "svc:user", "password": "svc:pass"},
                "secret_scope": "user",
            },
            http,
        )

    assert result["status"] == 200
    assert _sent_headers(http)["X-Service-Auth"] == "u1:p1"


@pytest.mark.asyncio
async def test_body_field_auth_injects_json_body():
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))

    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="k-9")):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "POST",
                "path": "/submit",
                "base_url": "https://api.example.com",
                "auth_type": "body_field",
                "auth_field_name": "apiKey",
                "secret_ref": "svc:key",
                "secret_scope": "user",
                "json_body": {"q": 1},
            },
            http,
            FakeSession(),  # POST → confirmation gate; auto-allow it
        )

    assert result["status"] == 200
    assert http.stream.call_args.kwargs["json"] == {"q": 1, "apiKey": "k-9"}


@pytest.mark.asyncio
async def test_missing_secret_after_prompt_returns_error():
    session = FakeSession(result="secret_saved")

    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value=None)):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "auth_type": "bearer",
                "secret_ref": "svc:token",
                "secret_scope": "user",
            },
            AsyncMock(),
            session,
        )

    assert "not available after prompt" in result["error"]


@pytest.mark.asyncio
async def test_api_request_no_confirm_keeps_prod_gate():
    """api_request_no_confirm 只绕过写操作确认，prd 环境的确认保留。"""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))
    session = FakeSession(result="allow")
    ctx = make_context(http=http, session=session)
    ctx.api_request_no_confirm = True

    result = await ApiRequestTool().execute(
        {
            "integration_key": "svc",
            "method": "POST",
            "path": "/submit",
            "base_url": "https://api.example.com",
            "environment": "prd",
            "auth_type": "none",
            "json_body": {"q": 1},
        },
        ctx,
    )

    assert result["status"] == 200
    assert len(session.calls) == 1  # prd 环境仍走确认门


@pytest.mark.asyncio
async def test_headers_only_redacts_secret_values():
    """headers_only responses must scrub resolved secret VALUES like the
    text/json bodies — an echo-anything gateway would otherwise leak the
    token via a header (Authorization echoed back, etc.)."""
    http = _mock_stream_response(
        AsyncMock(),
        FakeResponse(
            200,
            headers={
                "content-type": "application/json",
                "x-echo": "Bearer tok-123",
                "x-other": "harmless",
                "set-cookie": "session=abc",
            },
        ),
    )

    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="tok-123")):
        result = await _run(
            {
                "integration_key": "svc",
                "method": "GET",
                "path": "/health",
                "base_url": "https://api.example.com",
                "auth_type": "bearer",
                "secret_ref": "svc:token",
                "secret_scope": "user",
                "response_mode": "headers_only",
            },
            http,
        )

    assert result["headers"]["x-echo"] == "Bearer [REDACTED:_default]"
    assert result["headers"]["x-other"] == "harmless"
    assert "set-cookie" not in result["headers"]
