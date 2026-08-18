"""Unit tests for GitRepoTool — uses only local bare remote, no network."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_core.tools.git_repo as _git_repo_mod
from agent_core.tools.git_repo import GitRepoTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# Patch get_token_optional in git_repo's namespace so all tests use local no-auth repos.
@pytest.fixture(autouse=True)
def no_git_token(monkeypatch):
    async def _no_token(context, service_key):
        return None
    monkeypatch.setattr(_git_repo_mod, "get_token_optional", _no_token)


def _make_bare_remote(tmp_path: Path) -> Path:
    """Create a bare remote with an initial commit on main."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _run("init", "--bare", "--initial-branch=main", str(bare))
    # Make a working clone, commit, push
    work = tmp_path / "seed"
    _run("clone", str(bare), str(work))
    _run("config", "user.email", "test@example.com", cwd=work)
    _run("config", "user.name", "Tester", cwd=work)
    (work / "README.md").write_text("hello")
    _run("add", ".", cwd=work)
    _run("commit", "-m", "initial", cwd=work)
    _run("push", "origin", "main", cwd=work)
    return bare


def _clone_from(bare: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    _run("clone", str(bare), str(dest))
    _run("config", "user.email", "test@example.com", cwd=dest)
    _run("config", "user.name", "Tester", cwd=dest)
    return dest


# ---------------------------------------------------------------------------
# Path resolution / security
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_rejects_path_traversal(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "status", "repository_path": "../outer"},
        ctx,
    )
    assert "error" in result
    assert "traversal" in result["error"].lower() or "blocked" in result["error"].lower()


@pytest.mark.asyncio
async def test_status_rejects_absolute_path(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "status", "repository_path": "/tmp/something"},
        ctx,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_status_rejects_non_git_directory(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    notgit = workspace / "notgit"
    notgit.mkdir()
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "status", "repository_path": "notgit"},
        ctx,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_both_repository_and_repository_path_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {
            "operation": "status",
            "repository": "owner/repo",
            "repository_path": "repos/repo",
        },
        ctx,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_status_without_repository_lists_clones(tmp_path):
    workspace = tmp_path / "ws"
    bare = _make_bare_remote(tmp_path)
    _clone_from(bare, workspace / "repos" / "alpha")
    _clone_from(bare, workspace / "repos" / "beta")
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute({"operation": "status"}, ctx)
    assert "error" in result
    assert result["available_repos"] == ["alpha", "beta"]
    assert "alpha" in result["hint"] and "beta" in result["hint"]


@pytest.mark.asyncio
async def test_status_without_repository_uses_single_clone(tmp_path):
    workspace = tmp_path / "ws"
    bare = _make_bare_remote(tmp_path)
    _clone_from(bare, workspace / "repos" / "solo")
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute({"operation": "status"}, ctx)
    assert "error" not in result
    assert result["path"] == "repos/solo"


@pytest.mark.asyncio
async def test_status_without_repository_no_clones(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute({"operation": "status"}, ctx)
    assert "error" in result
    assert result["available_repos"] == []
    assert "list_repos" in result["hint"]


@pytest.mark.asyncio
async def test_clone_rejects_repository_path(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "clone", "repository_path": "repos/repo"},
        ctx,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# status — clean / dirty / staged / untracked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_clean(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "status", "repository_path": "repos/repo"},
        ctx,
    )
    assert result["clean"] is True
    assert result["dirty"] is False
    assert result["staged"] == []
    assert result["unstaged"] == []
    assert result["untracked"] == []
    assert result["branch"] == "main"


@pytest.mark.asyncio
async def test_status_dirty_modified(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    (checkout / "README.md").write_text("modified")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "status", "repository_path": "repos/repo"},
        ctx,
    )
    assert result["dirty"] is True
    assert "README.md" in result["unstaged"]


