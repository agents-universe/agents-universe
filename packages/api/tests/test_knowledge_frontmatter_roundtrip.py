"""GET/PUT round-trip contract of the knowledge editor.

GET strips frontmatter so the editor carries only the body; PUT rehydrates
that body into the on-disk frontmatter. Two read-side breaks corrupted files:

* A UTF-8 BOM hides the ``---`` block from python-frontmatter, so GET handed
  the WHOLE file (frontmatter included) to the editor as content — and
  ``_rehydrate_frontmatter`` failed to parse ``old_content`` the same way.
* An FM file with an empty body hit GET's ``else raw`` fallback (the parsed
  body is empty), so the editor received the frontmatter as "body" — but PUT
  parses old_content fine and merged it, nesting a SECOND copy of the
  frontmatter into the file on save.
"""
from __future__ import annotations

import frontmatter as fm

from api.paths import PROJECTS_ROOT


def _write(ws_slug: str, rel: str, content: str):
    p = PROJECTS_ROOT / ws_slug / "knowledge" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


async def test_bom_file_get_strips_frontmatter_and_put_preserves_it(client, make_project):
    project = await make_project()
    target = _write(
        project.slug,
        "domain/bom.md",
        "﻿---\ntitle: Bom\ncategory: technical\n---\n\nBody text.\n",
    )
    url = f"/api/projects/{project.project_id}/knowledge/domain/bom"

    resp = await client.get(url)
    assert resp.status_code == 200
    content = resp.json()["content"]
    # GET's contract is frontmatter-stripped content; behind a BOM the parse
    # fails and the editor receives the metadata as editable text.
    assert "title: Bom" not in content, content
    assert "Body text." in content

    saved = await client.put(url, json={"content": content})
    assert saved.status_code == 200

    post = fm.loads(target.read_text(encoding="utf-8").lstrip("﻿"))
    assert post.metadata.get("title") == "Bom"
    assert post.metadata.get("category") == "technical"
    assert post.content.strip() == "Body text."


async def test_empty_body_put_does_not_duplicate_frontmatter(client, make_project):
    project = await make_project()
    target = _write(
        project.slug,
        "domain/empty.md",
        "---\ntitle: Empty\ncategory: technical\n---\n",
    )
    url = f"/api/projects/{project.project_id}/knowledge/domain/empty"

    resp = await client.get(url)
    assert resp.status_code == 200
    content = resp.json()["content"]

    saved = await client.put(url, json={"content": content})
    assert saved.status_code == 200

    on_disk = target.read_text(encoding="utf-8")
    # The frontmatter must appear exactly once — the empty-body GET fallback
    # handed the whole file to the editor and rehydration nested it again.
    assert on_disk.count("title: Empty") == 1, on_disk
    post = fm.loads(on_disk)
    assert post.metadata.get("title") == "Empty"
    assert "---" not in post.content
