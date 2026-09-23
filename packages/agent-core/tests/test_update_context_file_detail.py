"""update_context_file three-branch behavior for detail/deferred writes.

A detail file written mid-conversation must NEVER enter the static
loaded_content: the tool result already carries the body this turn (double
injection), and next turn's context rebuild would drop it anyway (leaving a
stale full text behind the summary-only deferred row).
"""
from __future__ import annotations

from agent_core.knowledge.loader import (
    KnowledgeContextResult,
    KnowledgeEntry,
    update_context_file,
)

_DETAIL_CONTENT = (
    "---\ntitle: Users Service\nknowledge_level: detail\nparent: technical/api-map\n"
    "summary: users endpoints\n---\nGET /users lists users.\n"
)

_PRIMARY_CONTENT = "---\ntitle: Context\n---\nPrimary body text.\n"


def _deferred_entry(slug: str = "technical/api/users") -> KnowledgeEntry:
    return KnowledgeEntry(
        knowledge_id="db:1",
        slug=slug,
        title="Users Service",
        fs_path="/p/knowledge/technical/api/users.md",
        category="technical",
        cross_references=[],
        word_count=0,
        knowledge_level="detail",
        parent_slug="technical/api-map",
        summary="old summary",
        project_id="p1",
    )


def test_deferred_write_never_enters_loaded_content():
    ctx = KnowledgeContextResult()
    ctx.deferred_entries["technical/api/users"] = _deferred_entry()

    update_context_file(ctx, "technical/api/users", _DETAIL_CONTENT)

    assert "technical/api/users" not in ctx.loaded_content
    entry = ctx.deferred_entries["technical/api/users"]
    assert entry.summary == "users endpoints"  # frontmatter summary wins
    assert entry.word_count > 0
    assert entry.knowledge_level == "detail"
    assert entry.parent_slug == "technical/api-map"


def test_deferred_write_clears_dirty_static_copy():
    """A slug wrongly sitting in loaded_content is dropped, not duplicated."""
    ctx = KnowledgeContextResult()
    ctx.deferred_entries["technical/api/users"] = _deferred_entry()
    ctx.loaded_content["technical/api/users"] = "stale full text"

    update_context_file(ctx, "technical/api/users", _DETAIL_CONTENT)

    assert "technical/api/users" not in ctx.loaded_content


def test_new_detail_write_creates_deferred_entry():
    """Writing a brand-new detail file surfaces it as deferred immediately."""
    ctx = KnowledgeContextResult()

    update_context_file(ctx, "technical/api/new-service", _DETAIL_CONTENT)

    assert "technical/api/new-service" not in ctx.loaded_content
    entry = ctx.deferred_entries["technical/api/new-service"]
    assert entry.knowledge_level == "detail"
    assert entry.parent_slug == "technical/api-map"
    assert entry.summary == "users endpoints"


def test_auto_with_parent_write_is_deferred():
    """The documented auto+parent rule applies to writes too."""
    ctx = KnowledgeContextResult()
    content = (
        "---\ntitle: Child\nparent: domain/context\nsummary: a child\n---\nChild body.\n"
    )

    update_context_file(ctx, "domain/child", content)

    assert "domain/child" not in ctx.loaded_content
    assert "domain/child" in ctx.deferred_entries


def test_dynamic_load_refresh_still_updates_content():
    ctx = KnowledgeContextResult()
    ctx.dynamically_loaded["technical/api/users"] = "old"

    update_context_file(ctx, "technical/api/users", _DETAIL_CONTENT)

    assert ctx.dynamically_loaded["technical/api/users"] == _DETAIL_CONTENT
    assert "technical/api/users" not in ctx.loaded_content


def test_primary_write_updates_static_content_and_word_count():
    ctx = KnowledgeContextResult()
    entry = KnowledgeEntry(
        knowledge_id="db:2", slug="domain/context", title="Context",
        fs_path="/p/knowledge/domain/context.md", category="domain",
        cross_references=[], word_count=1, project_id="p1",
    )
    ctx.loaded_entries.append(entry)

    update_context_file(ctx, "domain/context", _PRIMARY_CONTENT)

    assert ctx.loaded_content["domain/context"] == _PRIMARY_CONTENT
    assert entry.word_count == len(_PRIMARY_CONTENT.split())


def test_log_role_content_is_ignored():
    ctx = KnowledgeContextResult()
    ctx.loaded_content["system/history"] = "old"

    update_context_file(
        ctx, "system/history",
        "---\nknowledge_role: log\n---\n- 2026-01-01 | new entry",
    )

    assert ctx.loaded_content["system/history"] == "old"
