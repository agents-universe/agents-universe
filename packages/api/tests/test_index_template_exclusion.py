"""_template/ must never become global knowledge rows.

The deployment entrypoint runs `index --global-dir ./knowledge` on every
start. The indexer used to walk _template/ as well, minting 29
project_id NULL rows (`_template/<file>`) whose titles duplicate every
project's creation-time copy — while delete_one matches an exact project_id
and can never remove them, so the knowledge panel showed the whole template
set twice with the extra copy undeletable. The template source tree now
reaches projects only via project creation and the copy endpoint.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from agent_core.knowledge import index as index_mod
from api.models.knowledge import KnowledgeMetadata


def _template_tree(tmp_path: Path) -> Path:
    kdir = tmp_path / "knowledge"
    (kdir / "_template").mkdir(parents=True)
    (kdir / "system").mkdir()
    (kdir / "_template" / "context.md").write_text(
        "---\ntitle: Context\ncategory: domain\nslug: domain/context\n---\nBody.\n",
        encoding="utf-8",
    )
    (kdir / "system" / "intro.md").write_text(
        "---\ntitle: Intro\ncategory: system\n---\nBody.\n",
        encoding="utf-8",
    )
    return kdir


async def _slugs(db, project_id) -> list[str]:
    """Slugs at one scope (None = global rows)."""
    result = await db.execute(
        select(KnowledgeMetadata.slug).where(KnowledgeMetadata.project_id == project_id)
    )
    return sorted(str(s) for s in result.scalars().all())


async def test_index_directory_global_scope_skips_template_tree(db, tmp_path):
    kdir = _template_tree(tmp_path)

    try:
        await index_mod.index_directory(kdir, project_id=None, db_session=db)

        slugs = await _slugs(db, None)
        assert "system/intro" in slugs, slugs
        assert not any(s.startswith("_template/") for s in slugs), slugs
        # The exclusion must not over-reach: only the top-level _template/ dir
        # is skipped (project-scope behavior asserted in the next test).
        assert (kdir / "_template" / "context.md").exists()
    finally:
        # Global rows are session-shared across tests: leaving our
        # fixture-created `system/intro` behind would collide (same slug,
        # different fs_path) with any later test that inserts its own —
        # .limit(1) then picks a nondeterministic one of the two.
        await index_mod.delete_one("system/intro", None, db)


async def test_index_directory_project_scope_indexes_template_tree(db, make_project, tmp_path):
    """The guard is global-only — a project workspace indexing a _template/
    dir (never produced by creation/copy today) keeps its normal semantics."""
    project = await make_project()
    kdir = _template_tree(tmp_path)

    await index_mod.index_directory(kdir, project_id=project.project_id, db_session=db)

    slugs = await _slugs(db, project.project_id)
    assert "_template/context" in slugs, slugs
    assert "system/intro" in slugs, slugs


async def test_reindex_one_global_template_slug_skipped(db, tmp_path):
    kdir = _template_tree(tmp_path)
    src = kdir / "_template" / "context.md"

    result = await index_mod.reindex_one(str(src), project_id=None, db_session=db)

    # No "error" key: callers branch on it and a deliberate skip is not a
    # failure (reindex_knowledge would surface it as an exception result).
    assert result["action"] == "skipped", result
    assert result["reason"] == "template_not_indexed_globally"
    assert "error" not in result
    slugs = await _slugs(db, None)
    assert not any(s.startswith("_template/") for s in slugs), slugs
