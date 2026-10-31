"""Filesystem tool — read/write/list files within the project scope."""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..definition_check import check_definition, project_slug_from_workspace
from ..paths import canonical
from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

# Byte caps : reading feeds the LLM context directly (2MB is
# already huge there); writing is capped to bound disk usage.
_MAX_READ_BYTES = 2_000_000
_MAX_WRITE_BYTES = 5_000_000

# tree operation: build/scratch directories are hidden from the overview
# (still reachable via list_dir), temp work trees are shown but not
# recursed, and depth/entry caps bound the cost of huge repos/ checkouts.
_TREE_NOISE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".cache",
}
_TREE_NO_DESCEND = {".tmp/work"}
_MAX_TREE_DEPTH = 4
_MAX_TREE_ENTRIES = 200


def _definition_check(
    rel_path: str, content: str, context: ToolContext, scope: str
) -> dict[str, Any] | None:
    """Advisory validation block for a definition file, or None.

    Registration of agents/skills/workflows happens later and fails silently,
    so the writer is the only party that can still fix the file — it gets the
    verdict with the write result instead of having to guess.
    """
    return check_definition(
        rel_path,
        content,
        scope=scope,
        project_slug=project_slug_from_workspace(context.project_fs_path) if scope == "project" else None,
        project_fs_path=context.project_fs_path or None,
        framework_root=context.framework_root or None,
    )


def _attach_definition_check(
    result: dict[str, Any], rel_path: str, context: ToolContext, scope: str
) -> dict[str, Any]:
    """Attach the advisory block to a read result; reads stay unmodified."""
    content = result.get("content")
    if isinstance(content, str):
        check = _definition_check(rel_path, content, context, scope)
        if check is not None:
            result["definition_check"] = check
    return result


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

    # Strip an absolute prefix matching the workspace. Prefix candidates
    # include the canonical form (realpath+normcase) so a drive-letter-case
    # or 8.3-short-name variant of the same directory still matches;
    # comparison is case-insensitive and slices the original-cased string.
    # The leading slash is dropped first so "/e:/..."-shaped input reaches
    # the comparison at all.
    stripped = normalized.lstrip("/")
    for prefix in (
        canonical(project_fs_path).as_posix(),
        str(base).replace("\\", "/"),
        project_fs_path.replace("\\", "/"),
    ):
        prefix_no_slash = prefix.rstrip("/").lstrip("/")
        if not prefix_no_slash:
            continue
        if stripped.lower().startswith(prefix_no_slash.lower() + "/"):
            normalized = stripped[len(prefix_no_slash) + 1:]
            return normalized or rel_path
    normalized = stripped

    # Strip "projects/slug/" prefix — only when no real projects/{slug}/
    # subdirectory exists; the model may legitimately mean that subdir (e.g.
    # a monorepo checkout inside the workspace).
    if not (base / "projects" / slug).is_dir():
        prefix_pattern = re.compile(rf"^projects/{re.escape(slug)}/", re.IGNORECASE)
        normalized = prefix_pattern.sub("", normalized)

    # Strip bare "slug/" prefix — same guard: with a real top-level {slug}/
    # subdirectory, `web/index.html` means the subdir, not a path prefix to
    # strip (which would silently read/write/delete the WRONG file).
    if normalized.lower().startswith(f"{slug.lower()}/") and not (base / slug).is_dir():
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


def _not_found_hint(
    rel_path: str,
    target: Path,
    scope_root: Path,
    scope: str,
    extra_names: list[str] | None = None,
    label: str = "File",
) -> dict[str, Any]:
    """Error payload for a missing path, with self-correction aids.

    Walks up from the target's parent to the deepest directory that exists —
    a typo in a middle segment then still gets listing context — and offers
    case-insensitive / close-name candidates for the first missing segment
    (one scandir of that single directory, never a workspace-wide walk).
    Mirrors knowledge_rw's did_you_mean pattern so both tools teach the model
    the same way to recover. Host absolute paths are never included; the
    system prompt already names the workspace root.
    """
    hint: dict[str, Any] = {"error": f"{label} not found: {rel_path}", "scope": scope}

    closest: Path | None = None
    probe = target.parent
    while True:
        if probe.is_dir():
            closest = probe
            break
        if probe == scope_root or probe.parent == probe:
            break
        probe = probe.parent

    if closest is None:
        if extra_names:
            hint["available_in_parent"] = sorted(extra_names)[:30]
        return hint

    try:
        closest_rel = closest.relative_to(scope_root).as_posix() or "."
    except ValueError:
        closest_rel = "."
    hint["closest_existing_dir"] = closest_rel

    names: list[str] = []
    try:
        for i, entry in enumerate(closest.iterdir()):
            if i >= 1000:  # absurdly large dirs: listing is an aid, not an audit
                break
            names.append(entry.name)
    except OSError:
        names = []
    names.sort()

    all_names = sorted(set(names + (extra_names or [])))
    if all_names:
        hint["available_in_parent"] = all_names[:30]

    missing_tail = target.relative_to(closest).parts if target.is_relative_to(closest) else ()
    if missing_tail and names:
        first_missing = missing_tail[0]
        matches = [n for n in names if n.lower() == first_missing.lower()]
        if not matches:
            matches = difflib.get_close_matches(first_missing, names, n=5, cutoff=0.6)
        if matches:
            base_parts = [] if closest_rel == "." else closest_rel.split("/")
            suggestions: list[str] = []
            for m in matches[:5]:
                # Case-correct every tail segment the disk can confirm, not
                # just the first: "reports/summary.md" should suggest
                # "Reports/Summary.md", not "Reports/summary.md". Each step
                # descends only into a directory that really exists; from the
                # first non-existent (or non-directory) level on, the tail is
                # unverifiable and passes through unchanged.
                parts = [*base_parts, m]
                cursor = closest / m
                verified = True
                for seg in missing_tail[1:]:
                    real: str | None = None
                    if verified and cursor.is_dir():
                        try:
                            real = next(
                                (e.name for e in cursor.iterdir() if e.name.lower() == seg.lower()),
                                None,
                            )
                        except OSError:
                            real = None
                    if real is not None:
                        parts.append(real)
                        cursor = cursor / real
                    else:
                        # Unverifiable from here on: pass the rest through
                        # unchanged so the suggestion keeps the full tail.
                        parts.append(seg)
                        verified = False
                suggestions.append("/".join(parts))
            hint["did_you_mean"] = suggestions
    return hint


