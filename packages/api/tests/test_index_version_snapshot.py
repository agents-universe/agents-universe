"""index_directory must snapshot content when it bumps a file's version.

The version number was incremented in place with no knowledge_versions row,
so the history skipped the newest version entirely and no snapshot matched
the current file body.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_core.knowledge import index as index_mod


def _write_file(kdir, body: str) -> None:
    kdir.mkdir(exist_ok=True)
    (kdir / "alpha.md").write_text(
        "---\ntitle: Alpha\ncategory: technical\n---\n\n" + body + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_index_directory_snapshots_updated_content(db, make_project, tmp_path):
    from api.models.knowledge import KnowledgeMetadata, KnowledgeVersion

    project = await make_project()
    kdir = tmp_path / "knowledge"
    _write_file(kdir, "first body")

    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["created"] == 1, stats

    km = (await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.project_id == project.project_id,
            KnowledgeMetadata.slug == "alpha",
        )
    )).scalar_one()
    # The create branch stores no snapshot; the row itself is version 1.
    assert km.version == 1
    first_versions = (await db.execute(
        select(KnowledgeVersion).where(
            KnowledgeVersion.knowledge_id == km.knowledge_id
        )
    )).scalars().all()
    assert first_versions == []

    _write_file(kdir, "second body")
    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["updated"] == 1, stats

    await db.refresh(km)
    assert km.version == 2
    versions = (await db.execute(
        select(KnowledgeVersion).where(
            KnowledgeVersion.knowledge_id == km.knowledge_id
        )
    )).scalars().all()
    assert len(versions) == 1
    assert versions[0].version_num == 2
    assert "second body" in versions[0].content

    # Unchanged content is skipped: no further bump, no duplicate snapshot.
    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["skipped"] == 1, stats
    await db.refresh(km)
    assert km.version == 2
    versions = (await db.execute(
        select(KnowledgeVersion).where(
            KnowledgeVersion.knowledge_id == km.knowledge_id
        )
    )).scalars().all()
    assert len(versions) == 1
