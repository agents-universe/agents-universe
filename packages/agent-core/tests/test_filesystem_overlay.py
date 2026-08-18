"""Filesystem tool overlay semantics for agents/ and workflows/ paths."""
from __future__ import annotations

from pathlib import Path

from agent_core.tools.base import ToolContext
from agent_core.tools.filesystem import FilesystemTool


def _ctx(project_root: Path, framework_root: Path) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(project_root),
        conversation_id="c1",
        user_id="u1",
        framework_root=str(framework_root),
    )


def _mkdir(*parts: Path) -> None:
    for part in parts:
        part.mkdir(parents=True, exist_ok=True)


async def test_write_agents_goes_to_workspace(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw)
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "write_file", "path": "agents/foo.agent.md", "content": "hello"},
        _ctx(proj, fw),
    )

    assert result.get("success") is True
    assert (proj / "agents" / "foo.agent.md").read_text(encoding="utf-8") == "hello"
    assert not (fw / "agents" / "foo.agent.md").exists()


async def test_read_workflows_workspace_shadows_framework(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj / "workflows", fw / "workflows")
    (proj / "workflows" / "x.workflow.md").write_text("PROJECT WORKFLOW", encoding="utf-8")
    (fw / "workflows" / "x.workflow.md").write_text("FRAMEWORK WORKFLOW", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": "workflows/x.workflow.md"},
        _ctx(proj, fw),
    )
    assert result["content"] == "PROJECT WORKFLOW"


async def test_read_agents_falls_back_to_framework_when_workspace_missing(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw / "agents")
    (fw / "agents" / "global.agent.md").write_text("FRAMEWORK AGENT", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": "agents/global.agent.md"},
        _ctx(proj, fw),
    )
    assert result["content"] == "FRAMEWORK AGENT"


async def test_delete_workflow_removes_workspace_copy_only(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj / "workflows", fw / "workflows")
    (proj / "workflows" / "y.workflow.md").write_text("PROJECT", encoding="utf-8")
    (fw / "workflows" / "y.workflow.md").write_text("FRAMEWORK", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute({"operation": "delete_file", "path": "workflows/y.workflow.md"}, _ctx(proj, fw))
    assert result.get("success") is True
    assert not (proj / "workflows" / "y.workflow.md").exists()
    assert (fw / "workflows" / "y.workflow.md").read_text(encoding="utf-8") == "FRAMEWORK"


async def test_template_write_still_rejected(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw / "knowledge" / "_template")
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "write_file", "path": "knowledge/_template/ctx.md", "content": "x"},
        _ctx(proj, fw),
    )
    assert "read-only" in result.get("error", "")


async def test_path_traversal_rejected(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw)
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": "../outside.txt"},
        _ctx(proj, fw),
    )
    assert "Access denied" in result.get("error", "")


# --- In-memory upload store -------------------------------------------------
# User attachments never land on disk — the API holds the bytes in memory for
# the turn and exposes them via ToolContext upload hooks. read_file/list_dir
# must serve those bytes as if the file existed.


def _ctx_with_uploads(project_root: Path, framework_root: Path) -> ToolContext:
    ctx = _ctx(project_root, framework_root)
    store = {
        "db9a8f11cb014974bec3e99b72cd1b9f.ps1": b"Write-Host 'hello'\r\n",
        "notes.txt": b"hello world",
        "blob.bin": b"\x89PNG\r\n\x1a\n" + bytes(range(128, 256)),
    }
    ctx.upload_file_lookup = lambda fname: store.get(fname)
    ctx.upload_file_names = lambda: list(store)
    return ctx


async def test_read_file_serves_in_memory_upload(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw)
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": ".tmp/media/c1/db9a8f11cb014974bec3e99b72cd1b9f.ps1"},
        _ctx_with_uploads(proj, fw),
    )
    assert result["content"] == "Write-Host 'hello'\r\n"
    assert result["size_bytes"] == len(b"Write-Host 'hello'\r\n")
    assert not (proj / ".tmp" / "media" / "c1").exists()  # nothing hit the disk


async def test_read_file_binary_upload_reports_binary_error(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw)
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": ".tmp/media/c1/blob.bin"},
        _ctx_with_uploads(proj, fw),
    )
    assert "not valid UTF-8" in result.get("error", "")


async def test_read_file_missing_upload_hint_lists_store_names(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw)
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": ".tmp/media/c1/nope.ps1"},
        _ctx_with_uploads(proj, fw),
    )
    assert "File not found" in result["error"]
    assert "db9a8f11cb014974bec3e99b72cd1b9f.ps1" in result["available_in_parent"]
    assert "notes.txt" in result["available_in_parent"]


async def test_list_dir_merges_store_uploads(tmp_path):
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw)
    (proj / ".tmp" / "media" / "c1").mkdir(parents=True)
    (proj / ".tmp" / "media" / "c1" / "legacy.txt").write_text("x", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "list_dir", "path": ".tmp/media/c1"},
        _ctx_with_uploads(proj, fw),
    )
    names = [e["name"] for e in result["entries"]]
    assert "db9a8f11cb014974bec3e99b72cd1b9f.ps1" in names
    assert "notes.txt" in names
    assert "legacy.txt" in names
    sizes = {e["name"]: e["size_bytes"] for e in result["entries"]}
    assert sizes["notes.txt"] == len(b"hello world")


async def test_write_into_git_rejected(tmp_path):
    """Filesystem writes into .git (hooks, config) install code git executes
    on later commands — same rejection as the shell sandbox. Reads from
    .git stay allowed (no legitimate write use; fail-closed)."""
    proj = tmp_path / "proj"
    fw = tmp_path / "fw"
    _mkdir(proj, fw)
    tool = FilesystemTool()

    for path in (
        ".git/hooks/pre-commit",
        "subdir/.git/hooks/post-checkout",
        ".git/config",
        ".git/modules/foo/hooks/pre-push",
    ):
        result = await tool.execute(
            {"operation": "write_file", "path": path, "content": "#!/bin/sh\ncat /etc/passwd\n"},
            _ctx(proj, fw),
        )
        assert "error" in result and ".git" in result["error"], path
        assert not (proj / path).exists(), path

    result = await tool.execute(
        {"operation": "create_dir", "path": ".git/hooks"},
        _ctx(proj, fw),
    )
    assert "error" in result

    # Reads from .git stay allowed
    (proj / ".git" / "config").parent.mkdir(parents=True)
    (proj / ".git" / "config").write_text("[core]", encoding="utf-8")
    result = await tool.execute(
        {"operation": "read_file", "path": ".git/config"},
        _ctx(proj, fw),
    )
    assert result.get("content") == "[core]"
