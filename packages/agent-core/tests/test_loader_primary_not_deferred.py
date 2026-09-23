"""Tier-1/Tier-2 separation: a primary file loaded in full from disk must not
also be listed under "Available Detail Knowledge" (deferred_entries).

The indexer writes a knowledge_metadata row for every .md file, including
primary ones, so the DB loop sees primary slugs too. Listing them as deferred
duplicated the prompt entry and let `knowledge_rw load` inject content that was
already in context.
"""
from __future__ import annotations

from agent_core.knowledge.cache import CachedProjectKnowledge
from agent_core.knowledge.loader import KnowledgeEntry, load_project_context


def _entry(
    slug: str,
    fs_path: str,
    project_id: str | None = "p1",
    level: str = "auto",
    summary: str = "",
) -> KnowledgeEntry:
    return KnowledgeEntry(
        knowledge_id=f"db:{slug}",
        slug=slug,
        title=slug,
        fs_path=fs_path,
        category=slug.split("/")[0],
        cross_references=[],
        word_count=1,
        knowledge_level=level,
        summary=summary,
        project_id=project_id,
    )


class _FakeCache:
    def __init__(self, entries):
        self._entries = entries

    async def get_or_load(self, project_id, db_session):
        return CachedProjectKnowledge(project_id=project_id, entries=self._entries, content={})


def _write_knowledge_dir(tmp_path):
    kdir = tmp_path / "proj" / "knowledge"
    (kdir / "domain").mkdir(parents=True)
    (kdir / "domain" / "primary.md").write_text(
        "---\ntitle: Primary\n---\nPRIMARY BODY\n", encoding="utf-8"
    )
    (kdir / "domain" / "detail.md").write_text(
        "---\ntitle: Detail\nknowledge_level: detail\nsummary: a detail file\n---\nDETAIL BODY\n",
        encoding="utf-8",
    )
    return kdir


async def test_primary_row_is_not_listed_as_deferred(tmp_path):
    kdir = _write_knowledge_dir(tmp_path)
    entries = [
        _entry("domain/primary", str(kdir / "domain" / "primary.md")),
        _entry(
            "domain/detail",
            str(kdir / "domain" / "detail.md"),
            level="detail",
            summary="a detail file",
        ),
    ]
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
    )
    assert set(result.loaded_content) == {"domain/primary"}
    assert set(result.deferred_entries) == {"domain/detail"}
    assert result.deferred_entries["domain/detail"].summary == "a detail file"


async def test_global_primary_row_still_reachable_as_deferred(tmp_path):
    """Framework knowledge (project_id NULL) has no on-disk Tier-1 copy in the
    project dir — it must stay discoverable through deferred_entries."""
    kdir = _write_knowledge_dir(tmp_path)
    fw = tmp_path / "framework" / "knowledge"
    fw.mkdir(parents=True)
    fw_file = fw / "system" / "overview.md"
    fw_file.parent.mkdir(parents=True)
    fw_file.write_text("---\ntitle: Overview\n---\nFRAMEWORK\n", encoding="utf-8")
    entries = [_entry("system/overview", str(fw_file), project_id=None)]

    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
        framework_knowledge_dir=fw,
    )
    assert "system/overview" not in result.loaded_content
    assert "system/overview" in result.deferred_entries
    # The disk fallback also surfaces the on-disk detail file (no DB row in
    # this fixture) — every deferred-level file is discoverable now.
    assert "domain/detail" in result.deferred_entries
    assert set(result.loaded_content) == {"domain/primary"}


async def test_oversized_primary_row_is_not_listed_as_deferred(tmp_path, monkeypatch):
    """Oversized files already surface via overflow_slugs — listing them as
    deferred too double-registers the same slug."""
    import agent_core.knowledge.loader as loader

    monkeypatch.setattr(loader, "MAX_FILE_SIZE", 10)
    kdir = _write_knowledge_dir(tmp_path)
    entries = [
        _entry("domain/primary", str(kdir / "domain" / "primary.md")),
    ]
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
    )
    assert "domain/primary" in result.overflow_slugs
    assert "domain/primary" not in result.deferred_entries


# ── Tier-1 hierarchy fields ─────────────────────────────────────────────────


