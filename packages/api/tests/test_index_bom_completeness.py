"""reindex_one must score completeness from the same BOM-stripped text as
index_directory.

reindex_one strips the BOM before parsing (content_hash, title, category...),
but its completeness call omitted ``content=`` — compute_completeness then
re-reads the file from disk with the BOM still attached. python-frontmatter
does not recognize a ``---`` block behind a BOM: metadata comes back empty
(category, template_words lost) and the frontmatter itself is counted as body
text. Same file, two entry points, two different stored scores.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_core.knowledge import index as index_mod


def _bom_knowledge_file(tmp_path, name: str = "beta.md"):
    kdir = tmp_path / "knowledge"
    kdir.mkdir(exist_ok=True)
    # template_words: 500 — with intact frontmatter the net word count is 0
    # (content_depth 0); with the BOM breaking the parse the baseline is lost
    # and the frontmatter words count toward depth, so the scores diverge.
    text = (
        "---\n"
        "title: Beta\n"
        "category: technical\n"
        "template_words: 500\n"
        "---\n\n"
        "## Section\n\nSome real body words here.\n"
    )
    (kdir / name).write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    return kdir


async def _stored_score(db, project_id: str, slug: str):
    from api.models.knowledge import KnowledgeMetadata

    return (await db.execute(
        select(KnowledgeMetadata.completeness_score).where(
            KnowledgeMetadata.project_id == project_id,
            KnowledgeMetadata.slug == slug,
        )
    )).scalar_one()


@pytest.mark.asyncio
async def test_reindex_one_scores_bom_file_like_index_directory(db, make_project, tmp_path):
    project = await make_project()
    kdir = _bom_knowledge_file(tmp_path)

    stats = await index_mod.index_directory(kdir, project.project_id, db)
    assert stats["created"] == 1, stats
    full_score = await _stored_score(db, project.project_id, "beta")

    result = await index_mod.reindex_one(
        fs_path=str(kdir / "beta.md"),
        project_id=project.project_id,
        db_session=db,
    )
    assert "error" not in result, result

    reindexed_score = await _stored_score(db, project.project_id, "beta")
    assert reindexed_score == full_score, (
        f"reindex_one scored {reindexed_score}, index_directory scored {full_score}"
    )
    assert result["completeness_score"] == full_score
