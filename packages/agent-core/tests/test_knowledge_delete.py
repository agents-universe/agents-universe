"""Tests for knowledge_rw delete/purge and the index.py DB deletion helpers.

Covers:
- delete_one: FK-safe row deletion, idempotency, project isolation
- purge_residue: stale-row cleanup based on missing fs_path
- knowledge_rw(operation="delete"): file + DB row removed together
- knowledge_rw(operation="purge"): DB-only residue cleanup
"""
from __future__ import annotations

from agent_core.knowledge.index import delete_one, purge_residue
from agent_core.knowledge.loader import (
    DynamicLoadRecord,
    KnowledgeContextResult,
    KnowledgeEntry,
)
from agent_core.tools.base import ToolContext
from agent_core.tools.knowledge_rw import KnowledgeRWTool


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        return next(iter(self._rows[0].values()))

    def mappings(self):
        return _Mappings(self._rows)


class FakeDb:
    """In-memory stand-in for an async SQLAlchemy session.

    Holds three tables (metadata / load_events / versions) as lists of dicts
    and dispatches raw-SQL statements by table-name substring. Records the
    order of executed DELETE statements for FK-order assertions.
    """

    def __init__(self, metadata=None, load_events=None, versions=None):
        self.metadata = list(metadata or [])
        self.load_events = list(load_events or [])
        self.versions = list(versions or [])
        self.deleted_order = []
        self.commits = 0

    _TABLE_TO_ATTR = {
        "knowledge_load_events": "load_events",
        "knowledge_versions": "versions",
        "knowledge_metadata": "metadata",
    }

    def _delete_from(self, table, params):
        kid = params["kid"]
        attr = self._TABLE_TO_ATTR[table]
        rows = getattr(self, attr)
        if rows and isinstance(rows[0], dict):
            setattr(self, attr, [r for r in rows if r.get("knowledge_id") != kid])
        else:
            setattr(self, attr, [k for k in rows if k != kid])
        self.deleted_order.append(table)
        return []

    def _select_metadata(self, sql, params):
        rows = self.metadata
        if "project_id IS NULL" in sql:
            rows = [r for r in rows if r["project_id"] is None]
        elif params and "pid" in params:
            rows = [r for r in rows if r["project_id"] == params["pid"]]
        if params and "slug" in params:
            rows = [r for r in rows if r["slug"] == params["slug"]]
        return _Result(rows)

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "DELETE" in sql:
            for table in ("knowledge_load_events", "knowledge_versions", "knowledge_metadata"):
                if table in sql:
                    self._delete_from(table, params)
            return _Result([])
        if "SELECT" in sql and "knowledge_metadata" in sql:
            return self._select_metadata(sql, params)
        return _Result([])

    async def commit(self):
        self.commits += 1

    async def add(self, *args, **kwargs):
        pass

    async def flush(self, *args, **kwargs):
        pass


class FakeCache:
    def __init__(self):
        self.invalidated = []
        self.invalidated_slugs = []

    def invalidate(self, project_id):
        self.invalidated.append(project_id)

    def invalidate_slug(self, project_id, slug):
        self.invalidated_slugs.append((project_id, slug))


class FakeEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, event, **kwargs):
        self.events.append((event, kwargs))


def _row(kid, slug, project_id, fs_path):
    return {"knowledge_id": kid, "slug": slug, "project_id": project_id, "fs_path": fs_path}


def _context(tmp_path, db=None, cache=None, emitter=None, project_ctx=None):
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="u1",
        db_session=db,
        knowledge_cache=cache,
        session=emitter,
        project_context=project_ctx,
    )


def _entry(slug: str, fs_path: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        knowledge_id=f"db:{slug}",
        slug=slug,
        title=slug,
        fs_path=fs_path,
        category=slug.split("/")[0],
        cross_references=[],
        word_count=1,
    )


# ---------------------------------------------------------------------------
# delete_one
# ---------------------------------------------------------------------------


async def test_delete_one_removes_row_and_dependents_fk_order():
    db = FakeDb(
        metadata=[_row("k1", "technical/foo", "p1", "/x/technical/foo.md")],
        load_events=["k1"],
        versions=["k1"],
    )
    result = await delete_one("technical/foo", "p1", db)

    assert result == {"action": "deleted", "slug": "technical/foo", "knowledge_id": "k1"}
    assert db.metadata == []
    assert db.load_events == []
    assert db.versions == []
    # FK-safe order: load_events (no ondelete) → versions → metadata
    assert db.deleted_order == ["knowledge_load_events", "knowledge_versions", "knowledge_metadata"]
    assert db.commits == 1


async def test_delete_one_missing_row_is_idempotent():
    db = FakeDb(metadata=[_row("k1", "technical/foo", "p1", "/x/foo.md")])
    result = await delete_one("technical/bar", "p1", db)

    assert result == {"action": "not_found", "slug": "technical/bar"}
    assert db.metadata == [_row("k1", "technical/foo", "p1", "/x/foo.md")]
    assert db.commits == 0