@pytest.mark.asyncio
async def test_status_staged_file(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    (checkout / "README.md").write_text("modified")
    _run("add", "README.md", cwd=checkout)
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "status", "repository_path": "repos/repo"},
        ctx,
    )
    assert result["dirty"] is True
    assert "README.md" in result["staged"]


@pytest.mark.asyncio
async def test_status_untracked_file(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    (checkout / "newfile.txt").write_text("new")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "status", "repository_path": "repos/repo"},
        ctx,
    )
    assert result["dirty"] is True
    assert "newfile.txt" in result["untracked"]


# ---------------------------------------------------------------------------
# pull — dirty gate and --ff-only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pull_blocked_on_dirty_tree(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    (checkout / "README.md").write_text("modified")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "pull", "repository_path": "repos/repo"},
        ctx,
    )
    assert "error" in result
    assert "dirty" in result.get("error", "").lower() or result.get("status") == "dirty"


@pytest.mark.asyncio
async def test_pull_returns_before_and_after_sha(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")

    # Push a new commit from another clone
    other = _clone_from(bare, tmp_path / "other")
    (other / "extra.txt").write_text("from other")
    _run("add", ".", cwd=other)
    _run("commit", "-m", "second", cwd=other)
    _run("push", "origin", "main", cwd=other)

    workspace = tmp_path / "ws"
    before_sha = _run("rev-parse", "HEAD", cwd=checkout)
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "pull", "repository_path": "repos/repo"},
        ctx,
    )
    assert result.get("status") == "updated"
    assert result["before_sha"] == before_sha
    assert result["after_sha"] != before_sha


# ---------------------------------------------------------------------------
# branch_prepare
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_prepare_blocked_on_dirty_tree(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    (checkout / "README.md").write_text("dirty")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "branch_prepare", "repository_path": "repos/repo", "branch": "feature/ABC-123"},
        ctx,
    )
    assert "error" in result
    assert "dirty" in result.get("error", "").lower() or result.get("status") in {"dirty", "main_update_failed"}


@pytest.mark.asyncio
async def test_branch_prepare_creates_feature_branch(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "branch_prepare", "repository_path": "repos/repo", "branch": "feature/ABC-123"},
        ctx,
    )
    assert result.get("status") == "prepared"
    assert result["branch"] == "feature/ABC-123"
    current = _run("symbolic-ref", "--short", "HEAD", cwd=checkout)
    assert current == "feature/ABC-123"


@pytest.mark.asyncio
async def test_branch_prepare_reuses_existing_local_branch(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    # Create the branch locally first
    _run("checkout", "-b", "feature/ABC-456", cwd=checkout)
    _run("checkout", "main", cwd=checkout)

    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "branch_prepare", "repository_path": "repos/repo", "branch": "feature/ABC-456"},
        ctx,
    )
    assert result.get("status") == "prepared"
    assert result["action"] in {"reused_local", "tracked_remote"}


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkout_switches_to_existing_branch(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    _run("checkout", "-b", "feature/ABC-789", cwd=checkout)
    _run("checkout", "main", cwd=checkout)
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "checkout", "repository_path": "repos/repo", "branch": "feature/ABC-789"},
        ctx,
    )
    assert result.get("status") == "checked_out"
    assert result["branch"] == "feature/ABC-789"
    assert result["path"] == "repos/repo"
    assert result["head"]
    assert _run("symbolic-ref", "--short", "HEAD", cwd=checkout) == "feature/ABC-789"


@pytest.mark.asyncio
async def test_checkout_blocks_on_dirty_tree(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    (checkout / "README.md").write_text("dirty")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "checkout", "repository_path": "repos/repo", "branch": "main"},
        ctx,
    )
    assert "error" in result
    assert "dirty" in result.get("error", "").lower() or result.get("status") == "dirty"


@pytest.mark.asyncio
async def test_checkout_requires_branch(tmp_path):
    bare = _make_bare_remote(tmp_path)
    _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "checkout", "repository_path": "repos/repo"},
        ctx,
    )
    assert "error" in result
    assert "branch" in result["error"].lower()