def _tree(target: Path, rel_path: str, depth: int) -> dict[str, Any]:
    """Indented text overview of the directory tree under *target*.

    Disk-only: in-memory uploads under .tmp/media are not merged — tree is an
    orientation aid, list_dir keeps the per-directory listing contract.
    """
    lines: list[str] = []
    count = 0
    truncated = False

    root_label = "." if rel_path in (".", "") else rel_path.rstrip("/") + "/"
    lines.append(root_label)

    def _walk(dir_path: Path, dir_rel: str, level: int) -> None:
        nonlocal count, truncated
        if truncated:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda e: e.name.lower())
        except OSError:
            return
        for entry in entries:
            if entry.name in _TREE_NOISE:
                continue
            if count >= _MAX_TREE_ENTRIES:
                truncated = True
                return
            is_dir = entry.is_dir()
            entry_rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
            lines.append("  " * level + entry.name + ("/" if is_dir else ""))
            count += 1
            if is_dir and level + 1 < depth and entry_rel not in _TREE_NO_DESCEND:
                _walk(entry, entry_rel, level + 1)

    _walk(target, "" if rel_path in (".", "") else rel_path.rstrip("/"), 0)
    if truncated:
        lines.append(
            f"… (truncated at {_MAX_TREE_ENTRIES} entries — use list_dir to go deeper)"
        )
    return {
        "path": rel_path,
        "tree": "\n".join(lines),
        "entry_count": count,
        "truncated": truncated,
    }


