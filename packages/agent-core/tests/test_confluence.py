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
