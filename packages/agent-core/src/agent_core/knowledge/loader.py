"""Project knowledge loader — two-tier loading model.

Knowledge files are partitioned into two tiers:
- Primary (knowledge_level != detail, no parent): loaded directly from disk
  at project start. Not indexed in the database.
- Detail (knowledge_level: detail, or auto with a parent): indexed in DB with
  metadata + summary only. Content is NOT loaded automatically — agent loads
  on demand via knowledge_rw load.

Dynamic loading brings detail files into context mid-conversation. Load events
are persisted per conversation (knowledge_load_events) and rehydrated at the
start of every turn, so a loaded file survives across user messages and task
boundaries; unloading happens on explicit knowledge_rw unload (or when the
file disappears and the event is self-healed).
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

from agent_core.paths import KNOWLEDGE_SLUG_RE, is_within

MAX_FILE_SIZE = 512 * 1024  # 512 KB per file

# Budget for the dynamic (load) region. The static region has a demotion
# loop; the dynamic region has none — an unbounded series of loads would
# blow the request size, so both the load tool and turn rehydrate enforce
# these caps at the entry point.
DYNAMIC_LOAD_MAX_FILES = 10
DYNAMIC_LOAD_MAX_BYTES = 256 * 1024

_CROSS_REF_RE = re.compile(r"\[\[([^\]]+)\]\]")
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


def _is_log_role(content: str) -> bool:
    try:
        return frontmatter.loads(content).metadata.get("knowledge_role") == "log"
    except Exception:
        return False


def _normalize_knowledge_level(level: str) -> str:
    """Normalize frontmatter knowledge_level: the legacy 'index' means 'root'."""
    return "root" if level == "index" else level


def _is_deferred_level(meta: dict) -> bool:
    """True when a file's frontmatter places it in the deferred (Tier 2) set.

    Documented rule (knowledge-manager / tool-reference): `detail` defers
    always; `auto` defers when it carries a `parent` (depth > 0). Root-level
    auto files stay primary.
    """
    level = _normalize_knowledge_level(str(meta.get("knowledge_level", "auto")))
    if level == "detail":
        return True
    return level == "auto" and bool(meta.get("parent"))


def derive_summary(content: str, limit: int = 160) -> str:
    """Fallback summary when frontmatter has none: first prose paragraph, truncated.

    Skips frontmatter and drops heading lines entirely (a title line is not a
    summary) so the deferred-knowledge table never shows an empty shell row.
    Hand-written frontmatter summaries always win — this only fills the gap.
    """
    try:
        post = frontmatter.loads(content)
        body = post.content
        if post.metadata.get("summary"):
            return str(post.metadata["summary"])[:limit]
    except Exception:
        body = content
    text = _CROSS_REF_RE.sub(lambda m: m.group(1), body or "")
    # Drop whole heading lines; stripping only the '#' marker would leave the
    # title text as the "first paragraph".
    text = "\n".join(line for line in text.splitlines() if not _HEADING_RE.match(line))
    for para in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not para:
            continue
        # Strip list bullets at line starts, then inline markers — keep
        # hyphens so slugs like technical/api-map survive intact.
        para = re.sub(r"^\s*[-*+]\s+", "", para, flags=re.MULTILINE)
        para = re.sub(r"[*_`>|]+", " ", para)
        para = re.sub(r"\s+", " ", para).strip()
        if para:
            return para[:limit]
    return ""


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
    task_id: str | None  # optional task binding; None = persists until explicit unload


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
    conversation_id: str | None = None,
) -> KnowledgeContextResult:
    """Load project knowledge using a two-tier model.

    Tier 1 — Primary files: read directly from disk (knowledge_dir).
        All .md files in the knowledge directory that are NOT deferred-level
        (detail, or auto with a parent) are loaded in full.

    Tier 2 — Detail files: indexed in DB, only metadata+summary exposed.
        Disk fallback covers detail files missing an index row (reindex
        failure must not make an entry vanish). Agent loads content on demand
        via knowledge_rw load.

    When conversation_id is given, previously loaded detail files are
    rehydrated from knowledge_load_events — a load persists across turns
    until an explicit unload.

    framework_knowledge_dir is the framework's global knowledge directory;
    DB-backed reads are verified against it (global rows) or knowledge_dir
    (project rows) before touching disk.
    """
    result = KnowledgeContextResult()

    # --- Tier 1: Load primary files directly from disk ---
    deferred_disk: list[KnowledgeEntry] = []
    if knowledge_dir:
        kdir = Path(knowledge_dir)
        if kdir.exists():
            primary_entries, overflow_entries, deferred_disk = await _load_primary_from_disk(
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
        if entry.slug in result.loaded_content or entry.slug in result.overflow_slugs:
            # Tier 1 already surfaced this slug from disk (full content, or the
            # overflow list for oversized files). Listing it again under
            # "Available Detail Knowledge" duplicates the prompt entry and lets
            # knowledge_rw load inject the same content a second time.
            continue
        result.deferred_entries[entry.slug] = entry

    # Disk fallback: a detail file whose DB row is missing (reindex failed or
    # was skipped) would otherwise appear in neither tier — invisible.
    for entry in deferred_disk:
        if (
            entry.slug in result.deferred_entries
            or entry.slug in result.loaded_content
            or entry.slug in result.overflow_slugs
        ):
            continue
        result.deferred_entries[entry.slug] = entry

    result.loaded_entries.sort(key=lambda entry: (_category_sort_key(entry.category), entry.slug))

    # Rehydrate detail files this conversation had loaded before — the context
    # is rebuilt from scratch every user message, and without this a load
    # silently expires at the next turn boundary.
    if conversation_id and knowledge_dir:
        await rehydrate_dynamic_entries(
            result,
            conversation_id=conversation_id,
            db_session=db_session,
            knowledge_dir=Path(knowledge_dir),
            framework_knowledge_dir=framework_knowledge_dir,
        )
    return result


# ---------------------------------------------------------------------------
# Turn-start rehydration of previously loaded detail files
# ---------------------------------------------------------------------------


def dynamic_budget_error(result: KnowledgeContextResult, incoming_bytes: int) -> str | None:
    """Return an error message when a load would exceed the dynamic budget."""
    if len(result.dynamically_loaded) >= DYNAMIC_LOAD_MAX_FILES:
        return (
            f"DYNAMIC CONTEXT FULL: {len(result.dynamically_loaded)}/{DYNAMIC_LOAD_MAX_FILES} files "
            "already loaded. Unload files you no longer need with "
            "`knowledge_rw(operation=\"unload\", slug=...)` before loading another."
        )
    used = sum(len(c.encode("utf-8")) for c in result.dynamically_loaded.values())
    if used + incoming_bytes > DYNAMIC_LOAD_MAX_BYTES:
        return (
            f"DYNAMIC CONTEXT BUDGET EXCEEDED: {used} bytes loaded + {incoming_bytes} incoming "
            f"> {DYNAMIC_LOAD_MAX_BYTES}. Unload files with `knowledge_rw unload` first, "
            "or use `read` for a one-off lookup instead of `load`."
        )
    return None


async def rehydrate_dynamic_entries(
    result: KnowledgeContextResult,
    conversation_id: str,
    db_session: Any,
    knowledge_dir: Path,
    framework_knowledge_dir: str | Path | None = None,
) -> list[str]:
    """Restore detail files loaded earlier in this conversation.

    Folds knowledge_load_events per knowledge_id — the latest event decides
    (load = active, unload = inactive). Content is re-read from disk (never
    stored), so a rehydrate always reflects the current file. Slugs whose
    file is gone/oversized are skipped and their stale load events are
    self-healed with an unload so they stop being queried every turn.
    """
    if db_session is None:
        return []
    try:
        from sqlalchemy import text

        rows = (await db_session.execute(
            text(
                "SELECT e.event_type, e.knowledge_id, m.slug "
                "FROM knowledge_load_events e "
                "JOIN knowledge_metadata m ON m.knowledge_id = e.knowledge_id "
                "WHERE e.conversation_id = :cid "
                "ORDER BY e.created_at ASC, e.turn_number ASC"
            ),
            {"cid": conversation_id},
        )).mappings().all()
    except Exception:
        _log.warning("rehydrate: failed to query load events for conversation %s",
                     conversation_id, exc_info=True)
        return []

    # Fold events: last event per knowledge_id wins.
    active: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        slug = row["slug"]
        if row["event_type"] == "load":
            if slug not in active:
                order.append(slug)
            active[slug] = row
        else:
            active.pop(slug, None)
            if slug in order:
                order.remove(slug)
    if not active:
        return []

    reloaded: list[str] = []
    used_bytes = sum(len(c.encode("utf-8")) for c in result.dynamically_loaded.values())
    for slug in order:
        row = active[slug]
        if slug in result.dynamically_loaded or slug in result.loaded_content:
            continue
        if len(result.dynamically_loaded) >= DYNAMIC_LOAD_MAX_FILES:
            _log.info("rehydrate: file budget reached, leaving %s deferred", slug)
            break
        candidate = Path(knowledge_dir) / f"{slug}.md"
        if not candidate.exists() and framework_knowledge_dir:
            fw_candidate = Path(framework_knowledge_dir) / f"{slug}.md"
            if fw_candidate.exists():
                candidate = fw_candidate
        content: str | None = None
        try:
            if candidate.exists() and candidate.stat().st_size <= MAX_FILE_SIZE:
                content = candidate.read_text("utf-8").lstrip("﻿")
        except (OSError, UnicodeDecodeError):
            content = None
        if content is None:
            _log.warning("rehydrate: skipping slug=%s (file missing or oversized)", slug)
            await _self_heal_stale_load(db_session, row, conversation_id)
            continue
        size = len(content.encode("utf-8"))
        if used_bytes + size > DYNAMIC_LOAD_MAX_BYTES:
            _log.info("rehydrate: byte budget reached, leaving %s deferred", slug)
            break
        load_dynamic_entry(result, slug, content, current_turn=0, task_id=None)
        used_bytes += size
        reloaded.append(slug)
    return reloaded


async def _self_heal_stale_load(db_session: Any, event_row: dict, conversation_id: str) -> None:
    """Record an unload for a load event whose file no longer loads."""
    try:
        from api.models.knowledge import KnowledgeLoadEvent
    except ImportError:
        return
    try:
        db_session.add(KnowledgeLoadEvent(
            knowledge_id=str(event_row["knowledge_id"]),
            conversation_id=conversation_id,
            event_type="unload",
            reason="rehydrate_missing",
            turn_number=0,
        ))
        await db_session.commit()
    except Exception:
        _log.debug("rehydrate: self-heal unload failed", exc_info=True)


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
    """Update a single file in an already-loaded context.

    Three branches — the deferred one exists because detail content must
    NEVER enter the static loaded_content: it would be injected twice this
    turn (tool result already carries it) and would evaporate next turn
    anyway (context rebuild), leaving a stale full-text copy behind the
    summary-only deferred row.
    """
    if _is_log_role(new_content):
        return
    if slug in result.dynamically_loaded:
        result.dynamically_loaded[slug] = new_content
        return

    try:
        post = frontmatter.loads(new_content)
        meta = post.metadata
        body = post.content
    except Exception:
        meta, body = {}, new_content

    if slug in result.deferred_entries or _is_deferred_level(meta):
        # Deferred: refresh metadata in place (or create the row for a newly
        # written detail file), drop any dirty static copy, never store body.
        prior = result.deferred_entries.get(slug) or next(
            (e for e in result.loaded_entries if e.slug == slug), None
        ) or result.overflow_entries.get(slug)
        result.loaded_content.pop(slug, None)
        if slug in result.overflow_slugs:
            result.overflow_slugs.remove(slug)
            result.overflow_entries.pop(slug, None)
        result.loaded_entries = [e for e in result.loaded_entries if e.slug != slug]
        entry = result.deferred_entries.get(slug)
        if entry is None:
            entry = KnowledgeEntry(
                knowledge_id=f"disk:{slug}",
                slug=slug,
                title=str(meta.get("title") or slug.rsplit("/", 1)[-1].replace("-", " ").title()),
                fs_path=prior.fs_path if prior else "",
                category=str(meta.get("category") or slug.split("/", 1)[0]),
                cross_references=_CROSS_REF_RE.findall(body) if body else [],
                word_count=len(body.split()) if body else 0,
                knowledge_level=_normalize_knowledge_level(str(meta.get("knowledge_level", "auto"))),
                parent_slug=meta.get("parent"),
                children_slugs=list(meta.get("children") or []),
                summary=str(meta.get("summary") or derive_summary(new_content)),
                project_id=prior.project_id if prior else None,
            )
            result.deferred_entries[slug] = entry
        else:
            entry.word_count = len(body.split()) if body else 0
            entry.summary = str(meta.get("summary") or derive_summary(new_content))
            if meta.get("title"):
                entry.title = str(meta["title"])
            if "knowledge_level" in meta:
                entry.knowledge_level = _normalize_knowledge_level(str(meta["knowledge_level"]))
            if "parent" in meta:
                entry.parent_slug = meta["parent"]
            if "children" in meta:
                entry.children_slugs = list(meta["children"] or [])
        return

    # Primary: full content lives in the static region.
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
    else:
        for entry in result.loaded_entries:
            if entry.slug == slug:
                entry.word_count = len(new_content.split())
                break


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_primary_from_disk(
    knowledge_dir: Path,
    knowledge_filter: list[str] | None = None,
    project_id: str | None = None,
) -> tuple[
    list[tuple[KnowledgeEntry, str]],
    list[KnowledgeEntry],
    list[KnowledgeEntry],
]:
    """Scan knowledge_dir for primary files (non-detail) and return entries with content.

    Returns (loaded, overflow, deferred): loaded entries carry full content;
    overflow entries are oversized primary files registered for discovery;
    deferred entries are detail / auto+parent files held back from the static
    region (disk fallback when the DB row is missing, so a failed reindex
    cannot make an entry vanish).
    """
    results: list[tuple[KnowledgeEntry, str]] = []
    overflows: list[KnowledgeEntry] = []
    deferred: list[KnowledgeEntry] = []

    def _scan() -> tuple[
        list[tuple[Path, str, dict, str]],
        list[tuple[Path, str]],
        list[tuple[Path, str, dict, str]],
    ]:
        items = []
        overflow_scans = []
        deferred_scans = []
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
            slug = str(md_path.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")
            if not KNOWLEDGE_SLUG_RE.match(slug):
                # ASCII-only slugs are a path-traversal defense; a CJK filename
                # must move its name to frontmatter `title` instead.
                _log.warning(
                    "Skipping %s: slug %r fails ASCII slug validation "
                    "(rename the file to ASCII; put the Chinese name in frontmatter `title`)",
                    md_path, slug,
                )
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
            if meta.get("knowledge_role") == "log":
                continue
            # Deferred-tier files (detail, or auto with a parent) are scanned
            # but their body never joins the static region.
            if _is_deferred_level(meta):
                deferred_scans.append((md_path, content, meta, post.content))
                continue
            items.append((md_path, content, meta, post.content))
        return items, overflow_scans, deferred_scans

    items, overflow_scans, deferred_scans = await asyncio.to_thread(_scan)

    # One parent map across every scanned file (primary, deferred, oversized)
    # so depth stays consistent even when the parent itself is deferred.
    slug_parent: dict[str, str | None] = {}

    def _slug_of(md_path: Path) -> str:
        return str(md_path.relative_to(knowledge_dir).with_suffix("")).replace("\\", "/")

    def _depth_of(slug: str) -> int:
        depth, current = 0, slug_parent.get(slug)
        while current and depth < 5:
            depth += 1
            current = slug_parent.get(current)
        return depth

    for md_path, _c, meta, _b in items + deferred_scans:
        slug_parent[_slug_of(md_path)] = meta.get("parent") or None
    for md_path, head in overflow_scans:
        try:
            slug_parent[_slug_of(md_path)] = frontmatter.loads(head).metadata.get("parent") or None
        except Exception:
            slug_parent[_slug_of(md_path)] = None

    def _hierarchy_fields(slug: str, meta: dict) -> dict:
        return {
            "knowledge_level": _normalize_knowledge_level(str(meta.get("knowledge_level", "auto"))),
            "parent_slug": meta.get("parent") or None,
            "children_slugs": list(meta.get("children") or []),
            "depth": _depth_of(slug),
        }

    deferred_seen: set[str] = set()

    for md_path, head in overflow_scans:
        try:
            post = frontmatter.loads(head)
        except Exception:
            post = None
        meta = post.metadata if post is not None else {}
        body = post.content if post is not None else head
        if meta.get("knowledge_role") == "log":
            continue
        slug = _slug_of(md_path)
        category = meta.get("category") or slug.split("/")[0]
        if knowledge_filter and not _matches_filter(slug, category, knowledge_filter):
            continue
        # An oversized deferred file belongs in the deferred set (metadata
        # only): the overflow list promises "read can fetch it", which the
        # size cap would then refuse.
        if _is_deferred_level(meta):
            if slug not in deferred_seen:
                deferred_seen.add(slug)
                deferred.append(KnowledgeEntry(
                    knowledge_id=f"disk:{slug}",
                    slug=slug,
                    title=meta.get("title") or md_path.stem.replace("-", " ").title(),
                    fs_path=str(md_path),
                    category=category,
                    cross_references=_CROSS_REF_RE.findall(body) if body else [],
                    word_count=0,
                    summary=str(meta.get("summary") or ""),
                    project_id=project_id,
                    **_hierarchy_fields(slug, meta),
                ))
            continue
        overflows.append(KnowledgeEntry(
            knowledge_id=f"disk:{slug}",
            slug=slug,
            title=meta.get("title") or md_path.stem.replace("-", " ").title(),
            fs_path=str(md_path),
            category=category,
            cross_references=_CROSS_REF_RE.findall(body) if body else [],
            word_count=0,
            summary=str(meta.get("summary") or ""),
            project_id=project_id,
            **_hierarchy_fields(slug, meta),
        ))

    for md_path, content, meta, body in items:
        slug = _slug_of(md_path)
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
            summary=str(meta.get("summary") or derive_summary(content)),
            project_id=project_id,
            **_hierarchy_fields(slug, meta),
        )
        results.append((entry, content))

    for md_path, content, meta, body in deferred_scans:
        slug = _slug_of(md_path)
        if slug in deferred_seen:
            continue
        category = meta.get("category") or slug.split("/")[0]
        if knowledge_filter and not _matches_filter(slug, category, knowledge_filter):
            continue
        deferred_seen.add(slug)
        deferred.append(KnowledgeEntry(
            knowledge_id=f"disk:{slug}",
            slug=slug,
            title=meta.get("title") or md_path.stem.replace("-", " ").title(),
            fs_path=str(md_path),
            category=category,
            cross_references=_CROSS_REF_RE.findall(body) if body else [],
            word_count=len(body.split()) if body else 0,
            summary=str(meta.get("summary") or derive_summary(content)),
            project_id=project_id,
            **_hierarchy_fields(slug, meta),
        ))

    results.sort(key=lambda x: (_category_sort_key(x[0].category), x[0].slug))
    return results, overflows, deferred


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
