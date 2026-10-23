"""Personal-memory tag filtering must match the stored JSON encoding.

Tags are persisted with json.dumps (ensure_ascii=True), so a Chinese tag is
stored as \\uXXXX escapes and a tag containing a quote is stored with a
backslash-escaped inner quote. The filter used to search for the raw tag text,
so neither could ever match the row it was just saved on.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def memory(client, make_project):
    project = await make_project()
    tags = ["中文", 'a"b', "100%", "phase[1]", "plain"]
    resp = await client.post(
        f"/api/projects/{project.project_id}/memories/personal",
        json={"content": "tag filter target", "tags": tags},
    )
    assert resp.status_code == 201
    return project, resp.json(), tags


@pytest.mark.parametrize("tag", ["中文", 'a"b', "100%", "phase[1]", "plain"])
async def test_tag_filter_matches_every_saved_tag(client, memory, tag):
    project, created, _ = memory

    resp = await client.get(
        f"/api/projects/{project.project_id}/memories/personal",
        params={"tag": tag},
    )

    assert resp.status_code == 200
    ids = [m["memory_id"] for m in resp.json()]
    assert ids == [created["memory_id"]]


async def test_tag_filter_still_rejects_a_non_matching_tag(client, memory):
    project, _, _ = memory
    resp = await client.get(
        f"/api/projects/{project.project_id}/memories/personal",
        params={"tag": "missing"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_tag_needle_escapes_bracket_on_mssql_only():
    """T-SQL reads [abc] as a character class; only MSSQL needs the escape."""
    from api.routers.memories import _tag_needle

    assert _tag_needle("phase[1]", "mssql") == '"phase[[]1]"'
    assert _tag_needle("100%", "mssql") == '"100\\%"'
    assert _tag_needle("phase[1]", "postgresql") == '"phase[1]"'
    assert _tag_needle("phase[1]", "sqlite") == '"phase[1]"'
    assert _tag_needle("phase[1]", "mysql") == '"phase[1]"'


# 20 six-character CJK tags: 200 characters as text (under the 500 limit an
# ensure_ascii=False check would measure), but each character becomes \uXXXX
# in the stored JSON — about 800 characters, past the Unicode(500) column.
_OVERFLOWING_TAGS = ["一二三四五六"] * 20


async def test_create_rejects_tags_that_only_overflow_after_json_escaping(client, make_project):
    project = await make_project()
    resp = await client.post(
        f"/api/projects/{project.project_id}/memories/personal",
        json={"content": "x", "tags": _OVERFLOWING_TAGS},
    )
    assert resp.status_code == 422


async def test_update_rejects_tags_that_only_overflow_after_json_escaping(
    client, memory
):
    project, created, _ = memory
    resp = await client.put(
        f"/api/projects/{project.project_id}/memories/personal/{created['memory_id']}",
        json={"tags": _OVERFLOWING_TAGS},
    )
    assert resp.status_code == 422
