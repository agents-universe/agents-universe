"""Skill-source catalog loader: project file, framework template fallback, tolerance."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.tools._skill_sources_catalog import load_skill_sources
from agent_core.tools.base import ToolContext

_TEMPLATE_BODY = """---
category: integrations
slug: integrations/skill-sources
---

# Skill Sources

```yaml
sources:
  - key: template-src
    repo: acme/template-skills
    ref: main
```
"""


def _make_context(fs_path: Path, framework_root: Path | None) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(fs_path),
        conversation_id="c1",
        user_id="u1",
        framework_root=str(framework_root) if framework_root else None,
    )


def _write_project_catalog(fs_path: Path, body: str) -> None:
    target = fs_path / "knowledge" / "integrations" / "skill-sources.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _write_template(framework_root: Path, body: str = _TEMPLATE_BODY) -> None:
    target = framework_root / "knowledge" / "_template" / "skill-sources.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_project_catalog_wins_over_template(tmp_path):
    _write_template(tmp_path / "framework")
    _write_project_catalog(tmp_path, """---
slug: integrations/skill-sources
---

```yaml
sources:
  - key: mine
    repo: acme/mine
```
""")
    result = await load_skill_sources(_make_context(tmp_path, tmp_path / "framework"))

    assert result["origin"] == "project"
    assert result["catalog_path"] == "knowledge/integrations/skill-sources.md"
    assert [s["key"] for s in result["sources"]] == ["mine"]


@pytest.mark.asyncio
async def test_template_fallback_when_project_has_no_catalog(tmp_path):
    _write_template(tmp_path / "framework")
    result = await load_skill_sources(_make_context(tmp_path, tmp_path / "framework"))

    assert result["origin"] == "template"
    assert result["catalog_path"] == "knowledge/_template/skill-sources.md"
    assert [s["key"] for s in result["sources"]] == ["template-src"]


@pytest.mark.asyncio
async def test_missing_project_and_template_yields_empty(tmp_path):
    result = await load_skill_sources(_make_context(tmp_path, None))

    assert result == {"catalog_path": None, "origin": None, "sources": []}


@pytest.mark.asyncio
async def test_missing_framework_template_yields_empty(tmp_path):
    result = await load_skill_sources(_make_context(tmp_path, tmp_path / "framework"))

    assert result["sources"] == []
    assert result["origin"] is None


@pytest.mark.asyncio
async def test_malformed_yaml_block_is_skipped_not_fatal(tmp_path):
    _write_project_catalog(tmp_path, """---
slug: integrations/skill-sources
---

```yaml
sources:
  - key: good
    repo: acme/good
```

```yaml
sources: [this, is: not, valid
```

```yaml
sources:
  - key: also-good
    repo: acme/also-good
```
""")
    result = await load_skill_sources(_make_context(tmp_path, None))

    assert [s["key"] for s in result["sources"]] == ["good", "also-good"]


@pytest.mark.asyncio
async def test_entries_without_address_or_with_placeholders_are_flagged(tmp_path):
    _write_project_catalog(tmp_path, """---
slug: integrations/skill-sources
---

```yaml
sources:
  - key: no-address
  - key: placeholder
    repo: <owner>/<repo>
  - key: bad-repo
    repo: not a repo
  - key: ok
    url: https://git.example.com/team/tools.git
```
""")
    result = await load_skill_sources(_make_context(tmp_path, None))
    by_key = {s["key"]: s for s in result["sources"]}

    assert by_key["no-address"]["valid"] is False
    assert by_key["placeholder"]["valid"] is False
    assert "placeholder" in by_key["placeholder"]["error"]
    assert by_key["bad-repo"]["valid"] is False
    assert by_key["ok"]["valid"] is True


@pytest.mark.asyncio
async def test_key_derived_from_repo_and_url(tmp_path):
    _write_project_catalog(tmp_path, """---
slug: integrations/skill-sources
---

```yaml
sources:
  - repo: acme/Some-Skills
  - url: https://git.example.com/team/tools.git
```
""")
    result = await load_skill_sources(_make_context(tmp_path, None))

    assert [s["key"] for s in result["sources"]] == ["some-skills", "tools"]


@pytest.mark.asyncio
async def test_mapping_form_and_duplicate_keys(tmp_path):
    _write_project_catalog(tmp_path, """---
slug: integrations/skill-sources
---

```yaml
sources:
  first:
    repo: acme/first
  second:
    repo: acme/second
```

```yaml
sources:
  - key: second
    repo: acme/second-updated
```
""")
    result = await load_skill_sources(_make_context(tmp_path, None))
    by_key = {s["key"]: s for s in result["sources"]}

    assert set(by_key) == {"first", "second"}
    assert by_key["second"]["repo"] == "acme/second-updated"


@pytest.mark.asyncio
async def test_shipped_template_is_parseable_and_carries_real_sources():
    """The framework template is the fallback every legacy project sees."""
    framework_root = Path(__file__).resolve().parents[3]
    result = await load_skill_sources(
        _make_context(Path(__file__).resolve().parent, framework_root)
    )

    assert result["origin"] == "template"
    repos = {s["repo"] for s in result["sources"] if s["valid"]}
    assert "anthropics/skills" in repos
    assert all(s["error"] is None for s in result["sources"] if s["valid"])
