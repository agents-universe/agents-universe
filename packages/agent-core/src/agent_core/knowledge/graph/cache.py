"""SHA256 incremental cache for per-file parse results.

``cache/index.json`` IS the per-file result store: graph.json is re-assembled
purely from it, so an unchanged repo is never re-parsed (and a repo checked
out back and forth needs no work at all — the head_sha fast path in builder
skips even hashing).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

CACHE_VERSION = 1
_MANIFEST = "index.json"


class GraphCacheState:
    def __init__(self, version: int = CACHE_VERSION, files: dict[str, dict] | None = None):
        self.version = version
        self.files: dict[str, dict] = files if files is not None else {}

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "files": self.files}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphCacheState":
        return cls(version=data.get("version", CACHE_VERSION), files=data.get("files", {}))


class GraphCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.manifest = cache_dir / _MANIFEST

    def load(self) -> GraphCacheState:
        try:
            with open(self.manifest, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            state = GraphCacheState.from_dict(data)
            if state.version != CACHE_VERSION:
                return GraphCacheState()
            return state
        except (OSError, ValueError):
            # Missing or corrupt manifest (crash mid-write) -> full rebuild.
            _log.info("graph cache manifest missing/corrupt; rebuilding %s", self.cache_dir)
            return GraphCacheState()

    def save(self, state: GraphCacheState) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state.to_dict(), handle)
            os.replace(tmp_path, self.manifest)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def matches(self, state: GraphCacheState, rel: str, sha: str) -> bool:
        entry = state.files.get(rel)
        return entry is not None and entry.get("sha") == sha

    def get(self, state: GraphCacheState, rel: str) -> dict | None:
        return state.files.get(rel)

    def set_file(self, state: GraphCacheState, rel: str, result) -> None:
        state.files[rel] = {
            "sha": result.sha256,
            "lang": result.lang,
            "symbols": result.symbols,
            "edges": result.edges,
            "imports": result.imports,
            "stats": result.stats,
            "lines": result.lines,
        }

    def drop(self, state: GraphCacheState, rel: str) -> None:
        state.files.pop(rel, None)