@pytest.mark.asyncio
async def test_checkout_unknown_branch_fails(tmp_path):
    bare = _make_bare_remote(tmp_path)
    _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "checkout", "repository_path": "repos/repo", "branch": "no-such-branch"},
        ctx,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# commit — exact paths, no git add -A
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_exact_paths_only(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")

    (checkout / "target.py").write_text("print('hello')")
    (checkout / "unrelated.txt").write_text("should not be committed")
    _run("add", "target.py", "unrelated.txt", cwd=checkout)  # both staged
    # But we only commit target.py
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {
            "operation": "commit",
            "repository_path": "repos/repo",
            "paths": ["target.py"],
            "message": "add target",
        },
        ctx,
    )
    # Should reject because unrelated.txt is already staged
    assert "error" in result
    assert "unexpected" in result.get("error", "").lower() or "staged" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_commit_clean_exact_paths_succeeds(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")

    (checkout / "target.py").write_text("print('hello')")
    (checkout / "unrelated.txt").write_text("not staged")
    # Only target.py is unstaged; unrelated.txt is just untracked, not staged
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {
            "operation": "commit",
            "repository_path": "repos/repo",
            "paths": ["target.py"],
            "message": "add target",
        },
        ctx,
    )
    assert result.get("status") == "committed"
    assert "target.py" in result["files"]
    assert "unrelated.txt" not in result["files"]
    # unrelated.txt should still be untracked
    porcelain = _run("status", "--porcelain", cwd=checkout)
    assert "unrelated.txt" in porcelain


@pytest.mark.asyncio
async def test_commit_requires_paths(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {
            "operation": "commit",
            "repository_path": "repos/repo",
            "message": "no paths",
        },
        ctx,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_commit_rejects_path_traversal(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {
            "operation": "commit",
            "repository_path": "repos/repo",
            "paths": ["../../../etc/passwd"],
            "message": "bad",
        },
        ctx,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# push — no force
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_push_succeeds_on_clean_fast_forward(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    _run("checkout", "-b", "feature/PUSH-1", cwd=checkout)
    (checkout / "newfile.py").write_text("x = 1")
    _run("add", "newfile.py", cwd=checkout)
    _run("commit", "-m", "add file", cwd=checkout)

    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "push", "repository_path": "repos/repo", "branch": "feature/PUSH-1"},
        ctx,
    )
    assert result.get("status") == "pushed"


@pytest.mark.asyncio
async def test_push_non_fast_forward_returns_sync_error(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout_a = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    checkout_b = _clone_from(bare, tmp_path / "other")

    # Both branch from main
    _run("checkout", "-b", "feature/PUSH-2", cwd=checkout_a)
    _run("checkout", "-b", "feature/PUSH-2", cwd=checkout_b)

    # B commits and pushes first
    (checkout_b / "b.txt").write_text("b")
    _run("add", "b.txt", cwd=checkout_b)
    _run("commit", "-m", "from B", cwd=checkout_b)
    _run("push", "origin", "feature/PUSH-2", cwd=checkout_b)

    # A commits something different
    (checkout_a / "a.txt").write_text("a")
    _run("add", "a.txt", cwd=checkout_a)
    _run("commit", "-m", "from A", cwd=checkout_a)

    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {"operation": "push", "repository_path": "repos/repo", "branch": "feature/PUSH-2"},
        ctx,
    )
    assert result.get("status") == "rejected"
    assert "sync" in result.get("message", "").lower() or "non-fast-forward" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_push_with_target_repository_pushes_to_fork_url(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")
    _run("checkout", "-b", "feature/PUSH-3", cwd=checkout)
    (checkout / "f.txt").write_text("fork me")
    _run("add", "f.txt", cwd=checkout)
    _run("commit", "-m", "add file", cwd=checkout)

    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    ctx.cfg.return_value = "https://ghe.example.com"  # GIT_BASE_URL for fork target

    # Intercept only the real network push; let every other git command run.
    real_run = tool._run_git
    captured: list[list[str]] = []

    async def fake_run(args, cwd, timeout=None, token=None):
        if args[0] == "push":
            captured.append(list(args))
            return {"stdout": "", "stderr": "", "exit_code": 0}
        return await real_run(args, cwd, timeout, token)

    tool._run_git = fake_run  # type: ignore[method-assign]

    result = await tool.execute(
        {
            "operation": "push",
            "repository_path": "repos/repo",
            "branch": "feature/PUSH-3",
            "target_repository": "mybot/repo",
        },
        ctx,
    )

    assert result.get("status") == "pushed"
    assert captured == [
        ["push", "https://ghe.example.com/mybot/repo.git", "feature/PUSH-3:feature/PUSH-3"]
    ]


@pytest.mark.asyncio
async def test_push_with_target_repository_requires_git_base_url(tmp_path):
    bare = _make_bare_remote(tmp_path)
    _clone_from(bare, tmp_path / "ws" / "repos" / "repo")

    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))  # cfg returns "" — no GIT_BASE_URL

    result = await tool.execute(
        {
            "operation": "push",
            "repository_path": "repos/repo",
            "branch": "feature/PUSH-4",
            "target_repository": "mybot/repo",
        },
        ctx,
    )
    assert "error" in result
    assert "GIT_BASE_URL" in result["error"]


