"""Ownership checks for DB-backed knowledge file reads (loader)."""
from __future__ import annotations

import logging

from agent_core.knowledge.cache import CachedProjectKnowledge
from agent_core.knowledge.loader import (
    KnowledgeEntry,
    _try_read_file,
    load_project_context,
)


def _entry(slug: str, fs_path: str, project_id: str | None, level: str = "root") -> KnowledgeEntry:
    return KnowledgeEntry(
        knowledge_id=f"db:{slug}",
        slug=slug,
        title=slug,
        fs_path=fs_path,
        category=slug.split("/")[0],
        cross_references=[],
        word_count=1,
        knowledge_level=level,
        project_id=project_id,
    )


class _FakeCache:
    def __init__(self, entries):
        self._entries = entries

    async def get_or_load(self, project_id, db_session):
        return CachedProjectKnowledge(project_id=project_id, entries=self._entries, content={})


async def test_global_row_inside_framework_dir_loads(tmp_path):
    fw = tmp_path / "framework" / "knowledge"
    f = fw / "system" / "intro.md"
    f.parent.mkdir(parents=True)
    f.write_text("hello", encoding="utf-8")
    entry = _entry("system/intro", str(f), project_id=None)
    content = await _try_read_file(entry, framework_knowledge_dir=fw, project_knowledge_dir=None)
    assert content == "hello"


async def test_global_row_pointing_into_project_dir_refused(tmp_path, caplog):
    fw = tmp_path / "framework" / "knowledge"
    fw.mkdir(parents=True)
    proj_file = tmp_path / "proj-a" / "knowledge" / "secret.md"
    proj_file.parent.mkdir(parents=True)
    proj_file.write_text("secret", encoding="utf-8")
    entry = _entry("system/evil", str(proj_file), project_id=None)
    with caplog.at_level(logging.WARNING, logger="agent_core.knowledge"):
        content = await _try_read_file(entry, framework_knowledge_dir=fw, project_knowledge_dir=None)
    assert content is None
    assert "outside its owning directory" in caplog.text


async def test_project_row_inside_project_dir_loads(tmp_path):
    proj_k = tmp_path / "proj-a" / "knowledge"
    f = proj_k / "domain" / "ctx.md"
    f.parent.mkdir(parents=True)
    f.write_text("ctx", encoding="utf-8")
    entry = _entry("domain/ctx", str(f), project_id="p1")
    content = await _try_read_file(entry, framework_knowledge_dir=None, project_knowledge_dir=proj_k)
    assert content == "ctx"


async def test_project_row_outside_project_dir_refused(tmp_path, caplog):
    proj_k = tmp_path / "proj-a" / "knowledge"
    proj_k.mkdir(parents=True)
    other = tmp_path / "proj-b" / "knowledge" / "x.md"
    other.parent.mkdir(parents=True)
    other.write_text("x", encoding="utf-8")
    entry = _entry("domain/evil", str(other), project_id="p1")
    with caplog.at_level(logging.WARNING, logger="agent_core.knowledge"):
        content = await _try_read_file(entry, framework_knowledge_dir=None, project_knowledge_dir=proj_k)
    assert content is None
    assert "outside its owning directory" in caplog.text


async def test_missing_base_fails_closed(tmp_path, caplog):
    f = tmp_path / "anywhere.md"
    f.write_text("data", encoding="utf-8")
    entry = _entry("domain/x", str(f), project_id="p1")
    with caplog.at_level(logging.WARNING, logger="agent_core.knowledge"):
        content = await _try_read_file(entry, framework_knowledge_dir=None, project_knowledge_dir=None)
    assert content is None
    assert "no base directory" in caplog.text


async def test_load_project_context_compat_path_enforces_ownership(tmp_path):
    """Legacy cache rows: out-of-bounds fs_path rows are skipped, in-bounds load."""
    fw = tmp_path / "framework" / "knowledge"
    good = fw / "system" / "ok.md"
    good.parent.mkdir(parents=True)
    good.write_text("ok", encoding="utf-8")
    evil = tmp_path / "proj-b" / "knowledge" / "evil.md"
    evil.parent.mkdir(parents=True)
    evil.write_text("evil", encoding="utf-8")
    entries = [
        _entry("system/ok", str(good), project_id=None),
        _entry("system/evil", str(evil), project_id=None),
    ]
    result = await load_project_context(
        project_id="p1",
        db_session=None,
        cache=_FakeCache(entries),
        knowledge_dir=None,
        framework_knowledge_dir=fw,
    )
    assert result.loaded_content == {"system/ok": "ok"}
    assert "system/evil" not in result.overflow_slugs


async def test_try_read_file_strips_utf8_bom(tmp_path):
    """A UTF-8 BOM (Windows editors) must not stick to the frontmatter —
    \ufefftitle would break metadata parsing and hash matching."""
    proj = tmp_path / "proj" / "knowledge"
    proj.mkdir(parents=True)
    f = proj / "bom.md"
    f.write_bytes(b"\xef\xbb\xbf---\ntitle: BOM File\n---\nBody text")

    text = await _try_read_file(
        _entry("bom", str(f), project_id="p1"),
        project_knowledge_dir=str(proj),
    )

    assert text is not None
    assert not text.startswith("\ufeff")
    assert text.startswith("---")
