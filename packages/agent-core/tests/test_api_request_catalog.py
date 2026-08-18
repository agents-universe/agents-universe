"""Tests for api_request endpoint_key resolution from the integration catalog."""
import json

from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_core.tools.api_request import ApiRequestTool
from agent_core.tools.base import ToolContext


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        # Streaming reads consume self.text — default it from the payload so
        # fake responses without explicit text still carry a body.
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = {"content-type": "application/json"}
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


DUMMY_CATALOG = """\
---
category: integrations
slug: integrations/custom-api
tags: [integration, api, third-party]
---

# Custom Third-Party API Integrations

## Integration Catalog

```yaml
integration_key: dummy
display_name: Dummy Service
environments:
  dev:
    base_url: https://dummy.example.com
    allowed_hosts:
      - dummy.example.com
  uat:
    base_url: https://dummy-uat.example.com
    allowed_hosts:
      - dummy-uat.example.com
auth:
  type: bearer
  secret_ref: third_party:dummy:{environment}
defaults:
  timeout_seconds: 30
  max_response_chars: 20000
endpoints:
  health:
    method: GET
    path: /api/v1/health
    description: Health check
    side_effect: false
    response_json_path: $.data
  ping:
    method: GET
    path: /api/v1/ping
    description: Ping
    side_effect: false
  create:
    method: POST
    path: /api/v1/items
    description: Create an item
    side_effect: true
```
"""


def _mock_stream_response(http, response):
    """Configure an AsyncMock *http* so `async with http.stream(...) as r`
    yields *response* and the call is recorded on http.stream."""
    stream = Mock()
    stream.return_value = AsyncMock()
    stream.return_value.__aenter__.return_value = response
    stream.return_value.__aexit__.return_value = False
    http.stream = stream
    return http

def make_catalog_context(
    tmp_path,
    catalog_content: str,
    http=None,
    session=None,
    integration_settings=None,
) -> ToolContext:
    kdir = tmp_path / "knowledge" / "integrations"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "custom-api.md").write_text(catalog_content, encoding="utf-8")
    ctx = ToolContext(
        project_id="proj",
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
        http_client=http,
        session=session,
    )
    if integration_settings:
        ctx.integration_settings = integration_settings
    return ctx


async def _run(params, context: ToolContext):
    return await ApiRequestTool().execute(params, context)


def _sent_request(http):
    return http.stream.call_args


def _sent_url(http) -> str:
    return _sent_request(http).args[1]


def _sent_headers(http) -> dict:
    return _sent_request(http).kwargs["headers"]


# ── Happy path ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_endpoint_key_happy_path(tmp_path):
    """method/path/base_url/allowed_hosts/response_json_path/auth all from catalog."""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {"data": {"status": "ok"}}))
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG, http=http)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok-1")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "health"}, ctx
        )

    assert result["status"] == 200
    # Catalog resolved: method default, path, base_url, env
    assert _sent_url(http) == "https://dummy.example.com/api/v1/health"
    assert _sent_request(http).args[0] == "GET"
    # Auth from catalog secret_ref pattern with environment substituted
    assert _sent_headers(http)["Authorization"] == "Bearer tok-1"
    # response_json_path normalized from $.data and applied
    assert result["body"] == {"status": "ok"}
    # Resolution info echoed to the LLM
    assert result["catalog"]["resolved_from_catalog"] is True
    assert result["catalog"]["endpoint_key"] == "health"
    assert result["catalog"]["resolved_environment"] == "dev"


# ── Base URL precedence ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_base_url_precedence_params_over_catalog(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG, http=http)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {
                "integration_key": "dummy",
                "endpoint_key": "health",
                "method": "GET",
                "base_url": "https://explicit.example.com",
                "allowed_hosts": ["explicit.example.com"],
            },
            ctx,
        )

    assert result["status"] == 200
    assert _sent_url(http) == "https://explicit.example.com/api/v1/health"


@pytest.mark.asyncio
async def test_base_url_precedence_catalog_over_env_var(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(
        tmp_path,
        DUMMY_CATALOG,
        http=http,
        integration_settings={"THIRD_PARTY_DUMMY_BASE_URL_DEV": "https://env.example.com"},
    )

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "health", "method": "GET"}, ctx
        )

    assert result["status"] == 200
    # Catalog wins over the env-var fallback
    assert _sent_url(http) == "https://dummy.example.com/api/v1/health"


# ── Environment selection ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_environment_selects_catalog_env(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG, http=http)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "health", "method": "GET", "environment": "uat"},
            ctx,
        )

    assert result["status"] == 200
    assert _sent_url(http) == "https://dummy-uat.example.com/api/v1/health"
    assert result["catalog"]["resolved_environment"] == "uat"


@pytest.mark.asyncio
async def test_environment_none_selects_first_catalog_env(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG, http=http)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "health", "method": "GET"}, ctx
        )

    assert _sent_url(http) == "https://dummy.example.com/api/v1/health"
    assert result["catalog"]["resolved_environment"] == "dev"


@pytest.mark.asyncio
async def test_unknown_environment_falls_back_to_env_var(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(
        tmp_path,
        DUMMY_CATALOG,
        http=http,
        integration_settings={"THIRD_PARTY_DUMMY_BASE_URL_INT": "https://int.example.com"},
    )

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "health", "method": "GET", "environment": "int"},
            ctx,
        )

    assert result["status"] == 200
    assert _sent_url(http) == "https://int.example.com/api/v1/health"


