"""Project knowledge loader — two-tier loading model.

Knowledge files are partitioned into two tiers:
- Primary (all files without knowledge_level: detail): loaded directly from disk
  at project start. Not indexed in the database.
- Detail (knowledge_level: detail): indexed in DB with metadata + summary only.
  Content is NOT loaded automatically — agent loads on demand via knowledge_rw load.

Dynamic loading allows the agent to bring detail files into context mid-conversation.
Unloading happens when the associated task completes or on explicit unload.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

from agent_core.paths import is_within

MAX_FILE_SIZE = 512 * 1024  # 512 KB per file

_CROSS_REF_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _is_log_role(content: str) -> bool:
    try:
        return frontmatter.loads(content).metadata.get("knowledge_role") == "log"
    except Exception:
        return False


CATEGORY_PRIORITY = ["domain", "technical", "skills", "system"]

_log = logging.getLogger("agent_core.knowledge")


@dataclass
class KnowledgeEntry:
    knowledge_id: str
    slug: str
    title: str
    fs_path: str
    category: str
    cross_references: list[str]
    word_count: int
    knowledge_level: str = "auto"
    parent_slug: str | None = None
    children_slugs: list[str] = field(default_factory=list)
    summary: str = ""
    depth: int = 0
    project_id: str | None = None  # None = global (framework) row


@dataclass
class DynamicLoadRecord:
    slug: str
    loaded_at_turn: int
    task_id: str | None  # associated task; released when task completes


@dataclass
class KnowledgeContextResult:
    loaded_entries: list[KnowledgeEntry] = field(default_factory=list)
    loaded_content: dict[str, str] = field(default_factory=dict)  # slug -> content (static)
    overflow_slugs: list[str] = field(default_factory=list)  # files too large to load
    overflow_entries: dict[str, KnowledgeEntry] = field(default_factory=dict)  # metadata for overflow files
    # Hierarchy additions
    deferred_entries: dict[str, KnowledgeEntry] = field(default_factory=dict)  # detail files
    dynamically_loaded: dict[str, str] = field(default_factory=dict)  # slug -> content (dynamic)
    dynamic_records: dict[str, DynamicLoadRecord] = field(default_factory=dict)


def _category_sort_key(category: str) -> int:
    try:
        return CATEGORY_PRIORITY.index(category)
    except ValueError:
        return len(CATEGORY_PRIORITY)


def _matches_filter(slug: str, category: str, filters: list[str]) -> bool:
    """Return True if the entry matches any of the filter patterns."""
    for f in filters:
        if f.endswith("/*"):
            prefix = f[:-2]
            if category == prefix or slug.startswith(f"{prefix}/"):
                return True
        elif f == slug or f == category:
            return True
    return False


async def load_project_context(
    project_id: str,
    db_session: Any,
    cache: Any,
    knowledge_filter: list[str] | None = None,
    knowledge_dir: str | Path | None = None,
    framework_knowledge_dir: str | Path | None = None,
) -> KnowledgeContextResult:
    """Load project knowledge using a two-tier model.

    Tier 1 — Primary files: read directly from disk (knowledge_dir).
        All .md files in the knowledge directory that do NOT have
        knowledge_level: detail in frontmatter are loaded in full.

    Tier 2 — Detail files: indexed in DB, only metadata+summary exposed.
        Agent loads content on demand via knowledge_rw load.

    framework_knowledge_dir is the framework's global knowledge directory;
    DB-backed reads are verified against it (global rows) or knowledge_dir
    (project rows) before touching disk.
    """
    result = KnowledgeContextResult()

    # --- Tier 1: Load primary files directly from disk ---
    if knowledge_dir:
        kdir = Path(knowledge_dir)
        if kdir.exists():
            primary_entries, overflow_entries = await _load_primary_from_disk(
                kdir, knowledge_filter, project_id=project_id
            )
            for entry, content in primary_entries:
                result.loaded_entries.append(entry)
                result.loaded_content[entry.slug] = content
            # oversized primary files were silently dropped (the
            # _scan `continue`) — the agent never learned they existed, so it
            # could not read them on demand. Register them like the DB-backed
            # compatibility path below does, so the system prompt / status
            # surface the entries as overflow.
            for entry in overflow_entries:
                result.overflow_slugs.append(entry.slug)
                result.overflow_entries[entry.slug] = entry

    # --- Tier 2: Detail files from DB (deferred, on-demand) ---
    cached = await cache.get_or_load(project_id, db_session)
    entries = cached.entries

    if knowledge_filter:
        entries = [e for e in entries if _matches_filter(e.slug, e.category, knowledge_filter)]

    for entry in entries:
        # Current indexes contain detail files only. Keep this compatibility path
        # for callers/tests backed by older caches that still contain primary rows.
        if knowledge_dir is None and entry.knowledge_level != "detail":
            content = cached.content.get(entry.slug)
            if content is None and entry.slug not in cached.content:
                content = await _try_read_file(
                    entry,
                    framework_knowledge_dir=framework_knowledge_dir,
                    project_knowledge_dir=knowledge_dir,
                )
            if content is not None:
                result.loaded_entries.append(entry)
                result.loaded_content[entry.slug] = content
            elif entry.slug in cached.content and not entry.fs_path:
                # Legacy caches used None without a path as an oversized-file
                # sentinel. Preserve that behavior for callers that still use it.
                result.overflow_slugs.append(entry.slug)
                result.overflow_entries[entry.slug] = entry
            elif entry.fs_path and _fs_path_oversized(entry, framework_knowledge_dir, knowledge_dir):
                result.overflow_slugs.append(entry.slug)
                result.overflow_entries[entry.slug] = entry
            continue
        result.deferred_entries[entry.slug] = entry

    result.loaded_entries.sort(key=lambda entry: (_category_sort_key(entry.category), entry.slug))
    return result


# ---------------------------------------------------------------------------
# Dynamic loading / unloading
# ---------------------------------------------------------------------------


def load_dynamic_entry(
    result: KnowledgeContextResult,
    slug: str,
    content: str,
    current_turn: int,
    task_id: str | None = None,
) -> None:
    """Load a detail file into dynamic context."""
    if slug in result.dynamically_loaded:
        return
    result.dynamically_loaded[slug] = content
    result.dynamic_records[slug] = DynamicLoadRecord(
        slug=slug,
        loaded_at_turn=current_turn,
        task_id=task_id,
    )
    result.deferred_entries.pop(slug, None)


def unload_dynamic_entry(result: KnowledgeContextResult, slug: str) -> bool:
    """Remove a dynamically loaded file from context. Returns True if removed."""
    content = result.dynamically_loaded.pop(slug, None)
    result.dynamic_records.pop(slug, None)  # always clean up, even if content was empty
    return content is not None


def unload_by_task(result: KnowledgeContextResult, task_id: str) -> list[str]:
    """Release all dynamic knowledge associated with a completed task."""
    unloaded: list[str] = []
    for slug, record in list(result.dynamic_records.items()):
        if record.task_id == task_id:
            unload_dynamic_entry(result, slug)
            unloaded.append(slug)
    return unloaded


def unload_all_dynamic(result: KnowledgeContextResult) -> list[str]:
    """Release all dynamic knowledge (conversation end or topic switch)."""
    unloaded = list(result.dynamically_loaded.keys())
    for slug in unloaded:
        unload_dynamic_entry(result, slug)
    return unloaded


def demote_loaded_entry(result: KnowledgeContextResult, slug: str) -> bool:
    """Move one loaded static knowledge file into overflow. Returns True.

    Used by the request-size degradation loop, which peels the largest file
    and re-measures the payload before demoting the next. Both loaded_content
    and loaded_entries drop the slug so a subsequent system-prompt rebuild is
    consistent; the overflow registry keeps the slug discoverable so the agent
    can still fetch it with knowledge_rw read.
    """
    if slug not in result.loaded_content:
        return False
    result.loaded_content.pop(slug)
    entry = next((e for e in result.loaded_entries if e.slug == slug), None)
    if entry is not None:
        result.loaded_entries.remove(entry)
        if slug not in result.overflow_slugs:
            result.overflow_slugs.append(slug)
            result.overflow_entries[slug] = entry
    return True


def refresh_dynamic_entry(
    result: KnowledgeContextResult,
    slug: str,
    new_content: str,
) -> bool:
    """Refresh a dynamically loaded file's content. Returns True if refreshed."""
    if slug not in result.dynamically_loaded:
        return False
    result.dynamically_loaded[slug] = new_content
    return True


