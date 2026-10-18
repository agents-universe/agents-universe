"""Hierarchy closure: write/delete keeps parent `children` in sync, and the
children operation answers from memory, DB, then disk (three tiers).

Before this, knowledge_rw write never touched the parent file (children lists
went stale the moment a detail file was created) and the children operation
only consulted the in-memory conversation context.
"""
from __future__ import annotations

import json

from agent_core.knowledge.loader import KnowledgeContextResult, KnowledgeEntry
from agent_core.tools.base import ToolContext
from agent_core.tools.knowledge_rw import KnowledgeRWTool


def _context(tmp_path, db=None, project_ctx=None):
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="u1",
        db_session=db,
        knowledge_cache=None,
        session=None,
        project_context=project_ctx,
    )


def _write_parent(kdir):
    parent = kdir / "technical" / "api-map.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntitle: API Map\nknowledge_level: root\nchildren: []\n---\nAPI index.\n",
        encoding="utf-8",
    )
    return parent


# ── write → parent children sync ────────────────────────────────────────────


async def test_write_child_adds_it_to_parent_children(tmp_path):
    kdir = tmp_path / "knowledge"
    _write_parent(kdir)
    tool = KnowledgeRWTool()
    ctx = _context(tmp_path)

    content = (
        "---\ntitle: Users Service\nknowledge_level: detail\n"
        'parent: "technical/api-map"\n---\nGET /users\n'
    )
    result = await tool.execute(
        {"operation": "write", "slug": "technical/api/users", "content": content},
        ctx,
    )

    assert result["changed"] is True
    import frontmatter

    parent_meta = frontmatter.loads(
        (kdir / "technical" / "api-map.md").read_text(encoding="utf-8")
    ).metadata
    assert "technical/api/users" in (parent_meta.get("children") or [])


async def test_write_child_updates_in_memory_parent_entry(tmp_path):
    kdir = tmp_path / "knowledge"
    _write_parent(kdir)
    proj_ctx = KnowledgeContextResult()
    parent_entry = KnowledgeEntry(
        knowledge_id="db:p", slug="technical/api-map", title="API Map",
        fs_path=str(kdir / "technical" / "api-map.md"), category="technical",
        cross_references=[], word_count=10, knowledge_level="root",
        children_slugs=[], project_id="p1",
    )
    proj_ctx.deferred_entries["technical/api-map"] = parent_entry

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, project_ctx=proj_ctx)
    content = "---\ntitle: S\nknowledge_level: detail\nparent: technical/api-map\n---\nb\n"

    await tool.execute(
        {"operation": "write", "slug": "technical/api/s", "content": content}, ctx
    )

    assert "technical/api/s" in parent_entry.children_slugs


async def test_write_child_idempotent_parent_children(tmp_path):
    """Writing the same child twice must not duplicate the children entry."""
    kdir = tmp_path / "knowledge"
    _write_parent(kdir)
    tool = KnowledgeRWTool()
    ctx = _context(tmp_path)
    content = "---\ntitle: S\nknowledge_level: detail\nparent: technical/api-map\n---\nb\n"

    await tool.execute(
        {"operation": "write", "slug": "technical/api/s", "content": content}, ctx
    )
    # Second write: content identical → changed False, no sync; force change.
    await tool.execute(
        {"operation": "write", "slug": "technical/api/s", "content": content + "\nmore"},
        ctx,
    )

    import frontmatter

    parent_meta = frontmatter.loads(
        (kdir / "technical" / "api-map.md").read_text(encoding="utf-8")
    ).metadata
    assert (parent_meta.get("children") or []).count("technical/api/s") == 1


# ── delete → parent children sync (reverse) ─────────────────────────────────


async def test_delete_child_removes_it_from_parent_children(tmp_path):
    kdir = tmp_path / "knowledge"
    parent = kdir / "technical" / "api-map.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntitle: API Map\nchildren: [technical/api/users]\n---\nAPI index.\n",
        encoding="utf-8",
    )
    child = kdir / "technical" / "api" / "users.md"
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text(
        "---\ntitle: Users\nknowledge_level: detail\nparent: technical/api-map\n---\nb\n",
        encoding="utf-8",
    )

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path)  # db None: file-level sync still runs

    result = await tool.execute(
        {"operation": "delete", "slug": "technical/api/users"}, ctx
    )
    assert result["success"] is True

    import frontmatter

    parent_meta = frontmatter.loads(parent.read_text(encoding="utf-8")).metadata
    assert "technical/api/users" not in (parent_meta.get("children") or [])


# ── children operation: three tiers ─────────────────────────────────────────


def _child_entry(slug: str, parent: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        knowledge_id=f"db:{slug}", slug=slug, title=slug, fs_path="",
        category=slug.split("/")[0], cross_references=[], word_count=1,
        knowledge_level="detail", parent_slug=parent, summary="s", depth=1,
    )


async def test_children_from_memory_context(tmp_path):
    proj_ctx = KnowledgeContextResult()
    proj_ctx.deferred_entries["technical/api/users"] = _child_entry(
        "technical/api/users", "technical/api-map"
    )
    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, project_ctx=proj_ctx)

    result = await tool.execute(
        {"operation": "children", "slug": "technical/api-map"}, ctx
    )

    assert result["count"] == 1
    assert result["children"][0]["slug"] == "technical/api/users"
    assert result["children"][0]["summary"] == "s"


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        class M:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        return M(self._rows)


