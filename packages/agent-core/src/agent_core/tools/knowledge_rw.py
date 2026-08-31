"""Knowledge read/write tool — manages Markdown knowledge files in the project.

Supports hierarchical knowledge with dynamic loading/unloading:
- read: one-time file access (content in tool result only)
- write: persist changes to disk
- load: bring a detail file into persistent dynamic context
- unload: release a dynamically loaded file
- refresh: re-read a file already in context (static or dynamic)
- status: show context state (static, dynamic, deferred, overflow)
- list: list files with status badges
- search_by_slug: partial slug matching
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

import frontmatter

from ..knowledge.loader import MAX_FILE_SIZE
from ..paths import KNOWLEDGE_SLUG_RE, PathEscapeError, resolve_within
from .base import Tool, ToolContext

_log = logging.getLogger("agent_core.knowledge_rw")

_SLUG_RE = KNOWLEDGE_SLUG_RE

# Writes are capped in UTF-8 BYTES just under loader.MAX_FILE_SIZE (512 KB) so
# a written file always loads back into context. A character-based cap would
# overrun the loader's byte limit on CJK-heavy content (3 bytes/char) and the
# file would silently fall out of context as "overflow".
_MAX_WRITE_BYTES = 500 * 1024


async def _read_knowledge_file(path: Path) -> str:
    """Read a knowledge file, enforcing the loader's 512 KB cap.

    loader.py indexes only files up to MAX_FILE_SIZE; reading a bigger file
    whole into the LLM context here would blow the context budget where the
    loader would have refused it.
    """
    stat = path.stat()
    if stat.st_size > MAX_FILE_SIZE:
        raise ValueError(
            f"Knowledge file {path.name} is {stat.st_size} bytes — above the "
            f"{MAX_FILE_SIZE} byte load limit. Split the file or lower its "
            f"knowledge_level."
        )
    # A UTF-8 BOM would stick to the content start and skew hash/parse.
    return (await asyncio.to_thread(path.read_text, "utf-8")).lstrip("\ufeff")


def _safe_resolve(slug: str, knowledge_dir: Path) -> Path | None:
    """Resolve slug to a path within knowledge_dir. Returns None on invalid
    slug format or traversal."""
    if not slug or not _SLUG_RE.match(slug):
        return None
    try:
        return resolve_within(knowledge_dir, f"{slug}.md")
    except PathEscapeError:
        return None


class KnowledgeRWTool(Tool):
    name = "knowledge_rw"
    prompt_hint = (
        "Read/write/delete project knowledge Markdown and manage what is loaded into context; "
        "writes and deletes through this tool keep the knowledge index in sync, so prefer it over "
        "filesystem for knowledge files. Before 'write', use 'list' to see the project's existing "
        "files and prefer updating an existing file — especially an empty template file with "
        "'(to be filled …)' placeholders — over creating a new slug. Use 'load' for files needed "
        "across turns, 'read' for one-off lookups."
    )
    description = (
        "Read, write, delete, load, unload, refresh, or list Markdown knowledge files. "
        "Use 'load' to bring detail files into persistent context (visible across turns). "
        "Use 'read' for one-time access. Use 'unload' to release dynamic context. "
        "Use 'refresh' to reload a file that was updated. "
        "Use 'children' to list immediate children of a knowledge file. "
        "Use 'delete' to remove a knowledge file and its database index row together. "
        "Use 'purge' to clean up database index rows whose files no longer exist "
        "(optional slug purges a single row; without slug purges all residue for the project). "
        "Slug format: '{category}/{filename-without-extension}'. "
        "Before 'write', check 'list' for an existing file that covers the content and update "
        "it instead of creating a new slug."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "write", "delete", "purge", "load", "unload", "refresh", "status", "list", "search_by_slug", "children"],
            },
            "slug": {
                "type": "string",
                "description": "Knowledge slug, e.g. 'domain/context' or 'technical/api/get-users'",
            },
            "content": {
                "type": "string",
                "description": "Full Markdown content including frontmatter (for write only)",
            },
            "change_summary": {
                "type": "string",
                "description": "Brief description of what changed (for write only)",
            },
            "category": {
                "type": "string",
                "description": "Filter by category for list operation",
            },
            "root_only": {
                "type": "boolean",
                "description": "Only list root-level files (no parent) for list operation",
            },
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]
        knowledge_dir = Path(context.knowledge_dir())

        if operation == "list":
            return await self._op_list(params, knowledge_dir, context)

        elif operation == "search_by_slug":
            return await self._op_search(params, knowledge_dir)

        elif operation == "read":
            return await self._op_read(params, knowledge_dir)

        elif operation == "write":
            return await self._op_write(params, knowledge_dir, context)

        elif operation == "load":
            return await self._op_load(params, knowledge_dir, context)

        elif operation == "unload":
            return self._op_unload(params, context)

        elif operation == "refresh":
            return await self._op_refresh(params, knowledge_dir, context)

        elif operation == "status":
            return self._op_status(context)

        elif operation == "children":
            return self._op_children(params, context)

        elif operation == "delete":
            return await self._op_delete(params, knowledge_dir, context)

        elif operation == "purge":
            return await self._op_purge(params, context)

        return {"error": f"Unknown operation: {operation}"}

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def _op_list(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        category = params.get("category")
        root_only = params.get("root_only", False)
        base = knowledge_dir.resolve()
        search_root = (knowledge_dir / category).resolve() if category else base
        if not search_root.is_relative_to(base):
            return {"error": f"Invalid category: {category!r}"}

        def _scan():
            files = []
            if not search_root.exists():
                return files
            for md_file in sorted(search_root.rglob("*.md")):
                if md_file.name.startswith("_"):
                    continue
                rel = md_file.relative_to(knowledge_dir)
                slug = str(rel.with_suffix("")).replace("\\", "/")
                try:
                    post = frontmatter.load(str(md_file))
                    title = post.metadata.get("title", md_file.stem)
                    summary = post.metadata.get("summary", "")
                    level = post.metadata.get("knowledge_level", "auto")
                    parent = post.metadata.get("parent", None)
                    children = post.metadata.get("children", [])
                except Exception:
                    title = md_file.stem
                    summary = ""
                    level = "auto"
                    parent = None
                    children = []
                files.append({
                    "slug": slug,
                    "title": title,
                    "knowledge_level": level,
                    "summary": summary,
                    "parent_slug": parent,
                    "children_count": len(children) if isinstance(children, list) else 0,
                })
            return files

        files = await asyncio.to_thread(_scan)
        if root_only:
            files = [f for f in files if not f["parent_slug"]]
        for f in files:
            f["status"] = self._get_slug_status(f["slug"], context)
        return {"files": files, "count": len(files)}

    async def _op_search(self, params: dict, knowledge_dir: Path) -> dict:
        slug = params.get("slug", "")

        def _scan():
            results = []
            if not knowledge_dir.exists():
                return results
            for md_file in knowledge_dir.rglob("*.md"):
                rel_slug = str(md_file.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
                if slug.lower() in rel_slug.lower():
                    results.append(rel_slug)
            return results

        results = await asyncio.to_thread(_scan)
        return {"matches": results}

    async def _op_read(self, params: dict, knowledge_dir: Path) -> dict:
        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for read operation"}
        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}"}
        import logging
        _log = logging.getLogger("agent_core.knowledge_rw")
        # Log the slug only — avoid leaking host filesystem structure into logs
        _log.info("knowledge_rw read: slug=%s", slug)
        if not await asyncio.to_thread(file_path.exists):
            kdir_exists = await asyncio.to_thread(knowledge_dir.exists)
            suggestions = []
            if kdir_exists:
                for md in knowledge_dir.rglob("*.md"):
                    rel = str(md.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
                    if rel == slug or rel.endswith(f"/{slug}"):
                        suggestions.append(rel)
            err: dict = {
                "error": f"Knowledge file not found: {slug}",
                "knowledge_dir_exists": kdir_exists,
            }
            if not kdir_exists:
                err["hint"] = (
                    f"Knowledge directory does not exist: {knowledge_dir}. "
                    f"Check that PROJECTS_ROOT is configured correctly and the "
                    f"project workspace was initialized on disk."
                )
            if suggestions:
                err["did_you_mean"] = suggestions
            return err
        try:
            content = await _read_knowledge_file(file_path)
        except ValueError as e:
            return {"error": str(e)}
        post = frontmatter.loads(content)
        return {
            "slug": slug,
            "content": content,
            "metadata": dict(post.metadata),
            "word_count": len(post.content.split()),
        }

    async def _op_write(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        slug = params.get("slug")
        content = params.get("content", "")
        change_summary = params.get("change_summary", "Updated")

        if not slug:
            return {"error": "slug is required for write operation"}
        if not _SLUG_RE.match(slug):
            return {"error": f"Invalid slug format: {slug!r}. Use path-safe segments (letters, digits, '-', '_', '.') joined by slashes."}
        # loader.MAX_FILE_SIZE (512 KB) silently drops oversized
        # files from context — cap writes (in UTF-8 bytes) below that so what
        # the agent writes is always loadable again.
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > _MAX_WRITE_BYTES:
            return {"error": (
                f"Content is too large ({content_bytes} bytes > {_MAX_WRITE_BYTES}). "
                "Split it into smaller files (e.g. an index plus detail files "
                "with knowledge_level: detail)."
            )}

        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            # _safe_resolve also rejects a slug whose file already exists as
            # a symlink pointing outside the knowledge dir (resolve_within
            # realpaths) — writing there would overwrite an external file.
            return {"error": f"Invalid or escaping slug: {slug!r}"}

        # A slug is "new" if its file does not exist on disk yet — the DB index
        # row is only created by reindex_one after this write, so the disk is
        # the authoritative check for "did the project already have this file".
        is_new_file = not await asyncio.to_thread(file_path.exists)

        def _do_write() -> dict:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            if file_path.exists():
                existing_hash = hashlib.sha256(file_path.read_text(encoding="utf-8").encode()).hexdigest()
                if existing_hash == content_hash:
                    return {"success": True, "slug": slug, "changed": False, "reason": "content identical",
                            "content_hash": content_hash}
            file_path.write_text(content, encoding="utf-8")
            return {
                "success": True,
                "slug": slug,
                "path": str(file_path),
                "word_count": len(content.split()),
                "content_hash": content_hash,
                "change_summary": change_summary,
                "changed": True,
            }

        result = await asyncio.to_thread(_do_write)
        if result.get("changed"):
            # Update knowledge_metadata DB so completeness scores reflect the new content.
            reindexed = False
            if context.db_session is not None:
                try:
                    from agent_core.knowledge.index import reindex_one
                    await reindex_one(
                        fs_path=str(file_path),
                        project_id=context.project_id,
                        db_session=context.db_session,
                    )
                    reindexed = True
                except Exception:
                    _log.warning("Knowledge reindex failed for slug=%s project=%s", slug, context.project_id, exc_info=True)
            # Full invalidate: the cached entries list is what feeds
            # deferred_entries/status on the next conversation, and
            # invalidate_slug only evicts content (which is never populated)
            # — a reindexed file would otherwise keep serving stale metadata
            # (title/summary/word_count) until process restart. Mirrors _op_delete.
            if reindexed and context.knowledge_cache is not None:
                context.knowledge_cache.invalidate(context.project_id)
            # Notify the frontend so the knowledge progress bar updates in real time.
            if context.session is not None:
                try:
                    await context.session.emit("knowledge_updated", slug=slug)
                except Exception:
                    _log.debug("knowledge_updated emit failed for slug=%s", slug, exc_info=True)

            # Warn if a primary API file is too large without detail children
            result = self._check_split_warning(slug, content, knowledge_dir, result)

            # Soft-nudge "fill existing files first" when the write created a
            # brand-new slug. Advisory only — the file stays written and indexed.
            if is_new_file:
                result = self._check_placement_note(slug, content, knowledge_dir, result, context)

        return result

    async def _op_delete(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        """Delete a knowledge file and its DB metadata row together."""
        if context.project_id is None:
            return {"error": "No project context — refusing to delete knowledge"}
        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for delete operation"}
        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}"}

        # Remove the file; a missing file is not an error — the DB row may
        # still be stale and needs cleaning up below.
        file_existed = await asyncio.to_thread(file_path.exists)
        if file_existed:
            await asyncio.to_thread(file_path.unlink)

        # Best-effort DB row deletion (never blocks success of the file delete).
        db_action = "skipped"
        if context.db_session is not None:
            try:
                from agent_core.knowledge.index import delete_one

                result = await delete_one(slug, context.project_id, context.db_session)
                db_action = result.get("action", "error")
            except Exception:
                _log.warning("Knowledge delete failed for slug=%s project=%s", slug, context.project_id, exc_info=True)

        # Drop the entry from the in-memory conversation context so
        # status/children/deferred reflect the removal immediately.
        ctx = context.project_context
        if ctx is not None:
            ctx.deferred_entries.pop(slug, None)
            ctx.loaded_content.pop(slug, None)
            ctx.overflow_slugs = [s for s in ctx.overflow_slugs if s != slug]
            ctx.overflow_entries.pop(slug, None)
            from agent_core.knowledge.loader import unload_dynamic_entry

            unload_dynamic_entry(ctx, slug)

        # Full invalidate: invalidate_slug only evicts cached content, while
        # the stale entries list is what feeds deferred_entries next time.
        if context.knowledge_cache is not None:
            context.knowledge_cache.invalidate(context.project_id)

        if context.session is not None:
            try:
                await context.session.emit("knowledge_updated", slug=slug)
            except Exception:
                _log.debug("knowledge_updated emit failed for slug=%s", slug, exc_info=True)

        return {"success": True, "slug": slug, "file_deleted": file_existed, "db_row": db_action}

    async def _op_purge(self, params: dict, context: ToolContext) -> dict:
        """Delete stale knowledge_metadata rows only (no file changes).

        With slug: delete that single DB row (file state irrelevant). Without
        slug: purge every row in the project whose file no longer exists on
        disk — cleans up residue from earlier file deletions.
        """
        if context.project_id is None:
            return {"error": "No project context — refusing to purge knowledge"}
        if context.db_session is None:
            return {"error": "No database session available"}

        slug = params.get("slug")
        try:
            if slug:
                if not _SLUG_RE.match(slug):
                    return {"error": f"Invalid slug: {slug!r}"}
                from agent_core.knowledge.index import delete_one

                result = await delete_one(slug, context.project_id, context.db_session)
                deleted = result.get("action") == "deleted"
            else:
                from agent_core.knowledge.index import purge_residue

                result = await purge_residue(context.project_id, context.db_session)
                deleted = result.get("count", 0) > 0
        except Exception:
            _log.warning("Knowledge purge failed for project=%s slug=%s", context.project_id, slug, exc_info=True)
            return {"error": f"Purge failed: {slug or 'all'}"}

        if deleted:
            if context.knowledge_cache is not None:
                context.knowledge_cache.invalidate(context.project_id)
            if context.session is not None:
                try:
                    await context.session.emit("knowledge_updated", slug=slug or "")
                except Exception:
                    _log.debug("knowledge_updated emit failed after purge project=%s", context.project_id, exc_info=True)

        return result

    async def _op_load(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        from agent_core.knowledge.loader import load_dynamic_entry

        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for load operation"}

        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}"}

        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}

        # Already in static context
        if slug in ctx.loaded_content:
            return {"status": "already_loaded", "slug": slug, "source": "static"}

        # Already dynamically loaded
        if slug in ctx.dynamically_loaded:
            return {"status": "already_loaded", "slug": slug, "source": "dynamic"}
        if not await asyncio.to_thread(file_path.exists):
            # Global (project_id NULL) detail entries live in the framework
            # knowledge dir, not the project dir — the deferred list promises
            # them via load, so fall back there before giving up.
            fallback = None
            if context.framework_root and ctx.deferred_entries.get(slug) is not None:
                entry = ctx.deferred_entries[slug]
                if entry.project_id is None:
                    fw_candidate = Path(context.framework_root) / "knowledge" / f"{slug}.md"
                    if await asyncio.to_thread(fw_candidate.exists):
                        fallback = fw_candidate
            if fallback is None:
                kdir_exists = await asyncio.to_thread(knowledge_dir.exists)
                err: dict = {
                    "error": f"Knowledge file not found: {slug}",
                    "knowledge_dir_exists": kdir_exists,
                }
                if not kdir_exists:
                    err["hint"] = (
                        f"Knowledge directory does not exist: {knowledge_dir}. "
                        f"Check PROJECTS_ROOT configuration."
                    )
                return err
            file_path = fallback

        try:
            content = await _read_knowledge_file(file_path)
        except ValueError as e:
            return {"error": str(e)}

        # Load into dynamic context
        load_dynamic_entry(
            ctx, slug, content,
            context.current_turn,
            task_id=context.current_task_id,
        )

        return {
            "status": "loaded",
            "slug": slug,
            "bound_to_task": context.current_task_id,
        }

    def _op_unload(self, params: dict, context: ToolContext) -> dict:
        from agent_core.knowledge.loader import unload_dynamic_entry

        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for unload operation"}

        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}

        removed = unload_dynamic_entry(ctx, slug)
        if not removed:
            return {"status": "not_loaded", "slug": slug}
        return {"status": "unloaded", "slug": slug}

    async def _op_refresh(self, params: dict, knowledge_dir: Path, context: ToolContext) -> dict:
        """Re-read a file already in context (static or dynamic) to pick up changes."""
        from agent_core.knowledge.loader import refresh_dynamic_entry, update_context_file

        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for refresh operation"}

        file_path = _safe_resolve(slug, knowledge_dir)
        if file_path is None:
            return {"error": f"Invalid slug: {slug!r}"}

        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}
        if not await asyncio.to_thread(file_path.exists):
            kdir_exists = await asyncio.to_thread(knowledge_dir.exists)
            err: dict = {
                "error": f"Knowledge file not found: {slug}",
                "knowledge_dir_exists": kdir_exists,
            }
            if not kdir_exists:
                err["hint"] = (
                    f"Knowledge directory does not exist: {knowledge_dir}. "
                    f"Check PROJECTS_ROOT configuration."
                )
            return err

        try:
            new_content = await _read_knowledge_file(file_path)
        except ValueError as e:
            return {"error": str(e)}

        if slug in ctx.dynamically_loaded:
            refresh_dynamic_entry(ctx, slug, new_content)
            return {"status": "refreshed", "slug": slug, "source": "dynamic"}
        elif slug in ctx.loaded_content:
            update_context_file(ctx, slug, new_content)
            return {"status": "refreshed", "slug": slug, "source": "static"}
        else:
            return {"status": "not_loaded", "slug": slug, "hint": "Use 'load' to bring this file into context first."}

    def _op_status(self, context: ToolContext) -> dict:
        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}

        return {
            "static_loaded": len(ctx.loaded_content),
            "dynamically_loaded": [
                {"slug": s, "bound_to_task": r.task_id}
                for s, r in ctx.dynamic_records.items()
                if s in ctx.dynamically_loaded
            ],
            "dynamic_count": len(ctx.dynamically_loaded),
            "deferred_available": [
                {"slug": e.slug, "summary": e.summary, "parent": e.parent_slug, "depth": e.depth}
                for e in ctx.deferred_entries.values()
            ],
            "overflow": ctx.overflow_slugs,
        }

    def _op_children(self, params: dict, context: ToolContext) -> dict:
        slug = params.get("slug")
        if not slug:
            return {"error": "slug is required for children operation"}

        ctx = context.project_context
        if ctx is None:
            return {"error": "No project context available"}

        children = []
        all_entries = list(ctx.deferred_entries.values()) + ctx.loaded_entries
        for entry in all_entries:
            if entry.parent_slug == slug:
                children.append({
                    "slug": entry.slug,
                    "title": entry.title,
                    "summary": entry.summary,
                    "depth": entry.depth,
                    "has_children": bool(entry.children_slugs),
                })
        return {"parent_slug": slug, "children": children, "count": len(children)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    _SPLIT_REQUIRED_SLUGS = ("technical/api-map", "technical/kong-map")
    _SPLIT_WORD_THRESHOLD = 2000
    _SPLIT_CHILD_DIRS = {
        "technical/api-map": "technical/api",
        "technical/kong-map": "technical/kong",
    }

    def _check_split_warning(self, slug: str, content: str, knowledge_dir: Path, result: dict) -> dict:
        """Append a warning if a primary API file exceeds word threshold without detail children."""
        if slug not in self._SPLIT_REQUIRED_SLUGS:
            return result
        word_count = len(content.split())
        if word_count < self._SPLIT_WORD_THRESHOLD:
            return result

        child_dir = knowledge_dir / self._SPLIT_CHILD_DIRS[slug]
        has_children = child_dir.exists() and any(child_dir.glob("*.md"))
        if has_children:
            return result

        result["warning"] = (
            f"MANDATORY SPLIT REQUIRED: '{slug}' has {word_count} words but NO detail child files exist. "
            f"You MUST create per-service detail files under '{self._SPLIT_CHILD_DIRS[slug]}/' with "
            f"knowledge_level: detail and parent: \"{slug}\". "
            f"The task is NOT complete until detail files are created. "
            f"See knowledge-manager skill 'API Documentation: Mandatory Two-Level Structure' for format."
        )
        return result

    _PLACEMENT_NOTE_EXEMPT_PREFIXES = ("technical/api/", "technical/kong/")
    _NOTE_SLUG_LIMIT = 30

    def _check_placement_note(
        self, slug: str, content: str, knowledge_dir: Path, result: dict, context: ToolContext
    ) -> dict:
        """Append an advisory note when the write created a brand-new slug.

        Soft-nudges "fill existing files first" without blocking legitimate new
        files: the mandatory two-level API/Kong detail files and hierarchy
        children (frontmatter `parent`) are exempt.
        """
        if slug.startswith(self._PLACEMENT_NOTE_EXEMPT_PREFIXES):
            return result
        try:
            meta = frontmatter.loads(content).metadata
        except Exception:
            meta = {}
        if meta.get("parent"):  # legitimate hierarchy child of an existing index
            return result
        existing = self._existing_slugs(slug, knowledge_dir, context)
        if not existing:
            return result
        result["note"] = (
            f"Created NEW knowledge file '{slug}'. The project already has these knowledge files: "
            + ", ".join(f"'{s}'" for s in existing)
            + ". If this content fits one of them (including empty template files with "
            "'(to be filled …)' placeholders), merge it into that file and delete this new one "
            'with knowledge_rw(operation="delete", slug="' + slug + '"). Create a new file only '
            "when no existing file covers the content."
        )
        return result

    def _existing_slugs(self, written_slug: str, knowledge_dir: Path, context: ToolContext) -> list[str]:
        """Slugs the agent should have considered before creating a new file.

        Prefers in-context entries (exactly what the prompt shows under
        '## Project Knowledge'/'Additional Knowledge'), falls back to a disk
        scan. Excludes the file just written.
        """
        ctx = context.project_context
        if ctx is not None:
            slugs = set(ctx.loaded_content) | set(ctx.overflow_slugs) | set(ctx.deferred_entries)
            slugs.discard(written_slug)
            if slugs:
                return sorted(slugs)[: self._NOTE_SLUG_LIMIT]
        if not knowledge_dir.exists():
            return []
        return sorted(
            str(p.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
            for p in knowledge_dir.rglob("*.md")
            if not any(part.startswith("_") for part in p.relative_to(knowledge_dir).parts)
            and str(p.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/") != written_slug
        )[: self._NOTE_SLUG_LIMIT]

    def _get_slug_status(self, slug: str, context: ToolContext) -> str:
        ctx = context.project_context
        if ctx is None:
            return "unknown"
        if slug in ctx.loaded_content:
            return "loaded"
        if slug in ctx.dynamically_loaded:
            return "dynamic"
        if slug in ctx.deferred_entries:
            return "deferred"
        if slug in ctx.overflow_slugs:
            return "overflow"
        return "unindexed"
