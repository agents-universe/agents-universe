"""Cross-project isolation tests for the knowledge router (PR-1)."""
from __future__ import annotations

from pathlib import Path

from api.models.knowledge import KnowledgeMetadata
from api.paths import PROJECTS_ROOT


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


# ---------------------------------------------------------------------------
# Disk fallback branch — slug traversal
# ---------------------------------------------------------------------------


async def test_get_normal_slug_regression(client, make_project):
    project = await make_project()
    _write(project.slug, "domain/foo.md", "---\ntitle: Foo\n---\nhello world")
    resp = await client.get(f"/api/projects/{project.project_id}/knowledge/domain/foo")
    assert resp.status_code == 200
    assert "hello world" in resp.json()["content"]


async def test_get_traversal_slug_rejected(client, make_project):
    a = await make_project()
    b = await make_project()
    secret = _write(b.slug, "domain/secret.md", "TOP SECRET")

    variants = [
        f"..%2F..%2F{b.slug}%2Fknowledge%2Fdomain%2Fsecret",
        f"%2e%2e%2F%2e%2e%2F{b.slug}%2Fknowledge%2Fdomain%2Fsecret",
        f"..%5C..%5C{b.slug}%5Cknowledge%5Cdomain%5Csecret",
    ]
    for variant in variants:
        resp = await client.get(f"/api/projects/{a.project_id}/knowledge/{variant}")
        assert resp.status_code in (400, 404), (variant, resp.status_code)
        assert "TOP SECRET" not in resp.text
    assert secret.read_text(encoding="utf-8") == "TOP SECRET"


async def test_put_traversal_slug_rejected(client, make_project):
    a = await make_project()
    b = await make_project()
    target = _write(b.slug, "domain/doc.md", "original")

    resp = await client.put(
        f"/api/projects/{a.project_id}/knowledge/..%2F..%2F{b.slug}%2Fknowledge%2Fdomain%2Fdoc",
        json={"content": "pwned"},
    )
    assert resp.status_code in (400, 404)
    assert target.read_text(encoding="utf-8") == "original"


async def test_put_normal_slug_regression(client, make_project):
    project = await make_project()
    target = _write(project.slug, "domain/foo.md", "before")
    resp = await client.put(
        f"/api/projects/{project.project_id}/knowledge/domain/foo",
        json={"content": "after"},
    )
    assert resp.status_code == 200
    assert target.read_text(encoding="utf-8") == "after"


async def test_get_legacy_uppercase_slug(client, make_project):
    """Filenames are indexed verbatim: a manually added API-Gateway.md must
    remain readable/writable (regression: the slug regex was lowercase-only)."""
    project = await make_project()
    _write(project.slug, "domain/API-Gateway.md", "---\ntitle: GW\n---\nlegacy doc")
    resp = await client.get(f"/api/projects/{project.project_id}/knowledge/domain/API-Gateway")
    assert resp.status_code == 200
    assert "legacy doc" in resp.json()["content"]


async def test_get_dotted_slug(client, make_project):
    project = await make_project()
    _write(project.slug, "notes.v2.md", "v2 notes")
    resp = await client.get(f"/api/projects/{project.project_id}/knowledge/notes.v2")
    assert resp.status_code == 200
    assert "v2 notes" in resp.json()["content"]


async def test_dot_segments_still_rejected(client, make_project):
    project = await make_project()
    for slug in ("a/./b", "a/../b"):
        resp = await client.get(f"/api/projects/{project.project_id}/knowledge/{slug}")
        assert resp.status_code in (400, 404), (slug, resp.status_code)


# ---------------------------------------------------------------------------
# DB branch — fs_path ownership
# ---------------------------------------------------------------------------


async def test_get_db_row_fs_path_outside_project_refused(client, db, make_project):
    a = await make_project()
    b = await make_project()
    secret = _write(b.slug, "domain/secret.md", "TOP SECRET")
    db.add(_db_row(str(a.project_id), "domain/evil", str(secret)))
    await db.commit()

    resp = await client.get(f"/api/projects/{a.project_id}/knowledge/domain/evil")
    assert resp.status_code == 400
    assert "TOP SECRET" not in resp.text


async def test_get_db_row_fs_path_inside_project_ok(client, db, make_project):
    a = await make_project()
    f = _write(a.slug, "domain/detail.md", "detail content")
    db.add(_db_row(str(a.project_id), "domain/detail", str(f)))
    await db.commit()

    resp = await client.get(f"/api/projects/{a.project_id}/knowledge/domain/detail")
    assert resp.status_code == 200
    assert "detail content" in resp.json()["content"]


