"""reindex_one's version snapshot must be filed under the NEW version number.

The canonical convention is api.services.knowledge_service.write_knowledge_version:
the snapshot row carries version_num = (km.version or 0) + 1 with the NEW body,
and km.version is set to that same number. The indexer's update branch used to
file the snapshot under the PRE-bump number (with the new body as content),
producing two rows with the same version_num and none for the latest — the
version list attributed every snapshot to the wrong version.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_core.knowledge import index as index_mod


def _knowledge_dir(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir(exist_ok=True)
    f = kdir / "alpha.md"
    f.write_text(
        "---\ntitle: Alpha\ncategory: system\n---\n\nAlpha body text.\n",
        encoding="utf-8",
    )
    return kdir, f


@pytest.mark.asyncio
async def test_reindex_snapshot_is_numbered_like_the_api_path(db, make_project, tmp_path):
    project = await make_project()
    kdir, f = _knowledge_dir(tmp_path)

    # First index: row created at version 1 (model default), no snapshot.
    result = await index_mod.reindex_one(
        fs_path=str(f), project_id=project.project_id, db_session=db,
    )
    assert "error" not in result, result
    assert result.get("action") == "created"

    # Change the file → update branch writes the v2 snapshot.
    f.write_text(
        "---\ntitle: Alpha\ncategory: system\n---\n\nAlpha body text, edited.\n",
        encoding="utf-8",
    )
    result = await index_mod.reindex_one(
        fs_path=str(f), project_id=project.project_id, db_session=db,
    )
    assert "error" not in result, result
    assert result.get("action") == "updated"

    from api.models.knowledge import KnowledgeMetadata, KnowledgeVersion

    km = (
        await db.execute(
            select(KnowledgeMetadata).where(
                KnowledgeMetadata.project_id == project.project_id,
                KnowledgeMetadata.slug == "alpha",
            )
        )
    ).scalars().one()
    assert km.version == 2

    versions = (
        await db.execute(
            select(KnowledgeVersion).where(
                KnowledgeVersion.knowledge_id == km.knowledge_id
            )
        )
    ).scalars().all()
    assert [v.version_num for v in versions] == [2]
    assert "edited" in versions[0].content  # the snapshot holds the NEW body

    # A second edit keeps the numbering contiguous (no duplicate numbers).
    f.write_text(
        "---\ntitle: Alpha\ncategory: system\n---\n\nThird revision.\n",
        encoding="utf-8",
    )
    await index_mod.reindex_one(
        fs_path=str(f), project_id=project.project_id, db_session=db,
    )
    versions = (
        await db.execute(
            select(KnowledgeVersion).where(
                KnowledgeVersion.knowledge_id == km.knowledge_id
            ).order_by(KnowledgeVersion.version_num)
        )
    ).scalars().all()
    assert [v.version_num for v in versions] == [2, 3]
    assert "Third revision" in versions[1].content