def _write_hierarchy_dir(tmp_path):
    kdir = tmp_path / "proj" / "knowledge"
    (kdir / "technical").mkdir(parents=True)
    (kdir / "technical" / "api-map.md").write_text(
        "---\ntitle: API Map\nknowledge_level: root\n"
        "children: [technical/api/users]\nsummary: api index\n---\nAPI index body.\n",
        encoding="utf-8",
    )
    (kdir / "technical" / "api").mkdir()
    (kdir / "technical" / "api" / "users.md").write_text(
        "---\ntitle: Users\nknowledge_level: detail\nparent: technical/api-map\n"
        "summary: users endpoints\n---\nGET /users\n",
        encoding="utf-8",
    )
    # auto + parent: documented as deferred even without knowledge_level: detail
    (kdir / "technical" / "api" / "orders.md").write_text(
        "---\ntitle: Orders\nparent: technical/api-map\nsummary: orders endpoints\n"
        "---\nPOST /orders\n",
        encoding="utf-8",
    )
    # auto + parent with no summary of its own → derived-summary fallback
    (kdir / "technical" / "auto-child.md").write_text(
        "---\ntitle: Auto Child\nparent: technical/api-map\n---\nchild body\n",
        encoding="utf-8",
    )
    return kdir


async def test_primary_entries_carry_hierarchy_fields(tmp_path):
    kdir = _write_hierarchy_dir(tmp_path)
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache([]),
        knowledge_dir=kdir,
    )

    primary = next(e for e in result.loaded_entries if e.slug == "technical/api-map")
    assert primary.knowledge_level == "root"
    assert primary.children_slugs == ["technical/api/users"]
    assert primary.parent_slug is None
    assert primary.depth == 0
    assert primary.summary == "api index"


async def test_auto_with_parent_goes_to_deferred(tmp_path):
    """The documented rule — auto + parent defers like detail — now holds."""
    kdir = _write_hierarchy_dir(tmp_path)
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache([]),
        knowledge_dir=kdir,
    )

    # detail file: deferred, not loaded
    assert "technical/api/users" not in result.loaded_content
    assert "technical/api/users" in result.deferred_entries
    # auto + parent: deferred too (previously flooded the static region)
    assert "technical/api/orders" not in result.loaded_content
    assert "technical/api/orders" in result.deferred_entries
    assert "technical/auto-child" not in result.loaded_content
    assert "technical/auto-child" in result.deferred_entries
    # the root file stays primary
    assert "technical/api-map" in result.loaded_content


async def test_deferred_disk_entries_carry_hierarchy_and_summary(tmp_path):
    kdir = _write_hierarchy_dir(tmp_path)
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache([]),
        knowledge_dir=kdir,
    )

    users = result.deferred_entries["technical/api/users"]
    assert users.parent_slug == "technical/api-map"
    assert users.knowledge_level == "detail"
    assert users.depth == 1
    assert users.summary == "users endpoints"  # frontmatter summary
    # summary fallback: no frontmatter summary → derived from body
    auto_child = result.deferred_entries["technical/auto-child"]
    assert auto_child.summary  # non-empty derived summary
    assert auto_child.depth == 1


async def test_db_missing_detail_falls_back_to_disk(tmp_path):
    """A detail file whose index row is missing (reindex failed) must still
    appear in deferred_entries — previously it vanished entirely."""
    kdir = _write_hierarchy_dir(tmp_path)
    # DB has no rows at all — only the disk fallback can surface the details.
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache([]),
        knowledge_dir=kdir,
    )

    assert "technical/api/users" in result.deferred_entries
    assert "technical/api/orders" in result.deferred_entries


async def test_disk_fallback_does_not_duplicate_db_row(tmp_path):
    kdir = _write_hierarchy_dir(tmp_path)
    entries = [
        _entry(
            "technical/api/users",
            str(kdir / "technical" / "api" / "users.md"),
            level="detail",
            summary="users endpoints",
        ),
    ]
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
    )

    # Exactly one entry — the DB row wins over the disk fallback.
    assert "technical/api/users" in result.deferred_entries
    assert result.deferred_entries["technical/api/users"].knowledge_id == "db:technical/api/users"


async def test_invalid_cjk_slug_file_is_skipped_with_warning(tmp_path, caplog):
    """ASCII-only slugs are a traversal defense — CJK filenames are skipped
    (and logged) rather than half-loaded."""
    import logging

    kdir = tmp_path / "proj" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "api详情.md").write_text("---\ntitle: API 详情\n---\nbody\n", encoding="utf-8")
    (kdir / "ok.md").write_text("---\ntitle: OK\n---\nbody\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_core.knowledge"):
        result = await load_project_context(
            project_id="p1",
            db_session=None,
            cache=_FakeCache([]),
            knowledge_dir=kdir,
        )

    assert "ok" in result.loaded_content
    assert not any(s.startswith("api") for s in result.loaded_content)
    assert any("ASCII slug" in r.message for r in caplog.records)
