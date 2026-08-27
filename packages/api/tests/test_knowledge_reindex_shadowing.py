"""Regression test for reindex_one depth-walk project shadowing.

A global knowledge row and a project row can share the same slug (project
shadowing). reindex_one's parent-depth walk previously matched both with a
dual-scope filter and used scalar_one_or_none() → MultipleResultsFound when
a project detail file's parent slug existed at both scopes. The walk must
prefer the project row (same semantics as loader._fetch_all_entries).
"""
from __future__ import annotations

from pathlib import Path

from agent_core.knowledge.index import reindex_one


async def test_reindex_one_depth_walk_shadowed_parent_uses_project_row(db, make_project, tmp_path):
    # Project workspace + a global-scope knowledge row and a project-scope row
    # sharing the parent slug (technical/api-map).
    project = await make_project(slug="shadow-test")
    ws = tmp_path / project.slug

    from api.models.knowledge import KnowledgeMetadata

    # Global row (project_id NULL) and project row (project_id = project.id)
    # for the SAME parent slug — the shadowing collision.
    db.add_all([
        KnowledgeMetadata(
            project_id=None,
            slug="technical/api-map",
            title="Global API Map",
            category="technical",
            fs_path="/g/technical/api-map.md",
            knowledge_level="root",
        ),
        KnowledgeMetadata(
            project_id=project.project_id,
            slug="technical/api-map",
            title="Project API Map",
            category="technical",
            fs_path="/p/technical/api-map.md",
            knowledge_level="root",
        ),
    ])
    await db.commit()

    # Detail file whose parent is the shadowed slug.
    detail_path = ws / "knowledge" / "technical" / "api" / "users-service.md"
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    detail_path.write_text(
        "---\ntitle: Users Service\ncategory: technical\nknowledge_level: detail\n"
        "parent: \"technical/api-map\"\n---\nBody.",
        encoding="utf-8",
    )

    result = await reindex_one(str(detail_path), project.project_id, db)

    # Previously: MultipleResultsFound (both rows matched the dual-scope
    # filter). Now the project row wins and the depth walk completes.
    assert result["action"] == "created"
    assert result["slug"] == "technical/api/users-service"

    # The project row must own the resulting row (project shadowing).
    from sqlalchemy import select

    check = await db.execute(
        select(KnowledgeMetadata).where(KnowledgeMetadata.slug == "technical/api/users-service")
    )
    row = check.scalars().one()
    assert row.parent_slug == "technical/api-map"
    assert row.project_id == project.project_id
    assert row.depth == 1
