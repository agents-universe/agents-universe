"""System-template copy endpoint, knowledge DELETE, and is_global marking.

Covers the three behaviors behind "duplicates + undeletable system
templates": the template list is disk-scanned (never a DB row), copy is
never-overwrite project-owned material, and DELETE removes project knowledge
while refusing framework (global) knowledge with 403.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter as _fm
import pytest
from sqlalchemy import select

from agent_core.knowledge import index as index_mod
from api.models.knowledge import KnowledgeMetadata
from api.paths import PROJECTS_ROOT

CONTEXT_MD = (
    "---\n"
    "title: 项目背景\n"
    "category: domain\n"
    "slug: domain/context\n"
    "summary: 项目的背景、目标与边界\n"
    "---\n\n"
    "## 背景\n\nSystem template body with a {{placeholder}}.\n"
)
# Frontmatter WITHOUT a slug key: the list must fall back to the relative
# path (flat under _template/, so the stem "flat") like project creation.
FLAT_MD = (
    "---\n"
    "title: Flat Doc\n"
    "category: system\n"
    "---\n\n"
    "Plain body.\n"
)
BOM_MD = (
    "---\n"
    "title: BOM Skill\n"
    "category: skills\n"
    "slug: skills/bom-skill\n"
    "---\n\n"
    "BOM-prefixed template body.\n"
)


@pytest.fixture
def templates_dir(tmp_path, monkeypatch) -> Path:
    tdir = tmp_path / "template_src"
    tdir.mkdir()
    (tdir / "context.md").write_text(CONTEXT_MD, encoding="utf-8")
    (tdir / "flat.md").write_text(FLAT_MD, encoding="utf-8")
    (tdir / "bom.md").write_bytes(b"\xef\xbb\xbf" + BOM_MD.encode("utf-8"))
    monkeypatch.setattr("api.routers.knowledge.KNOWLEDGE_TEMPLATE_DIR", tdir)
    return tdir


def _write(ws_slug: str, rel: str, content: str) -> Path:
    p = PROJECTS_ROOT / ws_slug / "knowledge" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _db_row(project_id: str | None, slug: str, fs_path: str) -> KnowledgeMetadata:
    return KnowledgeMetadata(
        project_id=project_id,
        category=slug.split("/")[0],
        slug=slug,
        title=slug,
        fs_path=fs_path,
        knowledge_level="detail",
    )


def _entry(items: list[dict], slug: str) -> dict:
    return next(i for i in items if i["slug"] == slug)


async def _get_row(db, project_id, slug: str):
    result = await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.slug == slug,
            KnowledgeMetadata.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# GET /knowledge/_templates
# ---------------------------------------------------------------------------


async def test_templates_route_not_shadowed_by_catchall(client, make_project, templates_dir):
    """The literal route must win over the {slug:path} catch-all.

    If registration order regressed, the catch-all would answer with a 400
    (KNOWLEDGE_SLUG_RE rejects a leading underscore) or a disk-fallback 404.
    """
    project = await make_project()
    resp = await client.get(f"/api/projects/{project.project_id}/knowledge/_templates")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert isinstance(items, list)

    ctx = _entry(items, "domain/context")
    assert ctx["title"] == "项目背景"
    assert ctx["category"] == "domain"
    assert ctx["summary"]
    assert ctx["exists"] is False

    # No frontmatter slug → relative-path fallback (flat layout = stem).
    flat = _entry(items, "flat")
    assert flat["title"] == "Flat Doc"
    assert flat["category"] == "system"
    assert flat["summary"] == ""

    # BOM source still parses (BOM stripped before frontmatter).
    assert _entry(items, "skills/bom-skill")["title"] == "BOM Skill"


async def test_exists_flag_tracks_workspace_file(client, make_project, templates_dir):
    project = await make_project()

    async def _exists() -> bool:
        resp = await client.get(f"/api/projects/{project.project_id}/knowledge/_templates")
        return _entry(resp.json(), "domain/context")["exists"]

    assert await _exists() is False
    _write(project.slug, "domain/context.md", "---\ntitle: 已有\n---\nbody")
    assert await _exists() is True


# ---------------------------------------------------------------------------
# POST /knowledge/_templates/copy
# ---------------------------------------------------------------------------


async def test_copy_creates_file_row_and_zeroes_scores(client, db, make_project, templates_dir):
    project = await make_project()
    src = templates_dir / "context.md"

    resp = await client.post(
        f"/api/projects/{project.project_id}/knowledge/_templates/copy",
        json={"slug": "domain/context"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"slug": "domain/context", "created": True}

    dest = PROJECTS_ROOT / project.slug / "knowledge" / "domain" / "context.md"
    assert dest.read_bytes() == src.read_bytes()

    row = await _get_row(db, project.project_id, "domain/context")
    assert row is not None
    assert str(row.project_id) == str(project.project_id)
    # Score parity with project creation: a freshly copied scaffold reads 0%,
    # not the indexer's structural score.
    assert row.completeness_score == 0.0
    assert row.coverage_breadth == 0.0
    assert row.recency_score == 0.0


async def test_copy_bom_source_copies_bytes_verbatim(client, make_project, templates_dir):
    project = await make_project()
    src = templates_dir / "bom.md"

    resp = await client.post(
        f"/api/projects/{project.project_id}/knowledge/_templates/copy",
        json={"slug": "skills/bom-skill"},
    )
    assert resp.status_code == 200, resp.text

    dest = PROJECTS_ROOT / project.slug / "knowledge" / "skills" / "bom-skill.md"
    assert dest.read_bytes() == src.read_bytes()
    assert dest.read_bytes().startswith(b"\xef\xbb\xbf")


async def test_copy_is_idempotent_and_never_overwrites(client, make_project, templates_dir):
    project = await make_project()
    url = f"/api/projects/{project.project_id}/knowledge/_templates/copy"
    dest = PROJECTS_ROOT / project.slug / "knowledge" / "domain" / "context.md"

    first = await client.post(url, json={"slug": "domain/context"})
    assert first.json()["created"] is True

    dest.write_text("USER EDIT", encoding="utf-8")
    second = await client.post(url, json={"slug": "domain/context"})
    assert second.status_code == 200
    assert second.json() == {"slug": "domain/context", "created": False}
    assert dest.read_text(encoding="utf-8") == "USER EDIT"


async def test_copy_traversal_and_unknown_slug_rejected(client, make_project, templates_dir):
    project = await make_project()
    url = f"/api/projects/{project.project_id}/knowledge/_templates/copy"

    resp = await client.post(url, json={"slug": "../../etc/passwd"})
    assert resp.status_code == 400
    # Leading underscore fails KNOWLEDGE_SLUG_RE (first char must be
    # alphanumeric) — _template/ can never be requested as a destination.
    resp = await client.post(url, json={"slug": "_template/context"})
    assert resp.status_code == 400
    resp = await client.post(url, json={"slug": "nope"})
    assert resp.status_code == 404

    kdir = PROJECTS_ROOT / project.slug / "knowledge"
    assert not (kdir.parent.parent / "etc").exists()
    if kdir.exists():
        assert list(kdir.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# DELETE /knowledge/{slug}
# ---------------------------------------------------------------------------


async def test_delete_global_row_is_403(client, db, make_project):
    project = await make_project()
    # A framework row with no project row and no project file fails the
    # ownership gate — read-only through every project-scoped path.
    db.add(_db_row(None, "technical/db-schema", "/g/technical/db-schema.md"))
    await db.commit()

    resp = await client.delete(
        f"/api/projects/{project.project_id}/knowledge/technical/db-schema"
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "global_knowledge"
    assert await _get_row(db, None, "technical/db-schema") is not None


async def test_delete_syncs_parent_children(client, db, make_project):
    project = await make_project()
    kdir = PROJECTS_ROOT / project.slug / "knowledge"
    _write(
        project.slug, "domain/parent.md",
        "---\ntitle: Parent\ncategory: domain\nchildren:\n  - domain/child\n---\nParent body.\n",
    )
    _write(
        project.slug, "domain/child.md",
        "---\ntitle: Child\ncategory: domain\nparent: domain/parent\n---\nChild body.\n",
    )
    await index_mod.index_directory(kdir, project.project_id, db)

    resp = await client.delete(f"/api/projects/{project.project_id}/knowledge/domain/child")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["file_deleted"] is True
    assert body["db_row"] == "deleted"

    assert not (kdir / "domain" / "child.md").exists()
    assert await _get_row(db, project.project_id, "domain/child") is None

    # Parent cleanup mirrors the write-side sync: file frontmatter first…
    parent_meta = _fm.loads((kdir / "domain" / "parent.md").read_text(encoding="utf-8"))
    assert "domain/child" not in (parent_meta.metadata.get("children") or [])
    # …and the parent's DB row follows (sync reindexes the parent).
    parent_row = await _get_row(db, project.project_id, "domain/parent")
    assert parent_row is not None
    assert "domain/child" not in parent_row.children_list


async def test_delete_disk_only_file_reports_not_found_row(client, make_project):
    project = await make_project()
    f = _write(project.slug, "domain/unindexed.md", "no metadata row")

    resp = await client.delete(f"/api/projects/{project.project_id}/knowledge/domain/unindexed")
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_deleted"] is True
    assert resp.json()["db_row"] == "not_found"
    assert not f.exists()


async def test_delete_shadowed_slug_keeps_global_row(client, db, make_project):
    """Project + global rows sharing a slug: only the project side goes away."""
    project = await make_project()
    f = _write(project.slug, "domain/shadowed-global.md", "project copy")
    db.add(_db_row(None, "domain/shadowed-global", "/g/domain/shadowed-global.md"))
    db.add(_db_row(str(project.project_id), "domain/shadowed-global", str(f)))
    await db.commit()

    resp = await client.delete(
        f"/api/projects/{project.project_id}/knowledge/domain/shadowed-global"
    )
    assert resp.status_code == 200, resp.text
    assert not f.exists()
    assert await _get_row(db, project.project_id, "domain/shadowed-global") is None
    assert await _get_row(db, None, "domain/shadowed-global") is not None


async def test_delete_missing_slug_404(client, make_project):
    project = await make_project()
    resp = await client.delete(f"/api/projects/{project.project_id}/knowledge/domain/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# is_global shapes + completeness scope
# ---------------------------------------------------------------------------


async def test_is_global_shapes(client, db, make_project, monkeypatch, tmp_path):
    fw = tmp_path / "framework" / "knowledge"
    gfile = fw / "system" / "global-only.md"
    gfile.parent.mkdir(parents=True)
    gfile.write_text("framework body", encoding="utf-8")
    monkeypatch.setattr("api.routers.knowledge.FRAMEWORK_KNOWLEDGE_DIR", fw)

    project = await make_project()
    pfile = _write(project.slug, "domain/project-owned.md", "project body")
    _write(project.slug, "domain/no-row.md", "disk-only body")
    db.add(_db_row(None, "system/global-only", str(gfile)))
    db.add(_db_row(str(project.project_id), "domain/project-owned", str(pfile)))
    await db.commit()

    listing = await client.get(f"/api/projects/{project.project_id}/knowledge")
    items = listing.json()
    assert _entry(items, "system/global-only")["is_global"] is True
    assert _entry(items, "domain/project-owned")["is_global"] is False

    detail = await client.get(f"/api/projects/{project.project_id}/knowledge/system/global-only")
    assert detail.status_code == 200
    assert detail.json()["is_global"] is True

    detail = await client.get(f"/api/projects/{project.project_id}/knowledge/domain/project-owned")
    assert detail.status_code == 200
    assert detail.json()["is_global"] is False

    # Disk fallback: a file with no row at either scope is project-owned.
    detail = await client.get(f"/api/projects/{project.project_id}/knowledge/domain/no-row")
    assert detail.status_code == 200
    assert detail.json()["is_global"] is False
    assert detail.json()["knowledge_id"] == "disk:domain/no-row"


async def test_completeness_counts_project_rows_only(client, db, make_project):
    project = await make_project()
    g = _db_row(None, "domain/scored-global", "/g/domain/scored-global.md")
    g.completeness_score = 100.0
    p = _db_row(str(project.project_id), "domain/scored-project", "/p/domain/scored-project.md")
    p.completeness_score = 0.0
    db.add_all([g, p])
    await db.commit()

    resp = await client.get(f"/api/projects/{project.project_id}/knowledge/completeness")
    assert resp.status_code == 200
    body = resp.json()
    # Global 100 + project 0 in the same category: including the global row
    # would average to 50.0 — completeness answers for THIS project only.
    assert body.get("domain") == 0.0, body
    assert set(body) == {"domain"}, body
