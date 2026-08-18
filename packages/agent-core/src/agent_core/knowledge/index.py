"""Knowledge directory indexer.

Scans knowledge/ and projects/{ws}/{proj}/knowledge/ directories,
computes completeness scores, and upserts knowledge_metadata.

CLI usage:
    python -m agent_core.knowledge.index --global-dir ./knowledge
    python -m agent_core.knowledge.index --project-dir ./projects/ws/proj --project-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from .scorer import compute_completeness

_CROSS_REF_RE = re.compile(r"\[\[([^\]]+)\]\]")
_STALE_DAYS = 90
_BATCH_SIZE = 50
_MAX_HIERARCHY_DEPTH = 5


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _parse_tags(meta: dict) -> list[str]:
    tags = meta.get("tags", [])
    if isinstance(tags, list):
        return [str(t) for t in tags]
    return []


def _parse_cross_refs(body: str) -> list[str]:
    return _CROSS_REF_RE.findall(body)


def _days_since(updated_at: datetime | None) -> float:
    if updated_at is None:
        return 999.0
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - updated_at).total_seconds() / 86400)


def _apply_parsed_updates(existing: Any, pf: dict, completeness, file_mtime: datetime) -> None:
    """Apply parsed-file fields to an existing KnowledgeMetadata row.

    Shared by the update branch and the IntegrityError retry path (a
    concurrent indexer may win the INSERT race, in which case the winner's
    row is re-fetched and updated through here).
    """
    existing.title = pf["title"]
    existing.category = pf["category"]
    existing.fs_path = str(pf["md_path"])
    existing.completeness_score = completeness.final_score
    existing.coverage_breadth = completeness.coverage_breadth
    existing.recency_score = completeness.recency_score
    existing.cross_ref_density = completeness.cross_ref_density
    existing.agent_gap_score = completeness.agent_gap_score
    existing.tags = pf["tags"]
    existing.cross_references = pf["cross_refs"]
    existing.content_hash = pf["new_hash"]
    existing.word_count = pf["word_count"]
    existing.updated_at = file_mtime
    existing.version = (existing.version or 0) + 1
    existing.knowledge_level = pf["knowledge_level"]
    existing.parent_slug = pf["parent_slug"]
    existing.children_slugs = pf["children_slugs"]
    existing.summary = pf["summary"]
    existing.depth = pf["depth"]


async def index_directory(
    directory: Path,
    project_id: str | None,
    db_session,
) -> dict[str, int]:
    """Index all .md files in a directory. Returns stats."""
    stats = {"scanned": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}

    if not directory.exists():
        return stats

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    try:
        from api.models.knowledge import KnowledgeMetadata, KnowledgeVersion
    except ImportError:
        KnowledgeMetadata = None  # type: ignore
        KnowledgeVersion = None  # type: ignore

    # Collect all parsed file data first for batch processing
    parsed_files: list[dict] = []

    # A symlink inside the knowledge dir may point outside it (git clone or
    # user file system) — resolve() follows links, so the containment check
    # must run on the RESOLVED path or an external .md gets indexed with its
    # full content (same rule as loader.py).
    base_resolved = directory.resolve()
    for md_path in sorted(directory.rglob("*.md")):
        try:
            md_path.resolve().relative_to(base_resolved)
        except (OSError, ValueError):
            continue
        # The dot-check must look at the path RELATIVE to the knowledge dir —
        # md_path.parts is absolute, so any dot-prefixed ancestor of the
        # deployment path silently skipped every file (loader.py applies the
        # same rule).
        try:
            rel_parts = md_path.relative_to(directory).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue

        stats["scanned"] += 1
        try:
            # A UTF-8 BOM (VS Code/记事本 saves) would stick to the first
            # frontmatter key (\ufefftitle) and break metadata parsing.
            content = md_path.read_text(encoding="utf-8").lstrip("\ufeff")
            post = frontmatter.loads(content)
            meta = post.metadata
            body = post.content

            slug = str(md_path.relative_to(directory).with_suffix("")).replace("\\", "/")
            title = meta.get("title") or md_path.stem.replace("-", " ").title()
            category = meta.get("category") or slug.split("/")[0]
            tags = json.dumps(_parse_tags(meta))
            cross_refs_list = _parse_cross_refs(body)
            cross_refs = json.dumps(cross_refs_list)
            word_count = len(body.split())
            new_hash = _content_hash(content)
            knowledge_level = meta.get("knowledge_level", "auto")
            parent_slug = meta.get("parent", None)
            children_slugs = json.dumps(meta.get("children", []))
            summary = meta.get("summary", "")

            parsed_files.append({
                "md_path": md_path,
                "content": content,
                "slug": slug,
                "title": title,
                "category": category,
                "tags": tags,
                "cross_refs": cross_refs,
                "cross_refs_list": cross_refs_list,
                "word_count": word_count,
                "new_hash": new_hash,
                "knowledge_level": knowledge_level,
                "parent_slug": parent_slug,
                "children_slugs": children_slugs,
                "summary": summary,
            })
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR parsing {md_path}: {e}", file=sys.stderr)

    if not parsed_files:
        return stats

    # Compute hierarchy depths and normalize knowledge_level
    depths = _compute_depths(parsed_files)
    for pf in parsed_files:
        pf["depth"] = depths.get(pf["slug"], 0)
        pf["knowledge_level"] = _normalize_knowledge_level(pf["knowledge_level"])

    # Build inbound link counts from all parsed files
    inbound_counts: dict[str, int] = {}
    for pf in parsed_files:
        for ref in pf["cross_refs_list"]:
            inbound_counts[ref] = inbound_counts.get(ref, 0) + 1

    # Process in batches
    if db_session is not None and KnowledgeMetadata is not None:
        # Fetch all existing records for this project in one query
        existing_result = await db_session.execute(
            select(KnowledgeMetadata).where(
                KnowledgeMetadata.project_id == project_id,
            )
        )
        existing_map: dict[str, Any] = {
            km.slug: km for km in existing_result.scalars().all()
        }

        batch_count = 0
        for pf in parsed_files:
            existing = existing_map.get(pf["slug"])

            if existing and existing.content_hash == pf["new_hash"]:
                # Even if content hasn't changed, fs_path might have (file was moved)
                if existing.fs_path != str(pf["md_path"]):
                    existing.fs_path = str(pf["md_path"])
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
                continue

            try:
                file_mtime = datetime.fromtimestamp(pf["md_path"].stat().st_mtime, tz=timezone.utc)
            except OSError:
                # File removed between parse and stat (concurrent deletion) —
                # skip it instead of aborting the whole batch index.
                stats["skipped"] += 1
                continue
            days_since = _days_since(file_mtime)
            inbound_count = inbound_counts.get(pf["slug"], 0)

            completeness = compute_completeness(
                fs_path=str(pf["md_path"]),
                days_since_update=days_since,
                inbound_link_count=inbound_count,
                content=pf["content"],
            )

            if existing is None:
                km = KnowledgeMetadata(
                    project_id=project_id,
                    slug=pf["slug"],
                    title=pf["title"],
                    category=pf["category"],
                    fs_path=str(pf["md_path"]),
                    completeness_score=completeness.final_score,
                    coverage_breadth=completeness.coverage_breadth,
                    recency_score=completeness.recency_score,
                    cross_ref_density=completeness.cross_ref_density,
                    agent_gap_score=completeness.agent_gap_score,
                    tags=pf["tags"],
                    cross_references=pf["cross_refs"],
                    content_hash=pf["new_hash"],
                    word_count=pf["word_count"],
                    updated_at=file_mtime,
                    knowledge_level=pf["knowledge_level"],
                    parent_slug=pf["parent_slug"],
                    children_slugs=pf["children_slugs"],
                    summary=pf["summary"],
                    depth=pf["depth"],
                )
                db_session.add(km)
                # flush per-file inside a SAVEPOINT. A concurrent
                # index_directory/reindex_one can INSERT the same (project_id,
                # slug) between our SELECT and this flush — with the unique
                # constraint the loser's flush raises IntegrityError, and
                # without SAVEPOINT the whole batch would be left unusable.
                # On a lost race, re-fetch the winner's row and fall through
                # to the update branch instead of aborting the batch.
                try:
                    async with db_session.begin_nested():
                        await db_session.flush()
                    stats["created"] += 1
                except IntegrityError:
                    winner = (await db_session.execute(
                        select(KnowledgeMetadata).where(
                            KnowledgeMetadata.project_id == project_id,
                            KnowledgeMetadata.slug == pf["slug"],
                        )
                    )).scalars().first()
                    if winner is not None:
                        _apply_parsed_updates(winner, pf, completeness, file_mtime)
                        stats["updated"] += 1
                    else:
                        stats["skipped"] += 1
            else:
                _apply_parsed_updates(existing, pf, completeness, file_mtime)
                stats["updated"] += 1

            batch_count += 1
            if batch_count >= _BATCH_SIZE:
                await db_session.flush()
                batch_count = 0

        if batch_count > 0:
            await db_session.flush()

        # Second pass: recalculate cross_ref_density with inbound link counts.
        # Pass already-read file contents to avoid a second disk read per file.
        content_cache = {str(pf["md_path"]): pf["content"] for pf in parsed_files}
        await _recalculate_inbound_links(db_session, project_id, directory, KnowledgeMetadata, content_cache)

        # Validate hierarchy
        warnings = _validate_hierarchy(parsed_files)
        if warnings:
            import logging
            log = logging.getLogger("agent_core.knowledge.index")
            for w in warnings:
                log.warning("Hierarchy: %s", w)

        await db_session.commit()
    else:
        for pf in parsed_files:
            print(f"  {pf['slug']} (completeness=pending)")
            stats["created"] += 1

    return stats


def _compute_depths(parsed_files: list[dict]) -> dict[str, int]:
    """Compute depth for each file by walking parent chains."""
    slug_to_parent: dict[str, str | None] = {pf["slug"]: pf["parent_slug"] for pf in parsed_files}
    depths: dict[str, int] = {}
    for slug in slug_to_parent:
        d, current = 0, slug_to_parent.get(slug)
        while current and d < _MAX_HIERARCHY_DEPTH:
            d += 1
            current = slug_to_parent.get(current)
        depths[slug] = d
    return depths


def _normalize_knowledge_level(level: str) -> str:
    """Normalize knowledge_level: 'index' becomes 'root'."""
    if level == "index":
        return "root"
    return level


def _validate_hierarchy(parsed_files: list[dict]) -> list[str]:
    """Validate parent/child relationships. Returns warning messages."""
    warnings: list[str] = []
    all_slugs = {pf["slug"] for pf in parsed_files}

    slug_to_parent: dict[str, str | None] = {}
    slug_to_children: dict[str, list[str]] = {}

    for pf in parsed_files:
        slug_to_parent[pf["slug"]] = pf["parent_slug"]
        try:
            children = json.loads(pf["children_slugs"])
        except (json.JSONDecodeError, TypeError):
            children = []
        slug_to_children[pf["slug"]] = children

    for pf in parsed_files:
        slug = pf["slug"]
        parent = pf["parent_slug"]

        # Check parent exists
        if parent and parent not in all_slugs:
            warnings.append(f"{slug}: parent '{parent}' does not exist")

        # Check children exist
        children = slug_to_children.get(slug, [])
        for child in children:
            if child not in all_slugs:
                warnings.append(f"{slug}: child '{child}' does not exist")

        # Symmetry: if I declare a parent, that parent should list me as a child
        if parent and parent in slug_to_children:
            parent_children = slug_to_children[parent]
            if slug not in parent_children:
                warnings.append(f"{slug}: parent '{parent}' does not list this slug as a child")

        # Check for circular references (max depth)
        if parent:
            visited = {slug}
            current = parent
            depth = 0
            while current and depth < _MAX_HIERARCHY_DEPTH:
                if current in visited:
                    warnings.append(f"{slug}: circular reference detected via '{current}'")
                    break
                visited.add(current)
                current = slug_to_parent.get(current)
                depth += 1
            if depth >= _MAX_HIERARCHY_DEPTH:
                warnings.append(f"{slug}: hierarchy exceeds max depth ({_MAX_HIERARCHY_DEPTH})")

    return warnings


async def _recalculate_inbound_links(
    db_session, project_id, directory: Path, KnowledgeMetadata,
    content_cache: dict[str, str] | None = None,
) -> None:
    """Count how many files link to each slug and update cross_ref_density."""
    from sqlalchemy import select

    result = await db_session.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.project_id == project_id,
            KnowledgeMetadata.is_archived == False,  # noqa: E712
        )
    )
    all_items = result.scalars().all()

    # Parse cross_references once, cache for both passes
    refs_cache: dict[str, list[str]] = {}
    inbound: dict[str, int] = {}
    for km in all_items:
        try:
            refs = json.loads(km.cross_references or "[]")
        except (json.JSONDecodeError, TypeError):
            refs = []
        refs_cache[km.knowledge_id] = refs
        for ref in refs:
            inbound[ref] = inbound.get(ref, 0) + 1

    for km in all_items:
        outbound = len(refs_cache.get(km.knowledge_id, []))
        total = outbound + inbound.get(km.slug, 0)
        # Bonus for root/index files: children count contributes to density
        if km.knowledge_level in ("index", "root") and km.children_slugs:
            try:
                children_count = len(json.loads(km.children_slugs))
            except (json.JSONDecodeError, TypeError):
                children_count = 0
            total += children_count
        km.cross_ref_density = min(100.0, total * 10.0)

        # Recompute full score using scorer (uses cached content when available)
        days = 0.0
        if km.updated_at:  # UTC-aware via api.models UTCDateTime
            days = (datetime.now(timezone.utc) - km.updated_at).total_seconds() / 86400
        cached_content = content_cache.get(km.fs_path) if content_cache else None
        result = compute_completeness(km.fs_path, days, inbound.get(km.slug, 0), content=cached_content)
        km.completeness_score = result.final_score
        km.coverage_breadth = result.coverage_breadth
        km.recency_score = result.recency_score
        km.agent_gap_score = result.agent_gap_score


async def reindex_one(
    fs_path: str,
    project_id: str | None,
    db_session,
) -> dict:
    """Re-index a single knowledge file. Called from the sql_query tool."""
    path = Path(fs_path)
    if not path.exists():
        return {"error": f"File not found: {fs_path}"}

    try:
        from api.models.knowledge import KnowledgeMetadata, KnowledgeVersion
    except ImportError:
        return {"error": "DB models not available"}

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    content = path.read_text(encoding="utf-8")
    post = frontmatter.loads(content)
    meta = post.metadata
    body = post.content

    parts = path.parts
    try:
        # The knowledge root is the LAST "knowledge" directory segment on the
        # path — taking the first one mis-derives the slug when the deployment
        # path itself contains a "knowledge" dir (e.g. /app/knowledge/{slug}/
        # knowledge/foo.md would produce "{slug}/knowledge/foo").
        k_idx = next(i for i in range(len(parts) - 1, -1, -1) if parts[i] == "knowledge")
        slug = "/".join(parts[k_idx + 1:]).replace("\\", "/").removesuffix(".md")
    except StopIteration:
        slug = path.stem

    title = meta.get("title") or path.stem.replace("-", " ").title()
    category = meta.get("category") or slug.split("/")[0]
    new_hash = _content_hash(content)
    word_count = len(body.split())
    tags = json.dumps(_parse_tags(meta))
    cross_refs_list = _parse_cross_refs(body)
    cross_refs = json.dumps(cross_refs_list)
    knowledge_level = _normalize_knowledge_level(meta.get("knowledge_level", "auto"))
    parent_slug_val = meta.get("parent", None)
    children_slugs_val = json.dumps(meta.get("children", []))
    summary_val = meta.get("summary", "")

    # Compute depth by walking parent chain in DB
    depth_val = 0
    if parent_slug_val:
        current_parent = parent_slug_val
        while current_parent and depth_val < _MAX_HIERARCHY_DEPTH:
            depth_val += 1
            parent_row = await db_session.execute(
                select(KnowledgeMetadata.parent_slug).where(
                    KnowledgeMetadata.slug == current_parent,
                    (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
                )
            )
            row = parent_row.scalar_one_or_none()
            current_parent = row if row else None

    try:
        file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        # Removed between the exists() precheck above and this stat (TOCTOU)
        # — same contract as the precheck: report not found, don't crash.
        return {"error": f"File not found: {fs_path}"}
    days_since = _days_since(file_mtime)

    # Compute inbound links for this slug
    inbound_result = await db_session.execute(
        select(KnowledgeMetadata.cross_references).where(
            KnowledgeMetadata.project_id == project_id,
            KnowledgeMetadata.is_archived == False,  # noqa: E712
        )
    )
    inbound_count = 0
    for (refs_json,) in inbound_result.all():
        if refs_json:
            try:
                if slug in json.loads(refs_json):
                    inbound_count += 1
            except (json.JSONDecodeError, TypeError):
                pass

    completeness = compute_completeness(str(path), days_since, inbound_count)

    result = await db_session.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.slug == slug,
            KnowledgeMetadata.project_id == project_id,
        )
    )
    km = result.scalar_one_or_none()

    created = False
    if km is None:
        km = KnowledgeMetadata(
            project_id=project_id,
            slug=slug,
            title=title,
            category=category,
            fs_path=fs_path,
            completeness_score=completeness.final_score,
            coverage_breadth=completeness.coverage_breadth,
            recency_score=completeness.recency_score,
            cross_ref_density=completeness.cross_ref_density,
            agent_gap_score=completeness.agent_gap_score,
            tags=tags,
            cross_references=cross_refs,
            content_hash=new_hash,
            word_count=word_count,
            updated_at=file_mtime,
            knowledge_level=knowledge_level,
            parent_slug=parent_slug_val,
            children_slugs=children_slugs_val,
            summary=summary_val,
            depth=depth_val,
        )
        db_session.add(km)
        # SAVEPOINT-guarded flush — a concurrent index_directory/
        # reindex_one may INSERT this slug between our SELECT and flush
        # (unique constraint on project_id+slug). On a lost race, re-fetch
        # the winner's row and take the update branch below.
        try:
            async with db_session.begin_nested():
                await db_session.flush()
            created = True
        except IntegrityError:
            refetched = (await db_session.execute(
                select(KnowledgeMetadata).where(
                    KnowledgeMetadata.slug == slug,
                    KnowledgeMetadata.project_id == project_id,
                )
            )).scalars().first()
            if refetched is None:
                await db_session.rollback()
                return {"error": f"Concurrent reindex raced: {slug} not found after retry"}
            km = refetched

    if created:
        action = "created"
    else:
        old_version = KnowledgeVersion(
            knowledge_id=km.knowledge_id,
            version_num=km.version or 1,
            content=content,
            changed_by="indexer",
            change_summary="Reindex",
        )
        db_session.add(old_version)

        km.title = title
        km.category = category
        # the update branch dropped fs_path and the four component
        # scores. A moved file kept a stale fs_path → purge_residue later
        # treated it as residue and hard-deleted the row (+ versions); the
        # component scores stayed frozen while the final score moved.
        km.fs_path = fs_path
        km.completeness_score = completeness.final_score
        km.coverage_breadth = completeness.coverage_breadth
        km.recency_score = completeness.recency_score
        km.cross_ref_density = completeness.cross_ref_density
        km.agent_gap_score = completeness.agent_gap_score
        km.tags = tags
        km.cross_references = cross_refs
        km.content_hash = new_hash
        km.word_count = word_count
        km.updated_at = file_mtime
        km.version = (km.version or 0) + 1
        km.knowledge_level = knowledge_level
        km.parent_slug = parent_slug_val
        km.children_slugs = children_slugs_val
        km.summary = summary_val
        km.depth = depth_val
        action = "updated"

    await db_session.commit()

    return {
        "action": action,
        "slug": slug,
        "completeness_score": completeness.final_score,
        "word_count": word_count,
    }


async def _hard_delete_knowledge_id(kid: str, db_session) -> None:
    """Delete a knowledge row and its dependents (FK-safe order, no commit).

    knowledge_load_events has no ondelete clause, and knowledge_versions only
    has a DB-level CASCADE that SQLite tests do not enforce — so child rows
    are deleted explicitly before the metadata row. Caller commits once.
    """
    from sqlalchemy import text

    await db_session.execute(
        text("DELETE FROM knowledge_load_events WHERE knowledge_id = :kid"),
        {"kid": kid},
    )
    await db_session.execute(
        text("DELETE FROM knowledge_versions WHERE knowledge_id = :kid"),
        {"kid": kid},
    )
    await db_session.execute(
        text("DELETE FROM knowledge_metadata WHERE knowledge_id = :kid"),
        {"kid": kid},
    )


async def delete_one(
    slug: str,
    project_id: str | None,
    db_session,
) -> dict:
    """Hard-delete a single knowledge_metadata row (children first). Idempotent.

    Called from the knowledge_rw tool's delete/purge operations. Project rows
    are matched by exact project_id so a project-scoped call can never touch a
    global row (project_id NULL).
    """
    if db_session is None:
        return {"error": "No database session available"}

    from sqlalchemy import text

    if project_id is None:
        result = await db_session.execute(
            text(
                "SELECT knowledge_id FROM knowledge_metadata "
                "WHERE slug = :slug AND project_id IS NULL"
            ),
            {"slug": slug},
        )
    else:
        result = await db_session.execute(
            text(
                "SELECT knowledge_id FROM knowledge_metadata "
                "WHERE slug = :slug AND project_id = :pid"
            ),
            {"slug": slug, "pid": project_id},
        )
    kid = result.scalar_one_or_none()
    if kid is None:
        return {"action": "not_found", "slug": slug}

    await _hard_delete_knowledge_id(str(kid), db_session)
    await db_session.commit()
    return {"action": "deleted", "slug": slug, "knowledge_id": str(kid)}


async def purge_residue(
    project_id: str | None,
    db_session,
) -> dict:
    """Delete knowledge_metadata rows whose files no longer exist on disk.

    Cleans up stale rows left behind when a knowledge file was removed outside
    the knowledge_rw tool (e.g. via the filesystem tool) in earlier sessions.
    Criterion is fs_path-missing only — legacy rows may carry either
    is_archived value, so it is deliberately not consulted.
    """
    if db_session is None:
        return {"error": "No database session available"}

    from sqlalchemy import text

    if project_id is None:
        result = await db_session.execute(
            text(
                "SELECT knowledge_id, slug, fs_path FROM knowledge_metadata "
                "WHERE project_id IS NULL"
            )
        )
    else:
        result = await db_session.execute(
            text(
                "SELECT knowledge_id, slug, fs_path FROM knowledge_metadata "
                "WHERE project_id = :pid"
            ),
            {"pid": project_id},
        )

    deleted_slugs: list[str] = []
    for row in result.mappings().all():
        fs_path = row["fs_path"]
        # Empty string equals Path(".") (exists() is always True) so it would
        # never be purged; treat it as missing like a non-existent file.
        try:
            file_missing = (not fs_path) or (not Path(fs_path).exists())
        except (OSError, TypeError):
            file_missing = True
        if file_missing:
            await _hard_delete_knowledge_id(str(row["knowledge_id"]), db_session)
            deleted_slugs.append(row["slug"])

    if deleted_slugs:
        await db_session.commit()
    return {"deleted": deleted_slugs, "count": len(deleted_slugs)}


# ── CLI entry point ─────────────────────────────────────────────────────────

async def _cli_main(args: argparse.Namespace) -> None:
    db_session = None
    if not args.dry_run:
        try:
            import os, sys
            sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent / "api" / "src"))
            from api.database import AsyncSessionLocal
            db_session = AsyncSessionLocal()
        except Exception as e:
            print(f"Warning: Could not connect to DB ({e}). Running in dry-run mode.")

    if args.global_dir:
        global_dir = Path(args.global_dir)
        print(f"Indexing global knowledge: {global_dir}")
        stats = await index_directory(global_dir, project_id=None, db_session=db_session)
        print(f"  Scanned={stats['scanned']} Created={stats['created']} Updated={stats['updated']} Skipped={stats['skipped']} Errors={stats['errors']}")

    if args.project_dir and args.project_id:
        proj_dir = Path(args.project_dir) / "knowledge"
        print(f"Indexing project knowledge: {proj_dir} (project_id={args.project_id})")
        stats = await index_directory(proj_dir, project_id=args.project_id, db_session=db_session)
        print(f"  Scanned={stats['scanned']} Created={stats['created']} Updated={stats['updated']} Skipped={stats['skipped']} Errors={stats['errors']}")

    if db_session:
        await db_session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agents Universe knowledge indexer")
    parser.add_argument("--global-dir", help="Path to global knowledge/ directory")
    parser.add_argument("--project-dir", help="Path to project root (knowledge/ subdirectory will be indexed)")
    parser.add_argument("--project-id", help="Project UUID for project-scoped knowledge")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to DB")
    args = parser.parse_args()

    if not args.global_dir and not args.project_dir:
        parser.error("Provide --global-dir and/or --project-dir")

    asyncio.run(_cli_main(args))


if __name__ == "__main__":
    main()
