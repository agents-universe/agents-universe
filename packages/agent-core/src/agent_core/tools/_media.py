"""Shared helpers for turning workspace files into /api/media records.

The API's media router (api/routers/media.py) only serves files whose names
match a strict whitelist — server-generated names are mandatory, the client
filename rides in the record's ``name`` field. Tools that deliver files to the
user (code_executor outputs, deliver_file) share the sanitization and MIME
inference here so every produced URL is servable.

URLs are produced by :func:`media_url`: when the tool context carries an
``app_base_url`` (injected by the API layer from ``APP_BASE_URL``), the record
carries a fully-qualified URL (``https://host/agent/api/media/...``) so an LLM
quoting the URL into its reply gives the user a directly usable download
address. ``app_root_path`` (``APP_ROOT_PATH``) is appended when set, which
covers deployments behind a sub-path (e.g. ``/agent``) where the browser must
hit ``https://host/agent/api/media/...``. Without a configured base URL the
historical relative path is returned, which keeps local/dev/test behavior
unchanged.

LLMs occasionally still prepend a base URL even when told the tool URL is
already absolute (the model mistakes ``https://host/agent/api/media/...`` for
a relative path and writes ``https://host/agenthttps://host/agent/api/media/
...``). :func:`normalize_media_urls` is a rendering/persistence safety net that
collapses that duplicated base so the quoted link stays usable.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

# Matches the API whitelist: alphanumerics/dash/underscore/dots, no leading dot.
# The suffix (after sanitization) must be purely alphanumeric like upload_media.
_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Guessed types that matter for downloads; anything unmapped falls back to
# mimetypes.guess_type and then application/octet-stream.
_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".html": "text/html",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
}

_MEDIA_MARKER = "/api/media/"

# A duplicated absolute base directly before the media marker:
# ``<base><base>/api/media/...`` (the corruption) vs ``<base>/api/media/...``
# (correct). Group 1 captures ONE base copy; the backreference requires an
# identical second copy immediately after, and the lookahead guarantees the
# collapsed form only ever applies to media URLs (never to arbitrary text
# that happens to repeat a host). ``re.sub(..., r"\\1", text)`` keeps the
# first copy and drops the duplicate.
_DUPLICATED_BASE_RE = re.compile(
    r"(https?://[A-Za-z0-9._~-]+(?::\d+)?(?:/[A-Za-z0-9._~-]+)*)"
    r"\1(?=/api/media/)"
)


def sanitize_suffix(filename: str) -> str:
    """Lowercase, purely-alphanumeric suffix (≤16 chars) safe for the media
    whitelist; '' when nothing usable (extension missing or weird)."""
    suffix = Path(filename).suffix.lower()
    if not suffix or len(suffix) > 16 or not _SAFE_SUFFIX_RE.match(suffix[1:]):
        return ""
    return suffix


def media_type_for(filename: str) -> str:
    """MIME type for a delivered file — overrides first, then mimetypes,
    then a binary fallback (browsers download octet-stream)."""
    suffix = Path(filename).suffix.lower()
    override = _MIME_OVERRIDES.get(suffix)
    if override:
        return override
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def media_url(context, filename: str) -> str:
    """Build the media record URL for *filename* in the conversation media dir.

    Returns an absolute URL (``{app_base_url}[/{app_root_path}]/api/media/...``)
    when the tool context carries an ``app_base_url`` — the LLM can quote it
    verbatim as a working download address, including the deployment sub-path
    (``/agent`` etc.) so the browser hits the proxied location. Falls back to
    the relative ``/api/media/...`` path when no base URL is configured
    (local/dev/tests).
    """
    rel = f"{_MEDIA_MARKER}{context.project_id}/{context.conversation_id}/{filename}"
    base = getattr(context, "app_base_url", "") or ""
    base = base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return rel
    root = getattr(context, "app_root_path", "") or ""
    root = root.strip("/")
    if root and not base.endswith(f"/{root}"):
        base = f"{base}/{root}"
    return f"{base}{rel}"


def normalize_media_urls(text: str) -> str:
    """Collapse duplicated base URLs in *text* around /api/media/ links.

    The tool layer emits complete absolute URLs (``media_url``), but an LLM
    may still treat one as a relative path and prepend a base again, producing
    ``https://host/agenthttps://host/agent/api/media/...``. This rewrites any
    media URL whose base is duplicated directly before the ``/api/media/``
    marker to the single canonical form. Non-media URLs and already-correct
    URLs pass through untouched.

    Applied at the API persistence layer (keeps conversation history clean so
    later turns never see the corrupted form) and at the frontend render layer
    (fixes already-persisted messages live).
    """
    if not text or _MEDIA_MARKER not in text:
        return text
    return _DUPLICATED_BASE_RE.sub(r"\1", text)
