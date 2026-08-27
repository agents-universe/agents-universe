"""Mock tests for GitHub tool: create_pr, fork, star operations."""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_core.tools.github import GitHubTool


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP status {self.status_code}")


@pytest.mark.asyncio
async def test_create_pr_422_returns_unique_open_pr_as_already_exists():
    http = AsyncMock()
    http.post.return_value = FakeResponse(422, {"message": "Validation Failed"})
    http.get.return_value = FakeResponse(
        200,
        [{
            "number": 42,
            "html_url": "https://ghe.example/pull/42",
            "title": "PROJ-456 implementation",
            "head": {"ref": "feature/PROJ-456"},
            "base": {"ref": "main"},
        }],
    )

    result = await GitHubTool()._op_create_pr(
        {
            "repository": "team/service",
            "title": "PROJ-456 implementation",
            "head_branch": "feature/PROJ-456",
            "base_branch": "main",
        },
        "https://ghe.example/api/v3",
        {},
        http,
    )

    assert result["status"] == "already_exists"
    assert result["number"] == 42
    assert http.get.await_args.kwargs["params"] == {
        "state": "open",
        "head": "team:feature/PROJ-456",
        "base": "main",
        "per_page": 100,
    }


@pytest.mark.asyncio
async def test_create_pr_422_without_unique_match_returns_explicit_error():
    http = AsyncMock()
    http.post.return_value = FakeResponse(422, {})
    http.get.return_value = FakeResponse(200, [])

    result = await GitHubTool()._op_create_pr(
        {
            "repository": "team/service",
            "title": "PROJ-456 implementation",
            "head_branch": "feature/PROJ-456",
            "base_branch": "main",
        },
        "https://ghe.example/api/v3",
        {},
        http,
    )

    assert "error" in result
    assert "no unique open PR" in result["error"]


@pytest.mark.asyncio
async def test_create_pr_422_qualified_head_uses_cross_repo_head_as_is():
    # Fork PR: head_branch is already '<fork-owner>:<branch>' — the duplicate
    # query must use it verbatim, not double-qualify with the upstream owner.
    http = AsyncMock()
    http.post.return_value = FakeResponse(422, {"message": "Validation Failed"})
    http.get.return_value = FakeResponse(
        200,
        [{
            "number": 7,
            "html_url": "https://ghe.example/pull/7",
            "title": "PROJ-789 implementation",
            "head": {"ref": "feature/PROJ-789"},
            "base": {"ref": "main"},
        }],
    )

    result = await GitHubTool()._op_create_pr(
        {
            "repository": "team/service",
            "title": "PROJ-789 implementation",
            "head_branch": "mybot:feature/PROJ-789",
            "base_branch": "main",
        },
        "https://ghe.example/api/v3",
        {},
        http,
    )

    assert result["status"] == "already_exists"
    assert http.get.await_args.kwargs["params"] == {
        "state": "open",
        "head": "mybot:feature/PROJ-789",
        "base": "main",
        "per_page": 100,
    }


@pytest.mark.asyncio
async def test_fork_returns_fork_repo_info():
    http = AsyncMock()
    http.post.return_value = FakeResponse(
        202,
        {
            "full_name": "mybot/service",
            "clone_url": "https://ghe.example/mybot/service.git",
            "ssh_url": "git@ghe.example:mybot/service.git",
            "html_url": "https://ghe.example/mybot/service",
            "default_branch": "main",
        },
    )

    result = await GitHubTool()._op_fork(
        {"repository": "team/service"}, "https://ghe.example/api/v3", {}, http
    )

    assert result["status"] == "forked"
    assert result["full_name"] == "mybot/service"
    assert result["parent"] == "team/service"
    http.post.assert_awaited_once_with(
        "https://ghe.example/api/v3/repos/team/service/forks", headers={}, json={}
    )


@pytest.mark.asyncio
async def test_fork_requires_repository():
    http = AsyncMock()
    result = await GitHubTool()._op_fork({}, "https://ghe.example/api/v3", {}, http)
    assert "error" in result
    http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_starred_204_means_starred():
    http = AsyncMock()
    http.get.return_value = FakeResponse(204, None)

    result = await GitHubTool()._op_is_starred(
        {"repository": "team/service"}, "https://ghe.example/api/v3", {}, http
    )

    assert result == {"starred": True, "repository": "team/service"}
    http.get.assert_awaited_once_with(
        "https://ghe.example/api/v3/user/starred/team/service", headers={}
    )


@pytest.mark.asyncio
async def test_is_starred_404_means_not_starred():
    http = AsyncMock()
    http.get.return_value = FakeResponse(404, None)

    result = await GitHubTool()._op_is_starred(
        {"repository": "team/service"}, "https://ghe.example/api/v3", {}, http
    )

    assert result == {"starred": False, "repository": "team/service"}


@pytest.mark.asyncio
async def test_star_puts_star():
    http = AsyncMock()
    http.put.return_value = FakeResponse(204, None)

    result = await GitHubTool()._op_star(
        {"repository": "team/service"}, "https://ghe.example/api/v3", {}, http
    )

    assert result == {"status": "starred", "repository": "team/service"}
    http.put.assert_awaited_once_with(
        "https://ghe.example/api/v3/user/starred/team/service", headers={}
    )


class _FakeCtx:
    """Minimal ToolContext stand-in: token lookup + config + shared http client."""

    def __init__(self, http):
        self.user_id = "u1"
        self._token = None
        self.http_client = http
        self.http_client_no_proxy = None
        self.ssl_verify = True
        self._cfg = {"GIT_BASE_URL": "https://ghe.example", "GIT_API_BASE_PATH": "/api/v3"}
        self.secret_key = "test-secret-key"

    def cfg(self, key, default=None):
        return self._cfg.get(key, default)


async def _fake_get_token(context, service_key):
    return "ghp_secret-token-123456"


@pytest.mark.asyncio
async def test_http_error_body_redacts_token():
    """GitHub gateways echo the submitted credential back in error bodies
    (same pattern kong.py redacts). The resolved token must never reach the
    LLM/history inside the returned error message."""
    http = AsyncMock()

    async def _boom(*args, **kwargs):
        resp = httpx.Response(
            401,
            text='{"message": "Bad credentials: ghp_secret-token-123456"}',
            request=httpx.Request("GET", "https://ghe.example/api/v3/user"),
        )
        raise httpx.HTTPStatusError(
            "Unauthorized", request=resp.request, response=resp
        )

    http.get.side_effect = _boom

    ctx = _FakeCtx(http)
    with patch("agent_core.tools.github.get_token", _fake_get_token):
        result = await GitHubTool().execute(
            {"operation": "is_starred", "repository": "team/service"},
            ctx,
        )

    assert result["error"].startswith("GitHub API returned 401")
    assert "ghp_secret-token-123456" not in result["error"]
    assert "REDACTED" in result["error"]
