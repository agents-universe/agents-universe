---
slug: "analysis/dataviz"
description: "Choose the right chart and render it — matplotlib PNGs via code_executor OUTPUT_DIR (auto-served at /api/media/), CJK-safe fonts, Mermaid via chart_renderer for conceptual diagrams"
type: "guidance"
triggers:
  - "chart"
  - "plot"
  - "visualize"
  - "dashboard"
  - "画个图"
  - "可视化"
  - "图表"
tools:
  - code_executor
  - chart_renderer
---

# Skill: Dataviz

This is the global `dataviz` skill referenced by `[[skills/analysis-patterns]]` Chart Selection Rules. It owns chart selection, rendering, and styling for all data graphics.

## Trigger Conditions

- Any chart / plot / graph / visualization / dashboard request.
- Invoked by `analysis/report-writer` and the analysis workflows whenever a figure is needed.

## Rule 1 — Chart Selection

Follow `[[skills/analysis-patterns]]` Chart Selection Rules:

- Trend over time → line chart
- Category comparison → bar chart
- Composition → stacked bar / pie (few categories only — never pie with > ~6 slices)
- Contribution decomposition → waterfall or ranked bar
- Distribution → histogram / box plot
- Relationship → scatter

Dual axes only with explicit justification. Always prefer the chart the audience can read in 5 seconds.

## Rule 2 — Render via code_executor

Data charts are matplotlib PNGs written to `$OUTPUT_DIR`; files there are auto-served at `/api/media/` and displayed in chat:

```python
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

out = os.environ["OUTPUT_DIR"]
# ... build figure from the DataFrame ...
fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(x, y)
plt.savefig(os.path.join(out, "output_0.png"), dpi=150, bbox_inches="tight")
```

Multiple figures → `output_0.png`, `output_1.png`, … The tool result returns the `/api/media/` URLs — include them in your reply.

## Rule 3 — CJK Font Handling

Set the fallback chain **before** plotting anything with Chinese labels:

```python
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False
```

If the runtime lacks all CJK fonts (tofu boxes), fall back to English labels and say so.

## Rule 4 — Styling Defaults

All charts share one system look:

- Categorical palette (fixed order): `#4C78A8`, `#F58518`, `#54A24B`, `#E45756`, `#72B7B2`, `#EECA3B`, `#B279A2`, `#9D755D`
- Light horizontal grid only (`ax.grid(axis="y", alpha=0.3)`); no chartjunk, no 3D.
- Always: title, axis labels **with units**, legend when > 1 series.
- Footnote: data source + data window (e.g. `Source: dw.ads_orders · 2026-07-01~2026-07-31`).

## Rule 5 — Mermaid for Concepts Only

ER diagrams, pipeline DAGs, flowcharts → `chart_renderer(code="<mermaid source>")`. It validates and returns a PNG only when the source parses. Never use Mermaid for numeric data. The PNG is shown in chat automatically - do not repeat the Mermaid source or embed image markdown in the reply.

## Output Requirements

Every chart ships with: title, labeled axes with units, legend (if multi-series), source/window footnote, and a one-line "how to read this" takeaway. Chart images are displayed to the user automatically - never embed `![](/api/media/...)` markdown or repeat Mermaid source in the reply.
