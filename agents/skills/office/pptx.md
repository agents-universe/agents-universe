---
slug: "office/pptx"
description: "Generate and edit PowerPoint decks (.pptx) with python-pptx — 16:9 blank-layout slides, content-informed palettes, native charts, speaker notes, CJK-safe fonts; save to code_executor OUTPUT_DIR for auto-delivery as /api/media/ attachments"
type: "guidance"
triggers:
  - "生成ppt"
  - "做一份ppt"
  - "演示文稿"
  - "幻灯片"
  - "powerpoint"
  - "pitch deck"
tools:
  - code_executor
  - filesystem
  - user_confirm
---

# Skill: PPTX (PowerPoint)

Create and edit `.pptx` decks with `python-pptx` (preinstalled — import directly, never `pip install`). Ported from the official `anthropics/skills` pptx skill, adapted to the Python runtime.

## Trigger Conditions

- User asks for a PowerPoint / slide deck / presentation (生成ppt / 做一份ppt / 幻灯片 / 演示文稿).
- Editing an existing `.pptx` on disk (a file this conversation generated, or a workspace file).
- NOT for web-based slides — those go through `office/web-slides`.

## Rule 1 — Design Principles

**Don't create boring slides.** Plain bullets on white won't impress anyone.

- **Every slide needs a visual element** — shape, image, native chart, or big-number stat callout. Text-only slides are forgettable.
- **One point per slide.** Keep body lines ≤ 6 words where possible.
- **Pick a bold, content-informed palette**: one dominant color (60–70% visual weight), 1–2 supporting tones, one sharp accent. Never give all colors equal weight.
- **Dark/light contrast**: dark backgrounds for title + closing slides, light for content — or commit to dark throughout.
- **Commit to a visual motif** (rounded image frames, icons in colored circles) and carry it across every slide. Do not use a color bar as your motif.
- Start with title slide → agenda → content → closing. Vary layouts — don't put every section on the same title-and-bullets slide.

## Rule 2 — Layout Options

- Two-column (text left, illustration/chart right)
- Icon + text rows (icon in colored circle, bold header, description below)
- 2x2 grid (image on one side, grid of content blocks on other)
- Half-bleed image (full left or right side) with content overlay
- Big stat callouts (60–72pt numbers with small labels below)
- Comparison columns (before/after, pros/cons, side-by-side options)

## Rule 3 — python-pptx Gotchas

- **Can't duplicate slides** — `python-pptx` has no copy-slide API. Build each slide from `prs.slide_layouts[6]` (Blank) and add shapes explicitly.
- **`text_frame.text = "..."` collapses formatting** — it reduces the paragraph to a single unstyled run. Assign `run.text` per paragraph instead.
- **`add_picture` rejects SVG/EMF** (`UnidentifiedImageError`) — convert with Pillow first: `Image.open(src).convert("RGB").save(png, "PNG")`.
- **Speaker notes**: `slide.notes_slide.notes_text_frame.text = "..."` — never as a text box on the slide.
- **Keep charts native.** Use `shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, ...)` + `CategoriesChartData` — never screenshot a matplotlib figure into a slide. Only chart types PowerPoint has no native form for (Sankey, network) go in as images.
- **16:9 canvas**: `prs.slide_width = Inches(13.333)` and `prs.slide_height = Inches(7.5)` — the default is 10" × 7.5" (4:3).
- **Merged-cell / table cells**: write text into the cell's `text_frame` paragraphs like any text frame.

## Rule 4 — CJK Fonts (Required for Chinese Content)

Setting `run.font.name` alone leaves Chinese text as tofu boxes. Inject the `eastAsia` font domain on every run that may carry CJK text:

```python
from pptx.oxml.ns import qn

def set_cjk_font(run, name="Microsoft YaHei"):
    run.font.name = name  # latin typeface
    rPr = run._r.get_or_add_rPr()
    ea = rPr.makeelement(qn("a:ea"), {"typeface": name})  # east-asia typeface
    rPr.append(ea)
```

Fallbacks if YaHei is missing at render time: `Noto Sans CJK SC`. For embedded matplotlib charts set the CJK rcParams from `[[analysis/dataviz]]` Rule 3.

## Rule 5 — Output & Verify

- Save to `os.path.join(os.environ["OUTPUT_DIR"], "presentation.pptx")` — files there auto-deliver as `/api/media/` download links.
- **Verify before delivering**: reopen with `Presentation(path)`, assert slide count and that every slide has ≥ 1 shape; print a per-slide summary (slide N: title/speaker-notes) to stdout. Fix and re-run on any mismatch.
- Name files with ASCII names (`presentation.pptx`), never Chinese filenames.

## Output Requirements

Deliver the `/api/media/` link, a per-slide outline summary, data sources used, and any assumptions flagged `[inferred]`. State the palette and motif choices in one line so the user can ask for adjustments.