async def test_delete_one_project_isolation_and_global_untouched():
    row_a = _row("ka", "technical/foo", "p1", "/a/foo.md")
    row_b = _row("kb", "technical/foo", "p2", "/b/foo.md")
    row_g = _row("kg", "technical/foo", None, "/g/foo.md")
    db = FakeDb(metadata=[row_a, row_b, row_g])

    result = await delete_one("technical/foo", "p1", db)

    assert result["action"] == "deleted"
    assert db.metadata == [row_b, row_g]  # project p2 + global row untouched


# ---------------------------------------------------------------------------
# purge_residue
# ---------------------------------------------------------------------------


async def test_purge_residue_deletes_only_missing_files(tmp_path):
    alive = tmp_path / "technical" / "alive.md"
    alive.parent.mkdir(parents=True)
    alive.write_text("still here", encoding="utf-8")
    db = FakeDb(
        metadata=[
            _row("k1", "technical/alive", "p1", str(alive)),
            _row("k2", "technical/gone", "p1", str(tmp_path / "technical" / "gone.md")),
            _row("k3", "domain/gone2", "p1", str(tmp_path / "domain" / "gone2.md")),
        ]
    )

    result = await purge_residue("p1", db)

    assert result["count"] == 2
    assert sorted(result["deleted"]) == ["domain/gone2", "technical/gone"]
    assert [r["slug"] for r in db.metadata] == ["technical/alive"]
    assert db.deleted_order[-1] == "knowledge_metadata"
    assert db.commits == 1


async def test_purge_residue_nothing_stale(tmp_path):
    alive = tmp_path / "technical" / "alive.md"
    alive.parent.mkdir(parents=True)
    alive.write_text("still here", encoding="utf-8")
    db = FakeDb(metadata=[_row("k1", "technical/alive", "p1", str(alive))])

    result = await purge_residue("p1", db)

    assert result == {"deleted": [], "count": 0}
    assert db.commits == 0


# ---------------------------------------------------------------------------
# knowledge_rw delete
# ---------------------------------------------------------------------------


async def test_knowledge_rw_delete_removes_file_and_row(tmp_path):
    kdir = tmp_path / "knowledge"
    f = kdir / "technical" / "foo.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\ntitle: Foo\n---\nbody", encoding="utf-8")
    db = FakeDb(metadata=[_row("k1", "technical/foo", "p1", str(f))])
    cache = FakeCache()
    emitter = FakeEmitter()
    ctx = KnowledgeContextResult()
    ctx.deferred_entries["technical/foo"] = _entry("technical/foo", str(f))
    ctx.dynamically_loaded["technical/foo"] = "foo content"
    ctx.dynamic_records["technical/foo"] = DynamicLoadRecord(slug="technical/foo", loaded_at_turn=1, task_id=None)
    ctx.dynamically_loaded["domain/bar"] = "bar"
    ctx.dynamic_records["domain/bar"] = DynamicLoadRecord(slug="domain/bar", loaded_at_turn=1, task_id=None)
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=db, cache=cache, emitter=emitter, project_ctx=ctx)

    result = await tool.execute({"operation": "delete", "slug": "technical/foo"}, context)

    assert result["success"] is True
    assert result["file_deleted"] is True
    assert result["db_row"] == "deleted"
    assert not f.exists()
    assert db.metadata == []
    assert cache.invalidated == ["p1"]  # full invalidate, not invalidate_slug
    assert ("knowledge_updated", {"slug": "technical/foo"}) in emitter.events
    assert "technical/foo" not in ctx.deferred_entries  # conversation context cleaned
    assert "technical/foo" not in ctx.dynamically_loaded  # deleted entry unloaded
    assert "technical/foo" not in ctx.dynamic_records
    assert "domain/bar" in ctx.dynamically_loaded  # unrelated dynamic entries untouched


async def test_knowledge_rw_delete_file_already_missing(tmp_path):
    db = FakeDb(metadata=[_row("k1", "technical/foo", "p1", str(tmp_path / "knowledge" / "technical" / "foo.md"))])
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=db)

    result = await tool.execute({"operation": "delete", "slug": "technical/foo"}, context)

    assert result["success"] is True
    assert result["file_deleted"] is False
    assert result["db_row"] == "deleted"
    assert db.metadata == []


async def test_knowledge_rw_delete_without_db_session(tmp_path):
    kdir = tmp_path / "knowledge"
    f = kdir / "technical" / "foo.md"
    f.parent.mkdir(parents=True)
    f.write_text("body", encoding="utf-8")
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=None)

    result = await tool.execute({"operation": "delete", "slug": "technical/foo"}, context)

    assert result["success"] is True
    assert result["file_deleted"] is True
    assert result["db_row"] == "skipped"
    assert not f.exists()


async def test_knowledge_rw_delete_invalid_slug(tmp_path):
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=FakeDb())

    result = await tool.execute({"operation": "delete", "slug": "../evil"}, context)

    assert "error" in result


