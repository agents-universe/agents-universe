"""Tests for the doc_reader tool — real python-docx/pypdf/openpyxl fixtures.

Fixtures are built at test time (the libraries are runtime deps), covering the
docx/pdf/xlsx/text dispatch, page/char caps, path confinement, the in-memory
upload-store fallback, and the info operation.
"""
from __future__ import annotations

import io

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.doc_reader import DocReaderTool


def _context(tmp_path, **kwargs) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="c1",
        user_id="u1",
        **kwargs,
    )


def _write_docx(path, paragraphs, table_rows=None):
    import docx
    doc = docx.Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    if table_rows:
        table = doc.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row in enumerate(table_rows):
            for c, value in enumerate(row):
                table.cell(r, c).text = value
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def _write_pdf(path, pages):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()


def _write_xlsx(path, sheets: dict):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))


# ── docx ────────────────────────────────────────────────────────────────────


async def test_read_docx_paragraphs_and_tables(tmp_path):
    _write_docx(
        tmp_path / "resume.docx",
        ["张三", "五年 Python 经验"],
        table_rows=[["技能", "年限"], ["Python", "5"]],
    )
    result = await DocReaderTool().execute({"path": "resume.docx"}, _context(tmp_path))
    assert result["format"] == "docx"
    assert "张三" in result["text"]
    assert "Python | 5" in result["text"]
    assert result["truncated"] is False
    assert result["meta"]["tables"] == 1


async def test_read_docx_truncated_by_max_chars(tmp_path):
    _write_docx(tmp_path / "big.docx", ["x" * 100] * 50)
    result = await DocReaderTool().execute(
        {"path": "big.docx", "max_chars": 150}, _context(tmp_path)
    )
    assert result["truncated"] is True
    assert len(result["text"]) <= 150


# ── pdf ─────────────────────────────────────────────────────────────────────


async def test_read_pdf_page_cap(tmp_path):
    _write_pdf(tmp_path / "report.pdf", ["page one", "page two"])
    result = await DocReaderTool().execute(
        {"path": "report.pdf", "max_pages": 1}, _context(tmp_path)
    )
    assert result["format"] == "pdf"
    assert result["meta"]["pages_total"] == 2
    assert result["meta"]["pages_read"] == 1
    assert result["truncated"] is True
    assert "page one" in result["text"]
    assert "page two" not in result["text"]


async def test_read_encrypted_pdf_error(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    path = tmp_path / "locked.pdf"
    with open(path, "wb") as f:
        writer.write(f)
    result = await DocReaderTool().execute({"path": "locked.pdf"}, _context(tmp_path))
    assert "encrypted" in result["error"]


# ── xlsx ────────────────────────────────────────────────────────────────────


async def test_read_xlsx_sheet_selection(tmp_path):
    _write_xlsx(tmp_path / "candidates.xlsx", {
        "Sheet1": [["name", "score"], ["张三", 4]],
        "评分": [["维度", "分"], ["技能", 5]],
    })
    result = await DocReaderTool().execute(
        {"path": "candidates.xlsx", "sheet": "评分"}, _context(tmp_path)
    )
    assert result["format"] == "xlsx"
    assert result["meta"]["sheets"] == ["Sheet1", "评分"]
    assert result["meta"]["sheet"] == "评分"
    assert "技能\t5" in result["text"]


async def test_read_xlsx_default_first_sheet(tmp_path):
    _write_xlsx(tmp_path / "a.xlsx", {"First": [["only"]], "Second": [["other"]]})
    result = await DocReaderTool().execute({"path": "a.xlsx"}, _context(tmp_path))
    assert result["meta"]["sheet"] == "First"
    assert "only" in result["text"]


async def test_read_xlsx_unknown_sheet_error(tmp_path):
    _write_xlsx(tmp_path / "a.xlsx", {"First": [["only"]]})
    result = await DocReaderTool().execute(
        {"path": "a.xlsx", "sheet": "Missing"}, _context(tmp_path)
    )
    assert "not found" in result["error"]
    assert "First" in result["error"]


# ── text ────────────────────────────────────────────────────────────────────


async def test_read_txt_replaces_bad_bytes(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes("你好".encode("utf-8") + b"\xff\xfe" + b"tail")
    result = await DocReaderTool().execute({"path": "notes.txt"}, _context(tmp_path))
    assert "你好" in result["text"]
    assert "tail" in result["text"]


# ── unsupported / missing / escape ──────────────────────────────────────────


async def test_unsupported_format_hint(tmp_path):
    (tmp_path / "slides.pptx").write_bytes(b"PK\x03\x04")
    result = await DocReaderTool().execute({"path": "slides.pptx"}, _context(tmp_path))
    assert "Unsupported format" in result["error"]
    assert "code_executor" in result["error"]


async def test_missing_file(tmp_path):
    result = await DocReaderTool().execute({"path": "nope.docx"}, _context(tmp_path))
    assert "not found" in result["error"]


async def test_path_escape_rejected(tmp_path):
    result = await DocReaderTool().execute(
        {"path": "../outside.docx"}, _context(tmp_path)
    )
    assert "error" in result


async def test_absolute_windows_path_rejected(tmp_path):
    result = await DocReaderTool().execute(
        {"path": "C:\\Windows\\evil.docx"}, _context(tmp_path)
    )
    assert "error" in result


# ── upload-store fallback ───────────────────────────────────────────────────


async def test_upload_store_fallback_parses_docx(tmp_path):
    """A .tmp/media/<cid>/resume.docx resolves bytes from the upload store."""
    import docx
    doc = docx.Document()
    doc.add_paragraph("聊天里上传的简历")
    buf = io.BytesIO()
    doc.save(buf)

    def _lookup(name):
        return buf.getvalue() if name == "resume.docx" else None

    result = await DocReaderTool().execute(
        {"path": ".tmp/media/c1/resume.docx"},
        _context(tmp_path, upload_file_lookup=_lookup),
    )
    assert "聊天里上传的简历" in result["text"]


async def test_upload_store_miss_reports_expiry(tmp_path):
    result = await DocReaderTool().execute(
        {"path": ".tmp/media/c1/gone.docx"},
        _context(tmp_path, upload_file_lookup=lambda name: None),
    )
    assert "not found" in result["error"]
    assert "re-attach" in result["hint"]


# ── info ────────────────────────────────────────────────────────────────────


async def test_info_returns_meta_without_text(tmp_path):
    _write_pdf(tmp_path / "r.pdf", ["one", "two"])
    result = await DocReaderTool().execute(
        {"operation": "info", "path": "r.pdf"}, _context(tmp_path)
    )
    assert result["meta"] == {"pages": 2}
    assert "text" not in result


async def test_info_xlsx_sheets(tmp_path):
    _write_xlsx(tmp_path / "s.xlsx", {"A": [[1]], "B": [[2]]})
    result = await DocReaderTool().execute(
        {"operation": "info", "path": "s.xlsx"}, _context(tmp_path)
    )
    assert result["meta"]["sheets"] == ["A", "B"]
    assert "text" not in result