async def test_get_global_row_inside_framework_dir_ok(client, db, make_project, monkeypatch, tmp_path):
    fw = tmp_path / "framework" / "knowledge"
    f = fw / "system" / "intro.md"
    f.parent.mkdir(parents=True)
    f.write_text("global intro", encoding="utf-8")
    monkeypatch.setattr("api.routers.knowledge.FRAMEWORK_KNOWLEDGE_DIR", fw)

    a = await make_project()
    db.add(_db_row(None, "system/intro", str(f)))
    await db.commit()

    resp = await client.get(f"/api/projects/{a.project_id}/knowledge/system/intro")
    assert resp.status_code == 200
    assert "global intro" in resp.json()["content"]


async def test_get_global_row_pointing_into_project_refused(client, db, make_project, monkeypatch, tmp_path):
    fw = tmp_path / "framework" / "knowledge"
    fw.mkdir(parents=True)
    monkeypatch.setattr("api.routers.knowledge.FRAMEWORK_KNOWLEDGE_DIR", fw)

    a = await make_project()
    secret = _write(a.slug, "domain/secret.md", "PROJECT SECRET")
    db.add(_db_row(None, "system/evil", str(secret)))
    await db.commit()

    resp = await client.get(f"/api/projects/{a.project_id}/knowledge/system/evil")
    assert resp.status_code == 400
    assert "PROJECT SECRET" not in resp.text


async def test_put_db_row_fs_path_outside_project_refused(client, db, make_project):
    a = await make_project()
    b = await make_project()
    target = _write(b.slug, "domain/doc.md", "original")
    db.add(_db_row(str(a.project_id), "domain/doc", str(target)))
    await db.commit()

    resp = await client.put(
        f"/api/projects/{a.project_id}/knowledge/domain/doc",
        json={"content": "pwned"},
    )
    assert resp.status_code == 400
    assert target.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# Frontmatter preservation on PUT
# ---------------------------------------------------------------------------


async def test_put_preserves_frontmatter_primary_file(client, make_project):
    """GET strips frontmatter; PUT must merge the body-only edit back into the
    file's existing frontmatter instead of wiping it (regression: the raw body
    write dropped title/tags metadata on save)."""
    project = await make_project()
    _write(
        project.slug,
        "domain/foo.md",
        "---\ntitle: Foo Doc\ntags: [a, b]\nparent: domain\n---\nhello world",
    )

    # What GET surfaces is body-only.
    get_resp = await client.get(f"/api/projects/{project.project_id}/knowledge/domain/foo")
    assert get_resp.status_code == 200
    assert get_resp.json()["content"] == "hello world"

    resp = await client.put(
        f"/api/projects/{project.project_id}/knowledge/domain/foo",
        json={"content": "edited body"},
    )
    assert resp.status_code == 200

    content = (PROJECTS_ROOT / project.slug / "knowledge" / "domain" / "foo.md").read_text(
        encoding="utf-8"
    )
    assert "title: Foo Doc" in content
    assert "tags:" in content
    assert "a" in content and "b" in content
    assert "parent: domain" in content
    assert "edited body" in content
    assert "hello world" not in content

    # And the file still reads back cleanly with its metadata intact.
    get_resp = await client.get(f"/api/projects/{project.project_id}/knowledge/domain/foo")
    assert get_resp.status_code == 200
    assert get_resp.json()["content"] == "edited body"
    assert get_resp.json()["title"] == "Foo Doc"


async def test_put_preserves_frontmatter_db_row(client, db, make_project):
    """Same guarantee for a DB-indexed (detail) knowledge file."""
    project = await make_project()
    f = _write(
        project.slug,
        "domain/detail.md",
        "---\ntitle: Detail Doc\ncategory: domain\n---\noriginal body",
    )
    db.add(_db_row(str(project.project_id), "domain/detail", str(f)))
    await db.commit()

    resp = await client.put(
        f"/api/projects/{project.project_id}/knowledge/domain/detail",
        json={"content": "new body"},
    )
    assert resp.status_code == 200

    content = f.read_text(encoding="utf-8")
    assert "title: Detail Doc" in content
    assert "category: domain" in content
    assert "new body" in content
    assert "original body" not in content


async def test_put_plain_file_without_frontmatter_stays_verbatim(client, make_project):
    """A file without frontmatter must keep the old raw-write behavior (no
    synthetic frontmatter is introduced)."""
    project = await make_project()
    _write(project.slug, "domain/plain.md", "plain body")

    resp = await client.put(
        f"/api/projects/{project.project_id}/knowledge/domain/plain",
        json={"content": "edited plain"},
    )
    assert resp.status_code == 200
    content = (PROJECTS_ROOT / project.slug / "knowledge" / "domain" / "plain.md").read_text(
        encoding="utf-8"
    )
    assert content == "edited plain"
    assert "---" not in content
