"""c7e4a9f2b6d1 must purge historical global _template/* rows (and their
children) while leaving other global knowledge untouched.

The buggy global indexer minted project_id NULL rows for every file under
knowledge/_template/; those duplicate every project's creation-time copy and
are undeletable from any project (delete_one matches exact project_id). The
purge is a one-time data migration — downgrade is deliberately a no-op
(the rows were pure indexer residue, unrecoverable and unrecoverable-needed).
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from api.models.knowledge import KnowledgeMetadata, KnowledgeVersion

_ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"
_PARENT_REV = "e4f6a8c1d273"


def _alembic_cfg() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "alembic"))
    # Same guard as conftest/api.main._run_migrations: without it env.py runs
    # fileConfig(alembic.ini), replacing the app's root log handlers.
    cfg.attributes["configure_logger"] = False
    return cfg


def _global_row(slug: str, fs_path: str) -> KnowledgeMetadata:
    return KnowledgeMetadata(
        project_id=None,
        category=slug.split("/")[0],
        slug=slug,
        title=slug,
        fs_path=fs_path,
        knowledge_level="root",
    )


async def _slugs(db) -> set[str]:
    result = await db.execute(
        select(KnowledgeMetadata.slug).where(KnowledgeMetadata.project_id == None)  # noqa: E711
    )
    return {str(s) for s in result.scalars().all()}


async def test_purge_removes_template_rows_and_children(db):
    cfg = _alembic_cfg()
    # One step back runs only c7e4a9f2b6d1's (empty) downgrade — the schema
    # is unchanged; this just rewinds alembic_version so upgrade re-plays the
    # purge against rows we insert now.
    command.downgrade(cfg, _PARENT_REV)

    kid_with_child = "purge-child-owner"
    kid_plain = "purge-plain"
    # knowledge_id has a client-side default; pin it so the child FK is exact.
    row_child = _global_row("_template/context", "/g/_template/context.md")
    row_child.knowledge_id = kid_with_child
    row_plain = _global_row("_template/history", "/g/_template/history.md")
    row_plain.knowledge_id = kid_plain
    db.add(row_child)
    db.add(row_plain)
    db.add(_global_row("system/framework-overview", "/g/system/framework-overview.md"))
    db.add(KnowledgeVersion(
        knowledge_id=kid_with_child, version_num=1, content="template snapshot",
    ))
    await db.commit()
    # Alembic opens its own sync connection to the same file — release any
    # write transaction on the async side first (SQLite single-writer).
    await db.rollback()

    try:
        command.upgrade(cfg, "head")

        # Async session may hold pre-purge state; expire so re-SELECTs hit the DB.
        db.expire_all()
        remaining = await _slugs(db)
        assert not any(s.startswith("_template/") for s in remaining), remaining
        assert "system/framework-overview" in remaining, remaining

        # The child row must go too: knowledge_load_events' FK has no ON DELETE
        # clause, so children-before-parent ordering is the tested contract.
        child_count = (await db.execute(
            text("SELECT COUNT(*) FROM knowledge_versions WHERE knowledge_id = :k"),
            {"k": kid_with_child},
        )).scalar()
        assert child_count == 0
        plain_count = (await db.execute(
            text("SELECT COUNT(*) FROM knowledge_metadata WHERE knowledge_id = :k"),
            {"k": kid_plain},
        )).scalar()
        assert plain_count == 0
    finally:
        # Idempotent: no-op when the upgrade already ran; restores head (and
        # purges our fixture rows) when the assertion above aborted mid-way.
        command.upgrade(cfg, "head")
        db.expire_all()


async def test_purge_is_a_noop_when_no_template_rows(db):
    cfg = _alembic_cfg()
    await db.rollback()
    try:
        command.downgrade(cfg, _PARENT_REV)
        # Distinct slug from the first test: (project_id NULL, slug) is the
        # unique index, and SQL Server treats NULLs as equal in UNIQUE
        # constraints — reusing the slug would trip it there even though
        # SQLite/PostgreSQL allow duplicate NULLs.
        db.add(_global_row("system/tool-reference", "/g/system/tool-reference.md"))
        await db.commit()
        await db.rollback()

        command.upgrade(cfg, "head")

        db.expire_all()
        remaining = await _slugs(db)
        assert "system/tool-reference" in remaining, remaining
        assert not any(s.startswith("_template/") for s in remaining), remaining
    finally:
        command.upgrade(cfg, "head")
        db.expire_all()