# ── Fallbacks and errors ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_catalog_raw_path_still_works(tmp_path):
    """Without endpoint_key the legacy raw-path flow is untouched."""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, "", http=http)

    result = await _run(
        {
            "integration_key": "svc",
            "method": "GET",
            "path": "/health",
            "base_url": "https://api.example.com",
            "allowed_hosts": ["api.example.com"],
            "auth_type": "none",
        },
        ctx,
    )

    assert result["status"] == 200
    assert _sent_url(http) == "https://api.example.com/health"


@pytest.mark.asyncio
async def test_endpoint_key_with_missing_catalog_errors(tmp_path):
    ctx = make_catalog_context(tmp_path, "")

    result = await _run({"integration_key": "dummy", "endpoint_key": "health", "method": "GET"}, ctx)

    assert "integration catalog not found" in result["error"]
    assert "custom-api" in result["error"]
    assert result["endpoint_key"] == "health"


@pytest.mark.asyncio
async def test_unknown_endpoint_key_errors(tmp_path):
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG)

    result = await _run({"integration_key": "dummy", "endpoint_key": "nope", "method": "GET"}, ctx)

    assert "nope" in result["error"]
    assert "not found in catalog" in result["error"]


@pytest.mark.asyncio
async def test_malformed_yaml_block_does_not_break_catalog(tmp_path):
    broken = """\
```yaml
integration_key: broken
environments:
  dev:
    base_url: [unclosed
```

```yaml
integration_key: good
environments:
  dev:
    base_url: https://good.example.com
    allowed_hosts: [good.example.com]
auth:
  type: none
endpoints:
  ping:
    method: GET
    path: /ping
    side_effect: false
```
"""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, broken, http=http)

    result = await _run({"integration_key": "good", "endpoint_key": "ping", "method": "GET"}, ctx)

    assert result["status"] == 200
    assert _sent_url(http) == "https://good.example.com/ping"


@pytest.mark.asyncio
async def test_all_broken_catalog_errors_without_exception(tmp_path):
    broken = """\
```yaml
integration_key: [unclosed
```
"""
    ctx = make_catalog_context(tmp_path, broken)

    result = await _run({"integration_key": "dummy", "endpoint_key": "health", "method": "GET"}, ctx)

    # All blocks skipped → catalog is effectively empty → treated as not found
    assert "integration catalog not found" in result["error"]


# ── Overrides and confirmation gates ────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_method_overrides_catalog(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    session = FakeSession(result="allow")
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG, http=http, session=session)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "ping", "method": "POST", "json_body": {"a": 1}},
            ctx,
        )

    assert result["status"] == 200
    assert _sent_request(http).args[0] == "POST"
    assert len(session.calls) == 1  # write method triggered the confirmation gate


@pytest.mark.asyncio
async def test_side_effect_forces_confirmation_on_get(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    session = FakeSession(result="allow")
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG, http=http, session=session)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "create", "method": "GET"}, ctx
        )

    assert result["status"] == 200
    assert len(session.calls) == 1  # side_effect: true forces confirmation on GET


@pytest.mark.asyncio
async def test_no_side_effect_get_skips_confirmation(tmp_path):
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    session = FakeSession()
    ctx = make_catalog_context(tmp_path, DUMMY_CATALOG, http=http, session=session)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "health", "method": "GET"}, ctx
        )

    assert result["status"] == 200
    assert session.calls == []


# ── Multi-integration, frontmatter, auth defaults, guards ───────────────

@pytest.mark.asyncio
async def test_multi_integration_catalog_selects_correct_entry(tmp_path):
    catalog = DUMMY_CATALOG + """\
```yaml
integration_key: crm
environments:
  dev:
    base_url: https://crm.example.com
    allowed_hosts: [crm.example.com]
auth:
  type: none
endpoints:
  leads:
    method: GET
    path: /v2/leads
    side_effect: false
```
"""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, catalog, http=http)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run({"integration_key": "crm", "endpoint_key": "leads", "method": "GET"}, ctx)

    assert result["status"] == 200
    assert _sent_url(http) == "https://crm.example.com/v2/leads"


@pytest.mark.asyncio
async def test_catalog_file_without_frontmatter_parses(tmp_path):
    no_frontmatter = DUMMY_CATALOG.split("---\n", 2)[2]  # strip the frontmatter block
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, no_frontmatter, http=http)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="tok")):
        result = await _run(
            {"integration_key": "dummy", "endpoint_key": "health", "method": "GET"}, ctx
        )

    assert result["status"] == 200
    assert _sent_url(http) == "https://dummy.example.com/api/v1/health"


@pytest.mark.asyncio
async def test_catalog_auth_defaults(tmp_path):
    catalog = """\
```yaml
integration_key: svc
environments:
  dev:
    base_url: https://svc.example.com
    allowed_hosts: [svc.example.com]
auth:
  type: api_key_header
  secret_ref: svc:key
  header_name: X-API-Key
endpoints:
  ping:
    method: GET
    path: /ping
    side_effect: false
```
"""
    http = _mock_stream_response(AsyncMock(), FakeResponse(200, {}))
    ctx = make_catalog_context(tmp_path, catalog, http=http)

    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="k-9")):
        result = await _run({"integration_key": "svc", "endpoint_key": "ping", "method": "GET"}, ctx)

    assert result["status"] == 200
    assert _sent_headers(http)["X-API-Key"] == "k-9"


@pytest.mark.asyncio
async def test_missing_path_and_endpoint_key_errors(tmp_path):
    ctx = make_catalog_context(tmp_path, "")

    result = await _run(
        {
            "integration_key": "svc",
            "method": "GET",
            "base_url": "https://api.example.com",
            "auth_type": "none",
        },
        ctx,
    )

    assert "path is required" in result["error"]