async def test_knowledge_rw_delete_file_confined_to_current_project(tmp_path):
    # Two project workspaces; delete in p1 must not touch p2's same-slug file.
    kdir_a = tmp_path / "proj-a" / "knowledge"
    kdir_b = tmp_path / "proj-b" / "knowledge"
    f_a = kdir_a / "technical" / "foo.md"
    f_b = kdir_b / "technical" / "foo.md"
    f_a.parent.mkdir(parents=True)
    f_b.parent.mkdir(parents=True)
    f_a.write_text("a", encoding="utf-8")
    f_b.write_text("b", encoding="utf-8")
    db = FakeDb(metadata=[_row("ka", "technical/foo", "p1", str(f_a))])
    tool = KnowledgeRWTool()
    context = _context(tmp_path / "proj-a", db=db)  # p1 conversation

    result = await tool.execute({"operation": "delete", "slug": "technical/foo"}, context)

    assert result["success"] is True
    assert not f_a.exists()
    assert f_b.exists()  # other project's file untouched
    assert db.metadata == []


async def test_knowledge_rw_delete_without_project_context_refused(tmp_path):
    tool = KnowledgeRWTool()
    context = ToolContext(
        project_id=None,  # no project scope — must refuse
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="u1",
        db_session=FakeDb(),
    )

    result = await tool.execute({"operation": "delete", "slug": "technical/foo"}, context)

    assert "error" in result
    assert context.db_session.metadata == []


async def test_knowledge_rw_purge_without_project_context_refused(tmp_path):
    tool = KnowledgeRWTool()
    context = ToolContext(
        project_id=None,
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="u1",
        db_session=FakeDb(),
    )

    result = await tool.execute({"operation": "purge"}, context)

    assert "error" in result
    assert context.db_session.metadata == []


# ---------------------------------------------------------------------------
# knowledge_rw purge
# ---------------------------------------------------------------------------


async def test_knowledge_rw_purge_all_residue(tmp_path):
    alive = tmp_path / "knowledge" / "technical" / "alive.md"
    alive.parent.mkdir(parents=True)
    alive.write_text("still here", encoding="utf-8")
    db = FakeDb(
        metadata=[
            _row("k1", "technical/alive", "p1", str(alive)),
            _row("k2", "technical/gone", "p1", str(tmp_path / "knowledge" / "technical" / "gone.md")),
        ]
    )
    cache = FakeCache()
    emitter = FakeEmitter()
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=db, cache=cache, emitter=emitter)

    result = await tool.execute({"operation": "purge"}, context)

    assert result == {"deleted": ["technical/gone"], "count": 1}
    assert [r["slug"] for r in db.metadata] == ["technical/alive"]
    assert cache.invalidated == ["p1"]
    assert emitter.events[0][0] == "knowledge_updated"


async def test_knowledge_rw_purge_single_slug(tmp_path):
    db = FakeDb(
        metadata=[
            _row("k1", "technical/foo", "p1", str(tmp_path / "knowledge" / "technical" / "foo.md")),
            _row("k2", "technical/bar", "p1", str(tmp_path / "knowledge" / "technical" / "bar.md")),
        ]
    )
    cache = FakeCache()
    emitter = FakeEmitter()
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=db, cache=cache, emitter=emitter)

    result = await tool.execute({"operation": "purge", "slug": "technical/foo"}, context)

    assert result["action"] == "deleted"
    assert [r["slug"] for r in db.metadata] == ["technical/bar"]
    assert cache.invalidated == ["p1"]
    assert len(emitter.events) == 1


async def test_knowledge_rw_purge_no_residue_no_emit(tmp_path):
    alive = tmp_path / "knowledge" / "technical" / "alive.md"
    alive.parent.mkdir(parents=True)
    alive.write_text("still here", encoding="utf-8")
    db = FakeDb(metadata=[_row("k1", "technical/alive", "p1", str(alive))])
    cache = FakeCache()
    emitter = FakeEmitter()
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=db, cache=cache, emitter=emitter)

    result = await tool.execute({"operation": "purge"}, context)

    assert result["count"] == 0
    assert cache.invalidated == []
    assert emitter.events == []


# ---------------------------------------------------------------------------
# knowledge_rw(operation="write") — UTF-8 byte cap keeps files loadable
# ---------------------------------------------------------------------------


async def test_write_caps_content_by_utf8_bytes(tmp_path):
    """The write cap must count UTF-8 BYTES (like loader.MAX_FILE_SIZE), not
    characters — a CJK-heavy body 3x over in bytes would silently fall out of
    context as an overflow file."""
    from agent_core.tools.knowledge_rw import _MAX_WRITE_BYTES

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=None)

    # Over the byte cap while far under it in characters.
    big = "中" * (_MAX_WRITE_BYTES // 3 + 10)
    result = await tool.execute(
        {"operation": "write", "slug": "technical/big", "content": big}, ctx
    )
    assert "error" in result
    assert "bytes" in result["error"]
    assert not (tmp_path / "knowledge" / "technical" / "big.md").exists()

    # Under the byte cap with multi-byte content: must be written.
    small = "中" * 100
    result = await tool.execute(
        {"operation": "write", "slug": "technical/small", "content": small}, ctx
    )
    assert "error" not in result
    assert result.get("changed") is True
    assert (tmp_path / "knowledge" / "technical" / "small.md").exists()
