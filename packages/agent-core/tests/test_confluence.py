"""Confluence tool: numeric params must tolerate LLM stringified numbers."""
from __future__ import annotations

import pytest

from agent_core.tools.confluence import ConfluenceTool


class _FakeClient:
    def __init__(self):
        self.tree = [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]
        self.pages = {p["id"]: {"body": f"body-{p['id']}"} for p in self.tree}

    async def get_page_tree(self, root_page_id: str, max_pages: int = 100) -> list[dict]:
        assert isinstance(max_pages, int), "max_pages must be an int for len() comparisons"
        return self.tree[:max_pages]

    async def get_page(self, page_id: str) -> dict:
        return {"id": page_id, "body": self.pages[page_id]["body"]}


@pytest.mark.asyncio
async def test_get_page_tree_coerces_string_max_pages():
    """LLM 把 max_pages 传成字符串时，get_page_tree 内部的
    `while len(pages) < max_pages` 会抛 TypeError——必须先 int()。"""
    tool = ConfluenceTool()
    client = _FakeClient()

    result = await tool._op_get_page_tree(
        {"root_page_id": "root-1", "max_pages": "2", "include_body": True},
        client,
    )

    assert result["count"] == 2
    assert [p["id"] for p in result["pages"]] == ["p1", "p2"]
    assert result["pages"][0]["body"] == "body-p1"


@pytest.mark.asyncio
async def test_get_page_tree_falls_back_on_garbage_max_pages():
    tool = ConfluenceTool()
    client = _FakeClient()

    result = await tool._op_get_page_tree(
        {"root_page_id": "root-1", "max_pages": "abc"},
        client,
    )

    assert result["count"] == 3


@pytest.mark.asyncio
async def test_get_pages_coerces_string_page_ids():
    """LLM 把 page_ids 传成字符串时，逐字符迭代会为每个字符发一次请求
    （'12,34' → '1','2',',','3','4'）。必须按逗号拆分成列表。"""
    tool = ConfluenceTool()
    client = _FakeClient()

    result = await tool._op_get_pages({"page_ids": "p1, p2"}, client)

    assert result["count"] == 2
    assert [p["id"] for p in result["pages"]] == ["p1", "p2"]


@pytest.mark.asyncio
async def test_get_pages_keeps_list_input():
    tool = ConfluenceTool()
    client = _FakeClient()

    result = await tool._op_get_pages({"page_ids": ["p1", "p3"]}, client)

    assert result["count"] == 2
    assert [p["id"] for p in result["pages"]] == ["p1", "p3"]


@pytest.mark.asyncio
async def test_http_error_body_redacts_credential():
    """Atlassian can echo the credential in 401 bodies — the resolved token
    must never reach the LLM/history inside the returned error message."""
    import httpx
    from unittest.mock import AsyncMock, patch
    from agent_core.tools.confluence import ConfluenceTool

    tool = ConfluenceTool()

    client = AsyncMock()
    client.api_token = "ATATT-secret-token-888"
    client.email = "agent@example.com"
    client.base_url = "https://conf.example.com"
    client.auth_type = "basic"

    def _boom(page_id):
        resp = httpx.Response(
            401,
            text='{"message": "Bad credentials: ATATT-secret-token-888"}',
            request=httpx.Request("GET", "https://conf.example.com/rest/api/content/1"),
        )
        raise httpx.HTTPStatusError("Unauthorized", request=resp.request, response=resp)

    client.get_page = AsyncMock(side_effect=_boom)

    with patch("agent_core.tools.confluence.get_token_optional", return_value="ATATT-secret-token-888"), \
         patch("agent_core.tools.confluence.get_token", return_value="ATATT-secret-token-888"), \
         patch.object(tool, "_build_client", new=AsyncMock(return_value=client)):
        result = await tool.execute(
            {"operation": "get_pages", "page_ids": ["1"]},
            _MinimalCtx(),
        )

    assert result["error"].startswith("Confluence API returned 401")
    assert "ATATT-secret-token-888" not in result["error"]
    assert "REDACTED" in result["error"]


class _MinimalCtx:
    """Minimal stand-in for execute(): _build_client is patched."""

    def __init__(self):
        self.user_id = "u1"
        self.secret_key = "test-secret-key"

    def cfg(self, key, default=None):
        return default