class FilesystemTool(Tool):
    name = "filesystem"
    prompt_hint = (
        "Preferred tool for ALL file reads, writes, and directory listings — never use "
        "shell cat/head/tail/echo>/sed for reading or writing files. If you are unsure "
        "where a file lives, call operation 'tree' (workspace overview) or 'list_dir' "
        "before reading — never guess file paths. It has no content "
        "search; shell grep/find is acceptable for searching only. Writing or reading an "
        "agent/skill/workflow definition returns a `definition_check` block: when `ok` is "
        "false the file will NOT be registered — fix the reported errors and rewrite the "
        "file before reporting the task as done."
    )
    description = (
        "Read, write, list, tree, or delete files within the current project workspace. "
        "All paths must be relative to the workspace root (e.g. 'reports/summary.md'); "
        "knowledge lives at knowledge/ inside the workspace — there is no separate "
        "knowledge-directory scope. Paths under agents/, workflows/, and "
        "knowledge/_template/ are additionally served read-only from the framework "
        "directory when the workspace copy is missing. "
        "Reads and writes of definition files (agents/*.agent.md, skills/*.md, "
        "workflows/*.workflow.md) also return an advisory `definition_check` block."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read_file", "write_file", "list_dir", "tree", "delete_file", "create_dir"],
                "description": "The filesystem operation to perform",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative path, e.g. 'reports/summary.md'; use '.' for the workspace root",
            },
            "content": {
                "type": "string",
                "description": "Content to write (for write_file only)",
            },
            "depth": {
                "type": "integer",
                "description": "tree only: recursion depth (default 2, max 4)",
                "minimum": 1,
                "maximum": 4,
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
            # Case-insensitive: on Windows/macOS `.GIT/hooks/x` IS the same
            # directory as `.git/hooks/x`, so an exact-match check let the
            # guard be bypassed by casing.
            if any(part.lower() == ".git" for part in _git_rel.split("/")):
                return {"error": ".git 目录为只读：hooks/config 会被 git 在后续命令中执行，请写到项目其他位置"}

        # Framework dirs (agents/, workflows/, knowledge/_template/) are read
        # from the framework root. agents/ and workflows/ are overlay paths:
        # the project workspace shadows the global copies — reads prefer the
        # workspace, writes are routed to the workspace. knowledge/_template
        # stays strictly read-only. tree bypasses the overlay entirely: it is
        # project-scope only and must never enumerate the framework tree.
        _is_framework_path = (
            rel_path.startswith("agents/")
            or rel_path.startswith("workflows/")
            or rel_path == "knowledge/_template"
            or rel_path.startswith("knowledge/_template/")
        )
        if _is_framework_path and context.framework_root and operation != "tree":
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
                        result = _attach_definition_check(
                            _read_file(ws_target, rel_path), rel_path, context, "project"
                        )
                        # Which copy served the read matters: a model asking
                        # for a workspace file must be able to tell it got
                        # the framework's global file instead.
                        result["source"] = "workspace"
                        return result
                if operation not in ("read_file", "list_dir"):
                    return {"error": f"{rel_path.split('/')[0]}/ directory is read-only"}
                target = (Path(context.framework_root) / rel_path).resolve()
                fw_root = Path(context.framework_root).resolve()
                # Containment must be against the framework DIRECTORY the
                # path names, not framework_root itself: with PROJECTS_ROOT
                # nested under it, `agents/../projects/other-slug/.env`
                # resolved inside framework_root and read a sibling
                # workspace, defeating the project isolation boundary.
                if rel_path.startswith("knowledge/_template"):
                    scope_root = fw_root / "knowledge" / "_template"
                else:
                    scope_root = fw_root / rel_path.split("/", 1)[0]
                if not target.is_relative_to(scope_root):
                    return {"error": f"Access denied: path {rel_path!r} is outside allowed scope"}
                if not target.exists():
                    return _not_found_hint(
                        rel_path, target, scope_root, "framework",
                        label="Directory" if operation == "list_dir" else "File",
                    )
                if operation == "list_dir":
                    return _list_dir(target, rel_path)
                result = _attach_definition_check(
                    _read_file(target, rel_path), rel_path, context, "global"
                )
                result["source"] = "framework"
                return result

        # Security: resolve and validate path is within project scope
        base = Path(context.project_fs_path).resolve()
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base):
            return {"error": f"Access denied: path {rel_path!r} is outside project scope"}
        # The raw-path .git guard above is bypassed by a workspace symlink
        # (hooks -> .git/hooks, preserved by some clones): the literal path
        # has no .git component, but resolve() follows the link into .git —
        # re-check the RESOLVED components or the write lands in .git/hooks.
        if operation in ("write_file", "delete_file", "create_dir"):
            _git_resolved = target.relative_to(base).parts
            if any(part.lower() == ".git" for part in _git_resolved):
                return {"error": ".git 目录为只读：hooks/config 会被 git 在后续命令中执行，请写到项目其他位置"}

        if operation == "read_file":
            if target.exists():
                return _attach_definition_check(
                    _read_file(target, rel_path), rel_path, context, "project"
                )
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
            return _not_found_hint(
                rel_path, target, base, "project",
                extra_names=_upload_store_names(media_parts[:-1], context) if media_parts else None,
            )

        elif operation == "write_file":
            # `null` becomes "" (the key is present so the default never fires
            # and encode() would raise); other non-strings are JSON-serialized.
            # `or ""` would have silently truncated the file to empty for
            # falsy non-strings (0, {}, [], False) — reported as success.
            raw_content = params.get("content")
            if raw_content is None:
                content = ""
            elif isinstance(raw_content, str):
                content = raw_content
            else:
                try:
                    content = json.dumps(raw_content, ensure_ascii=False)
                except (TypeError, ValueError):
                    return {"error": f"Cannot write {rel_path}: content must be a string or JSON-serializable value"}
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
            result: dict[str, Any] = {
                "success": True,
                "path": rel_path,
                "bytes_written": len(content.encode()),
            }
            # The file is on disk either way; a failing check means it will not
            # register, so the caller must rewrite rather than report success.
            check = _definition_check(rel_path, content, context, "project")
            if check is not None:
                result["definition_check"] = check
            return result

        elif operation == "list_dir":
            media_parts = _media_parts(rel_path)
            store_names = _upload_store_names(media_parts, context) if media_parts else []
            if not target.exists() and not store_names:
                return _not_found_hint(rel_path, target, base, "project", label="Directory")
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

        elif operation == "tree":
            if not target.exists():
                return _not_found_hint(rel_path, target, base, "project", label="Directory")
            if not target.is_dir():
                return {"error": f"Not a directory: {rel_path}"}
            try:
                depth = int(params.get("depth") or 2)
            except (TypeError, ValueError):
                depth = 2
            depth = max(1, min(depth, _MAX_TREE_DEPTH))
            return _tree(target, rel_path, depth)

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
            # mkdir(exist_ok=True) is a no-op for a directory but raises
            # FileExistsError when the path is an existing regular file.
            if target.is_file():
                return {"error": f"Cannot create directory {rel_path}: a file with that name already exists"}
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"error": f"Cannot create directory {rel_path}: {e}"}
            return {"success": True, "path": rel_path}

        return {"error": f"Unknown operation: {operation}"}
