---
slug: "office/pdf"
description: "Generate PDF documents with reportlab — heading hierarchy, styled paragraphs, tables, headers/footers, CJK-safe fonts (built-in CID fonts, no font files needed); pypdf verification before delivery"
type: "guidance"
triggers:
  - "生成pdf"
  - "pdf文件"
  - "pdf文档"
  - "导出pdf"
  - "pdf"
tools:
  - code_executor
  - filesystem
  - user_confirm
---

# Skill: PDF

Create `.pdf` documents with `reportlab` (preinstalled — import directly, never `pip install`). Built on the platypus layout engine: headings, paragraphs, tables, page breaks, headers/footers all flow automatically across pages.

## Trigger Conditions

- User asks for a PDF document / report / 导出 PDF / 生成 pdf 文件.
- Converting deliverable content (report, spec, analysis result) into a portable PDF.

## Rule 1 — Structure with Real Heading Styles

- Build a `ParagraphStyle` hierarchy — `Heading1` / `Heading2` / `Heading3` — and use it for every heading. Never fake headings with bold body paragraphs; outline/bookmark navigation depends on real styles.
- Register each style once with `addMapping`-style inheritance:

```python
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

styles = getSampleStyleSheet()
h1 = ParagraphStyle("Heading1", parent=styles["Heading1"], fontName=CJK_FONT, fontSize=18, spaceAfter=10)
h2 = ParagraphStyle("Heading2", parent=styles["Heading2"], fontName=CJK_FONT, fontSize=14, spaceAfter=8)
body = ParagraphStyle("Body", parent=styles["BodyText"], fontName=CJK_FONT, fontSize=10.5, leading=16)
```

- Page setup: `SimpleDocTemplate(path, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)`.

## Rule 2 — CJK Fonts (Required for Chinese Content)

Chinese must use a font that actually covers CJK. Two options, in order:

1. **Built-in CID font (always available, no font files)**: `pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))` — works in any container. **Caveat: CID fonts have no bold/italic variants** — `<b>` tags are silently ignored. Emphasize with size/color (e.g. `textColor=HexColor("#1F4E79")` on headings), never weight.
2. **System TTF (only if present)**: try `Microsoft YaHei` / `SimSun` / `Noto Sans CJK SC` via `TTFont` if the runtime ships them — these support real bold. Probe with `os.path.exists` and fall back to CID when absent.

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

CJK_FONT = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
# Or, when a TTF exists: pdfmetrics.registerFont(TTFont("YaHei", "/path/to/msyh.ttc"))
```

## Rule 3 — Paragraphs & Inline Markup

- `Paragraph(text, style)` accepts a small HTML-like subset: `<b>`, `<i>`, `<font color=...>`, `<br/>`. Escape `&` `<` `>` in raw content (or use `xml.sax.saxutils.escape`).
- One idea per paragraph; use `Spacer(1, 6)` between blocks. Lists: prefix `• ` / `1. ` in body paragraphs (platypus has no native list flowable in v4).

## Rule 4 — Tables

- `Table(data, colWidths=[...])` + `TableStyle` for borders and header shading:

```python
t = Table(data, colWidths=[40*mm, 80*mm, 30*mm], repeatRows=1)
t.setStyle(TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DEEAF6")),
    ("FONTNAME", (0, 0), (-1, -1), CJK_FONT),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
]))
```

- `repeatRows=1` repeats the header row across page breaks. Long cells wrap automatically; set `wordWrap="CJK"` in the cell paragraph style if wrap breaks on CJK text.

## Rule 5 — Headers, Footers & Page Numbers

- Use the `onFirstPage` / `onLaterPages` callbacks — draw the footer (title + page number) on each page:

```python
def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(CJK_FONT, 8)
    canvas.drawCentredString(A4[0] / 2, 10 * mm, f"— {doc.page} —")
    canvas.restoreState()

doc = SimpleDocTemplate(path, pagesize=A4, onFirstPage=footer, onLaterPages=footer)
```

## Rule 6 — Flow & Page Breaks

- platypus flows content automatically; insert `PageBreak()` explicitly only where a new section must start on a fresh page.
- Long documents split across pages automatically — no manual pagination logic.

## Rule 7 — Output & Verify

- Save to `os.path.join(os.environ["OUTPUT_DIR"], "document.pdf")` — auto-delivered as a `/api/media/` download link.
- **Verify before delivering**: reopen with `pypdf` and assert the file is a valid PDF, non-empty, and has the expected page count; print the page count and first-page text snippet to stdout:

```python
from pypdf import PdfReader
r = PdfReader(out_path)
assert len(r.pages) > 0, "empty PDF"
print("pages:", len(r.pages))
print("first page head:", (r.pages[0].extract_text() or "")[:120])
```

- Fix and re-run on any mismatch. Never deliver an unverified file.
- ASCII filenames only (`document.pdf`).

## Output Requirements

Deliver the `/api/media/` link, a section outline, data sources used, and assumptions flagged `[inferred]`. State the CID-font no-bold limitation in the reply only when the runtime lacks a CJK TTF and the design relies on weight contrast.
