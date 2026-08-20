"""Persistent graph storage: atomic graph.json writes + in-memory cache.

The in-memory cache is keyed by file mtime/size, so queries never serve a
stale graph after a rebuild, and re-loading an unchanged file is free. Safe
under the single-threaded asyncio event loop.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .model import RepoGraph

_log = logging.getLogger(__name__)

GRAPH_FILE = "graph.json"

_GRAPH_MEMO: dict[str, tuple[int, int, RepoGraph]] = {}


def load_repo_graph(kg_dir: Path) -> RepoGraph | None:
    path = kg_dir / GRAPH_FILE
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return RepoGraph.from_dict(data)
    except (OSError, ValueError, KeyError):
        return None


def save_repo_graph(graph: RepoGraph, kg_dir: Path) -> None:
    kg_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=kg_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(graph.to_dict(), handle)
        os.replace(tmp_path, kg_dir / GRAPH_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_cached(kg_dir: Path) -> RepoGraph | None:
    """Load graph.json with a process-level mtime-keyed cache."""
    path = kg_dir / GRAPH_FILE
    try:
        stat = path.stat()
    except OSError:
        _GRAPH_MEMO.pop(str(kg_dir), None)
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    memo = _GRAPH_MEMO.get(str(kg_dir))
    if memo and (memo[0], memo[1]) == key:
        return memo[2]
    graph = load_repo_graph(kg_dir)
    if graph is None:
        _GRAPH_MEMO.pop(str(kg_dir), None)
    else:
        _GRAPH_MEMO[str(kg_dir)] = (stat.st_mtime_ns, stat.st_size, graph)
    return graph


def invalidate_cached(kg_dir: Path) -> None:
    _GRAPH_MEMO.pop(str(kg_dir), None)