# ---------------------------------------------------------------------------
# Context update (called after knowledge_rw write)
# ---------------------------------------------------------------------------


def update_context_file(result: KnowledgeContextResult, slug: str, new_content: str) -> None:
    """Update a single file in an already-loaded context."""
    if _is_log_role(new_content):
        return
    if slug in result.dynamically_loaded:
        result.dynamically_loaded[slug] = new_content
    else:
        result.loaded_content[slug] = new_content
        if slug in result.overflow_slugs:
            result.overflow_slugs.remove(slug)
            if not any(e.slug == slug for e in result.loaded_entries):
                # Reuse the real DB-backed entry captured at load time instead of
                # fabricating one with an empty knowledge_id
                entry = result.overflow_entries.pop(slug, None)
                if entry is not None:
                    entry.word_count = len(new_content.split())
                    result.loaded_entries.append(entry)
                else:
                    # Compatibility for older callers that tracked only a slug.
                    result.loaded_entries.append(KnowledgeEntry(
                        knowledge_id=f"memory:{slug}",
                        slug=slug,
                        title=slug.rsplit("/", 1)[-1].replace("-", " ").title(),
                        fs_path="",
                        category=slug.split("/", 1)[0],
                        cross_references=[],
                        word_count=len(new_content.split()),
                    ))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_primary_from_disk(
    knowledge_dir: Path,
    knowledge_filter: list[str] | None = None,
    project_id: str | None = None,
) -> tuple[list[tuple[KnowledgeEntry, str]], list[KnowledgeEntry]]:
    """Scan knowledge_dir for primary files (non-detail) and return entries with content.

    Returns (loaded, overflow): loaded entries carry full content; overflow
    entries are oversized files whose metadata is registered for the agent to
    discover (never loaded into context).
    """
    results: list[tuple[KnowledgeEntry, str]] = []
    overflows: list[KnowledgeEntry] = []

    def _scan() -> tuple[list[tuple[Path, str, dict, str]], list[tuple[Path, str]]]:
        items = []
        overflow_scans = []
        # A symlink inside knowledge/ may point outside it (a git clone or a
        # user's file system can carry one) — resolve() follows links, so the
        # containment check must run on the RESOLVED path or an external .md
        # file's full content gets read into the project prompt.
        base_resolved = knowledge_dir.resolve()
        for md_path in sorted(knowledge_dir.rglob("*.md")):
            try:
                md_path.resolve().relative_to(base_resolved)
            except (OSError, ValueError):
                continue
            # the dot-check must look at the path RELATIVE to the
            # knowledge dir — md_path.parts is absolute, so any dot-prefixed
            # ancestor of the deployment path (e.g. /srv/.data/projects/...)
            # silently skipped every knowledge file with no log or warning.
            try:
                rel_parts = md_path.relative_to(knowledge_dir).parts
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel_parts):
                continue
            try:
                if md_path.stat().st_size > MAX_FILE_SIZE:
                    # Oversized file: read the head (frontmatter region) so we
                    # can still register slug/title/category for the overflow
                    # list — otherwise the entry silently vanishes from the
                    # agent's knowledge of the project.
                    try:
                        with md_path.open("r", encoding="utf-8") as f:
                            head = f.read(65536)
                    except (OSError, UnicodeDecodeError):
                        continue
                    overflow_scans.append((md_path, head))
                    continue
                # A UTF-8 BOM (Windows editors) would stick to the first
                # frontmatter key (title) and break metadata parsing.
                content = md_path.read_text("utf-8").lstrip("\ufeff")
            except (OSError, UnicodeDecodeError):
                # File may be removed/renamed between rglob and stat (race),
                # or replaced mid-read — skip it rather than crashing the load.
                continue
            try:
                post = frontmatter.loads(content)
            except Exception:
                _log.warning("Skipping %s: invalid frontmatter", md_path)
                continue
            meta = post.metadata
            if meta.get("knowledge_level") == "detail":
                continue
            if meta.get("knowledge_role") == "log":
                continue
            items.append((md_path, content, meta, post.content))
        return items, overflow_scans

    items, overflow_scans = await asyncio.to_thread(_scan)

    for md_path, head in overflow_scans:
        try:
            post = frontmatter.loads(head)
        except Exception:
            post = None
        meta = post.metadata if post is not None else {}
        body = post.content if post is not None else head
        if meta.get("knowledge_role") == "log":
            continue
        slug = str(md_path.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
        category = meta.get("category") or slug.split("/")[0]
        if knowledge_filter and not _matches_filter(slug, category, knowledge_filter):
            continue
        overflows.append(KnowledgeEntry(
            knowledge_id=f"disk:{slug}",
            slug=slug,
            title=meta.get("title") or md_path.stem.replace("-", " ").title(),
            fs_path=str(md_path),
            category=category,
            cross_references=_CROSS_REF_RE.findall(body) if body else [],
            word_count=0,
            knowledge_level="auto",
            summary=meta.get("summary", ""),
            project_id=project_id,
        ))

    for md_path, content, meta, body in items:
        slug = str(md_path.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
        category = meta.get("category") or slug.split("/")[0]

        if knowledge_filter and not _matches_filter(slug, category, knowledge_filter):
            continue

        title = meta.get("title") or md_path.stem.replace("-", " ").title()
        cross_refs = _CROSS_REF_RE.findall(body) if body else []

        entry = KnowledgeEntry(
            knowledge_id=f"disk:{slug}",
            slug=slug,
            title=title,
            fs_path=str(md_path),
            category=category,
            cross_references=cross_refs,
            word_count=len(body.split()) if body else 0,
            knowledge_level="auto",
            summary=meta.get("summary", ""),
            project_id=project_id,
        )
        results.append((entry, content))

    results.sort(key=lambda x: (_category_sort_key(x[0].category), x[0].slug))
    return results, overflows


def _entry_base_dir(
    entry: KnowledgeEntry,
    framework_knowledge_dir: str | Path | None,
    project_knowledge_dir: str | Path | None,
) -> Path | None:
    """Allowed base directory for a DB-backed entry.

    Global rows (project_id NULL) are owned by the framework knowledge dir,
    project rows by the project knowledge dir. Returns None when the base is
    unknown — callers must fail closed in that case.
    """
    base = framework_knowledge_dir if entry.project_id is None else project_knowledge_dir
    return Path(base) if base else None


def _fs_path_oversized(
    entry: KnowledgeEntry,
    framework_knowledge_dir: str | Path | None,
    project_knowledge_dir: str | Path | None,
) -> bool:
    """True if the entry's file exists, is ownership-verified, and exceeds the size cap."""
    base = _entry_base_dir(entry, framework_knowledge_dir, project_knowledge_dir)
    if base is None or not is_within(base, entry.fs_path):
        return False
    p = Path(entry.fs_path)
    try:
        return p.exists() and p.stat().st_size > MAX_FILE_SIZE
    except OSError:
        return False


async def _try_read_file(
    entry: KnowledgeEntry,
    *,
    framework_knowledge_dir: str | Path | None = None,
    project_knowledge_dir: str | Path | None = None,
) -> str | None:
    """Read a knowledge file, returning None on any error or size limit.

    Ownership is verified before touching disk: global rows must live under
    framework_knowledge_dir, project rows under project_knowledge_dir. A
    missing base directory fails closed.
    """
    base = _entry_base_dir(entry, framework_knowledge_dir, project_knowledge_dir)
    if base is None:
        _log.warning(
            "Refusing to read knowledge file %s (slug=%s): no base directory to verify ownership",
            entry.fs_path, entry.slug,
        )
        return None
    if not is_within(base, entry.fs_path):
        _log.warning(
            "Refusing to read knowledge file outside its owning directory: %s (slug=%s, project_id=%s)",
            entry.fs_path, entry.slug, entry.project_id,
        )
        return None
    p = Path(entry.fs_path)
    try:
        stat = await asyncio.to_thread(p.stat)
    except OSError:
        _log.debug("Cannot stat knowledge file %s (slug=%s)", entry.fs_path, entry.slug, exc_info=True)
        return None
    if stat.st_size > MAX_FILE_SIZE:
        return None
    try:
        text = await asyncio.to_thread(p.read_text, "utf-8")
        # Strip UTF-8 BOM so the loaded body matches the indexed hash.
        return text.lstrip("\ufeff")
    except (OSError, UnicodeDecodeError):
        _log.debug("Cannot read knowledge file %s (slug=%s)", entry.fs_path, entry.slug, exc_info=True)
        return None


async def _fetch_all_entries(db_session: Any, project_id: str) -> list[KnowledgeEntry]:
    """Fetch all knowledge_metadata rows for this project + global."""
    if db_session is None:
        return []
    try:
        from sqlalchemy import text
        query = text("""
            SELECT
                knowledge_id, slug, title, fs_path, category,
                cross_references, word_count,
                knowledge_level, parent_slug, children_slugs, summary, depth,
                project_id
            FROM knowledge_metadata
            WHERE (project_id = :pid OR project_id IS NULL)
              AND is_archived = :archived
            -- Project rows must shadow global rows with the same slug: the
            -- caller folds rows into a dict keyed by slug (later wins), and
            -- the row order here is otherwise undefined (GUID cluster keys).
            ORDER BY CASE WHEN project_id IS NULL THEN 0 ELSE 1 END, slug
        """)
        rows = (await db_session.execute(query, {"pid": project_id, "archived": False})).mappings().all()
        entries = []
        for row in rows:
            cross_refs = json.loads(row["cross_references"]) if row["cross_references"] else []
            children = json.loads(row["children_slugs"]) if row["children_slugs"] else []
            entries.append(KnowledgeEntry(
                knowledge_id=str(row["knowledge_id"]),
                slug=row["slug"],
                title=row["title"],
                fs_path=row["fs_path"],
                category=row["category"],
                cross_references=cross_refs,
                word_count=int(row["word_count"] or 0),
                knowledge_level=row["knowledge_level"] or "auto",
                parent_slug=row["parent_slug"],
                children_slugs=children,
                summary=row["summary"] or "",
                depth=int(row["depth"] or 0),
                project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            ))
        return entries
    except Exception as e:
        _log.error("Failed to fetch knowledge entries for project %s: %s", project_id, e, exc_info=True)
        raise
