"""Frontmatter localization helpers.

User-facing definition fields (agent ``display_name``/``description``/
``placeholder``/``starter_prompts``, skill/workflow ``description``) accept
either a plain scalar — one value shared by every locale, the pre-i18n shape
every existing file uses — or a locale map::

    description:
      en-US: "Authoritative English one-liner."
      zh-CN: "一句话中文说明。"

Normalizing both shapes here keeps every downstream consumer on the same
contract: a single **canonical** string for legacy columns, LLM-facing
prompts, and ordering keys, plus the full locale map for UI serialization.
The canonical language is caller-selectable because the two families differ:

- agent UI fields use zh-first so existing Chinese content keeps driving the
  ``agents.display_name`` sort key and the ``[display_name]:`` history prefix;
- skill/workflow descriptions use en-first so the text injected into the
  LLM prompt (``## Available Workflows``) stays byte-identical.
"""
from __future__ import annotations

import re

#: Locale-shaped mapping key: ``zh``, ``en-US`` — not arbitrary dict keys, so
#: a skill ref (``{slug, description}``) or other structured frontmatter
#: value is never mistaken for a translation map.
LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def is_locale_map(value: object) -> bool:
    """True when *value* is a non-empty dict whose keys are all locale codes."""
    return isinstance(value, dict) and bool(value) and all(
        isinstance(key, str) and LOCALE_RE.match(key) for key in value
    )


def normalize_i18n_field(
    value: object,
    *,
    canonical_order: tuple[str, ...] = ("zh-CN", "en-US"),
) -> tuple[str | None, dict[str, str] | None]:
    """Normalize a scalar-or-locale-map frontmatter field.

    Returns ``(canonical, locale_map)``:

    - plain string → ``(value, None)`` — one value shared by every locale;
    - locale map of string values → ``(selected, full_map)`` — the canonical
      entry is the first language in *canonical_order* present in the map
      (falling back to the map's first entry when none match);
    - anything else (None, numbers, dicts with non-locale keys or non-string
      values) → ``(None, None)`` — callers log/skip, never raise, so one bad
      file cannot fail a whole directory sync.
    """
    if isinstance(value, str):
        return value, None
    if is_locale_map(value):
        entries = {
            str(key): str(raw)
            for key, raw in value.items()  # type: ignore[union-attr]
            if isinstance(raw, str)
        }
        if not entries:
            return None, None
        for locale in canonical_order:
            if locale in entries:
                return entries[locale], entries
        first = next(iter(entries))
        return entries[first], entries
    return None, None


def normalize_i18n_prompts(
    value: object,
    *,
    canonical_order: tuple[str, ...] = ("zh-CN", "en-US"),
) -> tuple[list[str], dict[str, list[str]] | None]:
    """Normalize ``starter_prompts``: scalar, shared list, or locale map.

    Returns ``(canonical_list, locale_map)``. A bare string is ONE prompt
    (prompts contain commas — never comma-split); a list is shared by every
    locale; a locale map carries one list (or single string) per language.
    Blank entries are dropped. Any other shape yields ``([], None)``.
    """
    if isinstance(value, str):
        text = value.strip()
        return ([text] if text else []), None
    if isinstance(value, (list, tuple)):
        return (
            [item.strip() for item in value if isinstance(item, str) and item.strip()],
            None,
        )
    if is_locale_map(value):
        per_locale: dict[str, list[str]] = {}
        for key, raw in value.items():  # type: ignore[union-attr]
            if isinstance(raw, str):
                text = raw.strip()
                if text:
                    per_locale[str(key)] = [text]
            elif isinstance(raw, (list, tuple)):
                items = [
                    item.strip() for item in raw if isinstance(item, str) and item.strip()
                ]
                if items:
                    per_locale[str(key)] = items
        if not per_locale:
            return [], None
        for locale in canonical_order:
            if locale in per_locale:
                return per_locale[locale], per_locale
        first = next(iter(per_locale))
        return per_locale[first], per_locale
    return [], None
