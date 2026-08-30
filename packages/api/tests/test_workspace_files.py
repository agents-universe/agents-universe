"""Tests for the workspace file browser API (list/read/save within a project)."""
from __future__ import annotations

from pathlib import Path

import pytest

from api.paths import PROJECTS_ROOT


def _ws(project) -> Path:
    return PROJECTS_ROOT / project.slug


@pytest.mark.asyncio
async def test_list_root_shows_dirs_and_files(client, make_project):
    """Root listing surfaces the scaffolded directories and skips hidden/._tmp."""
    project = await make_project("ws-root")
    ws = _ws(project)
    (ws / "knowledge").mkdir(exist_ok=True)
    (ws / "README.md").write_text("# Hi", encoding="utf-8")
    (ws / ".hidden").write_text("x", encoding="utf-8")
    (ws / ".tmp").mkdir(exist_ok=True)
    (ws / ".tmp" / "media").mkdir(exist_ok=True)

    res = await client.get(f"/api/projects/{project.project_id}/workspace/files")
    assert res.status_code == 200
    data = res.json()
    names = {e["name"] for e in data["entries"]}
    assert "knowledge" in names
    assert "agents" in names
    assert "README.md" in names
    assert "skills" in names
    assert "workflows" in names
    # Hidden + internal dirs are never surfaced
    assert ".hidden" not in names
    assert ".tmp" not in names
    # Dirs sort first
    kinds = [e["type"] for e in data["entries"]]
    assert kinds.index("dir") < kinds.index("file")


@pytest.mark.asyncio
async def test_list_subdirectory_and_lazy_path(client, make_project):
    project = await make_project("ws-sub")
    ws = _ws(project)
    (ws / "tests" / "generated").mkdir(parents=True, exist_ok=True)
    (ws / "tests" / "generated" / "x.spec.ts").write_text("it('x')", encoding="utf-8")

    res = await client.get(
        f"/api/projects/{project.project_id}/workspace/files", params={"path": "tests/generated"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["path"] == "tests/generated"
    assert [e["name"] for e in data["entries"]] == ["x.spec.ts"]
    assert data["entries"][0]["type"] == "file"
    assert data["entries"][0]["size_bytes"] == len("it('x')")


@pytest.mark.asyncio
async def test_path_escape_rejected(client, make_project):
    project = await make_project("ws-escape")
    for path in ["..", "../outside", "knowledge/../../outside", "C:\\evil", "C:/evil"]:
        res = await client.get(
            f"/api/projects/{project.project_id}/workspace/files", params={"path": path}
        )
        assert res.status_code == 400, f"expected 400 for path {path!r}"


@pytest.mark.asyncio
async def test_read_and_write_file(client, make_project):
    project = await make_project("ws-rw")
    ws = _ws(project)
    (ws / "knowledge").mkdir(exist_ok=True)
    (ws / "knowledge" / "notes.md").write_text("old content", encoding="utf-8")

    res = await client.get(
        f"/api/projects/{project.project_id}/workspace/file", params={"path": "knowledge/notes.md"}
    )
    assert res.status_code == 200
    assert res.json()["content"] == "old content"

    res = await client.put(
        f"/api/projects/{project.project_id}/workspace/file",
        params={"path": "knowledge/notes.md"},
        json={"content": "new content"},
    )
    assert res.status_code == 200
    assert res.json()["saved"] is True
    assert (ws / "knowledge" / "notes.md").read_text(encoding="utf-8") == "new content"


@pytest.mark.asyncio
async def test_write_binary_file_rejected_on_read(client, make_project):
    project = await make_project("ws-binary")
    ws = _ws(project)
    (ws / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    res = await client.get(
        f"/api/projects/{project.project_id}/workspace/file", params={"path": "image.png"}
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_missing_file_404(client, make_project):
    project = await make_project("ws-missing")
    res = await client.get(
        f"/api/projects/{project.project_id}/workspace/file", params={"path": "nope.md"}
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_git_write_rejected(client, make_project):
    project = await make_project("ws-git")
    ws = _ws(project)
    (ws / ".git").mkdir(exist_ok=True)

    res = await client.put(
        f"/api/projects/{project.project_id}/workspace/file",
        params={"path": ".git/hooks/pre-commit"},
        json={"content": "evil"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_project_isolation(client, make_project, as_user):
    """Another user cannot browse a private project's workspace."""
    project = await make_project("ws-private", visibility="private")
    async with as_user("other-user"):
        res = await client.get(
            f"/api/projects/{project.project_id}/workspace/files"
        )
        assert res.status_code == 403
