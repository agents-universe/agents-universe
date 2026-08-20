"""Shared repo-path resolution for the git_repo and repo_graph tools.

git_repo keeps its exact behavior; repo_graph reuses the same validation so
both tools agree on what a checkout is and where it lives (and both block
the same traversal attempts).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Same name constraints as before: clone targets and checkout names.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def extract_repo_name(repository: str) -> str:
    name = repository.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def repos_dir(project_fs_path: str) -> Path:
    """The workspace checkout root: ``{project}/repos``."""
    return Path(project_fs_path).resolve() / "repos"


def list_clones(project_fs_path: str) -> list[str]:
    """Names of checkouts in the workspace (directories with a .git entry)."""
    directory = repos_dir(project_fs_path)
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and (entry / ".git").exists()
    )


def resolve_repo_path(
    params: dict[str, Any],
    project_fs_path: str,
    *,
    available: list[str] | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve 'repository' or 'repository_path' to an absolute checkout path.

    Returns (path, None) on success, (None, error) otherwise. When neither
    parameter is given and ``available`` holds exactly one clone, that clone
    is used (the model usually forgets to name the repo); otherwise the error
    lists the options. ``available=None`` skips the fallback entirely.
    """
    repository = params.get("repository")
    repository_path = params.get("repository_path")
    if bool(repository) == bool(repository_path):
        if repository_path:
            return None, {
                "error": "Exactly one of 'repository' or 'repository_path' is required"
            }
        if available is not None and len(available) == 1:
            return repos_dir(project_fs_path) / available[0], None
        error: dict[str, Any] = {
            "error": "Exactly one of 'repository' or 'repository_path' is required"
        }
        error["available_repos"] = available or []
        if available:
            error["hint"] = (
                "Retry with 'repository' set to one of: "
                + ", ".join(available) + "."
            )
        else:
            error["hint"] = (
                "Clone a repository first, or call list_repos to enumerate clones."
            )
        return None, error

    base = Path(project_fs_path).resolve()
    if repository_path:
        candidate = str(repository_path).replace("\\", "/")
        raw = Path(candidate)
        if raw.is_absolute() or candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
            return None, {"error": "'repository_path' must be a project-relative path"}
        resolved = (base / raw).resolve()
        if not resolved.is_relative_to(base):
            return None, {"error": "Path traversal blocked"}
        return resolved, None

    name = extract_repo_name(str(repository))
    # "." / ".." match the name regex but resolve to the workspace root
    # (repos/.. == the project itself) — if the workspace is a git repo,
    # every operation would silently target it instead of a cloned repo.
    if name in (".", "..") or not _REPO_NAME_RE.fullmatch(name):
        return None, {"error": f"Invalid repository name: {name!r}"}
    return (base / "repos" / name).resolve(), None
