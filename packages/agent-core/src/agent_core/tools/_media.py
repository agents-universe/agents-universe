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
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

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
}


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
    rel = f"/api/media/{context.project_id}/{context.conversation_id}/{filename}"
    base = getattr(context, "app_base_url", "") or ""
    base = base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return rel
    root = getattr(context, "app_root_path", "") or ""
    root = root.strip("/")
    if root and not base.endswith(f"/{root}"):
        base = f"{base}/{root}"
    return f"{base}{rel}"
