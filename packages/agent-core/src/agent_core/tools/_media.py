"""Shared helpers for turning workspace files into /api/media records.

The API's media router (api/routers/media.py) only serves files whose names
match a strict whitelist — server-generated names are mandatory, the client
filename rides in the record's ``name`` field. Tools that deliver files to the
user (code_executor outputs, deliver_file) share the sanitization and MIME
inference here so every produced URL is servable.
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
