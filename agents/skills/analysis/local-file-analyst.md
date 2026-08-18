---
slug: "analysis/local-file-analyst"
description: "Ingest local CSV/Excel/Parquet files into trustworthy pandas DataFrames via code_executor — encoding detection, sheet enumeration, sampling and chunked reads"
type: "guidance"
triggers:
  - "analyze this file"
  - "this CSV"
  - "this Excel"
  - "分析这个文件"
  - "这个表格"
  - "看看这份数据"
tools:
  - code_executor
  - filesystem
  - knowledge_rw
---

# Skill: Local File Analyst

Turn a local data file into a trustworthy pandas DataFrame. This skill owns **ingestion mechanics only** — once loaded, hand the data to `analysis/data-profiler` before any business analysis.

## Trigger Conditions

- The user uploads, drops, or points to a data file in the project workspace.
- A workflow step says "acquire" and the source is a file.

## Rule 1 — Locate and Identify

- `code_executor` runs with the project root as cwd; files are addressed by relative path (also available as `$PROJECT_DIR`).
- Record: path, extension, size (`os.path.getsize`). If the file is outside the project workspace, ask the user to place it there first.

## Rule 2 — Encoding Discipline (CSV/TSV)

Try decoders in a chain and **report which one succeeded** — never silently mis-decode:

```python
import pandas as pd
for enc in ("utf-8", "utf-8-sig", "gbk"):
    try:
        df = pd.read_csv(path, encoding=enc, nrows=1000)
        print("encoding:", enc)
        break
    except (UnicodeDecodeError, UnicodeError):
        continue
```

`gbk` covers most CJK Excel exports. If all fail, sample raw bytes and report.

## Rule 3 — Excel Files

Enumerate sheets first; read the requested sheet or, if ambiguous, ask:

```python
xl = pd.ExcelFile(path)
print(xl.sheet_names)
df = xl.parse(xl.sheet_names[0])
```

## Rule 4 — Parquet

`pd.read_parquet(path)` (pyarrow engine). For large files, read selected `columns=` only.

## Rule 5 — Sample-Then-Scale Within 30s

`code_executor` has a 30s budget, no network, and blocks `subprocess`/`os.system`:

- First pass: `nrows=1000` (CSV) or `.head(1000)` after a column-only read.
- Large files: `pd.read_csv(..., chunksize=100_000, dtype={...})` and aggregate inside the chunk loop; explicit `dtype` prevents type drift across chunks.
- If ingestion keeps timing out, ask the user for a smaller extract or pre-filter with `shell` text tools (`head`, `grep`).

## Rule 6 — Hand Off

After a successful load, immediately run `analysis/data-profiler` on the DataFrame. Do not answer business questions from un-profiled data.

## Output Requirements

Ingestion report: path, format, encoding (or sheet), rows × cols, dtypes, sample rows. If the file is a **recurring** source, append it to `[[technical/data-source-map]]` Source Inventory (Type = File) and note the change in `history.md`.
