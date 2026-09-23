"""Turn-start rehydration of dynamically loaded detail files.

knowledge_load_events persists every knowledge_rw load/unload per
conversation; rehydrate_dynamic_entries folds those events at the start of
each turn and re-reads the active slugs from disk. Without this, the context
rebuild at each user message silently expired every load.
"""
from __future__ import annotations

from agent_core.knowledge.loader import (
    DYNAMIC_LOAD_MAX_BYTES,
    DYNAMIC_LOAD_MAX_FILES,
    KnowledgeContextResult,
    dynamic_budget_error,
    rehydrate_dynamic_entries,
)


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class FakeEventDb:
    """Minimal async session answering the rehydrate event query."""

    def __init__(self, rows):
        self.rows = rows
        self.added = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        return _Result(list(self.rows))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _write(kdir, slug, body="BODY", frontmatter=""):
    path = kdir / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


async def test_active_load_rehydrates_from_disk(tmp_path):
    from agent_core.knowledge.loader import KnowledgeEntry

    kdir = tmp_path / "knowledge"
    _write(kdir, "technical/api/users", "USER API DETAIL",
           'knowledge_level: detail\nsummary: users api')
    db = FakeEventDb([
        {"event_type": "load", "knowledge_id": "k1", "slug": "technical/api/users"},
    ])
    ctx = KnowledgeContextResult()
    # The slug starts in the deferred set (summary-only) as it would after a
    # project-selection load; rehydrate must move it into dynamic context.
    ctx.deferred_entries["technical/api/users"] = KnowledgeEntry(
        knowledge_id="k1", slug="technical/api/users", title="Users",
        fs_path="", category="technical", cross_references=[], word_count=0,
        knowledge_level="detail", summary="users api",
    )

    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1", db_session=db, knowledge_dir=kdir,
    )

    assert reloaded == ["technical/api/users"]
    assert "USER API DETAIL" in ctx.dynamically_loaded["technical/api/users"]
    record = ctx.dynamic_records["technical/api/users"]
    assert record.task_id is None  # not bound to any task — persists until unload
    # rehydrate pops the slug out of the deferred set (it is now in dynamic context)
    assert "technical/api/users" not in ctx.deferred_entries


async def test_last_event_unload_wins(tmp_path):
    kdir = tmp_path / "knowledge"
    _write(kdir, "technical/api/users", "BODY")
    db = FakeEventDb([
        {"event_type": "load", "knowledge_id": "k1", "slug": "technical/api/users"},
        {"event_type": "unload", "knowledge_id": "k1", "slug": "technical/api/users"},
    ])
    ctx = KnowledgeContextResult()

    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1", db_session=db, knowledge_dir=kdir,
    )

    assert reloaded == []
    assert ctx.dynamically_loaded == {}


async def test_missing_file_is_skipped_and_self_healed(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True)
    db = FakeEventDb([
        {"event_type": "load", "knowledge_id": "k1", "slug": "technical/api/gone"},
    ])
    ctx = KnowledgeContextResult()

    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1", db_session=db, knowledge_dir=kdir,
    )

    assert reloaded == []
    assert ctx.dynamically_loaded == {}
    # The stale load event is self-healed with an unload so it stops being
    # queried every turn (the api.models import may be unavailable in
    # agent-core tests — then no event is added, which is fine).
    if db.added:
        assert getattr(db.added[0], "event_type", None) == "unload"


async def test_rehydrate_respects_file_budget(tmp_path):
    kdir = tmp_path / "knowledge"
    for i in range(DYNAMIC_LOAD_MAX_FILES + 2):
        _write(kdir, f"domain/extra{i}", f"BODY {i}")
    rows = [
        {"event_type": "load", "knowledge_id": f"k{i}", "slug": f"domain/extra{i}"}
        for i in range(DYNAMIC_LOAD_MAX_FILES + 2)
    ]
    ctx = KnowledgeContextResult()

    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1", db_session=FakeEventDb(rows), knowledge_dir=kdir,
    )

    assert len(reloaded) == DYNAMIC_LOAD_MAX_FILES
    assert len(ctx.dynamically_loaded) == DYNAMIC_LOAD_MAX_FILES


async def test_rehydrate_respects_byte_budget(tmp_path):
    kdir = tmp_path / "knowledge"
    body = "x" * 400
    for i in range(3):
        _write(kdir, f"domain/big{i}", body)
    rows = [
        {"event_type": "load", "knowledge_id": f"k{i}", "slug": f"domain/big{i}"}
        for i in range(3)
    ]
    ctx = KnowledgeContextResult()
    # Pre-load a file that leaves room for exactly one ~410-byte file: the
    # first rehydrated file fits, the second must stop the loop.
    ctx.dynamically_loaded["domain/pre"] = "y" * (DYNAMIC_LOAD_MAX_BYTES - 500)

    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1", db_session=FakeEventDb(rows), knowledge_dir=kdir,
    )

    assert len(reloaded) == 1
    assert "domain/pre" in ctx.dynamically_loaded
    assert len(ctx.dynamically_loaded) == 2


async def test_rehydrate_skips_slugs_already_static(tmp_path):
    kdir = tmp_path / "knowledge"
    _write(kdir, "domain/context", "STATIC BODY")
    ctx = KnowledgeContextResult()
    ctx.loaded_content["domain/context"] = "STATIC BODY"

    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1",
        db_session=FakeEventDb([
            {"event_type": "load", "knowledge_id": "k1", "slug": "domain/context"},
        ]),
        knowledge_dir=kdir,
    )

    assert reloaded == []
    assert "domain/context" not in ctx.dynamically_loaded


async def test_no_db_session_is_noop(tmp_path):
    ctx = KnowledgeContextResult()
    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1", db_session=None, knowledge_dir=tmp_path,
    )
    assert reloaded == []


async def test_no_events_is_noop(tmp_path):
    ctx = KnowledgeContextResult()
    reloaded = await rehydrate_dynamic_entries(
        ctx, conversation_id="c1", db_session=FakeEventDb([]), knowledge_dir=tmp_path,
    )
    assert reloaded == []


# ── Budget gate used by knowledge_rw load ───────────────────────────────────


def test_budget_error_empty_context_passes():
    ctx = KnowledgeContextResult()
    assert dynamic_budget_error(ctx, 1024) is None


def test_budget_error_file_count_full():
    ctx = KnowledgeContextResult()
    for i in range(DYNAMIC_LOAD_MAX_FILES):
        ctx.dynamically_loaded[f"domain/f{i}"] = "x"
    err = dynamic_budget_error(ctx, 10)
    assert err is not None
    assert "DYNAMIC CONTEXT FULL" in err
    assert "unload" in err


def test_budget_error_byte_overflow():
    ctx = KnowledgeContextResult()
    ctx.dynamically_loaded["domain/f"] = "x" * (DYNAMIC_LOAD_MAX_BYTES + 1)
    err = dynamic_budget_error(ctx, 10)
    assert err is not None
    assert "BUDGET EXCEEDED" in err
