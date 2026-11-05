---
slug: "demo-generation"
description:
  zh-CN: "Product Owner 工作流：生成单文件自包含 HTML 的产品演示页，优先参照项目上下文系统的现有页面，文件按 demo_<时间戳> 每次会话新建不覆盖——样式调研委派 QA、实现委派 Tech Lead、运行时验证委派 QA"
  en-US: "Product Owner workflow for generating a single-page product demo as one self-contained HTML file, prioritizing the existing pages of the project-context system with per-session demo_<timestamp> files that never overwrite prior demos — delegates style investigation to QA, implementation to Tech Lead, runtime verification to QA"
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
  - delegate_agent
  - list_agents
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

- **Path A — existing pages of the project-context system** (highest priority): the real pages of the system the project is about. (1) Structure: check `knowledge/page-map.md` / `knowledge/ui-patterns.md` for the interaction skeleton and an existing **Visual Style Baseline** section (previously deposited by QA). (2) Read the system's **CSS source files** first when the frontend source is reachable (via `git_repo`/`github`/`web_fetch`/`filesystem`) — the palette and variables extracted from source are exact, prefer them over screenshots. (3) Otherwise **`delegate_agent` the style investigation to `quality-assurance`** (one tool call, inside this turn — the result comes back to you and you keep working; do not end the turn asking the user to @-mention QA). QA reads CSS sources first, extracts computed styles (`getComputedStyle`) second, and screenshots with annotations last; QA returns a compact baseline list (palette with contrast hints, font stack/sizes/weights, button/card/table/nav/tab styles, layout, hover/active/focus states) and appends it to `knowledge/ui-patterns.md` under a **Visual Style Baseline** section (`[verified]` from real system/computed styles, `[inferred]` from estimation) plus `history.md`. The delegation result carries the baseline and the deposit confirmation.
- **Path B — existing demo in the workspace** (secondary): list `demos/demo_*.html` with `filesystem` and take the **newest timestamped file**; read its `<style>` block to extract the style baseline: CSS variables, color palette (hex/hsl/rgb), font stack and sizes, radius/shadow/spacing rhythm, component styles (buttons/cards/tables/nav/tabs). Copy from it — never rewrite the old file.
- **Path C — greenfield fallback**: no reachable business system at all: derive the baseline from `context.md`/design documents and use the `[[analysis/dataviz]]` fallback palette, marked `[inferred]`.

## 3. Produce the Demo Requirements Spec

Output a compact spec that carries everything the implementer needs without re-asking:

- Goal and audience.
- Page structure following the interaction skeleton in `knowledge/ui-patterns.md` (nav / list / detail patterns).
- Sample data as inline JSON, marked as demo data.
- Style baseline reference: Path A — cite the project system's page structure and Visual Style Baseline (or the QA baseline reply); Path B — "reuse the existing demo's style, improve content/structure only".
- The self-contained contract reference: "follow `agents/skills/generation/demo-maker.md` Rule 1" (Tech Lead reads the skill itself; do not paraphrase the contract).

## 4. Delegate implementation to Tech Lead

1. `delegate_agent` the spec to `tech-lead` with a brief that asks for: one self-contained HTML file at `demos/demo_<YYYYMMDD_HHMMSS>.html` — the timestamp fixed at session start (local time), a new file per session, never overwriting any earlier demo file — plus the `/api/media/` delivery link via the `code_executor` OUTPUT_DIR mechanism, followed by the read-only static self-check from the skill's Rule 4. The brief must be self-sufficient — the sub-agent sees the conversation, but state the deliverable and the acceptance criteria explicitly — and should ask for a **compact verdict line** (link + self-check result) rather than the file body: the result comes back as the sub-agent's reply, capped at 4000 characters.
2. Tech Lead reads `agents/skills/generation/demo-maker.md`, generates, self-checks, and the delegation result carries the delivery link and check results back to you.
3. **PO acceptance** — verify the delivered page against the spec: style matches the baseline, spec coverage is complete, single file with no external references. Reject (back to Tech Lead) or accept.

## 5. Delegate runtime verification to QA

1. `delegate_agent` the verification to `quality-assurance`: verify the session's `demos/demo_<timestamp>.html` with Playwright — load `file://.../demos/demo_<timestamp>.html` via an inline `code_executor` python-playwright script (no residue files — never `tests/generated/`, that is the Jira-card automation area); assert no console errors, no page errors, no failed requests; click the primary interactive controls and assert state changes; take a desktop-viewport screenshot written to OUTPUT_DIR (auto-delivered as evidence). Report pass/fail with concrete errors and screenshots.
2. **Fix loop** — on failure, `delegate_agent` the failure list back to `tech-lead` (update the session's own `demos/demo_<timestamp>.html`, re-run the self-check, re-deliver — never touch files from earlier sessions), then delegate re-verification to QA. At most two fix rounds; if still failing, escalate the issue list to the user. Every round is one more delegation inside the same turn — do not hand the turn back to the user between rounds.
3. On pass, proceed to delivery.

## 6. Deliver and close

1. PO summarizes to the user: the `/api/media/` link, the session file location `demos/demo_<timestamp>.html`, a page outline, the style source (Path A: "based on the project system's existing pages" / Path B: "reused the existing demo's style" / Path C: "inferred from design documents"), the verification summary (static checks + QA Playwright results), and the demo-data declaration.
2. Iteration within the session updates the session's own `demos/demo_<timestamp>.html` and re-delivers; a new demo request always creates a new timestamped file — previous demo files are never overwritten. Path B of stage 2 reads the newest `demos/demo_*.html` for the next demo.
