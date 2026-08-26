"""Incremental repo graph builder (mirrors graphify's design).

``cache/index.json`` IS the per-file parse store: graph.json is re-assembled
purely from it, so rebuilds only re-parse files whose SHA256 changed. The
head_sha fast path skips even hashing when nothing changed. Cross-file
resolution (imports -> module files, calls -> symbols) is a pure function
over the per-file results, so the whole pipeline is deterministic and
testable without a server.

Git subprocess calls here (rev-parse, ls-files) never take a token and run
no hooks; the env is filtered like the tool layer's safe_env for defense in
depth. knowledge/graph stays importable standalone — no dependency on the
tools package.
"""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .cache import GraphCache
from .languages import EXCLUDED_DIRS, detect_language
from .model import (
    EDGE_CALLS,
    EDGE_IMPORTS,
    EDGE_INHERITS,
    GraphEdge,
    GraphNode,
    RepoGraph,
    RepoMeta,
    file_id,
    now_epoch,
    parse_node_id,
    repo_graph_dir,
    symbol_id,
)
from .parser import file_sha256, parse_file
from .report import REPORT_FILE, compact_map, render_report
from .store import GRAPH_FILE, invalidate_cached, load_repo_graph, save_repo_graph

_log = logging.getLogger(__name__)

# Auto-builds (git_repo ops) skip repos larger than this — parsing thousands
# of files would stall the turn for little in-context value.
AUTO_BUILD_MAX_FILES = 3000
_PARSE_CONCURRENCY = 8  # tree-sitter releases the GIL during parse
_BUILD_LOCKS: dict[str, asyncio.Lock] = {}  # one build at a time per kg_dir

