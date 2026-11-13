"""Skill-source catalog loader — the recommended external skill repositories.

The catalog is Markdown with a YAML fenced block (see
knowledge/_template/skill-sources.md) holding a ``sources:`` list. Same
permissive contract as _catalog.py: a malformed block is skipped, a missing
file yields no sources, and the loader never raises.

Projects created before this file existed have no project catalog, so the
framework template is the fallback — otherwise the shipped defaults would only
reach newly created projects.

On top of the file tier, the deployment-wide ``SKILL_MARKETS`` env var merges
extra git-repository entries (env wins on key collisions): operators pin or
add markets for every sub-project without touching workspace files.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import frontmatter
import yaml

from ._catalog import _YAML_FENCE_RE
from ._repo_paths import _REPO_PATH_RE
from .base import ToolContext

_log = logging.getLogger(__name__)

CATALOG_REL_PATH = "knowledge/integrations/skill-sources.md"
TEMPLATE_REL_PATH = "knowledge/_template/skill-sources.md"
ENV_MARKETS_KEY = "SKILL_MARKETS"

# Mirrors skill_source._clean_ref without importing it (skill_source imports
# this module — a circular import). Refs reach git clone --branch at fetch
# time, so a leading dash or a commit SHA must be rejected here too.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# _REPO_PATH_RE tolerates a single segment (owner-less paths elsewhere); an env
# market token must be a real owner/name so `git clone` gets a full path.
_ENV_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _derive_key(entry: dict[str, Any]) -> str:
    """Best-effort key for an entry that declares none."""
    repo = str(entry.get("repo") or "").strip()
    if repo:
        return repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
    url = str(entry.get("url") or "").strip()
    if url:
        return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
    return ""


def _normalize_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    repo = str(raw.get("repo") or "").strip() or None
    url = str(raw.get("url") or "").strip() or None
    entry: dict[str, Any] = {
        "key": str(raw.get("key") or "").strip() or _derive_key(raw),
        "repo": repo,
        "url": url,
        "ref": str(raw.get("ref") or "").strip() or None,
        "path": str(raw.get("path") or "").strip().strip("/") or None,
        "notes": str(raw.get("notes") or "").strip(),
        "tags": [str(t) for t in raw.get("tags") or [] if str(t).strip()],
        "valid": True,
        "error": None,
    }
    if not repo and not url:
        entry["valid"] = False
        entry["error"] = "entry needs either 'repo' (owner/name) or 'url'"
    elif any("<" in (value or "") or ">" in (value or "") for value in (repo, url)):
        # The template documents the shape with <owner>/<repo> placeholders —
        # surfacing one as a real source would send the agent to a bogus host.
        entry["valid"] = False
        entry["error"] = "placeholder address — replace it with a real repository"
    elif repo and not _REPO_PATH_RE.fullmatch(repo):
        entry["valid"] = False
        entry["error"] = f"invalid repo {repo!r}: expected owner/name"
    if not entry["key"]:
        entry["valid"] = False
        entry["error"] = entry["error"] or "cannot derive a key — set 'key' explicitly"
    return entry


def _env_address_error(entry: dict[str, Any]) -> str | None:
    """Load-time checks for env tokens — fetch re-validates the full address."""
    repo = entry.get("repo")
    if repo and not _ENV_REPO_RE.fullmatch(repo):
        return f"invalid repo {repo!r}: expected owner/name"
    ref = entry.get("ref")
    if ref and (
        not _REF_RE.match(ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", "."))
        or ".lock" in ref.split("/")[-1]
        or _SHA_RE.match(ref)
    ):
        return f"invalid ref {ref!r} — use a branch or tag (never a commit SHA)"
    url = entry.get("url")
    if url:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return f"unsupported scheme {parts.scheme!r} — only https is allowed"
        if parts.username or parts.password:
            return "credentials must not be embedded in the URL"
        if parts.query or parts.fragment:
            return "query strings and fragments are not part of a skill address"
        if not parts.hostname:
            return f"invalid URL {url!r}: no host"
    return None


def _parse_env_markets(raw: str) -> list[dict[str, Any]]:
    """Parse SKILL_MARKETS into catalog entries (``origin: "env"``).

    Token forms: ``owner/repo``, ``owner/repo@ref`` (split on the *last* ``@``),
    or a full https URL (never ``@``-split — userinfo would be mangled).
    Malformed tokens become ``valid: false`` entries, never an exception.
    """
    entries: list[dict[str, Any]] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        decl: dict[str, Any] = {}
        if "://" in token:
            decl["url"] = token
        else:
            address, sep, ref = token.rpartition("@")
            if sep and address:
                decl["repo"], decl["ref"] = address, ref
            else:
                decl["repo"] = token
        entry = _normalize_entry(decl)
        if entry is None:
            continue
        if entry["valid"]:
            error = _env_address_error(entry)
            if error:
                entry["valid"] = False
                entry["error"] = error
        if not entry["valid"]:
            _log.warning("SKILL_MARKETS entry %r skipped: %s", token, entry["error"])
        entry["origin"] = "env"
        entries.append(entry)
    return entries


def _iter_entries(block: Any) -> list[Any]:
    """Yield raw entries from either the wrapper or the bare form."""
    if not isinstance(block, dict):
        return []
    sources = block.get("sources")
    if isinstance(sources, list):
        return sources
    if isinstance(sources, dict):
        # {sources: {key: {...}}} — the key doubles as the entry key.
        out = []
        for key, value in sources.items():
            if isinstance(value, dict):
                out.append({"key": key, **value})
        return out
    if block.get("repo") or block.get("url"):
        return [block]
    return []


def _parse_catalog(content: str) -> list[dict[str, Any]]:
    try:
        body = frontmatter.loads(content).content
    except Exception as exc:
        _log.warning("skill-source catalog frontmatter parse failed, using raw: %s", exc)
        body = content

    sources: dict[str, dict[str, Any]] = {}
    for match in _YAML_FENCE_RE.finditer(body):
        try:
            block = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            _log.warning("skill-source catalog YAML block skipped: %s", exc)
            continue
        for raw in _iter_entries(block):
            entry = _normalize_entry(raw)
            if entry is None:
                continue
            key = entry["key"]
            if key in sources:
                _log.warning("skill-source catalog duplicate key %r — later entry wins", key)
            sources[key] = entry
    return list(sources.values())


async def _read_catalog(path: Path) -> list[dict[str, Any]] | None:
    if not await asyncio.to_thread(path.exists):
        return None
    try:
        content = await asyncio.to_thread(path.read_text, "utf-8")
    except OSError as exc:
        _log.warning("skill-source catalog read failed for %s: %s", path, exc)
        return []
    return _parse_catalog(content)


async def load_skill_sources(context: ToolContext) -> dict[str, Any]:
    """Resolve the catalog: file tier (project else template) + env markets.

    Returns ``{catalog_path, origin, sources}`` where ``catalog_path`` is the
    workspace- or framework-relative path the agent can hand to the filesystem
    tool, and ``origin`` is ``"project"``/``"template"``/``None`` — both
    describing the file tier only. Entries merged in from ``SKILL_MARKETS``
    carry ``origin: "env"`` and win over file entries on key collisions
    (deployment operators outrank workspace files).
    """
    result: dict[str, Any] = {"catalog_path": None, "origin": None, "sources": []}

    project_path = Path(context.knowledge_dir()) / "integrations" / "skill-sources.md"
    sources = await _read_catalog(project_path)
    if sources is not None:
        result["catalog_path"] = CATALOG_REL_PATH
        result["origin"] = "project"
        result["sources"] = sources
    else:
        framework_root = getattr(context, "framework_root", None)
        if framework_root:
            template_path = Path(framework_root) / "knowledge" / "_template" / "skill-sources.md"
            sources = await _read_catalog(template_path)
            if sources is not None:
                result["catalog_path"] = TEMPLATE_REL_PATH
                result["origin"] = "template"
                result["sources"] = sources
        if result["origin"] is None:
            _log.debug(
                "skill-source catalog not found (project=%s, framework=%s)",
                project_path, framework_root,
            )

    env_sources = _parse_env_markets(context.cfg(ENV_MARKETS_KEY, ""))
    if env_sources:
        merged = {entry["key"]: entry for entry in result["sources"]}
        for entry in env_sources:
            if entry["key"] in merged:
                _log.warning(
                    "skill-source key %r from %s overrides file entry",
                    entry["key"], ENV_MARKETS_KEY,
                )
            merged[entry["key"]] = entry
        result["sources"] = list(merged.values())
    return result
