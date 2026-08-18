"""Project category registry, loaded from knowledge/categories.yaml.

The registry is shared by the API (template selection at project creation)
and the web UI (creation dialog) via GET /api/projects/categories.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache

import yaml

from .paths import KNOWLEDGE_TEMPLATE_DIR, PACKAGE_ROOT

_log = logging.getLogger("agents_universe.project_categories")

CATEGORIES_YAML = PACKAGE_ROOT / "knowledge" / "categories.yaml"
DEFAULT_CATEGORY = "software"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _available_template_slugs() -> set[str]:
    """Slugs declared in the frontmatter of knowledge/_template/*.md.

    Template files live flat on disk but their frontmatter ``slug`` (e.g.
    ``technical/api-map``) determines the destination path in a project.
    """
    slugs: set[str] = set()
    if not KNOWLEDGE_TEMPLATE_DIR.exists():
        return slugs
    try:
        import frontmatter
        for f in KNOWLEDGE_TEMPLATE_DIR.rglob("*.md"):
            try:
                slug = frontmatter.loads(f.read_text("utf-8")).metadata.get("slug")
            except Exception:
                slug = None
            if slug:
                slugs.add(str(slug))
            else:
                slugs.add(f.stem)
    except Exception:
        _log.warning("Failed to scan %s for template slugs", KNOWLEDGE_TEMPLATE_DIR, exc_info=True)
    return slugs


@lru_cache(maxsize=1)
def get_categories() -> list[dict]:
    """Return the ordered category list.

    On a missing/corrupt config, degrade to a software-only registry
    (all templates) and log an error — project creation keeps working
    exactly as before.
    """
    try:
        raw = yaml.safe_load(CATEGORIES_YAML.read_text("utf-8"))
        items = raw.get("categories", []) if isinstance(raw, dict) else []
        out = []
        for i, entry in enumerate(items):
            if not isinstance(entry, dict):
                _log.warning("category[%d]: not a mapping, skipped", i)
                continue
            slug = str(entry.get("slug", "")).strip()
            if not _SLUG_RE.match(slug):
                _log.warning("category[%d]: invalid or missing slug, skipped", i)
                continue
            templates = [str(t).strip() for t in entry.get("templates", [])]
            available = _available_template_slugs()
            for t in templates:  # 容错:不存在的知识条目仅告警
                if t not in available:
                    _log.warning("category %s references unknown template %r", slug, t)
            out.append({
                "slug": slug,
                "label": str(entry.get("label", slug)),
                "description": str(entry.get("description", "")),
                "templates": templates,
            })
        if out:
            return out
        raise ValueError("no valid categories in yaml")
    except Exception:
        _log.error(
            "Failed to load %s, falling back to software-only registry",
            CATEGORIES_YAML,
            exc_info=True,
        )
        return [{
            "slug": DEFAULT_CATEGORY,
            "label": "软件项目",
            "description": "全部知识条目",
            "templates": [],
        }]


def get_category(slug: str) -> dict | None:
    return next((c for c in get_categories() if c["slug"] == slug), None)


def get_template_slugs(category: str) -> set[str] | None:
    """Return the template slugs for a category, or None for "all templates".

    ``software`` (the default) without an explicit list copies every file in
    ``knowledge/_template/``, matching the pre-category behavior.
    """
    cat = get_category(category)
    if cat is None:
        return None
    if category == DEFAULT_CATEGORY and not cat["templates"]:
        return None
    return set(cat["templates"])


def is_valid_category(slug: str) -> bool:
    return get_category(slug) is not None
