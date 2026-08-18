---
slug: "demo-generation"
description: "Product Owner workflow for generating a single-page product demo as one self-contained HTML file, styled after the project's own business system style — delegates style investigation to QA, implementation to Tech Lead, runtime verification to QA"
agent: "project-owner"
triggers:
  - "demo"
  - "演示"
  - "演示页面"
  - "做个demo"
  - "做一个demo页面"
  - "单页demo"
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

# Demo Generation

Use this workflow when a demo / 演示页面 / prototype page is requested for a creative idea or requirement. The Product Owner coordinates; QA investigates style and verifies runtime behavior; Tech Lead implements the single-file HTML. Follow the stages in order.

## 1. Clarify the demo scope

Run one lightweight clarification round (per the agent's Requirements Clarification Methodology) before committing to a spec:

1. Audience — who watches this demo and what decision or impression should it drive?
2. Core page or flow — the single most important screen or user journey to show.
3. Data — real data source if available, otherwise fabricated sample data clearly labeled as demo data.
4. Language — the language of the page content (use the user's preferred language).

Only blocking ambiguities (audience or core flow) require questions; everything else defaults to a stated assumption.

## 2. Obtain the style baseline (project business-system style)

The demo must follow the visual style of the **project's own business system** — never the platform's own UI. Three paths, in priority order:

- **Path A — existing demo**: list `demos/*.html` in the project workspace with `filesystem`; read the `<style>` block of each to extract the style baseline: CSS variables, color palette (hex/hsl/rgb), font stack and sizes, radius/shadow/spacing rhythm, component styles (buttons/cards/tables/nav/tabs).
- **Path B — no demo**:
  1. Read the project's **CSS source files** first when the frontend source is reachable (via `git_repo`/`github`/`web_fetch`/`filesystem`) — the palette and variables extracted from source are exact, prefer them over screenshots.
  2. Otherwise check `knowledge/ui-patterns.md` for an existing **Visual Style Baseline** section (previously deposited by QA).
  3. Otherwise **delegate to QA**: end the reply with a request to `@quality-assurance` to investigate the project business system's visual style. QA reads CSS sources first, extracts computed styles (`getComputedStyle`) second, and screenshots with annotations last; QA returns a compact baseline list (palette with contrast hints, font stack/sizes/weights, button/card/table/nav/tab styles, layout, hover/active/focus states) and appends it to `knowledge/ui-patterns.md` under a **Visual Style Baseline** section (`[verified]` from real system/computed styles, `[inferred]` from estimation) plus `history.md`. This is the QA→PO hand-off: the baseline and the deposit confirmation.
- **Greenfield fallback** — no reachable business system at all: derive the baseline from `context.md`/design documents and use the `[[analysis/dataviz]]` fallback palette, marked `[inferred]`.

## 3. Produce the Demo Requirements Spec

Output a compact spec that carries everything the implementer needs without re-asking:

- Goal and audience.
- Page structure following the interaction skeleton in `knowledge/ui-patterns.md` (nav / list / detail patterns).
- Sample data as inline JSON, marked as demo data.
- Style baseline reference: Path A — "reuse the existing demo's style, improve content/structure only"; Path B — cite the Visual Style Baseline section (or the QA baseline reply).
- The self-contained contract reference: "follow `agents/skills/generation/demo-maker.md` Rule 1" (Tech Lead reads the skill itself; do not paraphrase the contract).

## 4. Delegate implementation to Tech Lead

1. End the reply with a request to `@Tech Lead` to implement the spec: one self-contained HTML file at `demos/demo.html` (canonical workspace copy) plus the `/api/media/` delivery link via the `code_executor` OUTPUT_DIR mechanism, followed by the read-only static self-check from the skill's Rule 4.
2. Tech Lead reads `agents/skills/generation/demo-maker.md`, generates, self-checks, and replies with the delivery link and check results.
3. **PO acceptance** — verify the delivered page against the spec: style matches the baseline, spec coverage is complete, single file with no external references. Reject (back to Tech Lead) or accept.

## 5. Delegate runtime verification to QA

1. End the reply with a request to `@quality-assurance` to verify `demos/demo.html` with Playwright: load `file://.../demos/demo.html` via an inline `code_executor` python-playwright script (no residue files — never `tests/generated/`, that is the Jira-card automation area); assert no console errors, no page errors, no failed requests; click the primary interactive controls and assert state changes; take a desktop-viewport screenshot written to OUTPUT_DIR (auto-delivered as evidence). Report pass/fail with concrete errors and screenshots.
2. **Fix loop** — on failure, hand the failure list back to Tech Lead (overwrite `demos/demo.html` in place, re-run the self-check, re-deliver), then QA re-verifies. At most two fix rounds; if still failing, escalate the issue list to the user.
3. On pass, proceed to delivery.

## 6. Deliver and close

1. PO summarizes to the user: the `/api/media/` link, a page outline, the style source (Path A: "reused the existing demo's style" / Path B: "based on the QA style baseline + screenshots"), the verification summary (static checks + QA Playwright results), and the demo-data declaration.
2. Iteration is in-place: overwrite `demos/demo.html` and re-deliver; the workspace always keeps exactly one canonical demo file. The canonical file is what Path A of stage 2 reads for the next demo.
