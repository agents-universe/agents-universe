"""Regression test for reindex_one depth-walk project shadowing.

A global knowledge row and a project row can share the same slug (project
shadowing). reindex_one's parent-depth walk previously matched both with a
dual-scope filter and used scalar_one_or_none() → MultipleResultsFound when
a project detail file's parent slug existed at both scopes. The walk must
prefer the project row (same semantics as loader._fetch_all_entries).
"""
from __future__ import annotations

import json

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


async def test_parent_self_check_reads_the_project_row(db, make_project, tmp_path):
    """The parent self-check must compare against the shadowing project row.

    Both scopes can carry the parent slug, and the query takes one row. With
    no ordering the global row won, so its children list — which does not
    know about this project's child — produced a false "parent does not list
    this child" warning on every write even though the project row was in
    sync.
    """
    project = await make_project(slug="shadow-selfcheck")
    ws = tmp_path / project.slug

    from api.models.knowledge import KnowledgeMetadata

    db.add_all([
        KnowledgeMetadata(
            project_id=None,
            slug="technical/api-map",
            title="Global API Map",
            category="technical",
            fs_path="/g/technical/api-map.md",
            knowledge_level="root",
            children_slugs=json.dumps([]),
        ),
        KnowledgeMetadata(
            project_id=project.project_id,
            slug="technical/api-map",
            title="Project API Map",
            category="technical",
            fs_path="/p/technical/api-map.md",
            knowledge_level="root",
            children_slugs=json.dumps(["technical/api/users-service"]),
        ),
    ])
    await db.commit()

    detail_path = ws / "knowledge" / "technical" / "api" / "users-service.md"
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    detail_path.write_text(
        "---\ntitle: Users Service\ncategory: technical\nknowledge_level: detail\n"
        "parent: \"technical/api-map\"\n---\nBody.",
        encoding="utf-8",
    )

    # Which of the two matching rows LIMIT 1 returns depends on the dialect's
    # plan (SQLite's OR index scan happens to visit the project row first;
    # a bitmap scan does not), so also pin the statement itself: the
    # self-check must order project-first rather than trust plan luck.
    captured: list[str] = []
    real_execute = db.execute

    async def _capture(statement, *args, **kwargs):
        captured.append(str(statement))
        return await real_execute(statement, *args, **kwargs)

    db.execute = _capture
    try:
        result = await reindex_one(str(detail_path), project.project_id, db)
    finally:
        db.execute = real_execute

    assert result["action"] == "created"
    assert not any("does not list" in w for w in result["warnings"]), result

    self_checks = [
        sql for sql in captured
        if "knowledge_metadata.children_slugs" in sql
        and "OR knowledge_metadata.project_id IS NULL" in sql
    ]
    assert self_checks, captured
    assert "ORDER BY CASE" in self_checks[0]
