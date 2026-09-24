"""system/history (knowledge_role: log) must stay out of the default context.

Two leak paths are guarded:
- Tier-1 static load — including when a rewrite drops the frontmatter (the
  skill's format example long showed the body alone, so that happens in
  practice) and the whole update log would load as a plain primary file.
- Tier-2 deferred listing — the indexer writes a row for every file, so the DB
  loop would otherwise advertise the log under "Available Detail Knowledge"
  in every system prompt.

The index row itself stays (cross-references / versions), and an explicit
knowledge_rw read/load still reaches the file.
"""
from __future__ import annotations

from agent_core.knowledge.cache import CachedProjectKnowledge
from agent_core.knowledge.loader import (
    KnowledgeContextResult,
    KnowledgeEntry,
    load_project_context,
    update_context_file,
)


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


_HISTORY_LOG_FM = (
    "---\ncategory: system\nslug: system/history\ntitle: Knowledge Update Log\n"
    "knowledge_role: log\n---\n"
    "<!-- format: - {date} | {source} | {description} -->\n"
)
_HISTORY_BODY_ONLY = "## Update Log\n\n- 2026-09-25 | test | appended entry\n"


def _write_kdir(tmp_path, history_text: str):
    kdir = tmp_path / "proj" / "knowledge"
    (kdir / "system").mkdir(parents=True)
    (kdir / "system" / "history.md").write_text(history_text, encoding="utf-8")
    (kdir / "domain").mkdir(parents=True)
    (kdir / "domain" / "context.md").write_text(
        "---\ntitle: Context\n---\nPRIMARY BODY\n", encoding="utf-8"
    )
    return kdir


async def test_history_with_log_frontmatter_in_neither_tier(tmp_path):
    """Baseline template shape: content not loaded AND not listed as deferred."""
    kdir = _write_kdir(tmp_path, _HISTORY_LOG_FM)
    entries = [_entry("system/history", str(kdir / "system" / "history.md"))]

    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
    )

    assert "system/history" not in result.loaded_content
    assert "system/history" not in result.deferred_entries
    assert "system/history" not in result.overflow_slugs
    # Control: a normal primary file still loads in full.
    assert "domain/context" in result.loaded_content


async def test_history_without_frontmatter_still_excluded(tmp_path):
    """A rewrite that dropped `knowledge_role: log` must not smuggle the log in."""
    kdir = _write_kdir(tmp_path, _HISTORY_BODY_ONLY)
    entries = [_entry("system/history", str(kdir / "system" / "history.md"))]

    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
    )

    assert "system/history" not in result.loaded_content
    assert "system/history" not in result.deferred_entries
    assert "domain/context" in result.loaded_content


async def test_history_disk_only_without_db_row_not_deferred(tmp_path):
    """Disk fallback must not resurrect the log either (reindex never ran)."""
    kdir = _write_kdir(tmp_path, _HISTORY_BODY_ONLY)

    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache([]),
        knowledge_dir=kdir,
    )

    assert "system/history" not in result.loaded_content
    assert "system/history" not in result.deferred_entries


async def test_other_log_role_file_never_in_context(tmp_path):
    """knowledge_role: log works at any slug, not just the canonical one."""
    kdir = _write_kdir(tmp_path, _HISTORY_LOG_FM)
    (kdir / "technical").mkdir(parents=True)
    (kdir / "technical" / "changelog.md").write_text(
        "---\ntitle: Changelog\nknowledge_role: log\n---\n- entry\n",
        encoding="utf-8",
    )
    entries = [
        _entry("system/history", str(kdir / "system" / "history.md")),
        _entry("technical/changelog", str(kdir / "technical" / "changelog.md")),
    ]

    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
    )

    assert "technical/changelog" not in result.loaded_content
    assert "technical/changelog" not in result.deferred_entries


async def test_history_oversized_not_registered_as_overflow(tmp_path, monkeypatch):
    """The slug guard runs before the size read — no overflow listing either."""
    import agent_core.knowledge.loader as loader

    monkeypatch.setattr(loader, "MAX_FILE_SIZE", 10)
    kdir = _write_kdir(tmp_path, _HISTORY_BODY_ONLY)  # larger than the cap
    entries = [_entry("system/history", str(kdir / "system" / "history.md"))]

    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=kdir,
    )

    assert "system/history" not in result.loaded_content
    assert "system/history" not in result.deferred_entries
    assert "system/history" not in result.overflow_slugs


async def test_legacy_path_without_knowledge_dir_skips_history(tmp_path):
    """knowledge_dir=None (old caches/tests) must not load the log content."""
    hist = tmp_path / "history.md"
    hist.write_text(_HISTORY_BODY_ONLY, encoding="utf-8")
    entries = [_entry("system/history", str(hist))]

    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
    )

    assert "system/history" not in result.loaded_content
    assert "system/history" not in result.deferred_entries


def test_body_only_history_write_not_injected():
    ctx = KnowledgeContextResult()

    update_context_file(ctx, "system/history", _HISTORY_BODY_ONLY)

    assert "system/history" not in ctx.loaded_content
    assert "system/history" not in ctx.deferred_entries


def test_history_write_leaves_stale_static_copy_untouched():
    """Log writes never inject; a stale copy is healed by the next context
    rebuild (Tier-1 slug guard), not by writing over it."""
    ctx = KnowledgeContextResult()
    ctx.loaded_content["system/history"] = "stale"

    update_context_file(ctx, "system/history", _HISTORY_BODY_ONLY)

    assert ctx.loaded_content["system/history"] == "stale"
