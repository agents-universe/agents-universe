---
slug: "office/xlsx"
description: "Generate and edit Excel workbooks (.xlsx) with openpyxl — formulas over hardcoded results, number formats, financial-model color conventions, native charts, static formula verification (no LibreOffice in the container); for creating/exporting/editing workbooks — not for reading files into analysis (that is analysis/local-file-analyst)"
type: "guidance"
triggers:
  - "生成excel"
  - "导出excel"
  - "编辑excel"
  - "excel文件"
  - "workbook"
tools:
  - code_executor
  - filesystem
  - user_confirm
---

# Skill: XLSX (Excel)

Create and edit `.xlsx` workbooks with `openpyxl` (preinstalled — import directly, never `pip install`). Ported from the official `anthropics/skills` xlsx skill, adapted for a container without LibreOffice.

## Trigger Conditions

- User asks to **create / export / edit** a spreadsheet or workbook (生成excel / 导出excel / 编辑excel / excel文件 / workbook).
- Data analyst flow: exporting analysis results as a formatted `.xlsx` deliverable.
- NOT for reading a file into an analysis — that is `analysis/local-file-analyst`.

## Rule 1 — openpyxl Gotchas & Formula-First

- **Use formulas, never hardcoded results.** Write `ws["B10"] = "=SUM(B2:B9)"`, not the Python-computed total — the sheet must recalculate when inputs change.
- **`load_workbook(data_only=True)` is destructive if you save** — that workbook has no formulas left, and saving replaces every one with a literal. Load with `data_only=False` (default) when you intend to save; use the `data_only=True` pass only to *read* cached values, and never save from it.
- **Reading a model takes two loads**: `data_only=True` yields cached values with formulas gone; the default yields formula strings with no values. One pass cannot give you both.
- **Merged cells**: write the top-left anchor only; every other cell in the range is a read-only `MergedCell`.
- **`.xlsm` loses macros** unless you pass `keep_vba=True` to `load_workbook`.
- **Editing an existing file: match its conventions exactly** — find its designated input cells (distinct font/fill marks them), write only there, leave every existing formula untouched.

## Rule 2 — Formula Verification (no LibreOffice)

The container has no LibreOffice, so there is no recalc pass. Verify formulas two ways before delivering:

**A. Static check (mandatory)** — reopen `data_only=False`, scan every formula cell for unbalanced parens, illegal references, and functions outside the whitelist; exit 1 with the list on any failure:

```python
import os, re
from openpyxl import load_workbook

ALLOWED = {"SUM","AVERAGE","COUNT","COUNTA","MIN","MAX","ROUND","ROUNDUP",
           "ROUNDDOWN","IF","IFERROR","SUMIF","SUMIFS","COUNTIF","COUNTIFS",
           "VLOOKUP","XLOOKUP","INDEX","MATCH","CONCAT","CONCATENATE","TEXTJOIN",
           "TRIM","LEFT","RIGHT","MID","LEN","SUBSTITUTE","TEXT","INT","MOD",
           "ABS","POWER","SQRT","PRODUCT","SUMPRODUCT","TODAY","NOW"}

errors = []
wb = load_workbook(os.path.join(os.environ["OUTPUT_DIR"], "workbook.xlsx"), data_only=False)
for ws in wb.worksheets:
    for row in ws.iter_rows():
        for cell in row:
            v = cell.value
            if isinstance(v, str) and v.startswith("="):
                if v.count("(") != v.count(")"):
                    errors.append(f"{ws.title}!{cell.coordinate}: unbalanced parens")
                for fn in re.findall(r"([A-Z][A-Z0-9.]*)\s*\(", v):
                    if fn not in ALLOWED:
                        errors.append(f"{ws.title}!{cell.coordinate}: unsupported function {fn}")
if errors:
    print("\n".join(errors)); raise SystemExit(1)
```

**B. Cross-check (recommended)** — keep the source data structures in the script; compute expected values for the key SUM/AVERAGE formulas in Python, compare against what the formula cells *would* produce, and print the comparison to stdout. **Never write results back into the sheet** (violates Rule 1). An off-by-one range yields a clean, error-free file with wrong numbers — the cross-check is what catches it.

**C. Delivery note** — formulas have no cached values until Excel/WPS recalculates on open. Say so in the reply: "打开后公式自动重算". If the user needs values visible immediately, deliver the formula version plus a one-page aggregate summary sheet.

## Rule 3 — Financial-Model Conventions

Unless the user says otherwise (or the file already does something else):

- **Colors**: blue text (`0000FF`) = hardcoded inputs and scenario levers · black = formulas · green (`008000`) = links to another sheet · yellow fill (`FFFF00`) = key assumptions and cells the user should fill in.
- **Numbers**: currency `$#,##0` with the unit in the header (`Revenue ($mm)`) · zeros render as `-` (`$#,##0;($#,##0);-`) · negatives in parentheses · percentages `0.0%` **stored as fractions** (`0.15` renders `15.0%`; storing `15` renders `1500.0%`) · years as text (`"2024"`, never `2,024`).
- **Structure**: every assumption in its own labeled cell, referenced by the formulas that use it (`=B5*(1+$B$6)`, never `=B5*1.05`); formulas consistent across every projection period; guard denominators that can be zero.

## Rule 4 — Layout

- Bold header row + `ws.freeze_panes = "A2"` + sane column widths (`ws.column_dimensions["A"].width = 12`).
- Document every assumption and hardcoded number where the reader will see it — a cell `Comment`, or an adjacent cell at a table's end. Cite a real source when one exists; when the number came from the user, say so plainly.
- A workbook **you create for someone to fill in** needs a short legend naming which cells to edit, and one example row of realistic values. Never add such a row to a file you were asked to edit.

## Rule 5 — Native Charts

`openpyxl` charts (`BarChart` / `LineChart` / `PieChart` from `openpyxl.chart`) — never matplotlib screenshots. Pie charts only with ≤ 6 slices. Reference data ranges by cell (`=Sheet1!$A$1:$B$10`).

## Rule 6 — Output & Verify

- Save to `os.path.join(os.environ["OUTPUT_DIR"], "workbook.xlsx")` — auto-delivered as a `/api/media/` download link.
- **Verify before delivering**: reopen and assert the sheet-name list, each sheet's row/column counts, and that formula cells exist and are non-empty. Run Rule 2 static check. Fix and re-run on any mismatch.
- ASCII filenames only (`workbook.xlsx`).

## Output Requirements

Deliver the `/api/media/` link, a per-sheet content summary, the formula/data-source notes, and the "opens and recalculates in Excel/WPS" note. Flag every assumption `[inferred]` when it came from the model rather than the user or project knowledge.
