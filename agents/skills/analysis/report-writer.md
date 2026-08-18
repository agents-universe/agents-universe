---
slug: "analysis/report-writer"
description: "Produce conclusion-first Markdown analysis reports and self-contained HTML dashboards in the project workspace, and write results back to the analysis-scenarios knowledge log"
type: "guidance"
triggers:
  - "write a report"
  - "generate a dashboard"
  - "summarize the analysis"
  - "生成报告"
  - "分析报告"
  - "做个看板"
tools:
  - filesystem
  - code_executor
  - knowledge_rw
  - user_confirm
---

# Skill: Report Writer

The final step of every analysis. Owns both output contracts — the in-chat Markdown report and the self-contained HTML dashboard — plus the knowledge write-back loop.

## Trigger Conditions

- The last step of `ad-hoc-analysis`, `metric-deep-dive`, and `recurring-report` workflows.
- Explicit report / dashboard / summary requests.

## Rule 1 — Structure

Follow `[[skills/analysis-patterns]]` Report Structure:

- **Conclusion first**, then evidence.
- State metric definitions per `[[domain/metric-catalog]]`.
- Note data window, filters, and known caveats — carry the `analysis/data-profiler` caveat list forward verbatim.

## Rule 2 — Markdown Report Contract

Sections in this order:

1. **Conclusion** — the answer in 1–3 sentences, business language.
2. **Key Numbers** — a compact table (metric | value | Δ vs baseline | window).
3. **Evidence** — charts as `/api/media/` links (from `analysis/dataviz`) + key tables.
4. **Method & Definitions** — formulas cited from `[[domain/metric-catalog]]`, segment logic.
5. **Caveats & Data Window** — freshness, DQ issues, `[inferred]` items.
6. **Appendix** — the SQL / code behind the numbers.

## Rule 3 — HTML Dashboard Contract

- One **self-contained** `.html` file: inline CSS, charts embedded as base64 PNGs (generate via `analysis/dataviz`, then inline them), no external runtime dependencies.
- Layout: KPI tiles on top → charts → detail tables.
- Write via `filesystem(operation="write", path="reports/<topic>-<period>.html")` in the project workspace; stable, dated paths.

## Rule 4 — Audience Register

When the report corresponds to a row in `[[domain/analysis-scenarios]]` Recurring Reports & Dashboards, match its Audience and Output Format. Business language up front; technical detail only in the appendix.

## Rule 5 — Write-Back

After every finished analysis:

1. Append to `[[domain/analysis-scenarios]]` Ad-hoc / Thematic Analysis Log (Date | Topic | Question | Conclusion | Output Location).
2. Append to `history.md`.
3. Follow `agents/skills/knowledge/knowledge-manager.md` conventions (cross-links, concise entries).

## Output Requirements

- Chat: conclusion-first summary + artifact links + which knowledge files were updated.
- `[inferred]` items are marked visibly in chat and in the artifact.
- Never embed secrets, DSNs, or raw credentials in any artifact.
