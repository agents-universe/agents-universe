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
    assert set(result.deferred_entries) == {"system/overview"}


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
