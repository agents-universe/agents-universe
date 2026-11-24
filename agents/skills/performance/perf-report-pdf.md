---
slug: "performance/perf-report-pdf"
description:
  zh-CN: "把压测 Markdown 报告转换为咨询级 PDF——封面、目录、密级页眉页脚、内嵌证据图；pypdf 校验小节与 p95 文本后与 MD 一并交付"
  en-US: "Convert the stress-test Markdown report into a consulting-grade PDF - cover, TOC, classification header/footer, embedded evidence figures; pypdf-verified for section headings and p95 text, then delivered alongside the MD"
type: "guidance"
triggers:
  - "generate report pdf"
  - "convert report to pdf"
  - "perf pdf report"
  - "压测报告pdf"
  - "性能报告转pdf"
  - "导出pdf报告"
tools:
  - code_executor
  - filesystem
---

# Skill: Perf Report PDF

Phase 5 second half: turn the canonical Markdown report into the **primary final deliverable** - a consulting-grade PDF. The MD stays in the workspace as the authoritative, greppable source; the PDF is what stakeholders read.

## When to Run

After `performance/perf-report-writer` has written and self-checked `performance/perf-report-{YYYYMMDD}.md`. Full engagements and report-only re-runs both end with the PDF; design-only or writeback-only slices do not.

## Contract

- **Input**: `performance/perf-report-{YYYYMMDD}.md` (workspace-relative).
- **Output**: `performance/perf-report-{YYYYMMDD}.pdf` in the workspace `performance/` directory - **not** `OUTPUT_DIR`. Writing to `OUTPUT_DIR` would auto-deliver a duplicate attachment; delivery is exactly two explicit `deliver_file` calls (MD + PDF).
- Layout conventions follow `agents/skills/office/pdf.md`: reportlab platypus, CJK-safe fonts, pypdf verification before delivery.

## How to Run

1. Take the **Converter Script** below verbatim - change **only** the three `CONFIG` values (`MD_PATH`, `OUT_PATH`, `CLASSIFICATION_OVERRIDE`). Never retype, restructure, or "simplify" the script.
2. Paste it into `code_executor(language="python")`.
3. Read stdout: expect `pages: N`, `first page head: ...`, and `pdf written: ...`. Any `WARN:` line must be addressed (usually a bad figure path in `Evidence figures:` - fix the MD, re-run) before delivery. An `AssertionError` from the script means the PDF is incomplete - fix and re-run.

## Converter Script

