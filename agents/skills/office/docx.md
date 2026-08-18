---
slug: "office/docx"
description: "Generate and edit Word documents (.docx) with python-docx — built-in heading hierarchy, styled paragraphs, tables, headers/footers, CJK-safe fonts; tracked changes not supported (v1)"
type: "guidance"
triggers:
  - "生成word"
  - "word文档"
  - "写一份文档"
  - "docx"
tools:
  - code_executor
  - filesystem
  - user_confirm
---

# Skill: DOCX (Word)

Create and edit `.docx` documents with `python-docx` (preinstalled — import directly, never `pip install`). Ported from the official `anthropics/skills` docx skill.

## Trigger Conditions

- User asks for a Word document / report / letter / doc (生成word / word文档 / 写一份文档 / docx).
- Editing an existing `.docx` on disk (a file this conversation generated, or a workspace file).

## Rule 1 — Structure with Built-in Headings

- One document title (`add_heading(title, 0)`), then `add_heading(..., 1|2|3)` for the hierarchy.
- **Never fake headings with bold runs** — the navigation pane, TOC, and outline depend on real heading styles.
- Keep paragraphs short; one idea per paragraph. Use `add_paragraph(text, style="List Bullet")` / `"List Number"` for lists.

## Rule 2 — Paragraphs & Runs

- `doc.add_paragraph("...", style=...)`; style runs individually (`run.bold = True`, `run.italic = True`).
- Set fonts on **runs**, not on the paragraph or document object.

## Rule 3 — CJK Fonts (Required for Chinese Content)

`run.font.name` alone leaves Chinese text as tofu boxes. Inject the `w:eastAsia` font domain:

```python
from docx.oxml.ns import qn

def set_cjk_font(run, latin="Microsoft YaHei", east="Microsoft YaHei"):
    run.font.name = latin                      # latin typeface
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.get_or_add_rFonts()
    rFonts.set(qn("w:eastAsia"), east)          # east-asia typeface
```

Body fallback: `SimSun`; headings: `Microsoft YaHei`. If the runtime lacks the font at render time, fall back to Noto Sans CJK SC.

## Rule 4 — Tables

- `doc.add_table(rows, cols)` then set `table.style = "Table Grid"` (or `"Light Grid Accent 1"`).
- Bold the header row; write cell text through each cell's paragraphs (`cell.paragraphs[0].add_run(...)`).

## Rule 5 — Page Setup

- A4 by default: `section.page_width = Cm(21)`, `section.page_height = Cm(29.7)`; set margins via `section.left_margin` / `right_margin` / `top_margin` / `bottom_margin`.
- Headers/footers via `section.header.paragraphs[0]` / `section.footer.paragraphs[0]` (page numbers: insert a PAGE field).

## Rule 6 — Editing Existing Documents

- Open with `Document(path)`, append or modify, save to a **new file** (`document-edited.docx`) — never overwrite the original; back it up first.
- **Tracked changes (修订) are NOT supported in v1.** When a user asks for redlining, say so plainly and deliver a clean edited copy instead.

## Rule 7 — Output & Verify

- Save to `os.path.join(os.environ["OUTPUT_DIR"], "document.docx")` — auto-delivered as a `/api/media/` download link.
- **Verify before delivering**: reopen and assert paragraph count, table count, and that the file is non-empty; print a heading outline (each `Heading 1/2/3` text) to stdout. Fix and re-run on any mismatch.
- ASCII filenames only (`document.docx`).

## Output Requirements

Deliver the `/api/media/` link, a section outline, data sources used, and assumptions flagged `[inferred]`. If the user asked for tracked changes, state the v1 limitation in the reply.
