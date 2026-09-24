"""Slug-race recovery in knowledge/index.py must survive a lost INSERT race.

Concurrent index_directory/reindex_one runs can both SELECT "no row yet" and
then both INSERT the same (project_id, slug) — the loser's flush raises
IntegrityError and the SAVEPOINT-guarded recovery re-fetches the winner's row.
Recovery only works when the failed object does not stay pending in the
session: a leftover pending INSERT re-raises the same IntegrityError on the
next flush — outside any savepoint — and aborts the whole batch.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_core.knowledge import index as index_mod


def _knowledge_dir(tmp_path, name: str = "alpha.md"):
    kdir = tmp_path / "knowledge"
    kdir.mkdir(exist_ok=True)
    (kdir / name).write_text(
        "---\ntitle: Alpha\ncategory: system\n---\n\nAlpha body text.\n",
        encoding="utf-8",
    )
    return kdir


def _install_lost_race(db, project_id: str, slug: str, monkeypatch) -> None:
    """Insert a rival row for (project_id, slug) right before the first
    SAVEPOINT-guarded flush so our INSERT loses the unique-index race.

    The rival goes in through the SAME session and is flushed BEFORE the
    savepoint opens — same visibility as a second indexer committing between
    our SELECT and our flush. A second connection cannot do this on SQLite:
    the open transaction holds the file lock ("database is locked"). index.py
    adds its pending row before calling begin_nested, so that row is parked
    and re-added around the rival's flush — otherwise both would flush
    together and the RIVAL would lose the race.
    """
    real_begin_nested = db.begin_nested
    state = {"raced": False}

    def rigged_begin_nested():
        real_cm = real_begin_nested()

        class _RivalFirstCM:
            async def __aenter__(self):
                if not state["raced"]:
                    state["raced"] = True
                    from api.models.knowledge import KnowledgeMetadata

                    parked = list(db.sync_session.new)
                    for obj in parked:
                        db.sync_session.expunge(obj)
                    db.sync_session.add(KnowledgeMetadata(
                        project_id=project_id,
                        slug=slug,
                        category="system",
                        title="Rival",
                        fs_path="/tmp/rival.md",
                    ))
                    await db.flush()  # rival INSERTed outside the savepoint
                    for obj in parked:
                        db.sync_session.add(obj)
                return await real_cm.__aenter__()

            async def __aexit__(self, *exc):
                return await real_cm.__aexit__(*exc)

        return _RivalFirstCM()

    monkeypatch.setattr(db, "begin_nested", rigged_begin_nested)


@pytest.mark.asyncio
async def test_index_directory_recovers_from_lost_slug_race(db, make_project, tmp_path, monkeypatch):
    project = await make_project()
    kdir = _knowledge_dir(tmp_path)
    _install_lost_race(db, project.project_id, "alpha", monkeypatch)

    stats = await index_mod.index_directory(kdir, project.project_id, db)

    # The loser must fall through to the update branch, not abort the batch.
    assert stats["created"] == 0
    assert stats["updated"] == 1

    # Exactly one row for the slug — the winner's, carrying this file's data.
    from api.models.knowledge import KnowledgeMetadata

    rows = (await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.project_id == project.project_id,
            KnowledgeMetadata.slug == "alpha",
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Alpha"


@pytest.mark.asyncio
async def test_reindex_one_recovers_from_lost_slug_race(db, make_project, tmp_path, monkeypatch):
    project = await make_project()
    kdir = _knowledge_dir(tmp_path)
    _install_lost_race(db, project.project_id, "alpha", monkeypatch)

    result = await index_mod.reindex_one(
        fs_path=str(kdir / "alpha.md"),
        project_id=project.project_id,
        db_session=db,
    )

    assert "error" not in result, result
    assert result.get("action") == "updated"

    from api.models.knowledge import KnowledgeMetadata

    rows = (await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.project_id == project.project_id,
            KnowledgeMetadata.slug == "alpha",
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Alpha"