```python
# Performance test report Markdown -> consulting-grade PDF.
# Edit ONLY the three CONFIG values below; never restructure this script.
import os
import re
from xml.sax.saxutils import escape as _xesc

MD_PATH = "performance/perf-report-20261124.md"   # CONFIG: input .md (workspace-relative or absolute)
OUT_PATH = "performance/perf-report-20261124.pdf"  # CONFIG: output .pdf (workspace-relative or absolute)
CLASSIFICATION_OVERRIDE = ""                       # CONFIG: e.g. "CONFIDENTIAL"; empty = parse from cover metadata

def _warn(msg):
    print("WARN:", msg)

_FENCE = chr(96) * 3  # markdown code fence marker; kept as a constant so this script source contains no literal fence

assert isinstance(MD_PATH, str) and MD_PATH.endswith(".md"), "CONFIG MD_PATH must end with .md"
assert isinstance(OUT_PATH, str) and OUT_PATH.endswith(".pdf"), "CONFIG OUT_PATH must end with .pdf"

_ROOT = os.environ.get("PROJECT_DIR") or os.getcwd()

def _resolve(p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(_ROOT, p))

md_path = _resolve(MD_PATH)
out_path = _resolve(OUT_PATH)
assert os.path.isfile(md_path), "report not found: %s" % md_path
md_text = open(md_path, encoding="utf-8").read()

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, LongTable, NextPageTemplate,
    PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

# --- Fonts: CJK CID font always available; probe for a bold-capable TTF -----
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
CJK_FONT = "STSong-Light"
BODY_FONT = CJK_FONT
BOLD_FONT = CJK_FONT
for _path, _idx in (
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf", 0),
    ("C:/Windows/Fonts/msyhbd.ttc", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
):
    try:
        if os.path.exists(_path):
            pdfmetrics.registerFont(TTFont("ReportBold", _path, subfontIndex=_idx))
            BOLD_FONT = "ReportBold"
            break
    except Exception:
        pass
pdfmetrics.registerFontFamily(
    CJK_FONT, normal=CJK_FONT, bold=BOLD_FONT, italic=CJK_FONT, boldItalic=BOLD_FONT
)

# --- Palette ---------------------------------------------------------------
ACCENT = colors.HexColor("#1F4E79")
GREY = colors.HexColor("#666666")
LIGHT = colors.HexColor("#F2F2F2")
HEADER_BG = colors.HexColor("#DEEAF6")
PASS_GREEN = colors.HexColor("#2E7D32")
FAIL_RED = colors.HexColor("#B00020")

PAGE_W, PAGE_H = A4
LM = RM = 20 * mm
TM = 24 * mm
BM = 18 * mm
FRAME_W = PAGE_W - LM - RM

def _st(name, **kw):
    kw.setdefault("fontName", BODY_FONT)
    return ParagraphStyle(name, **kw)

S_TITLE = _st("CoverTitle", fontSize=26, leading=34, alignment=TA_CENTER, textColor=ACCENT, spaceAfter=8, wordWrap="CJK")
S_COVER_K = _st("CoverKey", fontSize=10, leading=16, textColor=ACCENT, fontName=BOLD_FONT)
S_COVER_V = _st("CoverVal", fontSize=10, leading=16, wordWrap="CJK")
S_BODY = _st("Body", fontSize=10.5, leading=16, spaceAfter=6, wordWrap="CJK")
S_SEC = _st("SecHead", fontName=BOLD_FONT, fontSize=15, leading=20, textColor=ACCENT, spaceBefore=16, spaceAfter=8, wordWrap="CJK")
S_TOC_TITLE = _st("TocTitle", fontName=BOLD_FONT, fontSize=15, leading=20, textColor=ACCENT, spaceAfter=10, wordWrap="CJK")
S_FIND = _st("FindHead", fontName=BOLD_FONT, fontSize=12.5, leading=17, textColor=ACCENT, spaceBefore=12, spaceAfter=4, wordWrap="CJK")
S_CODE = _st("Code", fontName="Courier", fontSize=8.5, leading=12, wordWrap="CJK")
S_CODE_CJK = _st("CodeCJK", fontSize=8.5, leading=12, wordWrap="CJK")
S_CELL = _st("Cell", fontSize=9, leading=13, wordWrap="CJK")
S_CELL_H = _st("CellH", fontName=BOLD_FONT, fontSize=9, leading=13, textColor=ACCENT, wordWrap="CJK")
S_CAP = _st("Cap", fontSize=8, leading=11, alignment=TA_CENTER, textColor=GREY, spaceAfter=6, wordWrap="CJK")
S_BANNER = _st("Banner", fontName=BOLD_FONT, fontSize=14, leading=18, alignment=TA_CENTER, textColor=ACCENT)

_FIELD_RE = re.compile(
    r"^(SLA|Result|Profile|Target|Observed|p50|p95|p99|Endpoint|Method|"
    r"Bottleneck|Recommendation|Evidence|Evidence figures|Limitation|"
    r"Segment|Status|ID)\s*[:：]\s*(.*)$",
    re.I,
)

def _inline(text):
    """Escape first, then apply minimal markup."""
    t = _xesc(text.strip())
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', t)
    return t

def _para_html(text):
    m = _FIELD_RE.match(text.strip())
    if m:
        return "<b>%s:</b> %s" % (_xesc(m.group(1)), _inline(m.group(2)))
    return _inline(text)

# --- Parsing (structure-aware, keyed to the fixed 9-section contract) ------
def _split_table_row(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]

def _is_sep_row(cells):
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells)

def parse_md(text):
    lines = text.splitlines()
    title = "Performance Test Report"
    cover = []
    blocks = []
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    if i < len(lines):
        title = lines[i][2:].strip() or title
        i += 1
    while i < len(lines) and not lines[i].startswith("## "):
        s = lines[i].strip()
        if s and not s.startswith(("#", "---", "***")):
            m = re.match(r"^([^:：]{1,48})[:：]\s*(.+)$", s)
            if m and not s.startswith("!"):
                cover.append((m.group(1).strip(), m.group(2).strip()))
            else:
                cover.append(("", s))
        i += 1
    para_buf = []
    in_code = False
    code_buf = []
    def flush_para():
        if para_buf:
            blocks.append(("para", " ".join(para_buf)))
            del para_buf[:]
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if in_code:
            if s.startswith(_FENCE):
                blocks.append(("code", "\n".join(code_buf)))
                code_buf = []
                in_code = False
            else:
                code_buf.append(raw)
            i += 1
            continue
        if s.startswith(_FENCE):
            flush_para()
            in_code = True
            i += 1
            continue
        if s.startswith("## "):
            flush_para()
            blocks.append(("h2", s[3:].strip()))
        elif s.startswith("### "):
            flush_para()
            blocks.append(("h3", s[4:].strip()))
        elif s.startswith("|"):
            flush_para()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = _split_table_row(lines[i])
                if not _is_sep_row(cells):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        else:
            m_img = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", s)
            if m_img:
                flush_para()
                blocks.append(("image", (m_img.group(1), m_img.group(2).strip())))
            elif not s or s.startswith(("---", "***", "___")):
                flush_para()
            else:
                if s.startswith(("- ", "* ")):
                    s = "• " + s[2:]
                elif s.startswith(">"):
                    s = s.lstrip("> ").strip()
                para_buf.append(s)
        i += 1
    if in_code:
        _warn("unclosed code fence - treating remainder as code")
        blocks.append(("code", "\n".join(code_buf)))
    flush_para()
    return title, cover, blocks

# --- Flowable builders -----------------------------------------------------
def _code_flow(text):
    flows = []
    for ln in text.split("\n"):
        st = S_CODE if ln.isascii() else S_CODE_CJK
        flows.append(Paragraph(_xesc(ln) if ln else "&nbsp;", st))
    if not flows:
        flows = [Paragraph("&nbsp;", S_CODE)]
    tbl = Table([[flows]], colWidths=[FRAME_W])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return [Spacer(1, 4), tbl, Spacer(1, 6)]

def _table_flow(rows):
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    norm = [(list(r) + [""] * (ncols - len(r)))[:ncols] for r in rows]
    data = []
    for ri, row in enumerate(norm):
        st = S_CELL_H if ri == 0 else S_CELL
        data.append([Paragraph(_para_html(c) if c else "&nbsp;", st) for c in row])
    weights = [max(max(len(norm[r][c]) for r in range(len(norm))), 4) for c in range(ncols)]
    total = float(sum(weights)) or 1.0
    colw = [FRAME_W * w / total for w in weights]
    minw = 18 * mm
    colw = [max(w, minw) for w in colw]
    s = sum(colw)
    if s > FRAME_W:
        colw = [w * FRAME_W / s for w in colw]
    cls = LongTable if len(norm) > 12 else Table
    t = cls(data, colWidths=colw, repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return [Spacer(1, 4), t, Spacer(1, 8)]

def _image_flow(item):
    alt, raw_path = item
    p = raw_path if os.path.isabs(raw_path) else os.path.normpath(os.path.join(_ROOT, raw_path))
    if not os.path.isfile(p):
        _warn("evidence missing: %s" % raw_path)
        return [Paragraph(_xesc("[evidence missing: %s]" % raw_path), S_CAP)]
    try:
        from reportlab.lib.utils import ImageReader
        iw, ih = ImageReader(p).getSize()
        scale = min(FRAME_W / iw, 150 * mm / ih)
        if scale > 1:
            scale = 1.0
        img = Image(p, width=iw * scale, height=ih * scale)
        img.hAlign = "CENTER"
        out = [Spacer(1, 4), img]
        if alt:
            out.append(Paragraph(_xesc(alt), S_CAP))
        else:
            out.append(Spacer(1, 6))
        return out
    except Exception as e:
        _warn("evidence unreadable: %s (%s)" % (raw_path, e))
        return [Paragraph(_xesc("[evidence missing: %s]" % raw_path), S_CAP)]

def build_story(title, cover, blocks, classification):
    story = []
    banner = Table([[Paragraph(_xesc(classification), S_BANNER)]], colWidths=[FRAME_W])
    banner.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.5, ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(Spacer(1, 10 * mm))
    story.append(banner)
    story.append(Spacer(1, 18 * mm))
    story.append(Paragraph(_inline(title), S_TITLE))
    story.append(Spacer(1, 10 * mm))
    if cover:
        data = []
        for k, v in cover:
            if k:
                data.append([Paragraph(_xesc(k) + ":", S_COVER_K), Paragraph(_inline(v), S_COVER_V)])
            else:
                data.append([Paragraph(_inline(v), S_COVER_V), ""])
        ct = Table(data, colWidths=[FRAME_W * 0.35, FRAME_W * 0.65])
        ct.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(ct)
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())
    story.append(Paragraph("Table of Contents", S_TOC_TITLE))
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("TOC0", fontName=BOLD_FONT, fontSize=11, leading=18, textColor=ACCENT, spaceBefore=8),
        ParagraphStyle("TOC1", fontName=BODY_FONT, fontSize=9.5, leading=15, leftIndent=14),
    ]
    story.append(toc)
    story.append(PageBreak())
    for kind, payload in blocks:
        if kind == "h2":
            story.append(Paragraph(_inline(payload), S_SEC))
        elif kind == "h3":
            story.append(Paragraph(_inline(payload), S_FIND))
        elif kind == "para":
            story.append(Paragraph(_para_html(payload), S_BODY))
        elif kind == "code":
            story.extend(_code_flow(payload))
        elif kind == "table":
            story.extend(_table_flow(payload))
        elif kind == "image":
            story.extend(_image_flow(payload))
    return story

# --- Document template: TOC entries + outline bookmarks + header/footer ----
class ReportDoc(BaseDocTemplate):
    def __init__(self, filename, classification="", **kw):
        super().__init__(filename, **kw)
        self.classification = classification
        self.title_short = ""
        self._outline_seq = 0

    def beforeDocument(self):
        # multiBuild runs several passes on this same doc instance; without a
        # per-pass reset the outline keys differ every pass and the TOC never
        # converges ("Index entries not resolved after 10 passes").
        self._outline_seq = 0

    def afterFlowable(self, flowable):
        if not isinstance(flowable, Paragraph):
            return
        sn = flowable.style.name
        if sn == "SecHead":
            level = 0
        elif sn.startswith("FindHead"):
            level = 1
        else:
            return
        text = flowable.getPlainText()
        self._outline_seq += 1
        key = "sec-%d" % self._outline_seq
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text[:100], key, level=level, closed=(level == 1))
        self.notify("TOCEntry", (level, text, self.page, key))

def _cover_page(canvas, doc):
    canvas.saveState()
    canvas.setFont(BOLD_FONT, 9)
    canvas.setFillColor(ACCENT)
    canvas.drawCentredString(PAGE_W / 2, PAGE_H - 14 * mm, doc.classification)
    canvas.drawCentredString(PAGE_W / 2, 12 * mm, doc.classification)
    canvas.restoreState()

def _body_page(canvas, doc):
    canvas.saveState()
    canvas.setFont(CJK_FONT, 8)
    canvas.setFillColor(GREY)
    canvas.drawString(LM, PAGE_H - 12 * mm, doc.classification)
    canvas.drawRightString(PAGE_W - RM, PAGE_H - 12 * mm, doc.title_short)
    canvas.setStrokeColor(colors.HexColor("#BBBBBB"))
    canvas.setLineWidth(0.5)
    canvas.line(LM, PAGE_H - 14 * mm, PAGE_W - RM, PAGE_H - 14 * mm)
    canvas.line(LM, 14 * mm, PAGE_W - RM, 14 * mm)
    canvas.drawString(LM, 10 * mm, doc.classification)
    canvas.drawRightString(PAGE_W - RM, 10 * mm, "Page %d" % canvas.getPageNumber())
    canvas.restoreState()

# --- Main ------------------------------------------------------------------
title, cover, blocks = parse_md(md_text)
if len([b for b in blocks if b[0] == "h2"]) < 8:
    _warn("fewer than 8 ## sections - check report structure")

classification = CLASSIFICATION_OVERRIDE.strip()
if not classification:
    for k, v in cover:
        if re.search(r"classification|classified|密级", k, re.I):
            classification = v.strip()
            break
if not classification:
    classification = "CONFIDENTIAL"
    _warn("classification not found in cover metadata - defaulting to CONFIDENTIAL")

story = build_story(title, cover, blocks, classification)
out_dir = os.path.dirname(out_path)
if out_dir:
    os.makedirs(out_dir, exist_ok=True)

doc = ReportDoc(
    out_path, classification, pagesize=A4,
    leftMargin=LM, rightMargin=RM, topMargin=TM, bottomMargin=BM,
)
doc.title_short = title if len(title) <= 60 else title[:57] + "..."
frame_kw = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
doc.addPageTemplates([
    PageTemplate(id="cover", frames=[Frame(LM, BM, FRAME_W, PAGE_H - TM - BM, id="cover", **frame_kw)], onPage=_cover_page),
    PageTemplate(id="body", frames=[Frame(LM, BM, FRAME_W, PAGE_H - TM - BM, id="body", **frame_kw)], onPage=_body_page),
])
doc.multiBuild(story)

# --- pypdf verification (never deliver an unverified file) ------------------
from pypdf import PdfReader

reader = PdfReader(out_path)
n_pages = len(reader.pages)
assert n_pages > 0, "empty PDF"
extracted = "\n".join((p.extract_text() or "") for p in reader.pages)
required = [b[1] for b in blocks if b[0] == "h2"]
missing = [h for h in required if h not in extracted]
assert not missing, "section headings missing from extracted PDF text: %s" % missing
assert "p95" in extracted, "p95 token missing from extracted PDF text"
print("pages:", n_pages)
print("first page head:", (reader.pages[0].extract_text() or "")[:120].replace("\n", " / "))
print("pdf written:", out_path)
```

## Verification

The script self-verifies via pypdf before it prints `pdf written:`. On top of that, confirm from stdout:

1. `pages: > 0` and no `AssertionError`.
2. Every `##` section heading in the MD appears in the extracted PDF text, and the token `p95` is present (the script asserts both).
3. `Evidence figures:` images rendered - no `WARN: evidence missing` lines (a warning means the MD figure path is wrong; fix the MD and re-run).
4. If the reply must state a font limitation (weight contrast matters and no CJK TTF exists at runtime), note the CID no-bold caveat per `office/pdf` Rule 7.

## Delivery

1. `deliver_file` on the **MD** and again on the **PDF** - two explicit calls, both workspace `performance/` paths.
2. Quote both returned URLs verbatim; never rewrite them.
3. Chat reply: concise summary as in `performance/perf-report-writer.md` - SLA verdict, headline percentiles, top recommendation, and both file paths.
