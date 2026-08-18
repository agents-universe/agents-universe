"""Filesystem tool — read/write/list files within the project scope."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

# Byte caps : reading feeds the LLM context directly (2MB is
# already huge there); writing is capped to bound disk usage.
_MAX_READ_BYTES = 2_000_000
_MAX_WRITE_BYTES = 5_000_000


def _normalize_rel_path(rel_path: str, project_fs_path: str) -> str:
    """Strip erroneous absolute or project-prefixed paths to workspace-relative form.

    Models sometimes output paths like:
      - /app/projects/slug/tests/...  (container absolute)
      - projects/slug/tests/...       (repo-relative with project prefix)
      - slug/tests/...                (bare slug prefix)
    All should resolve to just: tests/...
    """
    base = Path(project_fs_path).resolve()
    slug = base.name

    normalized = rel_path.replace("\\", "/")

    # Strip absolute path prefix matching the workspace (resolved or raw form)
    for prefix in (
        str(base).replace("\\", "/") + "/",
        project_fs_path.replace("\\", "/").rstrip("/") + "/",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            return normalized or rel_path
        stripped = normalized.lstrip("/")
        prefix_no_slash = prefix.lstrip("/")
        if stripped.startswith(prefix_no_slash):
            normalized = stripped[len(prefix_no_slash):]
            return normalized or rel_path

    normalized = normalized.lstrip("/")

    # Strip "projects/slug/" prefix — only when no real projects/{slug}/
    # subdirectory exists; the model may legitimately mean that subdir (e.g.
    # a monorepo checkout inside the workspace).
    if not (base / "projects" / slug).is_dir():
        prefix_pattern = re.compile(rf"^projects/{re.escape(slug)}/", re.IGNORECASE)
        normalized = prefix_pattern.sub("", normalized)

    # Strip bare "slug/" prefix — same guard: with a real top-level {slug}/
    # subdirectory, `web/index.html` means the subdir, not a path prefix to
    # strip (which would silently read/write/delete the WRONG file).
    if normalized.startswith(f"{slug}/") and not (base / slug).is_dir():
        normalized = normalized[len(slug) + 1:]

    return normalized or rel_path


def _read_file(target: Path, rel_path: str) -> dict[str, Any]:
    try:
        # Byte cap before reading: a huge file would OOM and blow up the LLM
        # context (mirrors api_request's 5MB cap.
        if target.stat().st_size > _MAX_READ_BYTES:
            return {"error": f"Cannot read {rel_path}: file too large ({target.stat().st_size} bytes > {_MAX_READ_BYTES})"}
        # BOM (VS Code saves) would stick to the content and break consumers
        # that expect clean markdown/text.
        content = target.read_text(encoding="utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        _log.warning("filesystem read_file: not UTF-8: %s", rel_path)
        return {"error": f"Cannot read {rel_path}: file is not valid UTF-8 (binary?)"}
    except PermissionError:
        _log.warning("filesystem read_file: permission denied: %s", rel_path)
        return {"error": f"Permission denied: {rel_path}"}
    except OSError as e:
        _log.warning("filesystem read_file: OSError on %s: %s", rel_path, e)
        return {"error": f"Cannot read {rel_path}: {e}"}
    return {"content": content, "path": rel_path, "size_bytes": len(content.encode())}


def _list_dir(target: Path, rel_path: str) -> dict[str, Any]:
    if not target.exists():
        return {"error": f"Directory not found: {rel_path}"}
    if not target.is_dir():
        return {"error": f"Not a directory: {rel_path}"}
    entries = []
    for entry in sorted(target.iterdir()):
        entries.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "size_bytes": entry.stat().st_size if entry.is_file() else 0,
        })
    return {"entries": entries, "count": len(entries)}


def _media_parts(rel_path: str) -> list[str] | None:
    """Split a .tmp/media/... path into segments, or None if it isn't one.

    User attachments are never written to disk — they live in the API's
    in-memory upload store for the duration of the turn. The filesystem
    tool serves those bytes through ToolContext's optional upload hooks.
    """
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 3 or parts[:2] != [".tmp", "media"]:
        return None
    return parts


def _upload_store_lookup(parts: list[str], context: ToolContext) -> bytes | None:
    """Bytes for a .tmp/media/{conversation}/{file} path from the store."""
    if len(parts) != 4:
        return None
    lookup = getattr(context, "upload_file_lookup", None)
    return lookup(parts[3]) if lookup else None


def _upload_store_names(parts: list[str], context: ToolContext) -> list[str]:
    """Upload filenames for a .tmp/media/{conversation} directory."""
    if len(parts) != 3:
        return []
    names = getattr(context, "upload_file_names", None)
    return sorted(names()) if names else []


class FilesystemTool(Tool):
    name = "filesystem"
    prompt_hint = (
        "Preferred tool for ALL file reads, writes, and directory listings — never use "
        "shell cat/head/tail/echo>/sed for reading or writing files. It has no content "
        "search; shell grep/find is acceptable for searching only."
    )
    description = (
        "Read, write, list, or delete files within the current project directory. "
        "All paths must be relative to the project root or knowledge directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read_file", "write_file", "list_dir", "delete_file", "create_dir"],
                "description": "The filesystem operation to perform",
            },
            "path": {
                "type": "string",
                "description": "Relative path from the project root",
            },
            "content": {
                "type": "string",
                "description": "Content to write (for write_file only)",
            },
        },
        "required": ["operation", "path"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]
        rel_path = _normalize_rel_path(params["path"], context.project_fs_path)

        # Writes into .git (hooks, config) install code git executes
        # unvalidated on later commands or let config repoint hooksPath —
        # the shell sandbox rejects these targets, the filesystem tool must
        # too (nested repos: subdir/.git/hooks, submodules:
        # .git/modules/<name>/hooks). Reads from .git stay allowed.
        if operation in ("write_file", "delete_file", "create_dir"):
            _git_rel = rel_path.replace("\\", "/")
            if ".git" in _git_rel.split("/"):
                return {"error": ".git 目录为只读：hooks/config 会被 git 在后续命令中执行，请写到项目其他位置"}

        # Framework dirs (agents/, workflows/, knowledge/_template/) are read
        # from the framework root. agents/ and workflows/ are overlay paths:
        # the project workspace shadows the global copies — reads prefer the
        # workspace, writes are routed to the workspace. knowledge/_template
        # stays strictly read-only.
        _is_framework_path = (
            rel_path.startswith("agents/")
            or rel_path.startswith("workflows/")
            or rel_path == "knowledge/_template"
            or rel_path.startswith("knowledge/_template/")
        )
        if _is_framework_path and context.framework_root:
            _is_shadowed = rel_path.startswith("agents/") or rel_path.startswith("workflows/")
            if operation in ("write_file", "delete_file", "create_dir"):
                # Shadowed writes fall through to the project-scope handling
                # below (they belong to the workspace, never the framework).
                if not _is_shadowed:
                    return {"error": f"{rel_path.split('/')[0]}/ directory is read-only"}
            else:
                if _is_shadowed:
                    # Reads prefer the project workspace copy when present.
                    ws_base = Path(context.project_fs_path).resolve()
                    ws_target = (ws_base / rel_path).resolve()
                    if ws_target.is_relative_to(ws_base) and ws_target.exists():
                        if operation == "list_dir":
                            return _list_dir(ws_target, rel_path)
                        return _read_file(ws_target, rel_path)
                if operation not in ("read_file", "list_dir"):
                    return {"error": f"{rel_path.split('/')[0]}/ directory is read-only"}
                target = (Path(context.framework_root) / rel_path).resolve()
                fw_root = Path(context.framework_root).resolve()
                if not target.is_relative_to(fw_root):
                    return {"error": f"Access denied: path {rel_path!r} is outside allowed scope"}
                if not target.exists():
                    if operation == "list_dir":
                        return {"error": f"Directory not found: {rel_path}"}
                    parent = target.parent
                    hint: dict[str, Any] = {"error": f"File not found: {rel_path}"}
                    if parent.is_dir():
                        hint["available_in_parent"] = sorted(e.name for e in parent.iterdir())[:30]
                    return hint
                if operation == "list_dir":
                    return _list_dir(target, rel_path)
                return _read_file(target, rel_path)

        # Security: resolve and validate path is within project scope
        base = Path(context.project_fs_path).resolve()
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base):
            return {"error": f"Access denied: path {rel_path!r} is outside project scope"}

        if operation == "read_file":
            parent = target.parent
            if target.exists():
                return _read_file(target, rel_path)
            # User attachments live in the in-memory upload store (never on
            # disk) — serve those bytes so the agent can re-read full content
            # when the inline preview was truncated or omitted.
            media_parts = _media_parts(rel_path)
            store_data = _upload_store_lookup(media_parts, context) if media_parts else None
            if store_data is not None:
                if len(store_data) > _MAX_READ_BYTES:
                    return {"error": f"Cannot read {rel_path}: file too large ({len(store_data)} bytes > {_MAX_READ_BYTES})"}
                try:
                    content = store_data.decode("utf-8")
                except UnicodeDecodeError:
                    _log.warning("filesystem read_file: store upload not UTF-8: %s", rel_path)
                    return {"error": f"Cannot read {rel_path}: file is not valid UTF-8 (binary?)"}
                return {"content": content, "path": rel_path, "size_bytes": len(content.encode())}
            hint: dict[str, Any] = {"error": f"File not found: {rel_path}"}
            names = []
            if parent.is_dir():
                names = sorted(e.name for e in parent.iterdir())
            if media_parts:
                names = sorted(set(names + _upload_store_names(media_parts[:-1], context)))
            if names:
                hint["available_in_parent"] = names[:30]
            return hint

        elif operation == "write_file":
            content = params.get("content", "")
            if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
                return {"error": f"Cannot write {rel_path}: content too large ({len(content)} chars > {_MAX_WRITE_BYTES} bytes)"}
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(content, encoding="utf-8")
            except PermissionError:
                _log.warning("filesystem write_file: permission denied: %s", rel_path)
                return {"error": f"Permission denied: {rel_path}"}
            except OSError as e:
                _log.warning("filesystem write_file: OSError on %s: %s", rel_path, e)
                return {"error": f"Cannot write {rel_path}: {e}"}
            return {"success": True, "path": rel_path, "bytes_written": len(content.encode())}

        elif operation == "list_dir":
            media_parts = _media_parts(rel_path)
            store_names = _upload_store_names(media_parts, context) if media_parts else []
            if not target.exists() and not store_names:
                return {"error": f"Directory not found: {rel_path}"}
            if target.exists() and not target.is_dir():
                return {"error": f"Not a directory: {rel_path}"}
            entries = []
            if target.is_dir():
                for entry in sorted(target.iterdir()):
                    entries.append({
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size_bytes": entry.stat().st_size if entry.is_file() else 0,
                    })
            if store_names:
                # Merge in-memory uploads into the media dir listing.
                lookup = getattr(context, "upload_file_lookup", None)
                for name in store_names:
                    data = lookup(name) if lookup else None
                    entries.append({
                        "name": name,
                        "type": "file",
                        "size_bytes": len(data) if data else 0,
                    })
                entries.sort(key=lambda e: e["name"])
            return {"entries": entries, "count": len(entries)}

        elif operation == "delete_file":
            if not target.exists():
                return {"error": f"File not found: {rel_path}"}
            if target.is_dir():
                return {"error": f"Not a file: {rel_path}"}
            try:
                target.unlink()
            except OSError as e:
                return {"error": f"Failed to delete {rel_path}: {e.strerror or e}"}
            return {"success": True, "deleted": rel_path}

        elif operation == "create_dir":
            target.mkdir(parents=True, exist_ok=True)
            return {"success": True, "path": rel_path}

        return {"error": f"Unknown operation: {operation}"}
