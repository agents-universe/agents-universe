"""Incremental repo graph builder on top of graphify.

Candidate selection (git ls-files, exclusion dirs, size caps, head_sha fast
path) stays here; AST extraction, cross-file resolution and clustering are
graphify's job, reached through ``graphify_adapter`` — the only module that
imports the library. Results are converted to our schema-v2 RepoGraph, so
store/queries/report and every tool consumer are unchanged.

Git subprocess calls here (rev-parse, ls-files) never take a token and run
no hooks; the env is filtered like the tool layer's safe_env for defense in
depth. knowledge/graph stays importable standalone — no dependency on the
tools package.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .graphify_adapter import (
    GraphifyUnavailable,
    build_graph,
    lang_of,
    to_repo_graph,
)
from .model import (
    RepoGraph,
    repo_graph_dir,
)
from .report import REPORT_FILE, compact_map, render_report
from .store import GRAPH_FILE, invalidate_cached, load_repo_graph, save_repo_graph

_log = logging.getLogger(__name__)

# Auto-builds (git_repo ops) run inline up to this many files — small repos
# finish inside the turn so the clone result carries a fresh compact map.
# Larger repos (up to MAX_BUILD_FILES) queue a background build instead of
# being skipped: parsing thousands of files would stall the turn for little
# in-context value, but they should still end up indexed.
AUTO_BUILD_MAX_FILES = 3000
# Explicit `repo_graph build` may force past AUTO_BUILD_MAX_FILES but still
# refuses absurd trees: extracting tens of thousands of files holds the build
# lock and bloats graph.json far past what the LLM can use.
MAX_BUILD_FILES = 10_000
# Per-file skip reasons recorded in stats/report (rel -> reason). Bounded so
# a repo of a thousand giant generated files cannot bloat graph.json.
SKIPPED_FILES_MAX = 100
# Git listing/rev-parse should answer in seconds; a wedged git (stale index
# lock, hung fsmonitor) must not hold a build forever.
_GIT_TIMEOUT_S = 10.0
_BUILD_LOCKS: dict[str, asyncio.Lock] = {}  # one build at a time per kg_dir

# Background auto-builds: one live task per kg_dir (git ops during a build
# set `rerun` instead of stacking tasks), bounded by a semaphore so a burst
# of clones cannot fan out unbounded graphify extraction memory. In-process
# and single-loop, like the API scheduler: a process exit drops queued
# builds, recovered by the next git op or an explicit repo_graph build.
class _AutoBuild:
    __slots__ = ("task", "rerun", "files", "last", "repo", "project_fs_path", "repo_name")

    def __init__(self, task: asyncio.Task, files: int, repo: Path,
                 project_fs_path: str, repo_name: str) -> None:
        self.task = task
        self.rerun = False  # git op landed mid-build → run one more pass
        self.files = files  # candidate count, for the "in progress" hint
        self.last: dict[str, Any] | None = None  # most recent summary
        # For a done-callback restart (rerun set after the worker exited).
        self.repo = repo
        self.project_fs_path = project_fs_path
        self.repo_name = repo_name


_AUTO_BUILDS: dict[str, _AutoBuild] = {}  # key = str(kg_dir)


def _auto_build_sem() -> asyncio.Semaphore:
    """Concurrency cap for background builds, bound per running loop.

    A module-level Semaphore would bind to whichever loop first acquires it
    and then reject every other loop (pytest-asyncio runs each test on a
    fresh loop); recreate when the running loop changes. Production is a
    single loop for the process lifetime, so this is a no-op there.
    """
    loop = asyncio.get_running_loop()
    state = getattr(_auto_build_sem, "_state", None)
    if state is None or state[0] is not loop:
        state = (loop, asyncio.Semaphore(2))
        _auto_build_sem._state = state  # type: ignore[attr-defined]
    return state[1]

# Skip giant / minified / generated files outright. graphify's SQL extractor
# is an AST walk, not our old streaming parser: a spike-measured 6.9MB /
# 60k-table dump took ~3.5 minutes, so SQL gets a much smaller cap than the
# old 1GiB — anything past 2MB is a generated dump, not a migration script.
MAX_FILE_BYTES = 2 * 1024 * 1024
SQL_MAX_FILE_BYTES = 2 * 1024 * 1024

# Committed vendor/build dirs are still skipped even when git ls-files lists
# them — they are noise, not architecture. bin/obj/.vs are .NET build output
# and IDE state: a walked or include_untracked build would otherwise index
# thousands of generated .cs files.
EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "vendor", "dist", "build", "out", "coverage",
    "__pycache__", ".venv", "venv", ".next", ".nuxt", ".tox",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
    "bin", "obj", ".vs",
})

# What we index — graphify parses ~114 extensions, but the repo_graph tool's
# contract (and the agents' prompts) cover these languages only. Its broader
# set includes .json/.yaml, which would sweep lockfiles and our own
# .tmp/repo_graph artifacts into "source files".
INDEX_EXTS: frozenset[str] = frozenset({
    ".py", ".ts", ".mts", ".cts", ".tsx", ".js", ".mjs", ".cjs", ".jsx",
    ".vue", ".java", ".cs", ".sql",
})

# The per-repo graphify cache lives inside our storage dir, so git_repo's
# whole-directory cleanup also removes it. Never rename without checking
# git_repo.py's graph teardown.
GRAPHIFY_CACHE_DIRNAME = "graphify-out"

_MANIFEST_FILE = "build_manifest.json"

# Mirrors ToolContext._ENV_DENY_* — these git calls need no secrets.
_ENV_DENY_SUFFIXES = frozenset(
    {"TOKEN", "SECRET", "PASSWORD", "COOKIE", "KEY", "CREDENTIAL", "AUTH",
     "DRIVER", "URL", "DSN"}
)
_ENV_DENY_PREFIXES = (
    "DB_", "DATABASE", "REDIS", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL",
    "AUTH", "API_KEY", "PRIVATE", "AWS_", "AZURE_", "GOOGLE_", "GCP_", "ALICLOUD_",
)
_ENV_DENY_EXACT = frozenset(
    {"PROJECTS_ROOT", "DATABASE_URL", "SQLALCHEMY_DATABASE_URI", "MSSQL_CONNECTION_STRING"}
)


def _git_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _ENV_DENY_EXACT:
            continue
        if upper.rsplit("_", 1)[-1] in _ENV_DENY_SUFFIXES:
            continue
        if any(upper.startswith(p) for p in _ENV_DENY_PREFIXES):
            continue
        env[key] = value
    return env


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


async def _communicate(process: asyncio.subprocess.Process) -> tuple[bytes, bytes] | None:
    """communicate() bounded by _GIT_TIMEOUT_S; None on timeout (proc killed)."""
    try:
        return await asyncio.wait_for(process.communicate(), timeout=_GIT_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            process.kill()
            await process.wait()
        except OSError:
            pass
        return None


async def _git_head_sha(repo: Path) -> str:
    """Current HEAD sha; best-effort, "" when not resolvable (no fast path)."""
    git = _find_git()
    if git:
        try:
            process = await asyncio.create_subprocess_exec(
                git, "rev-parse", "HEAD", cwd=str(repo),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=_git_env(),
            )
            comm = await _communicate(process)
            if comm is not None and process.returncode == 0:
                out, _ = comm
                return out.decode("utf-8", "replace").strip()
        except OSError:
            pass
    return _manual_head_sha(repo)


def _manual_head_sha(repo: Path) -> str:
    """git-less fallback: read .git/HEAD and the referenced ref file."""
    gitdir = repo / ".git"
    if not gitdir.is_dir():
        return ""  # worktree (gitdir is a file) — no fast path, rebuild instead
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if not head.startswith("ref:"):
            return head  # detached HEAD
        ref = head[5:].strip()
        ref_path = gitdir / ref
        if ref_path.is_file():
            return ref_path.read_text(encoding="utf-8", errors="replace").strip()
        packed = gitdir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        pass
    return ""


async def _tracked_files(repo: Path) -> list[str] | None:
    """git ls-files -z; None when git is unusable (caller falls back to os.walk)."""
    git = _find_git()
    if not git:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            git, "-c", "core.quotepath=false", "ls-files", "-z", cwd=str(repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=_git_env(),
        )
        comm = await _communicate(process)
    except OSError:
        return None
    if comm is None or process.returncode != 0:
        return None
    out, _ = comm
    # core.quotepath=false keeps non-ASCII paths as raw bytes (same as the
    # commit op); -z splits on NUL, so paths with spaces/newlines are safe.
    return [path for path in out.decode("utf-8", "replace").split("\0") if path]


def _walk_files(repo: Path) -> list[str]:
    files: list[str] = []
    for root, dirs, names in os.walk(repo):
        # Case-insensitive: EXCLUDED_DIRS is all-lowercase and Windows checkouts
        # produce Bin/obj/ trees that would otherwise slip through.
        dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS)
        rel_root = Path(root).relative_to(repo)
        for name in sorted(names):
            rel = (rel_root / name).as_posix() if str(rel_root) != "." else name
            files.append(rel)
    return files


async def _untracked_files(repo: Path) -> list[str] | None:
    """git ls-files --others --exclude-standard -z; None when git is unusable.

    ``--exclude-standard`` respects .gitignore, so build artifacts never land
    in the graph. Used to warn about source files the graph is silently
    missing (they are not tracked yet) and, on explicit request, to index
    them.
    """
    git = _find_git()
    if not git:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            git, "-c", "core.quotepath=false", "ls-files", "--others",
            "--exclude-standard", "-z", cwd=str(repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=_git_env(),
        )
        comm = await _communicate(process)
    except OSError:
        return None
    if comm is None or process.returncode != 0:
        return None
    out, _ = comm
    # -z splits on NUL, so paths with spaces/newlines are safe (same as
    # _tracked_files).
    return [path for path in out.decode("utf-8", "replace").split("\0") if path]


def _candidate_files(files: list[str]) -> list[str]:
    """Files worth indexing: indexed ext, not in an excluded dir."""
    return sorted({
        rel for rel in files
        if not any(part.lower() in EXCLUDED_DIRS for part in rel.split("/"))
        and Path(rel).suffix.lower() in INDEX_EXTS
    })


def _untracked_hint(untracked_candidates: list[str]) -> str | None:
    """Human hint explaining why the graph is missing the untracked files."""
    shown = untracked_candidates[:3]
    more = len(untracked_candidates) - len(shown)
    detail = ", ".join(shown) + (f", +{more} more" if more > 0 else "")
    return (
        f"committed source only; {len(untracked_candidates)} untracked source "
        f"file(s) are not in the graph ({detail}). Commit them, or rebuild with "
        "include_untracked=true to index them."
    )


def _max_file_bytes(rel: str) -> int:
    return SQL_MAX_FILE_BYTES if rel.lower().endswith(".sql") else MAX_FILE_BYTES


def _load_manifest(kg_dir: Path) -> dict[str, Any] | None:
    """Small per-build manifest: candidate list + build mode for the fast path.

    graphify's AST cache alone cannot answer "does the stored graph cover
    every current candidate" — the head_sha fast path needs the exact list.
    """
    try:
        data = json.loads((kg_dir / _MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_manifest(kg_dir: Path, *, tracked_only: bool, candidates: list[str]) -> None:
    payload = json.dumps({"tracked_only": tracked_only, "candidates": candidates})
    tmp = kg_dir / (_MANIFEST_FILE + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, kg_dir / _MANIFEST_FILE)


async def build_repo_graph(
    repo: Path, kg_dir: Path, force: bool = False, include_untracked: bool = False
) -> dict[str, Any]:
    """Build/refresh the graph for one checkout; returns a summary dict.

    One build at a time per kg_dir — auto-builds (git_repo ops) and explicit
    ``repo_graph build`` share this lock, so a forced rebuild can never race
    an auto-build on the same cache. The lock lives here (not in
    maybe_build_auto) so every entry point serializes identically.

    See _build_repo_graph_locked for the build semantics.
    """
    lock = _BUILD_LOCKS.setdefault(str(kg_dir), asyncio.Lock())
    async with lock:
        return await _build_repo_graph_locked(repo, kg_dir, force, include_untracked)


def _persist(graph: RepoGraph, kg_dir: Path) -> str:
    """Persist graph + report; returns the compact repo map.

    Runs in a worker thread (one to_thread call for the whole tail): the JSON
    dump alone can freeze the event loop for seconds on a large repo, which
    would starve WS pings/heartbeats and drop the client connection.
    """
    save_repo_graph(graph, kg_dir)
    (kg_dir / REPORT_FILE).write_text(render_report(graph), encoding="utf-8")
    return compact_map(graph)


async def _build_repo_graph_locked(
    repo: Path, kg_dir: Path, force: bool = False, include_untracked: bool = False
) -> dict[str, Any]:
    """Build/refresh the graph for one checkout; returns a summary dict.

    Fast path: head unchanged AND the stored manifest already covers every
    candidate source file -> up_to_date with zero hashing. Otherwise every
    candidate is size/containment-checked and handed to graphify (its SHA256
    AST cache skips unchanged files internally), then converted to schema v2
    and persisted.

    By default only *tracked* files are indexed (the graph mirrors committed
    architecture). Files that exist on disk but are not tracked yet are
    reported in the summary's ``warning`` so an empty-looking graph is never
    silently misread as "no code". Pass ``include_untracked=True`` to fold
    them into the candidates as well.

    Every CPU/JSON-heavy step (walk, extraction, conversion, persistence)
    runs via asyncio.to_thread: this coroutine shares the process event loop
    with the WebSocket it must keep alive.
    """
    started = time.perf_counter()
    kg_dir.mkdir(parents=True, exist_ok=True)

    tracked = await _tracked_files(repo)
    if tracked is None:
        _log.info("git ls-files failed for %s; falling back to os.walk", repo)
        tracked = await asyncio.to_thread(_walk_files, repo)
    tracked_candidates = await asyncio.to_thread(_candidate_files, tracked)
    untracked = await _untracked_files(repo)
    untracked_candidates = await asyncio.to_thread(_candidate_files, untracked or [])
    if include_untracked:
        candidates = sorted(set(tracked_candidates) | set(untracked_candidates))
    else:
        candidates = tracked_candidates
    head = await _git_head_sha(repo)

    existing = await asyncio.to_thread(load_repo_graph, kg_dir)
    manifest = await asyncio.to_thread(_load_manifest, kg_dir)
    if (
        not force
        # "" = HEAD unresolvable (no git / worktree / empty repo). The stored
        # head_sha is "" too and would compare equal — the fast path must
        # rebuild instead (see _manual_head_sha) or content edits with an
        # unchanged file set never reach the graph.
        and head
        and existing is not None
        and manifest is not None
        and existing.repo.head_sha == head
        and manifest.get("tracked_only") == (not include_untracked)
        and set(candidates) <= set(manifest.get("candidates") or [])
    ):
        summary = {
            "status": "up_to_date",
            "head": head,
            "stats": existing.stats,
            "repo_map": await asyncio.to_thread(compact_map, existing),
            "graph_path": str(kg_dir / GRAPH_FILE),
            "build_ms": int((time.perf_counter() - started) * 1000),
        }
        if untracked_candidates and not include_untracked:
            summary["warning"] = _untracked_hint(untracked_candidates)
        return summary

    # Refuse absurd trees AFTER the fast path so an already-built oversized
    # graph still serves up_to_date; only new/forced builds are cut off.
    if len(candidates) > MAX_BUILD_FILES:
        return {
            "status": "skipped",
            "reason": "too_many_files",
            "files": len(candidates),
            "max": MAX_BUILD_FILES,
            "hint": (
                f"Repo has {len(candidates)} source files (> {MAX_BUILD_FILES}); "
                "the knowledge graph is not built for repos this size. "
                "Use search/read tools instead."
            ),
        }

    skipped_files: dict[str, str] = {}
    repo_resolved = repo.resolve()
    paths: list[Path] = []
    accepted: list[str] = []

    def _prepare() -> None:
        # resolve() follows symlinks/junctions: a listed path can leave the
        # checkout (external files' symbols would land in graph.json) or land
        # on a device file. Hash only regular files still inside the repo —
        # the same rule knowledge/loader.py applies (inlined; this module
        # must not import the tools package).
        for rel in candidates:
            try:
                path = (repo / rel).resolve()
                contained = path.is_relative_to(repo_resolved) and path.is_file()
            except OSError:
                contained = False
            if not contained:
                if len(skipped_files) < SKIPPED_FILES_MAX:
                    skipped_files[rel] = "missing or outside repo"
                continue
            try:
                size = path.stat().st_size
            except OSError:
                if len(skipped_files) < SKIPPED_FILES_MAX:
                    skipped_files[rel] = "unreadable"
                continue
            limit = _max_file_bytes(rel)
            if size > limit:
                if len(skipped_files) < SKIPPED_FILES_MAX:
                    skipped_files[rel] = f"over {limit // (1 << 20)}MB limit"
                continue
            paths.append(path)
            accepted.append(rel)

    await asyncio.to_thread(_prepare)

    # force only bypasses the head_sha fast path: graphify's SHA256 AST cache
    # still serves unchanged files, so "rebuild after edits" re-extracts only
    # what actually changed (same semantics the old manifest cache had).
    try:
        result = await asyncio.to_thread(
            build_graph, paths, root=repo, cache_root=kg_dir / GRAPHIFY_CACHE_DIRNAME
        )
    except GraphifyUnavailable as exc:
        _log.error("repo graph %s: %s", repo.name, exc)
        return {"status": "failed", "reason": str(exc)}

    langs = sorted({
        lang for rel in accepted
        if (lang := lang_of(rel))
    })
    graph = await asyncio.to_thread(
        to_repo_graph,
        result,
        repo_name=repo.name,
        head_sha=head,
        candidates=accepted,
        langs=langs,
        extra_stats={
            "skipped_files": skipped_files,
            "untracked_sources": 0 if include_untracked else len(untracked_candidates),
        },
    )
    graph.stats["build_ms"] = int((time.perf_counter() - started) * 1000)
    graph.stats["graphify_import_ms"] = result.import_ms

    await asyncio.to_thread(_save_manifest, kg_dir,
                            tracked_only=not include_untracked, candidates=candidates)
    repo_map = await asyncio.to_thread(_persist, graph, kg_dir)
    invalidate_cached(kg_dir)
    _log.info("repo graph %s: %d files, %d nodes, %d edges (%d ms)",
              repo.name, graph.stats.get("files", 0), graph.stats.get("nodes", 0),
              graph.stats.get("edges", 0), graph.stats["build_ms"])
    summary = {
        "status": "built",
        "head": head,
        "stats": graph.stats,
        "repo_map": repo_map,
        "graph_path": str(kg_dir / GRAPH_FILE),
    }
    if result.failed_sources:
        summary["warning"] = (
            f"{len(result.failed_sources)} source file(s) failed to extract "
            "(see graph_report.md)"
        )
    if untracked_candidates and not include_untracked:
        hint = _untracked_hint(untracked_candidates)
        summary["warning"] = f"{summary['warning']} {hint}" if "warning" in summary else hint
    return summary


def auto_build_files_in_progress(kg_dir: Path) -> int | None:
    """Candidate count while a background build runs for kg_dir, else None."""
    entry = _AUTO_BUILDS.get(str(kg_dir))
    return entry.files if entry is not None else None


def enqueue_auto_build(repo: Path, project_fs_path: str, repo_name: str, files: int) -> dict[str, Any]:
    """Queue a background auto-build; O(1), never awaits the build itself.

    One live task per kg_dir: a git op landing mid-build sets `rerun` so the
    worker runs one more pass instead of stacking tasks (the extra pass hits
    the fast path when nothing changed). Returns the summary dict the git op
    attaches to its result.
    """
    key = str(repo_graph_dir(project_fs_path, repo_name))
    existing = _AUTO_BUILDS.get(key)
    if existing is not None:
        existing.rerun = True
    else:
        task = asyncio.create_task(
            _auto_build_worker(repo, project_fs_path, repo_name, key)
        )
        entry = _AutoBuild(task, files, repo, project_fs_path, repo_name)
        _AUTO_BUILDS[key] = entry
        task.add_done_callback(lambda t, k=key, e=entry: _auto_build_done(t, k, e))
    return {
        "status": "queued",
        "reason": "background_build",
        "files": files,
        "hint": (
            f"Repo has {files} source files; building the knowledge graph in "
            "the background — run repo_graph query shortly "
            "(repo_graph build forces a synchronous rebuild)."
        ),
    }


def _auto_build_done(task: asyncio.Task, key: str, entry: _AutoBuild) -> None:
    # Pop only if this task is still the registered one (never replaced).
    if _AUTO_BUILDS.get(key) is entry:
        del _AUTO_BUILDS[key]
    if task.cancelled():
        _log.warning("background repo graph build cancelled for %s", key)
        return
    exc = task.exception()  # consume; the worker already logged failures
    if exc is not None:
        _log.error("background repo graph build crashed for %s: %s", key, exc)
        return
    if entry.rerun:
        # A git op landed between the worker's final rerun-check and this
        # callback — the worker never saw it. Start a fresh task rather than
        # dropping the rebuild request.
        enqueue_auto_build(entry.repo, entry.project_fs_path, entry.repo_name, entry.files)


async def _auto_build_worker(
    repo: Path, project_fs_path: str, repo_name: str, key: str
) -> None:
    entry = _AUTO_BUILDS[key]
    while True:
        entry.rerun = False  # a git op after this point sets it again
        async with _auto_build_sem():
            try:
                summary = await build_repo_graph(
                    repo, repo_graph_dir(project_fs_path, repo_name), force=False
                )
            except Exception as exc:
                _log.exception("background auto repo graph build failed for %s", repo_name)
                summary = {"status": "failed", "reason": str(exc)}
        entry.last = summary
        _log.info(
            "background repo graph build for %s: %s", repo_name, summary.get("status")
        )
        # No await from here through the task returning: on a single event
        # loop that segment is atomic, so a rerun set by enqueue_auto_build
        # either lands before this check (rebuild) or after the done-callback
        # pop (fresh task) — never in between and lost.
        if not entry.rerun:
            break


async def wait_pending_auto_builds() -> list[dict[str, Any]]:
    """Await in-flight background builds (tests / graceful drain).

    Tasks bound to a dead loop (pytest-asyncio rotates loops per test) are
    dropped rather than awaited — awaiting across loops raises.
    """
    summaries: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    for _ in range(10):  # bounded: a rerun keeps its task alive across gather
        for key, entry in list(_AUTO_BUILDS.items()):
            if entry.task.get_loop() is not loop:
                _log.warning("dropping stale background build from another loop: %s", key)
                del _AUTO_BUILDS[key]
        live = [e for e in _AUTO_BUILDS.values() if e.task.get_loop() is loop]
        if not live:
            break
        await asyncio.gather(*(e.task for e in live), return_exceptions=True)
        summaries.extend(e.last for e in live if e.last is not None)
    return summaries


async def maybe_build_auto(
    repo: Path, project_fs_path: str, repo_name: str, force: bool = False
) -> dict[str, Any] | None:
    """Best-effort auto-build on git ops; never raises, never fails the git op.

    Guard rails: requires a plain .git dir; builds inline up to
    AUTO_BUILD_MAX_FILES (the clone result carries a fresh compact map),
    queues a background build up to MAX_BUILD_FILES (the turn must not wait
    on thousands of files), refuses larger trees with a hint; one build at a
    time per kg_dir (the lock is taken inside build_repo_graph, so every
    entry point serializes identically).
    """
    if not (repo / ".git").is_dir():
        return None
    tracked = await _tracked_files(repo)
    if tracked is None:
        tracked = await asyncio.to_thread(_walk_files, repo)
    candidate_count = len(await asyncio.to_thread(_candidate_files, tracked))
    if candidate_count > AUTO_BUILD_MAX_FILES:
        if candidate_count > MAX_BUILD_FILES:
            return {
                "status": "skipped",
                "reason": "too_many_files",
                "files": candidate_count,
                "hint": f"Repo has {candidate_count} source files; run repo_graph build to force.",
            }
        return enqueue_auto_build(repo, project_fs_path, repo_name, candidate_count)
    kg_dir = repo_graph_dir(project_fs_path, repo_name)
    try:
        return await build_repo_graph(repo, kg_dir, force=force)
    except Exception as exc:
        _log.exception("auto repo graph build failed for %s", repo_name)
        return {"status": "failed", "reason": str(exc)}
