---
slug: "generation/demo-maker"
description: "Generate single-page product demos as one self-contained HTML file (all CSS/JS inlined, images base64/inline SVG, system fonts, zero external requests, no runtime errors), styled after the project's own business system style — reuses an existing demo's style or delegates a QA style investigation; implementation is handed to @Tech Lead, runtime verification to @QA"
type: "guidance"
triggers:
  - "demo"
  - "演示页面"
  - "做个demo"
  - "做一个demo页面"
  - "做个演示"
  - "做一个演示"
  - "单页demo"
  - "html demo"
  - "原型页面"
  - "产品演示"
tools:
  - filesystem
  - knowledge_rw
  - user_confirm
  - github
  - git_repo
  - web_fetch
---

# Skill: Demo Maker

Turn a creative idea or requirement into a **single-page product demo**: one self-contained HTML file styled after the **project's own business system style**, delivered in-platform via `/api/media/` with zero runtime errors. Three roles collaborate — the Product Owner coordinates (clarify, obtain style baseline, produce spec, accept), **Tech Lead implements** (writes the single-file HTML), **QA investigates style and verifies runtime behavior**. Follow `workflows/demo-generation.workflow.md` for the collaboration sequence; this skill defines the technical contract.

Boundaries: presentations → `[[office/web-slides]]`; pure charts/dashboards → `[[analysis/dataviz]]`; office documents → `[[office/docx]]` / `[[office/xlsx]]` / `[[office/pptx]]`. This skill owns single-page interactive demo pages.

## Rule 1 - Self-Contained Single-File Contract

The deliverable is exactly one file, `demos/demo.html` (ASCII filename, < 5MB). This contract is the first line of defense against broken pages — no external requests means no 404s and no dependency drift.

- All CSS inlined in one `<style>`, all JS inlined in one `<script>`; images as base64 `data:` URIs or hand-written inline SVG.
- **Zero external requests at view time** — no `src="http`, `href="http`, `url(http`, `@import`, `<link`, `<iframe`, `<img src="http`, no Google Fonts, no icon CDNs. The file must render offline.
- System font stack only, with CJK fallback: `system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans SC", "Microsoft YaHei", sans-serif`.
- Interactions use native JS / pure CSS only (tabs, modals, counters, collapsibles, scroll highlighting). Page data is inline JSON example data, visibly labeled "演示数据 / demo data".
- Charts follow the chart-selection rules of `[[analysis/dataviz]]` as hand-written inline SVG, colored from the project style baseline (Rule 2); the dataviz fallback palette applies only when no baseline exists.
- **Never write large library code from memory** (Vue/React/ECharts and friends — from-memory reproduction silently corrupts and breaks the page). If a library is genuinely needed (rare): follow the `office/web-slides` Rule 2 pattern — `code_executor` downloads the bytes via urllib, splices them with `str.replace` placeholders, and asserts the bytes appear verbatim in the assembled file. If the fetch fails, ship the hand-written fallback — never a CDN-linked file, never from-memory library code.

## Rule 2 - Style Baseline: the Project's Business-System Style

The demo must follow the visual style of the **project's own business system** (palette, typography, component shapes) — never the platform's own UI. Three sources, in priority order:

- **Path A — existing demo**: list `demos/*.html` in the workspace; extract the style baseline from their `<style>` blocks — CSS variables, all hex/hsl/rgb colors, font families/sizes/weights, radius/shadow/spacing rhythm, component styles (buttons/cards/tables/nav/tabs). "Optimize on top of it" means keep the style language (palette, fonts, component shapes, layout skeleton) and improve content/structure/function to carry the new requirement; fix contract violations in the old demo as you go.
- **Path B — no demo**: (1) read the project's **CSS source files** first when reachable (git repo / github / web_fetch) — source variables are exact, prefer them over screenshots; (2) check `knowledge/ui-patterns.md` for the deposited **Visual Style Baseline** section; (3) otherwise delegate to QA per the workflow — QA reads CSS sources, extracts computed styles, screenshots with annotations, and returns a compact baseline list that is also deposited into `knowledge/ui-patterns.md` (marked `[verified]` when from the real system/computed styles, `[inferred]` when estimated) plus `history.md`.
- **Greenfield fallback** — no reachable business system: derive from `context.md`/design documents with the `[[analysis/dataviz]]` fallback palette, marked `[inferred]`.

## Rule 3 - Requirements Spec

Run one lightweight clarification round first (audience / core page or flow / data source or fabricated sample data / language). Then produce the spec: goal, page structure following the interaction skeleton in `knowledge/ui-patterns.md` (nav/list/detail patterns), sample data as inline JSON marked as demo data, the style baseline reference (Path A reuse or Path B baseline), and the contract reference — the implementing agent reads this skill itself, the spec never paraphrases Rule 1.

## Rule 4 - Verification (the "no errors" acceptance points)

Two layers; both must pass before delivery.

- **Static self-check** (implementer side, read-only): a `code_executor` python script asserts: (a) `<!DOCTYPE html>` present; (b) zero external-reference patterns (`src="http`, `href="http`, `url(http`, `@import`, `<link`, `<iframe`, `<img src="http`); (c) `<script>`/`</script>` and `<style>`/`</style>` counts balanced; (d) file size < 5MB; (e) ASCII filename. **The script must not write anything — above all not to OUTPUT_DIR** (every OUTPUT_DIR write auto-delivers another download link; re-running the generation script as "verification" turns one demo into many links — same rule as `office/web-slides` Rule 6).
- **Runtime verification** (QA side, delegated): Playwright loads `file://.../demos/demo.html` via an inline python-playwright script and asserts: no console error messages, no page errors, no failed requests, the primary interactive controls clickable with state changes, and a desktop-viewport screenshot confirms rendering. Screenshot goes to OUTPUT_DIR as auto-delivered evidence.

## Rule 5 - Delivery & Iteration

- Implementer delivers the `/api/media/` link (via the `code_executor` OUTPUT_DIR auto-delivery) plus the canonical file location `demos/demo.html`.
- PO's final summary: link, page outline, style source (Path A: "reused the existing demo's style"; Path B: "based on the QA style baseline + screenshots"), verification summary (static checks + QA Playwright results), and the demo-data declaration.
- Iteration is in-place: overwrite `demos/demo.html` and re-deliver; the workspace keeps exactly one canonical demo file, which is what Path A of Rule 2 reads for the next demo.
