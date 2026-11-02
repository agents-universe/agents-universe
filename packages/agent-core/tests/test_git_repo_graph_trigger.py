"""git_repo operations trigger graph builds; failures never break git ops.

Same local-bare-remote pattern as test_git_repo.py — no network, no tokens.
Clone is the only op that needs auth; it is patched to the bare remote path.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_core.knowledge.graph.builder as _builder_mod
import agent_core.tools.git_repo as _git_repo_mod
from agent_core.tools.git_repo import GitRepoTool
from agent_core.tools.repo_graph import RepoGraphTool


def _run(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _make_context(project_fs_path: str) -> Any:
    ctx = MagicMock()
    ctx.project_fs_path = project_fs_path
    ctx.cfg.return_value = ""
    return ctx


@pytest.fixture(autouse=True)
def no_git_token(monkeypatch):
    async def _no_token(context, service_key):
        return None
    monkeypatch.setattr(_git_repo_mod, "get_token_optional", _no_token)


def _make_two_branch_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Bare remote (main: 2 files, dev: 3 files) + its seed worktree."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _run("init", "--bare", "--initial-branch=main", str(bare))
    work = tmp_path / "seed"
    _run("clone", str(bare), str(work))
    _run("config", "user.email", "t@t.t", cwd=work)
    _run("config", "user.name", "t", cwd=work)
    (work / "main.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (work / "utils.py").write_text("def util():\n    return 1\n", encoding="utf-8")
    _run("add", ".", cwd=work)
    _run("commit", "-m", "main", cwd=work)
    _run("push", "origin", "main", cwd=work)
    _run("checkout", "-b", "dev", cwd=work)
    (work / "dev_only.py").write_text("def dev_fn():\n    return 2\n", encoding="utf-8")
    (work / "main.py").write_text("def hello():\n    return 2\n", encoding="utf-8")
    _run("add", ".", cwd=work)
    _run("commit", "-m", "dev", cwd=work)
    _run("push", "origin", "dev", cwd=work)
    return bare, work


@pytest.fixture
def grammars():
    try:
        import graphify.extract  # noqa: F401
    except ImportError:
        pytest.skip("graphifyy not installed")
    return True


@pytest.fixture
def clone_ctx(tmp_path: Path) -> tuple[GitRepoTool, Any, Path, Path]:
    """Tool + workspace context + seed worktree; clone auth → local bare remote."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bare, seed = _make_two_branch_remote(tmp_path)
    tool = GitRepoTool()

    async def _local_url(self, params, context):
        return None, str(bare), None

    with patch.object(GitRepoTool, "_get_token_and_url", _local_url):
        ctx = _make_context(str(workspace))
        yield tool, ctx, workspace, seed


@pytest.mark.asyncio
async def test_clone_builds_graph(clone_ctx, grammars):
    tool, ctx, _, _ = clone_ctx
    result = await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)
    assert result["status"] == "cloned"
    graph = result.get("graph")
    assert graph is not None and graph["status"] == "built"
    assert graph["stats"]["files"] == 2  # main.py + utils.py
    assert "repo_map" in graph and "hint:" in graph["repo_map"]
    # artifacts on disk under .tmp/repo_graph/
    kg = Path(ctx.project_fs_path) / ".tmp" / "repo_graph" / "repo"
    assert (kg / "graph.json").is_file()
    assert (kg / "graph_report.md").is_file()


@pytest.mark.asyncio
async def test_checkout_rebuilds_graph(clone_ctx, grammars):
    tool, ctx, _, _ = clone_ctx
    await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)
    result = await tool.execute(
        {"operation": "checkout", "repository": "repo", "branch": "dev"}, ctx)
    assert result["status"] == "checked_out"
    graph = result.get("graph")
    assert graph is not None and graph["status"] == "built"
    assert graph["stats"]["files"] == 3  # main.py + utils.py + dev_only.py


@pytest.mark.asyncio
async def test_pull_rebuilds_only_on_change(clone_ctx, grammars):
    tool, ctx, _, seed = clone_ctx

    await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)

    # first pull: no remote change -> no graph key at all
    noop = await tool.execute({"operation": "pull", "repository": "repo"}, ctx)
    assert noop["status"] == "updated"
    assert "graph" not in noop

    # push a new commit, pull again -> head changed -> graph rebuilt
    _run("checkout", "main", cwd=seed)
    (seed / "extra.py").write_text("def extra():\n    return 3\n", encoding="utf-8")
    _run("add", ".", cwd=seed)
    _run("commit", "-m", "extra", cwd=seed)
    _run("push", "origin", "main", cwd=seed)

    changed = await tool.execute({"operation": "pull", "repository": "repo"}, ctx)
    assert changed["status"] == "updated"
    graph = changed.get("graph")
    assert graph is not None and graph["status"] == "built"
    assert graph["stats"]["files"] == 3  # extra.py now present


@pytest.mark.asyncio
async def test_too_many_files_skips_with_hint(clone_ctx, monkeypatch):
    tool, ctx, _, _ = clone_ctx
    monkeypatch.setattr(_builder_mod, "AUTO_BUILD_MAX_FILES", 1)
    monkeypatch.setattr(_builder_mod, "MAX_BUILD_FILES", 1)
    result = await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)
    assert result["status"] == "cloned"
    graph = result.get("graph")
    assert graph is not None and graph["status"] == "skipped"
    assert graph["reason"] == "too_many_files"
    assert "hint" in graph