_TS_EXTS = (".ts", ".mts", ".cts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".vue")

# Mirrors ToolContext._ENV_DENY_* — these git calls need no secrets.
_ENV_DENY_SUFFIXES = frozenset(
    {"TOKEN", "SECRET", "PASSWORD", "COOKIE", "KEY", "CREDENTIAL", "AUTH",
     "DRIVER", "URL", "DSN"}
)
_ENV_DENY_PREFIXES = (
    "DB_", "DATABASE", "REDIS", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL",
    "AUTH", "API_KEY", "PRIVATE", "AWS_", "AZURE_", "GOOGLE_", "GCP_", "ALICLOUD_",
)
_ENV_DENY_EXACT = frozenset({"PROJECTS_ROOT", "DATABASE_URL"})


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
            out, _ = await process.communicate()
            if process.returncode == 0:
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
        out, _ = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None
    # core.quotepath=false keeps non-ASCII paths as raw bytes (same as the
    # commit op); -z splits on NUL, so paths with spaces/newlines are safe.
    return [path for path in out.decode("utf-8", "replace").split("\0") if path]


def _walk_files(repo: Path) -> list[str]:
    files: list[str] = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        rel_root = Path(root).relative_to(repo)
        for name in sorted(names):
            rel = (rel_root / name).as_posix() if str(rel_root) != "." else name
            files.append(rel)
    return files


def _candidate_files(files: list[str]) -> list[str]:
    """Tracked files worth indexing: supported ext, not in an excluded dir."""
    return sorted({
        rel for rel in files
        if not any(part in EXCLUDED_DIRS for part in rel.split("/"))
        and detect_language(rel) is not None
    })


async def build_repo_graph(
    repo: Path, kg_dir: Path, force: bool = False
) -> dict[str, Any]:
    """Build/refresh the graph for one checkout; returns a summary dict.

    Fast path: head unchanged AND the manifest already covers every tracked
    source file -> up_to_date with zero hashing. Otherwise every candidate
    file is hashed but only changed ones are re-parsed (bounded thread pool);
    deleted files drop out of the manifest; then graph.json and
    graph_report.md are assembled purely from the manifest.
    """
    started = time.perf_counter()
    kg_dir.mkdir(parents=True, exist_ok=True)
    cache = GraphCache(kg_dir / "cache")
    state = cache.load()

    tracked = await _tracked_files(repo)
    if tracked is None:
        _log.info("git ls-files failed for %s; falling back to os.walk", repo)
        tracked = _walk_files(repo)
    candidates = _candidate_files(tracked)
    head = await _git_head_sha(repo)

    existing = load_repo_graph(kg_dir)
    if (
        not force
        and existing is not None
        and existing.repo.head_sha == head
        and set(candidates) <= set(state.files)
    ):
        summary = {
            "status": "up_to_date",
            "head": head,
            "stats": existing.stats,
            "repo_map": compact_map(existing),
            "graph_path": str(kg_dir / GRAPH_FILE),
            "build_ms": int((time.perf_counter() - started) * 1000),
        }
        return summary

    counters = {"parsed": 0, "reused": 0, "failed": 0, "skipped": 0}

    async def _handle(rel: str) -> None:
        path = (repo / rel).resolve()
        try:
            sha = await asyncio.to_thread(file_sha256, path)
        except OSError:  # deleted between ls-files and read — skip
            counters["skipped"] += 1
            state.files.pop(rel, None)
            return
        entry = cache.get(state, rel)
        if entry is not None and entry.get("sha") == sha:
            counters["reused"] += 1
            if entry.get("stats", {}).get("error"):
                counters["failed"] += 1  # broken files stay "failed" in reports
            return
        result = await asyncio.to_thread(parse_file, path, rel)
        if result is None:  # too big / unreadable
            counters["skipped"] += 1
            state.files.pop(rel, None)
            return
        if result.stats.get("error"):
            counters["failed"] += 1
        cache.set_file(state, rel, result)
        counters["parsed"] += 1

    semaphore = asyncio.Semaphore(_PARSE_CONCURRENCY)

    async def _guarded(rel: str) -> None:
        async with semaphore:
            await _handle(rel)

    await asyncio.gather(*(_guarded(rel) for rel in candidates))

    # Deleted files: entries that no longer exist as tracked sources.
    for rel in list(state.files):
        if rel not in candidates:
            state.files.pop(rel, None)

    results = {rel: state.files[rel] for rel in candidates if rel in state.files}
    graph = _assemble_graph(repo.name, head, results, counters)
    graph.stats["build_ms"] = int((time.perf_counter() - started) * 1000)

    cache.save(state)
    save_repo_graph(graph, kg_dir)
    (kg_dir / REPORT_FILE).write_text(render_report(graph), encoding="utf-8")
    invalidate_cached(kg_dir)
    _log.info("repo graph %s: %d files, %d nodes, %d edges (%d ms)",
              repo.name, graph.stats.get("files", 0), graph.stats.get("nodes", 0),
              graph.stats.get("edges", 0), graph.stats["build_ms"])
    return {
        "status": "built",
        "head": head,
        "stats": graph.stats,
        "repo_map": compact_map(graph),
        "graph_path": str(kg_dir / GRAPH_FILE),
    }


async def maybe_build_auto(
    repo: Path, project_fs_path: str, repo_name: str, force: bool = False
) -> dict[str, Any] | None:
    """Best-effort auto-build on git ops; never raises, never blocks the result.

    Guard rails: requires a plain .git dir; skips repos over
    AUTO_BUILD_MAX_FILES with a hint; one build at a time per kg_dir.
    """
    if not (repo / ".git").is_dir():
        return None
    tracked = await _tracked_files(repo)
    if tracked is None:
        tracked = _walk_files(repo)
    candidate_count = len(_candidate_files(tracked))
    if candidate_count > AUTO_BUILD_MAX_FILES:
        return {
            "status": "skipped",
            "reason": "too_many_files",
            "files": candidate_count,
            "hint": f"Repo has {candidate_count} source files; run repo_graph build to force.",
        }
    kg_dir = repo_graph_dir(project_fs_path, repo_name)
    lock = _BUILD_LOCKS.setdefault(str(kg_dir), asyncio.Lock())
    async with lock:
        try:
            return await build_repo_graph(repo, kg_dir, force=force)
        except Exception as exc:
            _log.exception("auto repo graph build failed for %s", repo_name)
            return {"status": "failed", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Cross-file assembly (pure — deterministic, unit-testable)
# ---------------------------------------------------------------------------

def _assemble_graph(
    repo_name: str,
    head_sha: str,
    results: dict[str, dict[str, Any]],
    counters: dict[str, int] | None = None,
) -> RepoGraph:
    """Derive a RepoGraph from per-file parse results (manifest entries)."""
    counters = counters or {}
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    def add_node(node: GraphNode) -> None:
        if node.id not in seen_nodes:
            seen_nodes.add(node.id)
            nodes.append(node)

    def add_edge(src: str, dst: str, etype: str) -> None:
        key = (src, dst, etype)
        if key not in seen_edges and src != dst:
            seen_edges.add(key)
            edges.append(GraphEdge(src=src, dst=dst, type=etype))

    langs = sorted({detect_language(rel) for rel in results if detect_language(rel)})
    module_index = set(results)
    # Java source roots (src/main/java, src/, repo root) — package paths are
    # relative to one of these. Derived from the tracked .java files.
    java_roots = _java_source_roots(results)

    # Pass 1: file nodes, symbol nodes, per-file indexes.
    # qname/last_seg indexes are keyed by (lang, name) so same-named symbols in
    # different languages (Java Greeter vs TS Greeter) never collide.
    file_node_ids: dict[str, str] = {}
    file_symbols: dict[str, set[str]] = {}
    qname_index: dict[tuple[str, str], list[str]] = {}
    last_seg_index: dict[tuple[str, str], list[str]] = {}  # (lang, last seg)
    for rel, entry in results.items():
        lang = entry.get("lang", "")
        file_node_ids[rel] = file_id(rel)
        add_node(GraphNode(
            id=file_id(rel), type="file", name=rel,
            lang=entry.get("lang"), lines=entry.get("lines", 0),
        ))
        symbols: set[str] = set()
        for symbol in entry.get("symbols", []):
            qname = symbol.get("name", "")
            if not qname:
                continue
            node_id = symbol_id(rel, qname)
            if node_id in seen_nodes:
                continue  # redefinition — keep the first
            symbols.add(qname)
            add_node(GraphNode(
                id=node_id, type=symbol.get("type", "symbol"),
                name=qname, line=symbol.get("line", 0),
            ))
            qname_index.setdefault((lang, qname), []).append(node_id)
            last_seg_index.setdefault((lang, qname.rsplit(".", 1)[-1]), []).append(node_id)
        file_symbols[rel] = symbols

    # Pass 2: imports -> file-level edges (external modules only counted).
    external_imports = 0
    for rel, entry in results.items():
        lang = entry.get("lang", "")
        for imp in entry.get("imports", []):
            module = imp.get("module") or ""
            if not module:
                continue
            target_rel = _resolve_module(module, rel, lang, module_index, java_roots)
            if target_rel is None:
                external_imports += 1
                continue
            add_edge(file_node_ids[rel], file_node_ids[target_rel], EDGE_IMPORTS)

    # Pass 3: calls / inherits -> symbol-or-file edges.
    unresolved = 0
    for rel, entry in results.items():
        lang = entry.get("lang", "")
        symbols = file_symbols[rel]
        aliases = _alias_map(entry)
        for edge in entry.get("edges", []):
            etype = edge.get("type")
            if etype not in (EDGE_CALLS, EDGE_INHERITS):
                continue
            target = edge.get("target") or ""
            if not target:
                continue
            src_qname = edge.get("from")
            if src_qname and src_qname in symbols:
                src_id = symbol_id(rel, src_qname)
            else:
                src_id = file_node_ids[rel]  # module-level call
            if etype == EDGE_CALLS:
                dst = _resolve_call(
                    target, rel, lang, symbols, aliases, file_symbols,
                    file_node_ids, qname_index, last_seg_index, module_index,
                    java_roots,
                )
            else:
                dst = _resolve_symbol(
                    target, rel, lang, file_symbols, qname_index, last_seg_index
                )
            if dst is None:
                unresolved += 1
            else:
                add_edge(src_id, dst, etype)

    stats: dict[str, int] = {
        "files": len(results),
        "nodes": len(nodes),
        "edges": len(edges),
        "parsed": counters.get("parsed", 0),
        "reused": counters.get("reused", 0),
        "failed": counters.get("failed", 0),
        "skipped": counters.get("skipped", 0),
        "unresolved_calls": unresolved,
        "external_imports": external_imports,
    }
    return RepoGraph(
        repo=RepoMeta(name=repo_name, head_sha=head_sha, langs=langs, built_at=now_epoch()),
        stats=stats,
        nodes=nodes,
        edges=edges,
    )


# --- module (import) resolution -------------------------------------------

def _java_source_roots(results: dict[str, dict[str, Any]]) -> list[str]:
    """Most-specific source roots for the tracked .java files.

    A FQCN import like com.example.lib.Greeter resolves relative to the root
    that contains its package dir. Common roots are src/main/java and src/;
    files at the repo root imply the root itself. Longer roots are tried
    first because they match the file most precisely.
    """
    java_files = [rel for rel, entry in results.items() if entry.get("lang") == "java"]
    if not java_files:
        return []
    roots = {""}  # repo root always applies
    for rel in java_files:
        parts = rel.split("/")
        # the package path is everything under the source root; any prefix of
        # the file path can be the root — collect the shortest sensible ones
        # (src/main/java, src/main, src, ...) up to the file's parent.
        for i in range(1, len(parts)):
            roots.add("/".join(parts[:i]))
    return sorted((r for r in roots if r), key=lambda r: -r.count("/"))


def _resolve_module(
    module: str,
    rel: str,
    lang: str,
    module_index: set[str],
    java_roots: list[str] | None = None,
) -> str | None:
    """Map an import specifier to a tracked rel path; None when external."""
    rel_dir = posixpath.dirname(rel)
    if lang == "python":
        candidates = _py_module_candidates(module, rel_dir)
    elif lang in ("typescript", "tsx", "javascript", "jsx"):
        candidates = _ts_module_candidates(module, rel_dir)
    elif lang == "java":
        candidates = _java_candidates_with_roots(module, java_roots or [""])
    else:
        return None
    for candidate in candidates:
        if candidate in module_index:
            return candidate
    return None


def _py_module_candidates(module: str, rel_dir: str) -> list[str]:
    stripped = module.lstrip(".")
    dotted = stripped.replace(".", "/")
    if not dotted:
        # `from . import x` — the package itself
        return [f"{rel_dir}/__init__.py"] if rel_dir else []
    prefix = f"{rel_dir}/" if module.startswith(".") else ""
    return [f"{prefix}{dotted}.py", f"{prefix}{dotted}/__init__.py"]


def _java_candidates_with_roots(module: str, roots: list[str]) -> list[str]:
    """FQCN candidates under every source root, most specific root first."""
    base = _java_module_candidates(module)
    ordered: list[str] = []
    seen: set[str] = set()
    for root in roots:  # roots are pre-sorted most-specific first
        for candidate in base:
            full = posixpath.join(root, candidate) if root else candidate
            if full not in seen:
                seen.add(full)
                ordered.append(full)
    return ordered


def _java_module_candidates(module: str) -> list[str]:
    """FQCN -> package-relative file-path candidates, most specific first.

    Java imports name a fully-qualified type (com.example.lib.Greeter), a
    package (com.example.lib.*) or a static member (com.example.lib.Util.run).
    Every dotted prefix maps to a candidate .java file (the last segment may be
    a type or a member), so a type import hits Greeter.java and a static import
    falls through to Util.java. Paths are relative to a source root; the
    builder tries each root (src/main/java, src, repo root) as a prefix.
    """
    parts = module.split(".")
    if not parts or not all(parts):
        return []
    return ["/".join(parts[:i]) + ".java" for i in range(len(parts), 0, -1)]


def _ts_module_candidates(module: str, rel_dir: str) -> list[str]:
    if module.startswith("/"):  # repo-root-absolute (rare)
        base = module[1:]
    else:
        base = posixpath.join(rel_dir, module) if rel_dir else module
    if any(base.endswith(ext) for ext in _TS_EXTS):
        return [posixpath.normpath(base)]
    candidates = []
    for ext in _TS_EXTS:
        candidates.append(posixpath.normpath(f"{base}{ext}"))
        candidates.append(posixpath.normpath(f"{base}/index{ext}"))
    return candidates


# --- import alias maps ----------------------------------------------------

def _alias_map(entry: dict[str, Any]) -> dict[str, tuple[str, str | None]]:
    """Local name -> (module, symbol|None). None means module-level binding."""
    lang = entry.get("lang", "")
    aliases: dict[str, tuple[str, str | None]] = {}
    for imp in entry.get("imports", []):
        module = imp.get("module") or ""
        name = imp.get("name")
        alias = imp.get("alias")
        if not module:
            continue
        if lang == "python":
            if name:
                aliases[alias or name] = (module, name)
            else:
                aliases[alias or module.split(".")[0]] = (module, None)
        elif name == "*":
            aliases[alias or module] = (module, None)
        elif name:
            aliases[alias or name] = (module, name)
        # side-effect imports bind nothing
    return aliases


# --- call/symbol resolution ------------------------------------------------

def _match_alias(target: str, aliases: dict[str, tuple[str, str | None]]):
    """(module, rest|None) for the longest alias matching target; None if none."""
    if target in aliases:
        module, name = aliases[target]
        return module, name
    best_alias: str | None = None
    for alias in aliases:
        if target.startswith(alias + ".") and (best_alias is None or len(alias) > len(best_alias)):
            best_alias = alias
    if best_alias is None:
        return None
    module, _ = aliases[best_alias]
    rest = target[len(best_alias) + 1:]
    return module, rest


def _lookup_in_file(rest: str, target_rel: str, file_symbols: dict[str, set[str]]) -> str | None:
    """Exact qname in the file, else the unique suffix match; None if ambiguous."""
    symbols = file_symbols.get(target_rel, set())
    if rest in symbols:
        return rest
    matches = [s for s in symbols if s.endswith("." + rest)]
    return matches[0] if len(matches) == 1 else None


def _resolve_call(
    target: str,
    rel: str,
    lang: str,
    symbols: set[str],
    aliases: dict[str, tuple[str, str | None]],
    file_symbols: dict[str, set[str]],
    file_node_ids: dict[str, str],
    qname_index: dict[tuple[str, str], list[str]],
    last_seg_index: dict[tuple[str, str], list[str]],
    module_index: set[str],
    java_roots: list[str] | None = None,
) -> str | None:
    """Resolve a call target to a node id, or None (unresolved).

    Order: same-file exact -> import alias (module then symbol, module-level
    fallback to the file node) -> global exact qname -> suffix match.
    """
    if target in symbols:
        return symbol_id(rel, target)
    matched = _match_alias(target, aliases)
    if matched is not None:
        module, rest = matched
        target_rel = _resolve_module(module, rel, lang, module_index, java_roots)
        if target_rel is None:
            return None
        if rest:
            symbol = _lookup_in_file(rest, target_rel, file_symbols)
            if symbol:
                return symbol_id(target_rel, symbol)
        return file_node_ids[target_rel]
    return _resolve_symbol(target, rel, lang, file_symbols, qname_index, last_seg_index)


def _resolve_symbol(
    target: str,
    rel: str,
    lang: str,
    file_symbols: dict[str, set[str]],
    qname_index: dict[tuple[str, str], list[str]],
    last_seg_index: dict[tuple[str, str], list[str]],
) -> str | None:
    """Language-scoped qname/suffix resolution; prefers unique or same-file
    matches. Scoping by lang keeps same-named symbols in different languages
    (Java Greeter vs TS Greeter) from colliding.

    Calls through local variables ("app.start", "g.greet") can't be typed
    statically, so when the full target doesn't match, progressively shorter
    suffixes are tried (longest first) and the first unambiguous hit wins.
    """
    ids = qname_index.get((lang, target))
    if ids:
        if len(ids) == 1:
            return ids[0]
        same_rel = [nid for nid in ids if parse_node_id(nid)[1] == rel]
        return same_rel[0] if len(same_rel) == 1 else None
    parts = target.split(".")
    # drop=0 covers single-segment targets ("_go" -> "MainApp._go") and
    # already-exact qnames; longer drops handle calls through local variables
    # ("app.start" -> "MainApp.start").
    for drop in range(len(parts)):
        candidate = ".".join(parts[drop:])
        matches = [
            nid for nid in last_seg_index.get((lang, parts[-1]), [])
            if (qname := parse_node_id(nid)[2]) and (
                qname == candidate or qname.endswith("." + candidate)
            )
        ]
        if not matches:
            continue
        same_rel = [nid for nid in matches if parse_node_id(nid)[1] == rel]
        if len(same_rel) == 1:
            return same_rel[0]
        if len(matches) == 1:
            return matches[0]
        # ambiguous at this length — a shorter suffix may still be unique
    return None
