"""Document reader — extract text from office/PDF files.

``filesystem read_file`` is UTF-8-only and rejects binary, and ``code_executor``
runs in a subprocess that cannot see the in-memory upload store — so a .docx
resume (downloaded by the email tool, or pasted into chat) was previously
unreadable. This tool is the one path for binary documents: python-docx /
pypdf / openpyxl are already runtime dependencies (generation skills use
them), so parsing adds no new package. Workspace files are read from disk;
``.tmp/media/<conversation>/...`` paths fall back to the upload store via
``ToolContext.upload_file_lookup`` (all three parsers accept file-like objects).
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext
from ..paths import PathEscapeError, resolve_within

_log = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 50_000
_MAX_CHARS_CAP = 200_000
_DEFAULT_MAX_PAGES = 20
_MAX_PAGES_CAP = 100
_DEFAULT_MAX_ROWS = 500
_MAX_ROWS_CAP = 5_000

_TEXT_SUFFIXES = {".txt", ".md", ".csv"}
_UNSUPPORTED_MSG = (
    "Unsupported format for {ext} — doc_reader reads .docx/.pdf/.xlsx/.xlsm/"
    ".txt/.md/.csv. Use code_executor for {ext}, or ask the user to export the "
    "file to .pdf/.docx first."
)


class DocReaderTool(Tool):
    name = "doc_reader"
    prompt_hint = (
        "Read the text of office/PDF files in the workspace (.docx/.pdf/.xlsx/"
        ".txt) — the only path for binary documents, since filesystem read_file "
        "rejects binary. Use info first to triage large PDFs/workbooks; prefer "
        "it over code_executor when you only need the text."
    )
    description = (
        "Extract text from documents: Word (.docx), PDF, Excel (.xlsx/.xlsm), and "
        "plain text (.txt/.md/.csv), including attachments the email tool saved "
        "under .tmp/mail/. Supports read (text) and info (metadata only)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "info"],
                "default": "read",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative file path, e.g. .tmp/mail/<cid>/resume.docx",
            },
            "max_chars": {"type": "integer", "description": "Text cap (default 50000, max 200000)"},
            "max_pages": {"type": "integer", "description": "PDF page cap (default 20, max 100)"},
            "sheet": {"type": "string", "description": "Excel sheet name (default: first sheet)"},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        rel_path = str(params.get("path") or "").strip()
        if not rel_path:
            return {"error": "`path` is required"}
        operation = params.get("operation") or "read"
        if operation not in ("read", "info"):
            return {"error": f"Unknown operation: {operation}"}

        max_chars = _clamp(params.get("max_chars"), _DEFAULT_MAX_CHARS, 1, _MAX_CHARS_CAP)
        max_pages = _clamp(params.get("max_pages"), _DEFAULT_MAX_PAGES, 1, _MAX_PAGES_CAP)
        max_rows = _DEFAULT_MAX_ROWS
        sheet = params.get("sheet")

        # Workspace confinement for on-disk paths.
        try:
            target = resolve_within(context.project_fs_path, rel_path)
        except PathEscapeError as e:
            return {"error": str(e)}

        suffix = Path(rel_path.replace("\\", "/")).suffix.lower()

        # In-memory upload store fallback: binary attachments never land on
        # disk, so a .tmp/media/<cid>/<file> path resolves through the lookup
        # hook (same shape as filesystem._media_parts).
        data: bytes | None = None
        parts = rel_path.replace("\\", "/").split("/")
        if len(parts) == 4 and parts[:2] == [".tmp", "media"]:
            lookup = getattr(context, "upload_file_lookup", None)
            data = lookup(parts[3]) if lookup else None
            if data is None:
                return {
                    "error": f"Uploaded file not found (or expired): {rel_path}",
                    "hint": "Uploads live only for the current conversation — re-attach the file.",
                }
        elif not target.is_file():
            return {"error": f"File not found: {rel_path}"}

        try:
            if operation == "info":
                return _info(suffix, target, data, rel_path, max_pages, sheet)
            return _read(
                suffix, target, data, rel_path,
                max_chars=max_chars, max_pages=max_pages, max_rows=max_rows, sheet=sheet,
            )
        except ValueError as e:
            # Parser-level problems (encrypted PDF, bad workbook) — actionable.
            return {"error": str(e), "path": rel_path}
        except Exception as e:
            _log.warning("doc_reader failed on %s: %s", rel_path, e, exc_info=True)
            return {"error": f"Failed to read {rel_path}: {e}"}


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        if value is None:
            return default
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _open_bytes(data: bytes | None):
    """File-like view of upload-store bytes, or None for disk reads."""
    return io.BytesIO(data) if data is not None else None


def _read(suffix, target: Path, data: bytes | None, rel_path: str,
          *, max_chars: int, max_pages: int, max_rows: int, sheet) -> dict[str, Any]:
    stream = _open_bytes(data)

    if suffix == ".docx":
        return _read_docx(target, stream, rel_path, max_chars)
    if suffix == ".pdf":
        return _read_pdf(target, stream, rel_path, max_chars, max_pages)
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(target, stream, rel_path, max_chars, max_rows, sheet)
    if suffix in _TEXT_SUFFIXES:
        return _read_text(target, data, rel_path, max_chars)
    return {"error": _UNSUPPORTED_MSG.format(ext=suffix or "(no extension)"), "path": rel_path}


def _info(suffix, target: Path, data: bytes | None, rel_path: str,
          max_pages: int, sheet) -> dict[str, Any]:
    if suffix == ".docx":
        import docx
        doc = docx.Document(str(target)) if data is None else docx.Document(stream)
        return {
            "path": rel_path, "format": "docx",
            "meta": {"paragraphs": len(doc.paragraphs), "tables": len(doc.tables)},
        }
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(target)) if data is None else PdfReader(stream)
        if reader.is_encrypted:
            raise ValueError("PDF is encrypted — cannot read")
        return {"path": rel_path, "format": "pdf", "meta": {"pages": len(reader.pages)}}
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(str(target), read_only=True, data_only=True) \
            if data is None else openpyxl.load_workbook(stream, read_only=True, data_only=True)
        try:
            return {
                "path": rel_path, "format": suffix.lstrip("."),
                "meta": {"sheets": list(wb.sheetnames)},
            }
        finally:
            wb.close()
    if suffix in _TEXT_SUFFIXES:
        raw = data if data is not None else target.read_bytes()
        return {
            "path": rel_path, "format": suffix.lstrip("."),
            "meta": {"bytes": len(raw)},
        }
    return {"error": _UNSUPPORTED_MSG.format(ext=suffix or "(no extension)"), "path": rel_path}


def _read_docx(target: Path, stream, rel_path: str, max_chars: int) -> dict[str, Any]:
    import docx
    doc = docx.Document(str(target)) if stream is None else docx.Document(stream)
    chunks: list[str] = []
    size = 0
    truncated = False
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if size + len(text) > max_chars:
            truncated = True
            break
        chunks.append(text)
        size += len(text) + 1
    # Tables append after paragraphs: pipe-joined rows keep cells readable
    # and survive markdown rendering in the chat.
    if not truncated:
        for table in doc.tables:
            for row in table.rows:
                line = " | ".join(cell.text.strip().replace("\n", " ") for cell in row.cells)
                if size + len(line) > max_chars:
                    truncated = True
                    break
                chunks.append(line)
                size += len(line) + 1
            if truncated:
                break
    return {
        "path": rel_path, "format": "docx",
        "text": "\n".join(chunks), "truncated": truncated,
        "meta": {"paragraphs": len(doc.paragraphs), "tables": len(doc.tables)},
    }


def _read_pdf(target: Path, stream, rel_path: str, max_chars: int, max_pages: int) -> dict[str, Any]:
    from pypdf import PdfReader
    reader = PdfReader(str(target)) if stream is None else PdfReader(stream)
    if reader.is_encrypted:
        raise ValueError("PDF is encrypted — cannot read")
    chunks: list[str] = []
    size = 0
    pages_read = 0
    truncated = False
    for page in reader.pages:
        if pages_read >= max_pages:
            truncated = True
            break
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if size + len(text) > max_chars:
            truncated = True
            break
        chunks.append(text)
        size += len(text) + 1
        pages_read += 1
    return {
        "path": rel_path, "format": "pdf",
        "text": "\n".join(chunks), "truncated": truncated,
        "meta": {"pages_total": len(reader.pages), "pages_read": pages_read},
    }


def _read_xlsx(target: Path, stream, rel_path: str, max_chars: int,
               max_rows: int, sheet) -> dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(str(target), read_only=True, data_only=True) \
        if stream is None else openpyxl.load_workbook(stream, read_only=True, data_only=True)
    try:
        sheets = list(wb.sheetnames)
        name = sheet or sheets[0]
        if name not in wb.sheetnames:
            raise ValueError(f"Sheet {name!r} not found; available: {', '.join(sheets)}")
        ws = wb[name]
        rows: list[str] = []
        size = 0
        rows_read = 0
        truncated = False
        for row in ws.iter_rows(values_only=True):
            if rows_read >= max_rows:
                truncated = True
                break
            line = "\t".join("" if v is None else str(v) for v in row)
            if size + len(line) > max_chars:
                truncated = True
                break
            rows.append(line)
            size += len(line) + 1
            rows_read += 1
        return {
            "path": rel_path, "format": target.suffix.lstrip(".").lower(),
            "text": "\n".join(rows), "truncated": truncated,
            "meta": {"sheets": sheets, "sheet": name, "rows_read": rows_read},
        }
    finally:
        wb.close()


def _read_text(target: Path, data: bytes | None, rel_path: str, max_chars: int) -> dict[str, Any]:
    raw = data if data is not None else target.read_bytes()
    # UTF-8 with BOM tolerance + replacement: a stray GBK byte must not kill
    # the read (same posture as web_fetch's errors="replace").
    text = raw.decode("utf-8-sig", errors="replace")
    truncated = len(text) > max_chars
    return {
        "path": rel_path, "format": target.suffix.lstrip(".").lower() or "txt",
        "text": text[:max_chars], "truncated": truncated,
        "meta": {"bytes": len(raw)},
    }