@pytest.mark.asyncio
async def test_large_repo_queues_background_build(clone_ctx, grammars, monkeypatch):
    """Between the two caps a clone returns immediately and builds off-turn."""
    tool, ctx, workspace, _ = clone_ctx
    monkeypatch.setattr(_builder_mod, "AUTO_BUILD_MAX_FILES", 1)
    result = await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)
    assert result["status"] == "cloned"
    graph = result.get("graph")
    # not "built": the turn must not have waited for the parse
    assert graph is not None and graph["status"] == "queued"
    assert graph["reason"] == "background_build"
    assert "background" in graph["hint"]

    await _builder_mod.wait_pending_auto_builds()
    kg = Path(workspace) / ".tmp" / "repo_graph" / "repo"
    assert (kg / "graph.json").is_file()
    found = await RepoGraphTool().execute(
        {"operation": "query", "repository": "repo", "query": "hello"}, ctx)
    assert any(m["name"] == "hello" for m in found["matches"])


@pytest.mark.asyncio
async def test_query_reports_running_build(clone_ctx, monkeypatch):
    """While the background build runs, query says so instead of 'run build'."""
    tool, ctx, _, _ = clone_ctx
    monkeypatch.setattr(_builder_mod, "AUTO_BUILD_MAX_FILES", 1)

    release = asyncio.Event()
    calls: list[int] = []
    real_build = _builder_mod.build_repo_graph

    async def blocking_build(*args, **kwargs):
        calls.append(1)
        await release.wait()
        return await real_build(*args, **kwargs)

    monkeypatch.setattr(_builder_mod, "build_repo_graph", blocking_build)

    result = await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)
    assert result["graph"]["status"] == "queued"
    for _ in range(200):  # worker reaches the blocking build
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls, "background worker never started the build"

    found = await RepoGraphTool().execute(
        {"operation": "query", "repository": "repo", "query": "hello"}, ctx)
    assert "Background build in progress" in found.get("hint", "")

    release.set()
    await _builder_mod.wait_pending_auto_builds()
    found = await RepoGraphTool().execute(
        {"operation": "query", "repository": "repo", "query": "hello"}, ctx)
    assert any(m["name"] == "hello" for m in found["matches"])


@pytest.mark.asyncio
async def test_enqueue_coalesces_rerun(clone_ctx, monkeypatch):
    """Git ops during a background build coalesce into one extra pass."""
    tool, ctx, workspace, _ = clone_ctx
    monkeypatch.setattr(_builder_mod, "AUTO_BUILD_MAX_FILES", 1)

    release = asyncio.Event()
    calls: list[int] = []
    real_build = _builder_mod.build_repo_graph

    async def counting_build(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            await release.wait()  # only the first pass blocks
        return await real_build(*args, **kwargs)

    monkeypatch.setattr(_builder_mod, "build_repo_graph", counting_build)

    result = await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)
    assert result["graph"]["status"] == "queued"
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls == [1], "background worker never reached the first build"

    # two more auto-build triggers while the first pass is in flight →
    # one live task, one rerun flag (not two extra builds)
    repo_path = Path(workspace) / "repos" / "repo"
    for _ in range(2):
        summary = await _builder_mod.maybe_build_auto(repo_path, str(workspace), "repo")
        assert summary["status"] == "queued"
    assert len(_builder_mod._AUTO_BUILDS) == 1

    release.set()
    await _builder_mod.wait_pending_auto_builds()
    assert len(calls) == 2, f"expected first pass + one coalesced rerun, got {calls}"
    assert not _builder_mod._AUTO_BUILDS
    kg = Path(workspace) / ".tmp" / "repo_graph" / "repo"
    assert (kg / "graph.json").is_file()


@pytest.mark.asyncio
async def test_graph_failure_never_breaks_git_op(clone_ctx, monkeypatch):
    tool, ctx, _, _ = clone_ctx

    async def _boom(repo, project_fs_path, repo_name, force=False):
        raise RuntimeError("graph broke")

    monkeypatch.setattr(_git_repo_mod, "maybe_build_auto", _boom)
    result = await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)
    assert result["status"] == "cloned"
    assert "graph" not in result


@pytest.mark.asyncio
async def test_manual_rebuild_after_filesystem_edits(clone_ctx, grammars):
    tool, ctx, workspace, _ = clone_ctx
    await tool.execute({"operation": "clone", "repository": "org/repo"}, ctx)

    checkout = workspace / "repos" / "repo"
    (checkout / "new_file.py").write_text("def fresh():\n    return 4\n", encoding="utf-8")
    _run("add", "new_file.py", cwd=checkout)  # only tracked files are indexed

    graph_tool = RepoGraphTool()
    built = await graph_tool.execute(
        {"operation": "build", "repository": "repo"}, ctx)
    assert built["status"] == "built"
    assert built["stats"]["files"] == 3

    found = await graph_tool.execute(
        {"operation": "query", "repository": "repo", "query": "fresh"}, ctx)
    assert any(m["name"] == "fresh" for m in found["matches"])
