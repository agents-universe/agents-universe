"""Shared resolver for upload payloads — one spec in, in-memory bytes out.

Several tools now need to send a file somewhere: the browser tool pushes it
into an ``<input type=file>`` (or a native file chooser), ``api_request`` sends
it as a multipart part. Three byte sources are supported, and all three
collapse to the same in-memory representation:

* ``path`` — a file inside the project workspace (``tests/fixtures/orders.csv``,
  a screenshot under ``.tmp/media/...``);
* ``attachment`` — a file the user attached to the conversation, which lives in
  the API's in-memory upload store and is *never* written to disk;
* ``inline`` — content the caller generated on the spot (text or base64), so a
  test can synthesize its payload without touching the filesystem at all.

The destination is not a trust boundary: ``page.route`` cannot inspect an
upload body, and ``api_request`` posts to customer-configured hosts by design.
The whole exfiltration guard therefore lives here, on the source side — project
root confinement, a hidden-file block, and byte caps — and every consumer gets
it for free by going through :func:`resolve_upload_specs`.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import PathEscapeError, resolve_within
from ._media import media_type_for

# A single uploaded file. Chat attachments are capped at max_upload_size_mb
# (10MB) by the API, so a path-sourced upload of the same ceiling keeps one
# consistent number in the agent's head.
MAX_UPLOAD_FILE_BYTES = 10 * 1024 * 1024
# Inline content travels through the LLM context (it is generated in a tool
# call), so it gets a much tighter cap — 1MB of base64 is already ~350k tokens
# of payload the model had to produce.
MAX_INLINE_BYTES = 1 * 1024 * 1024
MAX_UPLOAD_FILES = 10

_MIME_RE = re.compile(r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]*$")
# Filenames ride in multipart Content-Disposition headers and become the
# browser File.name — control characters (CR/LF especially) must not survive.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_NAME_CHARS = 255

_SOURCES = ("attachment", "path", "inline")


class UploadSourceError(ValueError):
    """An upload spec could not be resolved to bytes."""


@dataclass(frozen=True)
class UploadPayload:
    """Resolved upload bytes plus the metadata destinations need."""

    name: str
    data: bytes
    mime_type: str
    source: str
    origin: str
    # Multipart form field name (api_request). Ignored by the browser tool.
    field: str = "file"


def _clean_name(raw: Any, *, fallback: str = "") -> str:
    """Sanitize a caller-supplied filename for headers and ``File.name``."""
    text = str(raw or "").replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    name = _CONTROL_RE.sub("", name).strip()
    if name in ("", ".", ".."):
        return fallback
    return name[:_MAX_NAME_CHARS]


def normalize_mime_type(raw: Any, name: str, *, text_default: bool = False) -> str:
    """A caller-supplied MIME when it looks like one, else inferred from *name*.

    Unvalidated text would ride into a multipart Content-Type header (and a
    browser ``File.type``), where a stray CR/LF is a header-injection vector —
    anything not shaped like ``type/subtype`` falls back to the name-based
    guess. Shared with test_generator, which embeds the same value into a
    generated spec.
    """
    if isinstance(raw, str) and _MIME_RE.match(raw.strip()):
        return raw.strip()
    inferred = media_type_for(name)
    if text_default and inferred == "application/octet-stream":
        return "text/plain"
    return inferred


def _mime_for(spec: dict[str, Any], name: str, *, text_default: bool = False) -> str:
    return normalize_mime_type(spec.get("mime_type") or spec.get("mimeType"), name, text_default=text_default)


def _media_parts(rel: str) -> list[str] | None:
    """Split a ``.tmp/media/{conversation}/{file}`` path into segments."""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) != 4 or parts[:2] != [".tmp", "media"]:
        return None
    return parts


def _hidden_part(rel: str) -> str | None:
    """First hidden path component, or None when the path is acceptable.

    Reading arbitrary dotfiles out of a project workspace would hand an agent
    ``.env``/``.git/config`` as an upload payload, which is exactly what an
    injected instruction would ask for. ``.tmp`` is exempt in the leading
    position because the platform's own scratch tree lives there — screenshots,
    downloads and recordings all reach consumer tools through it.
    """
    for i, part in enumerate(p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")):
        if part == ".." or not part.startswith("."):
            continue
        if i == 0 and part == ".tmp":
            continue
        return part
    return None


def _resolve_path(context, spec: dict[str, Any], max_bytes: int) -> UploadPayload:
    rel = str(spec.get("path") or "").strip()
    if not rel:
        raise UploadSourceError("source='path' requires 'path' (workspace-relative)")

    # A virtual media path is offered to the upload store first: the filesystem
    # tool exposes the user's attachments at exactly this path, so the agent can
    # re-use a file it just read by quoting the same location (same
    # store-before-disk order as the media router's GET).
    parts = _media_parts(rel)
    if parts:
        lookup = getattr(context, "upload_file_lookup", None)
        if lookup is not None:
            data = lookup(parts[3])
            if data is not None:
                name = _clean_name(spec.get("name"), fallback=_clean_name(parts[3]))
                return UploadPayload(
                    name=name,
                    data=data,
                    mime_type=_mime_for(spec, name),
                    source="attachment",
                    origin=rel,
                )

    hidden = _hidden_part(rel)
    if hidden is not None:
        raise UploadSourceError(
            f"Hidden files and directories cannot be uploaded ({hidden!r}): {rel!r}"
        )
    try:
        full = resolve_within(context.project_fs_path, rel)
    except PathEscapeError as exc:
        raise UploadSourceError(f"Access denied: path is outside the project workspace ({rel!r})") from exc
    if not full.is_file():
        raise UploadSourceError(f"File not found: {rel}")

    size = full.stat().st_size
    if size > max_bytes:
        raise UploadSourceError(
            f"Upload {rel!r} is {_mb(size)}, above the {_mb(max_bytes)} limit"
        )
    data = full.read_bytes()
    if len(data) > max_bytes:
        raise UploadSourceError(
            f"Upload {rel!r} is {_mb(len(data))}, above the {_mb(max_bytes)} limit"
        )
    name = _clean_name(spec.get("name"), fallback=_clean_name(rel))
    return UploadPayload(
        name=name,
        data=data,
        mime_type=_mime_for(spec, name),
        source="path",
        origin=rel,
    )


def _resolve_attachment(context, spec: dict[str, Any], max_bytes: int) -> UploadPayload:
    raw_name = str(spec.get("name") or "").strip()
    if not raw_name:
        raise UploadSourceError("source='attachment' requires 'name' (the attached filename)")
    lookup = getattr(context, "upload_file_lookup", None)
    if lookup is None:
        raise UploadSourceError(
            "Conversation attachments are not available here; use source='path' or source='inline'"
        )
    # Look the name up verbatim first (the store is keyed by the server-stored
    # name), then by the sanitized form, so a name that only differs by path
    # decoration still resolves.
    cleaned = _clean_name(raw_name)
    data = None
    for candidate in dict.fromkeys([raw_name, cleaned]):
        data = lookup(candidate)
        if data is not None:
            break
    if data is None:
        names = getattr(context, "upload_file_names", None)
        available = sorted(names()) if names else []
        hint = ", ".join(available[:30]) if available else "none"
        raise UploadSourceError(f"Attachment {raw_name!r} not found. Available: {hint}")
    if len(data) > max_bytes:
        raise UploadSourceError(
            f"Attachment {raw_name!r} is {_mb(len(data))}, above the {_mb(max_bytes)} limit"
        )
    name = cleaned or _clean_name(raw_name, fallback="attachment")
    return UploadPayload(
        name=name,
        data=data,
        mime_type=_mime_for(spec, name),
        source="attachment",
        origin=raw_name,
    )


def _resolve_inline(context, spec: dict[str, Any], inline_max: int) -> UploadPayload:
    content = spec.get("content")
    content_b64 = spec.get("content_base64")
    if content is not None and content_b64 is not None:
        raise UploadSourceError("source='inline' takes either 'content' or 'content_base64', not both")
    if content_b64 is not None:
        if not isinstance(content_b64, str) or not _BASE64_RE.match(content_b64):
            raise UploadSourceError("content_base64 is not valid base64")
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            # Never echo the payload — it is caller content, not a credential,
            # but a decode error message must still stay value-free.
            raise UploadSourceError("content_base64 is not valid base64") from exc
        text_default = False
    elif content is not None:
        data = str(content).encode("utf-8")
        text_default = True
    else:
        raise UploadSourceError("source='inline' requires 'content' or 'content_base64'")

    if len(data) > inline_max:
        raise UploadSourceError(
            f"Inline content is {_mb(len(data))}, above the {_mb(inline_max)} limit"
        )
    name = _clean_name(spec.get("name"))
    if not name:
        raise UploadSourceError("source='inline' requires 'name' (the filename to present)")
    return UploadPayload(
        name=name,
        data=data,
        mime_type=_mime_for(spec, name, text_default=text_default),
        source="inline",
        origin=name,
    )


def _infer_source(spec: dict[str, Any]) -> str:
    explicit = spec.get("source")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    if spec.get("content_base64") is not None or spec.get("content") is not None:
        return "inline"
    if spec.get("path"):
        return "path"
    if spec.get("name"):
        return "attachment"
    raise UploadSourceError("Provide one of 'path', 'name' (attachment), or 'content'/'content_base64'")


def resolve_upload_spec(
    context,
    spec: Any,
    *,
    max_bytes: int = MAX_UPLOAD_FILE_BYTES,
    inline_max_bytes: int = MAX_INLINE_BYTES,
) -> UploadPayload:
    """Resolve one upload spec to bytes, or raise :class:`UploadSourceError`."""
    if not isinstance(spec, dict):
        raise UploadSourceError(f"Each file must be an object, got {type(spec).__name__}")
    source = _infer_source(spec)
    if source == "path":
        payload = _resolve_path(context, spec, max_bytes)
    elif source == "attachment":
        payload = _resolve_attachment(context, spec, max_bytes)
    elif source == "inline":
        payload = _resolve_inline(context, spec, inline_max_bytes)
    else:
        raise UploadSourceError(f"Unknown upload source {source!r} (allowed: {', '.join(_SOURCES)})")
    field = spec.get("field")
    if isinstance(field, str) and field.strip():
        payload = UploadPayload(
            name=payload.name,
            data=payload.data,
            mime_type=payload.mime_type,
            source=payload.source,
            origin=payload.origin,
            field=field.strip(),
        )
    return payload


def resolve_upload_specs(
    context,
    specs: Any,
    *,
    max_bytes: int = MAX_UPLOAD_FILE_BYTES,
    inline_max_bytes: int = MAX_INLINE_BYTES,
    max_files: int = MAX_UPLOAD_FILES,
) -> list[UploadPayload]:
    """Resolve a list of upload specs (schema-declared array, LLM reality included)."""
    if not isinstance(specs, list):
        # Mirrors the stringified-dict coercion contract elsewhere in the tool
        # layer: a JSON string is a model mistake worth naming precisely.
        if isinstance(specs, str):
            raise UploadSourceError("files must be an array, not a stringified JSON value")
        raise UploadSourceError(f"files must be an array, got {type(specs).__name__}")
    if not specs:
        raise UploadSourceError("files must contain at least one entry")
    if len(specs) > max_files:
        raise UploadSourceError(f"Too many files: {len(specs)} (limit {max_files} per call)")
    return [
        resolve_upload_spec(
            context, spec, max_bytes=max_bytes, inline_max_bytes=inline_max_bytes
        )
        for spec in specs
    ]


def _mb(num_bytes: int) -> str:
    """Human-readable size for error messages."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f}MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f}KB"
    return f"{num_bytes}B"
