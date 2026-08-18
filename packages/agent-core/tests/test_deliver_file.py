"""Tests for the deliver_file tool: workspace scoping, copying, media records."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.deliver_file import DeliverFileTool
from agent_core.tools.registry import build_tool_registry

# Same whitelist as the API media router — every produced URL must pass it.
_MEDIA_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-.]*$")


def make_context(project_fs_path: str) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path=project_fs_path,
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
    )


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj-a"
    proj.mkdir()
    return proj


async def test_deliver_file_copies_into_media_dir(project):
    src = project / "reports" / "summary.csv"
    src.parent.mkdir()
    src.write_text("a,b\n1,2\n", encoding="utf-8")

    result = await DeliverFileTool().execute(
        {"path": "reports/summary.csv"},
        make_context(str(project)),
    )
    files = result.get("files", [])
    assert len(files) == 1, result
    rec = files[0]
    assert rec["name"] == "summary.csv"
    assert rec["media_type"] == "text/csv"
    assert rec["size"] == src.stat().st_size
    fname = rec["url"].rsplit("/", 1)[-1]
    assert _MEDIA_FILENAME_RE.match(fname), f"unsafe media filename: {fname}"
    assert rec["url"] == f"/api/media/proj/conv/{fname}"
    # the copy lives in the conversation media dir, the source is untouched
    copied = project / ".tmp" / "media" / "conv" / fname
    assert copied.is_file()
    assert copied.read_text(encoding="utf-8") == "a,b\n1,2\n"
    assert src.is_file()


async def test_deliver_file_already_in_media_dir_no_copy(project):
    media_dir = project / ".tmp" / "media" / "conv"
    media_dir.mkdir(parents=True)
    existing = media_dir / "ab12cd34.txt"
    existing.write_text("hello", encoding="utf-8")

    result = await DeliverFileTool().execute(
        {"path": ".tmp/media/conv/ab12cd34.txt"},
        make_context(str(project)),
    )
    rec = result["files"][0]
    assert rec["name"] == "ab12cd34.txt"
    assert rec["url"].endswith("ab12cd34.txt")
    assert rec["size"] == 5


async def test_deliver_file_path_escape_rejected(project, tmp_path):
    # a file OUTSIDE the workspace must not be reachable via ../ or abs path
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    result = await DeliverFileTool().execute(
        {"path": f"../{outside.name}"},
        make_context(str(project)),
    )
    assert "error" in result
    assert "escape" in result["error"]

    result2 = await DeliverFileTool().execute(
        {"path": str(outside)},
        make_context(str(project)),
    )
    assert "error" in result2


async def test_deliver_file_missing_rejected(project):
    result = await DeliverFileTool().execute(
        {"path": "does-not-exist.csv"},
        make_context(str(project)),
    )
    assert "error" in result
    assert "not found" in result["error"]

    result2 = await DeliverFileTool().execute({"path": ""}, make_context(str(project)))
    assert "error" in result2


async def test_deliver_file_empty_suffix_gets_servable_name(project):
    src = project / "notes"
    src.write_text("plain text", encoding="utf-8")

    result = await DeliverFileTool().execute({"path": "notes"}, make_context(str(project)))
    rec = result["files"][0]
    fname = rec["url"].rsplit("/", 1)[-1]
    assert _MEDIA_FILENAME_RE.match(fname)
    assert rec["media_type"] == "application/octet-stream"
    assert rec["name"] == "notes"


async def test_deliver_file_registered_as_core_tool():
    registry = build_tool_registry()
    assert "deliver_file" in registry
    assert registry["deliver_file"].name == "deliver_file"