def test_no_force_push_in_schema():
    """Verify force_with_lease is not exposed in the tool schema."""
    tool = GitRepoTool()
    schema_str = str(tool.parameters)
    assert "force_with_lease" not in schema_str
    assert "force" not in schema_str


# ---------------------------------------------------------------------------
# sync_branch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_branch_merges_new_main_commit(tmp_path):
    bare = _make_bare_remote(tmp_path)
    checkout = _clone_from(bare, tmp_path / "ws" / "repos" / "repo")

    # Create feature branch
    _run("checkout", "-b", "feature/SYNC-1", cwd=checkout)
    (checkout / "feature.txt").write_text("feat")
    _run("add", "feature.txt", cwd=checkout)
    _run("commit", "-m", "feat commit", cwd=checkout)
    _run("push", "origin", "feature/SYNC-1", cwd=checkout)

    # Push new commit to main from another clone
    other = _clone_from(bare, tmp_path / "other")
    (other / "main_update.txt").write_text("main update")
    _run("add", "main_update.txt", cwd=other)
    _run("commit", "-m", "main update", cwd=other)
    _run("push", "origin", "main", cwd=other)

    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))
    result = await tool.execute(
        {
            "operation": "sync_branch",
            "repository_path": "repos/repo",
            "branch": "feature/SYNC-1",
        },
        ctx,
    )
    assert result.get("status") == "synced"
    assert "origin/main" in result.get("merged", [])
    # main_update.txt should now be present in checkout
    assert (checkout / "main_update.txt").exists()


@pytest.mark.asyncio
async def test_push_rejects_invalid_target_repository(tmp_path):
    """target_repository must be owner/name — anything else would point the
    project token's credentials at an arbitrary repo on the host (the same
    shape check clone enforces). No push may run."""
    bare = _make_bare_remote(tmp_path)
    _clone_from(bare, tmp_path / "ws" / "repos" / "repo")

    workspace = tmp_path / "ws"
    tool = GitRepoTool()
    ctx = _make_context(str(workspace))

    for bad in ("org/repo with space", "user@host/repo", "/abs/path", "org//repo", "org/repo/"):
        result = await tool.execute(
            {
                "operation": "push",
                "repository_path": "repos/repo",
                "branch": "feature/INV-1",
                "target_repository": bad,
            },
            ctx,
        )
        assert "error" in result and "Invalid target_repository" in result["error"], bad