class _DbWithChild:
    """Answers the children DB query with one row."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, stmt, params=None):
        return _RowsResult(self.rows)


async def test_children_from_db_when_memory_misses(tmp_path):
    db = _DbWithChild([{
        "slug": "technical/api/orders",
        "title": "Orders",
        "summary": "orders api",
        "depth": 1,
        "children_slugs": json.dumps([]),
        "fs_path": "",
    }])
    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=db)  # empty memory context

    result = await tool.execute(
        {"operation": "children", "slug": "technical/api-map"}, ctx
    )

    assert result["count"] == 1
    assert result["children"][0]["slug"] == "technical/api/orders"
    assert result["children"][0]["summary"] == "orders api"


async def test_children_from_disk_when_index_missing(tmp_path):
    """Reindex failure must not hide a child: frontmatter parent is the truth."""
    kdir = tmp_path / "knowledge"
    child = kdir / "technical" / "api" / "billing.md"
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text(
        "---\ntitle: Billing\nknowledge_level: detail\nparent: technical/api-map\n"
        "summary: billing endpoints\n---\nPOST /billing\n",
        encoding="utf-8",
    )
    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=None)  # no memory, no DB → disk tier

    result = await tool.execute(
        {"operation": "children", "slug": "technical/api-map"}, ctx
    )

    assert result["count"] == 1
    assert result["children"][0]["slug"] == "technical/api/billing"
    assert result["children"][0]["summary"] == "billing endpoints"


# ── scalar `children` frontmatter ───────────────────────────────────────────


def test_normalize_children_scalar_list_and_none():
    from agent_core.knowledge.loader import normalize_children

    assert normalize_children("technical/api-map") == ["technical/api-map"]
    assert normalize_children(["a", "b"]) == ["a", "b"]
    assert normalize_children(None) == []
    assert normalize_children("") == []


async def test_scalar_children_not_iterated_per_character_on_sync(tmp_path):
    """A parent authored with scalar `children: technical/api-map-old` must be
    rewritten as a list on sync — iterating the string yields its characters,
    which the write-back would persist as a per-character children list."""
    import frontmatter

    from agent_core.knowledge.index import sync_parent_children

    kdir = tmp_path / "knowledge"
    parent = kdir / "technical" / "api-map.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntitle: API Map\nknowledge_level: root\n"
        "children: technical/api-map-old\n---\nAPI index.\n",
        encoding="utf-8",
    )

    synced = await sync_parent_children(
        child_slug="technical/api/users",
        parent_slug="technical/api-map",
        project_id=None,
        db_session=None,
        knowledge_dir=kdir,
    )

    assert synced is True
    meta = frontmatter.loads(parent.read_text(encoding="utf-8")).metadata
    assert meta.get("children") == ["technical/api-map-old", "technical/api/users"]


async def test_list_counts_scalar_children_as_one(tmp_path):
    """The scan normalizes scalar children — one child, not one per character."""
    kdir = tmp_path / "knowledge"
    parent = kdir / "technical" / "api-map.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntitle: API Map\nknowledge_level: root\n"
        "children: technical/api-map-old\n---\nAPI index.\n",
        encoding="utf-8",
    )

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path)
    result = await tool.execute({"operation": "list"}, ctx)

    entry = next(f for f in result["files"] if f["slug"] == "technical/api-map")
    assert entry["children_count"] == 1


async def test_parent_reindex_failure_warns_without_raising(tmp_path, caplog):
    """The parent-reindex failure handler logs via module `_log` — a missing
    logger turned every warning path into NameError (surfacing only when a
    probe forced reindex_one to fail). The parent file must still count as
    synced: its frontmatter is the source of truth, the row catches up later.
    """
    import logging

    import agent_core.knowledge.index as index_mod

    kdir = tmp_path / "knowledge"
    parent = _write_parent(kdir)

    async def _boom(**kwargs):
        raise RuntimeError("forced parent reindex failure")

    real = index_mod.reindex_one
    index_mod.reindex_one = _boom
    try:
        from agent_core.knowledge.index import sync_parent_children

        with caplog.at_level(logging.WARNING, logger="agent_core.knowledge.index"):
            synced = await sync_parent_children(
                child_slug="technical/api/orders",
                parent_slug="technical/api-map",
                project_id="p1",
                db_session=object(),  # non-None → reindex branch runs
                knowledge_dir=kdir,
            )
    finally:
        index_mod.reindex_one = real

    assert synced is True
    assert "technical/api/orders" in parent.read_text(encoding="utf-8")
    assert any("reindex of parent" in r.message for r in caplog.records)


_KNOWLEDGE_METADATA_DDL = """
CREATE TABLE knowledge_metadata (
    knowledge_id TEXT PRIMARY KEY,
    project_id TEXT,
    slug TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    depth INTEGER DEFAULT 0,
    children_slugs TEXT,
    fs_path TEXT,
    parent_slug TEXT,
    is_archived BOOLEAN NOT NULL DEFAULT 0
)
"""


async def test_children_db_prefers_project_row_over_shadowed_global(tmp_path):
    """A project file shadows a global one with the same slug. The DB tier is
    first-wins, so its query must return the project row first regardless of
    physical row order."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_KNOWLEDGE_METADATA_DDL)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        for project_id, title in ((None, "Global"), ("p1", "Project")):
            await db.execute(
                text(
                    "INSERT INTO knowledge_metadata "
                    "(knowledge_id, project_id, slug, title, summary, depth, "
                    "children_slugs, fs_path, parent_slug, is_archived) "
                    "VALUES (:kid, :pid, 'technical/api/users', :title, 's', 1, "
                    "'[]', '', 'technical/api-map', 0)"
                ),
                {"kid": str(uuid.uuid4()), "pid": project_id, "title": title},
            )
        await db.commit()

        tool = KnowledgeRWTool()
        result = await tool.execute(
            {"operation": "children", "slug": "technical/api-map"},
            _context(tmp_path, db=db),
        )

    await engine.dispose()

    assert result["count"] == 1
    assert result["children"][0]["title"] == "Project"
