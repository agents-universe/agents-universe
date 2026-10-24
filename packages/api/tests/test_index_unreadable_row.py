"""_recalculate_inbound_links must tolerate per-row file read failures.

Pass 1 of index_directory records a per-file parse failure as
stats["errors"] and moves on. Pass 2 then re-reads every DB row whose
fs_path was not parsed in this run — so a file corrupted after its row was
created (or a legacy row whose fs_path is "", which pathlib treats as the
current directory) raised straight out of index_directory, discarding the
whole batch before its commit. One bad .md made the project unindexable.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_core.knowledge import index as index_mod


async def _index_row(db, project_id: str, slug: str):
    from api.models.knowledge import KnowledgeMetadata

    return (await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.project_id == project_id,
            KnowledgeMetadata.slug == slug,
        )
    )).scalar_one()


@pytest.mark.asyncio
async def test_index_survives_file_corrupted_after_row_created(db, make_project, tmp_path):
    project = await make_project()
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "good.md").write_text(
        "---\ntitle: Good\ncategory: technical\n---\n\nHealthy body words here.\n",
        encoding="utf-8",
    )
    (kdir / "victim.md").write_text(
        "---\ntitle: Victim\ncategory: technical\n---\n\nVictim body words here.\n",
        encoding="utf-8",
    )

    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["created"] == 2, stats

    # Corrupt the file after its row exists. Pass 1 records the parse error;
    # pass 2's scorer re-read must not abort the batch before commit.
    (kdir / "victim.md").write_bytes(b"\xff\xfe\x00 not utf-8")

    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["errors"] == 1, stats


@pytest.mark.asyncio
async def test_index_survives_legacy_empty_fs_path(db, make_project, tmp_path):
    """purge_residue documents "" fs_path rows: pathlib maps them to the
    current directory, so a scorer re-read raises IsADirectoryError."""
    project = await make_project()
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "ok.md").write_text(
        "---\ntitle: OK\ncategory: technical\n---\n\nBody words here.\n",
        encoding="utf-8",
    )

    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["created"] == 1, stats

    row = await _index_row(db, project.project_id, "ok")
    row.fs_path = ""
    await db.commit()
    # The file must leave the scan, or pass 1 repairs fs_path before pass 2.
    (kdir / "ok.md").rename(kdir / "renamed.md")

    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["created"] == 1, stats
