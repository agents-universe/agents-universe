"""Git repository operations scoped to a project's workspace."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ._auth import get_token_optional
from ._repo_paths import _REPO_PATH_RE, extract_repo_name, list_clones, repos_dir, resolve_repo_path
from .base import Tool, ToolContext
from ..knowledge.graph.builder import maybe_build_auto
from ..knowledge.graph.model import repo_graph_dir
from ..knowledge.graph.store import invalidate_cached

_log = logging.getLogger(__name__)
_TIMEOUT_CLONE = 300
_TIMEOUT_PULL = 120
_TIMEOUT_DEFAULT = 30
_MAX_OUTPUT = 20_000
# git refnames cannot start with '-' (or '.') — a leading '-' would be
# parsed by git as an OPTION, turning e.g. `checkout -f` / `merge --abort`
# into destructive commands on the wrong target.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9._~/@^:-]+$")
# git log options may not carry file-writing flags. git log accepts
# --output=<file> / -o <file> (and -o can combine: "-on" = -o -n) — with the
# agent's params flowing straight into argv, a crafted "options" value could
# truncate/write files OUTSIDE the repo (absolute paths allowed). Only
# printable-safe short/long options, no -o/--output, no non-option tokens.
_LOG_OPTION_RE = re.compile(r"^(?:-[A-Za-z0-9]+|--[a-z][a-z0-9-]*)(?:=[A-Za-z0-9.,_:/+@%^~-]*)?$")
_UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def _valid_log_options(tokens: list[str]) -> str | None:
    """Validate git log option tokens; return an error message or None."""
    for tok in tokens:
        if not tok.startswith("-"):
            return f"options must be git flags, got {tok!r}"
        if not _LOG_OPTION_RE.match(tok):
            return f"unsupported git log option: {tok!r}"
        if tok == "-o" or (tok.startswith("-o") and not tok.startswith("--")):
            return "option '-o' (--output) is not allowed"
        if tok.startswith("--output"):
            return "option '--output' is not allowed"
    return None


def _find_git() -> str | None:
    if found := shutil.which("git"):
        return found
    if sys.platform != "win32":
        return None
    roots = {
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LocalAppData"),
    }
    for root in roots - {None}:
        for relative in ("Git/cmd/git.exe", "Git/bin/git.exe"):
            candidate = Path(root) / relative
            if candidate.is_file():
                return str(candidate)
    return None


_GIT_BIN = _find_git()


def _dependency_missing(message: str) -> dict[str, Any]:
    return {"error": "dependency_missing", "dependency": "git", "message": message}


def _sanitize_output(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token and text else text


def _rmtree_force(path: Path) -> None:
    """Remove a directory tree, clearing read-only attributes first on win32.

    git loose objects are created with the read-only attribute on Windows, and
    shutil.rmtree then fails with WinError 5 on them (the clone rollback masks
    this with ignore_errors). Clear the attribute everywhere first so a genuine
    failure (e.g. an antivirus file lock) still surfaces as remove_failed.
    """
    if sys.platform == "win32":
        for root, _dirs, files in os.walk(path):
            for name in files + _dirs:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWRITE)
                except OSError:
                    pass
    shutil.rmtree(path)


def _safe_git_env() -> dict[str, str]:
    """os.environ with credential-like keys stripped for the git subprocess.

    Git hooks (post-commit, pre-push, ...) execute with this environment, so
    DB passwords / API keys in the server env must not reach them. Mirrors
    ToolContext.safe_env() filtering; GIT_ASKPASS_TOKEN is re-added by the
    caller after filtering.
    """
    deny_suffixes = ToolContext._ENV_DENY_SUFFIXES
    deny_prefixes = ToolContext._ENV_DENY_PREFIXES
    deny_exact = ToolContext._ENV_DENY_EXACT
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in deny_exact:
            continue
        if upper.rsplit("_", 1)[-1] in deny_suffixes:
            continue
        if any(upper.startswith(p) for p in deny_prefixes):
            continue
        env[key] = value
    return env


class GitRepoTool(Tool):
    name = "git_repo"
    prompt_hint = (
        "Clone and operate workspace-scoped git repositories with authentication "
        "injected automatically. Prefer it over raw shell git for cloned external "
        "repos; force push is not supported. For Jira-card or PR tasks, read the "
        "authoritative source first (`jira`/`github`); this tool is a supplement, "
        "and the primary tool for implementation work. Clone/checkout/pull results "
        "carry a compact code map (`graph`); consult the repo_graph tool before "
        "reading repository files."
    )
    description = (
        "Manage workspace-scoped git repositories. Operations: clone, checkout, pull, status, "
        "search, log, show, blame, list_repos, remove_clone, unshallow, branch_create, "
        "branch_prepare, sync_branch, commit, push. Authentication is injected automatically; "
        "force push is not supported. Every operation except list_repos requires exactly one of "
        "'repository' or 'repository_path'. remove_clone permanently deletes a cloned repository "
        "(and its cached code graph) after the user confirms."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "clone", "checkout", "pull", "status", "search", "log", "show",
                    "blame", "list_repos", "remove_clone", "unshallow", "branch_create",
                    "branch_prepare", "sync_branch", "commit", "push",
                ],
            },
            "repository": {
                "type": "string",
                "description": (
                    "Remote owner/repo or a clone name; exclusive with repository_path. "
                    "Required for every operation except list_repos."
                ),
            },
            "repository_path": {
                "type": "string",
                "description": (
                    "Project-relative existing checkout; exclusive with repository. "
                    "Required for every operation except list_repos."
                ),
            },
            "branch": {"type": "string"},
            "base": {"type": "string", "description": "Base branch for legacy branch_create."},
            "target_repository": {
                "type": "string",
                "description": (
                    "For push only: owner/repo of an alternative target (e.g. your fork "
                    "'<account>/<repo>') to push the branch to, instead of origin. "
                    "The URL is built from the configured GIT_BASE_URL; never pass a raw URL."
                ),
            },
            "message": {"type": "string"},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact repository-relative paths to stage for commit.",
            },
            "query": {"type": "string"},
            "path": {"type": "string"},
            "ref": {"type": "string"},
            "options": {"type": "string"},
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        handlers = {
            "clone": self._op_clone,
            "checkout": self._op_checkout,
            "pull": self._op_pull,
            "status": self._op_status,
            "search": self._op_search,
            "log": self._op_log,
            "show": self._op_show,
            "blame": self._op_blame,
            "list_repos": self._op_list_repos,
            "remove_clone": self._op_remove_clone,
            "unshallow": self._op_unshallow,
            "branch_create": self._op_branch_create,
            "branch_prepare": self._op_branch_prepare,
            "sync_branch": self._op_sync_branch,
            "commit": self._op_commit,
            "push": self._op_push,
        }
        handler = handlers.get(params.get("operation"))
        if not handler:
            return {"error": f"Unknown operation: {params.get('operation')}"}
        return await handler(params, context)

    def _repos_dir(self, context: ToolContext) -> Path:
        return repos_dir(context.project_fs_path)

    def _cloned_repo_names(self, context: ToolContext) -> list[str]:
        return list_clones(context.project_fs_path)

    def _resolve_repo_path(
        self, params: dict[str, Any], context: ToolContext
    ) -> tuple[Path | None, dict[str, Any] | None]:
        return resolve_repo_path(
            params, context.project_fs_path,
            available=self._cloned_repo_names(context),
        )

    @staticmethod
    def _display(path: Path, context: ToolContext) -> str:
        relative = path.resolve().relative_to(Path(context.project_fs_path).resolve()).as_posix()
        return relative or "."

    async def _attach_graph(
        self, repo: Path, context: ToolContext, result: dict[str, Any]
    ) -> None:
        """Best-effort graph build after a mutation; never affects the git result.

        Skips repos over the auto-build limit (with a hint), and swallows any
        exception — a failed graph must not fail a successful git operation.
        """
        try:
            summary = await maybe_build_auto(repo, context.project_fs_path, repo.name)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("repo graph auto-build skipped for %s: %s", repo.name, exc)
            return
        if summary is not None:
            result["graph"] = summary

    async def _require_repo(
        self, params: dict[str, Any], context: ToolContext
    ) -> tuple[Path | None, dict[str, Any] | None]:
        path, error = self._resolve_repo_path(params, context)
        if error:
            return None, error
        if not path.is_dir():
            return None, {"error": f"Repository not found at {self._display(path, context)}. Clone it first."}
        check = await self._run_git(["rev-parse", "--is-inside-work-tree"], path)
        if "error" in check or check.get("stdout", "").strip() != "true":
            return None, {"error": f"Path {self._display(path, context)} is not a Git working tree."}
        return path, None

    async def _run_git(
        self,
        args: list[str],
        cwd: str | Path,
        timeout: int = _TIMEOUT_DEFAULT,
        token: str | None = None,
    ) -> dict[str, Any]:
        if not _GIT_BIN:
            return _dependency_missing("Git executable was not found")
        env = _safe_git_env()
        askpass: Path | None = None
        if token:
            suffix = ".cmd" if sys.platform == "win32" else ".sh"
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=suffix, prefix="git-askpass-", delete=False,
                    encoding="utf-8", newline="",
                ) as helper:
                    askpass = Path(helper.name)
                    if sys.platform == "win32":
                        helper.write(
                            '@echo off\r\necho %~1 | findstr /I "password" >nul\r\n'
                            'if not errorlevel 1 (echo %GIT_ASKPASS_TOKEN%) else (echo oauth2)\r\n'
                        )
                    else:
                        helper.write(
                            '#!/bin/sh\ncase "$1" in\n'
                            '  *[Pp]assword*) printf \'%s\\n\' "$GIT_ASKPASS_TOKEN" ;;\n'
                            '  *) printf \'oauth2\\n\' ;;\nesac\n'
                        )
                if sys.platform != "win32":
                    askpass.chmod(0o700)
            except OSError as exc:
                return _dependency_missing(f"Could not create Git credential helper: {exc}")
            env.update(
                GIT_ASKPASS=str(askpass),
                GIT_ASKPASS_TOKEN=token,
                GIT_TERMINAL_PROMPT="0",
            )
        # process is pre-declared: a cancellation landing between the await of
        # create_subprocess_exec and its assignment would otherwise hit a
        # NameError in the CancelledError handler below (same pattern as
        # code_executor.py / shell.py).
        process: asyncio.subprocess.Process | None = None
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    _GIT_BIN, *args, cwd=str(cwd), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                if process is not None:
                    process.kill()
                    await process.wait()
                return {"error": f"Git command timed out after {timeout}s"}
            except asyncio.CancelledError:
                if process is not None:
                    process.kill()
                    await process.wait()
                raise
            except OSError as exc:
                return _dependency_missing(f"Git could not be started: {exc}")
            out = _sanitize_output(stdout.decode(errors="replace"), token)[:_MAX_OUTPUT]
            err = _sanitize_output(stderr.decode(errors="replace"), token)[:_MAX_OUTPUT]
            if process.returncode:
                _log.warning("git %s failed: %s", args[0] if args else "?", err[:500])
                return {
                    "error": f"Git exited with code {process.returncode}",
                    "exit_code": process.returncode,
                    "stdout": out,
                    "stderr": err,
                }
            return {"stdout": out, "stderr": err, "exit_code": 0}
        finally:
            if askpass:
                try:
                    askpass.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _is_clean(self, repo: Path) -> tuple[bool, dict[str, Any] | None]:
        result = await self._run_git(["status", "--porcelain=v1", "--untracked-files=all"], repo)
        if "error" in result:
            return False, result
        return not result.get("stdout", "").strip(), None

    async def _ref_exists(self, repo: Path, ref: str) -> bool:
        result = await self._run_git(["show-ref", "--verify", "--quiet", ref], repo)
        return "error" not in result

    async def _ahead_behind(self, repo: Path, left: str, right: str) -> dict[str, int] | None:
        result = await self._run_git(["rev-list", "--left-right", "--count", f"{left}...{right}"], repo)
        if "error" in result:
            return None
        try:
            ahead, behind = (int(value) for value in result["stdout"].split())
        except (KeyError, TypeError, ValueError):
            return None
        return {"ahead": ahead, "behind": behind}

    @staticmethod
    def _porcelain_path(line: str) -> str:
        path = line[3:]
        return path.split(" -> ", 1)[-1]

    async def _get_token_and_url(
        self, params: dict[str, Any], context: ToolContext
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        token = await get_token_optional(context, "git")
        base = context.cfg("GIT_BASE_URL", "")
        if not token:
            return None, None, {"error": "Git token not configured"}
        if not base:
            return None, None, {"error": "GIT_BASE_URL not configured"}
        repository = params.get("repository", "")
        if not repository:
            return None, None, {"error": "Parameter 'repository' is required for clone"}
        host = base.rstrip("/").removeprefix("https://").removeprefix("http://")
        return token, f"https://{host}/{repository}.git", None

    async def _op_clone(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        if params.get("repository_path"):
            return {"error": "clone does not accept repository_path"}
        repository = params.get("repository", "")
        if not repository or not _REPO_PATH_RE.fullmatch(repository):
            return {"error": "A valid repository (org/name or name) is required for clone"}
        name = extract_repo_name(repository)
        if name in (".", ".."):
            return {"error": f"Invalid repository name: {name!r}"}
        destination = (self._repos_dir(context) / name).resolve()
        base = Path(context.project_fs_path).resolve()
        if not destination.is_relative_to(base):
            return {"error": "Path traversal blocked"}
        if destination.is_dir() and await self._ref_exists(destination, "HEAD"):
            return {"status": "already_exists", "path": self._display(destination, context)}
        if destination.exists():
            return {"error": f"Clone destination already exists: {self._display(destination, context)}"}

        token, url, error = await self._get_token_and_url(params, context)
        if error:
            return error
        self._repos_dir(context).mkdir(parents=True, exist_ok=True)
        command = ["clone", "--depth", "1", "--no-single-branch"]
        branch = params.get("branch")
        if branch is not None:
            # _valid_branch rejects empty / traversal / option-like values —
            # every other git op validates it; clone was the only exception
            # (a malformed value would land in argv as --branch payload).
            if not isinstance(branch, str) or not self._valid_branch(branch):
                return {"error": f"Invalid branch name: {branch!r}"}
            command += ["--branch", branch]
        result = await self._run_git(
            command + [url, str(destination)], self._repos_dir(context), _TIMEOUT_CLONE, token
        )
        if "error" in result:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            return result
        result = {
            "status": "cloned",
            "repository": repository,
            "path": self._display(destination, context),
            "shallow": True,
        }
        await self._attach_graph(destination, context, result)
        return result

    async def _op_pull(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        clean, status_error = await self._is_clean(repo)
        if status_error:
            return status_error
        if not clean:
            return {"error": "Pull blocked: working tree is dirty", "status": "dirty"}
        before = await self._run_git(["rev-parse", "HEAD"], repo)
        if "error" in before:
            return before
        result = await self._run_git(
            ["pull", "--ff-only"], repo, _TIMEOUT_PULL, await get_token_optional(context, "git")
        )
        if "error" in result:
            return result
        after = await self._run_git(["rev-parse", "HEAD"], repo)
        if "error" in after:
            return after
        result = {
            "status": "updated",
            "path": self._display(repo, context),
            "before_sha": before["stdout"].strip(),
            "after_sha": after["stdout"].strip(),
            "output": result["stdout"],
        }
        if before["stdout"].strip() != after["stdout"].strip():
            await self._attach_graph(repo, context, result)
        return result

    async def _op_checkout(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        branch = params.get("branch", "")
        if not self._valid_branch(branch):
            return {"error": "Parameter 'branch' is required for checkout"}
        clean, status_error = await self._is_clean(repo)
        if status_error:
            return status_error
        if not clean:
            return {"error": "Checkout blocked: working tree is dirty", "status": "dirty"}
        result = await self._run_git(["checkout", branch], repo)
        if "error" in result:
            return result
        head = await self._run_git(["rev-parse", "HEAD"], repo)
        result = {
            "status": "checked_out",
            "branch": branch,
            "head": head.get("stdout", "").strip() if "error" not in head else "",
            "path": self._display(repo, context),
        }
        await self._attach_graph(repo, context, result)
        return result

    async def _op_status(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        porcelain = await self._run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"], repo
        )
        head = await self._run_git(["rev-parse", "HEAD"], repo)
        branch = await self._run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
        if "error" in porcelain or "error" in head:
            return {"error": "Could not read repository status"}
        lines = [line for line in porcelain["stdout"].splitlines() if line]
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        unmerged: list[str] = []
        for line in lines:
            code = line[:2]
            path = self._porcelain_path(line)
            if code == "??":
                untracked.append(path)
            elif code in _UNMERGED:
                unmerged.append(path)
            else:
                if code[0] != " ":
                    staged.append(path)
                if code[1] != " ":
                    unstaged.append(path)
        return {
            "path": self._display(repo, context),
            "clean": not lines,
            "dirty": bool(lines),
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "unmerged": unmerged,
            "head": head["stdout"].strip(),
            "branch": branch.get("stdout", "").strip() if "error" not in branch else "HEAD",
            "is_shallow": (repo / ".git" / "shallow").exists(),
        }

    async def _op_search(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        if not params.get("query"):
            return {"error": "Parameter 'query' is required for search"}
        command = ["grep", "-n", "--heading", "--", params["query"]]
        if params.get("path"):
            command.append(params["path"])
        return await self._run_git(command, repo)

    async def _op_log(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        command = ["log", "--oneline", "-30"]
        if params.get("options"):
            # options used to flow straight into argv — git log's
            # --output=/-o can write files outside the repo (SSRF-adjacent
            # server-side file write). Whitelist-validate every token.
            option_tokens = params["options"].split()
            option_error = _valid_log_options(option_tokens)
            if option_error:
                return {"error": f"Invalid git log options: {option_error}"}
            command += option_tokens
        if params.get("path"):
            command += ["--", params["path"]]
        return await self._run_git(command, repo)

    async def _op_show(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        ref = params.get("ref", "HEAD")
        # ref flowed into argv unvalidated — `git show --output=/x`
        # (no ref) still writes outside the repo. Restrict to ref-safe chars,
        # and a leading '-' is a flag, not a ref.
        if not _REF_RE.match(ref):
            return {"error": f"Invalid ref: {ref!r}"}
        target = f"{ref}:{params['path']}" if params.get("path") else ref
        return await self._run_git(["show", target], repo)

    async def _op_blame(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        if not params.get("path"):
            return {"error": "Parameter 'path' is required for blame"}
        # -- separates the path so a leading-dash path (or option-
        # injection value) can't be parsed as a git flag.
        return await self._run_git(["blame", "--", params["path"]], repo)

    async def _op_list_repos(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        directory = self._repos_dir(context)
        if not directory.exists():
            return {"repos": []}
        repos = []
        for path in sorted(directory.iterdir()):
            if path.is_dir():
                check = await self._run_git(["rev-parse", "--is-inside-work-tree"], path)
                if "error" not in check:
                    repos.append(path.name)
        return {"repos": repos}

    async def _op_remove_clone(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        # Same resolution as every other op, including the single-clone fallback
        # (auto-selecting the only clone). The confirm gate below is the backstop.
        path, error = self._resolve_repo_path(params, context)
        if error:
            return error

        repos_root = self._repos_dir(context)
        # Containment: the resolved path must be a DIRECT child of repos/.
        # resolve_repo_path blocks traversal and repository="."/"..", but
        # repository_path="." or "repos" resolves inside the workspace and would
        # pass is_relative_to — an rmtree there would destroy the project or every
        # clone at once.
        try:
            rel = path.relative_to(repos_root)
        except ValueError:
            return {"error": "remove_clone only supports clones directly under repos/"}
        if len(rel.parts) != 1:
            return {"error": "remove_clone only supports clones directly under repos/"}

        # Must look like a clone: a directory with a .git entry (same check
        # list_clones uses) — never rmtree an arbitrary directory.
        if not path.is_dir() or not (path / ".git").exists():
            return {"error": f"Not a cloned repository: {self._display(path, context)}"}

        if context.session is None:
            return {"error": "git_repo remove_clone requires an active conversation session."}

        # Cheap, best-effort dirty info for the question text (one git status).
        # On failure the note is skipped; never a hard gate — a dirty clone with
        # unpushed local commits is exactly what the confirm dialog protects.
        dirty_note = ""
        clean, status_error = await self._is_clean(path)
        if not status_error and not clean:
            dirty_note = "（工作区有未提交的改动）"

        try:
            result = await context.session.request_user_selection(
                prompt_id=str(uuid.uuid4()),
                field_key=f"remove_clone_{path.name}",
                question=f"确认删除克隆仓库 {path.name}？{dirty_note}",
                kind="selection",
                options=[
                    {"label": "确认删除", "value": "confirm"},
                    {"label": "取消", "value": "cancel"},
                ],
                allow_other=False,
                task_id=context.current_task_id,
                timeout=120.0,
            )
        except RuntimeError as exc:
            return {"error": str(exc)}

        repo_ref = params.get("repository") or params.get("repository_path") or path.name
        if result != "confirm":
            return {
                "status": "cancelled",
                "repository": repo_ref,
                "path": self._display(path, context),
            }

        # Remove the auto-built code-graph cache alongside the clone — a shell
        # `rm -rf` could never cover this; it is a selling point of routing
        # removal through git_repo. invalidate_cached drops the in-memory memo so
        # a same-named re-clone can never serve a stale graph; rmtree is a no-op
        # when the dir does not exist.
        kg_dir = repo_graph_dir(context.project_fs_path, path.name)
        invalidate_cached(kg_dir)
        shutil.rmtree(kg_dir, ignore_errors=True)

        # Precedent for sync rmtree on the event loop: _op_clone's failed-clone
        # rollback. _rmtree_force handles Windows read-only git objects; a real
        # failure (e.g. a file lock) surfaces as remove_failed.
        try:
            _rmtree_force(path)
        except OSError as exc:
            return {"error": f"Could not remove clone: {exc}", "status": "remove_failed"}

        return {
            "status": "removed",
            "repository": repo_ref,
            "path": self._display(path, context),
        }

    async def _op_unshallow(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        git_dir = await self._run_git(["rev-parse", "--git-dir"], repo)
        if "error" in git_dir:
            return git_dir
        shallow_file = (repo / git_dir["stdout"].strip() / "shallow").resolve()
        if not shallow_file.exists():
            return {"status": "already_full"}
        result = await self._run_git(
            ["fetch", "--unshallow"], repo, _TIMEOUT_CLONE,
            await get_token_optional(context, "git"),
        )
        return result if "error" in result else {
            "status": "unshallowed", "path": self._display(repo, context)
        }

    @staticmethod
    def _valid_branch(branch: str) -> bool:
        return bool(
            branch
            and _BRANCH_RE.fullmatch(branch)
            and not branch.startswith((".", "/"))
            and not branch.endswith((".", "/"))
            and ".." not in branch
            and "//" not in branch
        )

    async def _conflict_result(
        self, repo: Path, result: dict[str, Any], branch: str
    ) -> dict[str, Any]:
        status = await self._run_git(["status", "--porcelain=v1"], repo)
        files = [
            self._porcelain_path(line)
            for line in status.get("stdout", "").splitlines()
            if line[:2] in _UNMERGED
        ]
        return {
            "status": "conflict",
            "conflict": True,
            "branch": branch,
            "files": files,
            "error": result.get("stderr", "Merge conflict"),
        }

    async def _prepare_updated_main(
        self, repo: Path, context: ToolContext
    ) -> dict[str, Any] | None:
        clean, status_error = await self._is_clean(repo)
        if status_error:
            return status_error
        if not clean:
            return {"error": "Branch preparation blocked: working tree is dirty", "status": "dirty"}
        token = await get_token_optional(context, "git")
        commands = (
            (["fetch", "origin"], _TIMEOUT_PULL),
            (["checkout", "main"], _TIMEOUT_DEFAULT),
            (["pull", "--ff-only", "origin", "main"], _TIMEOUT_PULL),
        )
        for command, timeout in commands:
            result = await self._run_git(command, repo, timeout, token)
            if "error" in result:
                return {
                    **result,
                    "status": "main_update_failed",
                    "message": "Update main with a fast-forward before creating the feature branch.",
                }
        return None

    async def _op_branch_create(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Legacy branch creation with the same clean-tree safety guarantees."""
        if params.get("base", "main") == "main":
            return await self._op_branch_prepare(params, context)
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        branch = params.get("branch", "")
        base = params.get("base", "")
        if not self._valid_branch(branch) or not self._valid_branch(base):
            return {"error": "Valid branch and base names are required"}
        clean, status_error = await self._is_clean(repo)
        if status_error:
            return status_error
        if not clean:
            return {"error": "Branch creation blocked: working tree is dirty", "status": "dirty"}
        token = await get_token_optional(context, "git")
        for command, timeout in (
            (["fetch", "origin"], _TIMEOUT_PULL),
            (["checkout", base], _TIMEOUT_DEFAULT),
            (["pull", "--ff-only", "origin", base], _TIMEOUT_PULL),
            (["checkout", "-b", branch], _TIMEOUT_DEFAULT),
        ):
            result = await self._run_git(command, repo, timeout, token)
            if "error" in result:
                return result
        return {
            "status": "created", "branch": branch, "base": base,
            "path": self._display(repo, context),
        }

    async def _op_branch_prepare(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        feature = params.get("branch", "")
        if not self._valid_branch(feature) or feature in {"main", "master"}:
            return {"error": "A valid feature branch is required"}

        error = await self._prepare_updated_main(repo, context)
        if error:
            return error

        local_ref = f"refs/heads/{feature}"
        remote_ref = f"refs/remotes/origin/{feature}"
        local_exists = await self._ref_exists(repo, local_ref)
        remote_exists = await self._ref_exists(repo, remote_ref)
        if local_exists:
            checkout = await self._run_git(["checkout", feature], repo)
            action = "reused_local"
        elif remote_exists:
            checkout = await self._run_git(
                ["checkout", "--track", "-b", feature, f"origin/{feature}"], repo
            )
            action = "tracked_remote"
        else:
            checkout = await self._run_git(["checkout", "-b", feature, "main"], repo)
            action = "created"
        if "error" in checkout:
            return checkout

        if action != "created":
            merge = await self._run_git(["merge", "main"], repo, _TIMEOUT_PULL)
            if "error" in merge:
                return await self._conflict_result(repo, merge, feature)

        head = await self._run_git(["rev-parse", "HEAD"], repo)
        upstream = await self._run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], repo
        )
        result: dict[str, Any] = {
            "status": "prepared",
            "action": action,
            "branch": feature,
            "base": "main",
            "head": head.get("stdout", "").strip(),
            "upstream": upstream.get("stdout", "").strip() if "error" not in upstream else None,
            "path": self._display(repo, context),
            "relative_to_main": await self._ahead_behind(repo, feature, "main"),
        }
        if remote_exists:
            result["relative_to_remote_feature"] = await self._ahead_behind(
                repo, feature, f"origin/{feature}"
            )
        await self._attach_graph(repo, context, result)
        return result

    async def _op_sync_branch(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        feature = params.get("branch", "")
        if not self._valid_branch(feature) or feature in {"main", "master"}:
            return {"error": "A valid feature branch is required"}
        clean, status_error = await self._is_clean(repo)
        if status_error:
            return status_error
        if not clean:
            return {"error": "Branch synchronization blocked: working tree is dirty", "status": "dirty"}

        token = await get_token_optional(context, "git")
        fetch = await self._run_git(["fetch", "origin"], repo, _TIMEOUT_PULL, token)
        if "error" in fetch:
            return fetch
        checkout = await self._run_git(["checkout", feature], repo)
        if "error" in checkout:
            return checkout
        before = await self._run_git(["rev-parse", "HEAD"], repo)
        merged: list[str] = []
        for ref in ("origin/main", f"origin/{feature}"):
            full_ref = f"refs/remotes/{ref}"
            if not await self._ref_exists(repo, full_ref):
                if ref == "origin/main":
                    return {"error": "Remote main branch does not exist", "status": "sync_failed"}
                continue
            merge = await self._run_git(["merge", ref], repo, _TIMEOUT_PULL)
            if "error" in merge:
                return await self._conflict_result(repo, merge, feature)
            merged.append(ref)
        after = await self._run_git(["rev-parse", "HEAD"], repo)
        result = {
            "status": "synced",
            "branch": feature,
            "merged": merged,
            "before_sha": before.get("stdout", "").strip(),
            "after_sha": after.get("stdout", "").strip(),
            "relative_to_main": await self._ahead_behind(repo, feature, "origin/main"),
            "relative_to_remote_feature": (
                await self._ahead_behind(repo, feature, f"origin/{feature}")
                if f"origin/{feature}" in merged else None
            ),
            "path": self._display(repo, context),
        }
        await self._attach_graph(repo, context, result)
        return result

    def _validate_commit_paths(
        self, paths: Any
    ) -> tuple[list[str] | None, dict[str, Any] | None]:
        if not isinstance(paths, list) or not paths or any(
            not isinstance(path, str) or not path for path in paths
        ):
            return None, {"error": "'paths' is required for commit and must contain exact paths"}
        valid: list[str] = []
        for path in paths:
            normalized = path.replace("\\", "/").rstrip("/")
            raw = Path(normalized)
            if (
                raw.is_absolute()
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:", normalized)
                or normalized in {"", ".", ".."}
                or normalized.startswith("../")
                or "/../" in normalized
            ):
                return None, {"error": f"Commit path must be repository-relative: {path!r}"}
            valid.append(normalized)
        return list(dict.fromkeys(valid)), None

    @staticmethod
    def _path_selected(path: str, selected: list[str]) -> bool:
        return any(path == item or path.startswith(f"{item}/") for item in selected)

    async def _op_commit(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        message = params.get("message", "")
        if not message:
            return {"error": "Parameter 'message' is required for commit"}
        paths, error = self._validate_commit_paths(params.get("paths"))
        if error:
            return error

        # default core.quotepath=true makes git C-escape non-ASCII
        # paths ("\346\265\213\350\257\225.txt"), which never matches the raw
        # path the caller passed — Chinese/emoji filenames could never be
        # committed. `-c core.quotepath=false` keeps the raw bytes; note
        # `git diff` rejects the `--no-quotepath` flag (usage error 129), so
        # the config form is mandatory here.
        staged_before = await self._run_git(["-c", "core.quotepath=false", "diff", "--cached", "--name-only"], repo)
        if "error" in staged_before:
            return staged_before
        unexpected_before = [
            path for path in staged_before["stdout"].splitlines()
            if path and not self._path_selected(path, paths)
        ]
        if unexpected_before:
            return {
                "error": "Commit blocked: unrelated paths are already staged",
                "unexpected_staged": unexpected_before,
            }

        add = await self._run_git(["add", "--", *paths], repo)
        if "error" in add:
            return add
        staged = await self._run_git(["-c", "core.quotepath=false", "diff", "--cached", "--name-only"], repo)
        if "error" in staged:
            return staged
        staged_files = [path for path in staged["stdout"].splitlines() if path]
        if not staged_files:
            return {"error": "Nothing to commit for the selected paths"}
        unexpected = [path for path in staged_files if not self._path_selected(path, paths)]
        if unexpected:
            return {
                "error": "Commit blocked: staged changes exceed selected paths",
                "unexpected_staged": unexpected,
            }

        commit = await self._run_git(["commit", "-m", message], repo)
        if "error" in commit:
            return commit
        sha = await self._run_git(["rev-parse", "HEAD"], repo)
        return {
            "status": "committed",
            "sha": sha.get("stdout", "").strip(),
            "message": message,
            "files": staged_files,
            "path": self._display(repo, context),
        }

    async def _op_push(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = await self._require_repo(params, context)
        if error:
            return error
        branch = params.get("branch")
        if not branch:
            current = await self._run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
            branch = current.get("stdout", "").strip() if "error" not in current else ""
        if not self._valid_branch(branch):
            return {"error": "A valid branch is required; detached HEAD cannot be pushed"}

        # Existing checkouts use their configured origin. The askpass helper injects
        # the project token without exposing or rewriting the remote URL.
        token = await get_token_optional(context, "git")
        target_repository = params.get("target_repository")
        if target_repository:
            # Fork fallback: push straight to the fork's URL (built from the
            # configured host) instead of origin. No remote bookkeeping, and the
            # token still travels only via the askpass helper, never the URL.
            # Same shape check as clone's repository parameter — anything
            # outside owner/name would push the project token's credentials
            # to an arbitrary repo on the configured host.
            if not _REPO_PATH_RE.fullmatch(target_repository):
                return {"error": "Invalid target_repository (expected owner/name, e.g. 'my-fork/agent-world')"}
            base = context.cfg("GIT_BASE_URL", "")
            if not base:
                return {"error": "GIT_BASE_URL not configured — cannot resolve fork push target"}
            host = base.rstrip("/").removeprefix("https://").removeprefix("http://")
            push_target = f"https://{host}/{target_repository}.git"
        else:
            push_target = "origin"
        result = await self._run_git(
            ["push", push_target, f"{branch}:{branch}"], repo, _TIMEOUT_PULL, token
        )
        if "error" in result:
            stderr = result.get("stderr", "")
            if "non-fast-forward" in stderr.lower() or "rejected" in stderr.lower():
                return {
                    "status": "rejected",
                    "error": "Push rejected (non-fast-forward)",
                    "message": "Fetch and merge with sync_branch, rerun tests, then push normally.",
                    "branch": branch,
                }
            if "denied" in stderr.lower() or "403" in stderr.lower():
                return {
                    "error": (
                        f"git push failed: {stderr} "
                        "The Git token's account lacks push permission for this repository — "
                        "check that the personal access token has write scope "
                        "(classic PAT: 'repo'; fine-grained: Contents read-and-write) "
                        "and that the account is a collaborator with write access. "
                        "If contributing without write access, fall back to the fork flow: "
                        "github fork, push with target_repository=<fork>, create_pr with "
                        "head '<fork-owner>:<branch>' (see the tech-lead-jira-implementation-to-pr workflow)."
                    )
                }
            return {"error": f"git push failed: {stderr}"}
        return {
            "status": "pushed", "branch": branch, "path": self._display(repo, context)
        }
