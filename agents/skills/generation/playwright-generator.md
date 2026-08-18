---
slug: "generation/playwright-generator"
description: "Convert structured test cases into executable Playwright scripts via the test_generator tool"
---

# Skill: Playwright Generator

If `workflows/test-artifact-and-jira-conventions.workflow.md` is not loaded for the current task, read it first.

## Triggers

- The orchestration flow enters the `generate-spec` stage.
- A structured test design (test cases with title, steps, expected results) already exists.

## Generation

Call the `test_generator` tool — it generates executable Playwright specs directly:

```json
test_generator(
  operation="generate_spec",
  issue_key="PROJ-456",
  test_cases=[{"title": "...", "steps": ["..."], "expected_results": ["..."]}, ...]
)
```

The tool performs the following automatically:

1. Writes one spec file at `tests/generated/{slug}.spec.ts`, where `{slug}` is the issue key lowercased with non-alphanumeric characters replaced by `-` (e.g. `PROJ-456` → `proj-456.spec.ts`).
2. Creates the `tests/` scaffold if missing (`package.json` with `@playwright/test`, `playwright.config.ts`, `tsconfig.json`).
3. Adds a focused npm script `test:{slug}` to `tests/package.json` (e.g. `test:proj-456`).
4. Generates a **self-contained** spec: imports only `@playwright/test`, embeds a login helper (unless `include_login=false`), groups cases with `test.describe('{issue_key}')`, and renders each case as a `test(...)` block with step actions and assertions. There is no framework runtime wrapper — do not import anything from `src/orchestration` or call functions like `runGeneratedCase`; they do not exist.

## Generation Principles

1. Each test case in `test_cases` maps to one `test()` block; the filename comes from `issue_key` — no manual filename suffix.
2. Provide `steps` as natural-language actions ("navigate to ...", "click ...", "fill ...", "select ..."); the tool converts them into best-effort Playwright actions. Review the generated spec and tighten selectors where the heuristic falls short.
3. Provide `expected_results` as expectations ("... is visible", "URL contains ...", "text contains ...") so the tool can emit assertions.
4. Set `output_dir` only when the project convention differs from `tests/generated`.
5. If the product login flow is exercised inside each case instead, pass `include_login=false`.

## Selector Strategy (Reference Knowledge)

Read `knowledge/{project}/ui-patterns.md`. If it already records a commonly used selector pattern for the page, reuse it directly:

```typescript
// ui-patterns.md record: on the order page, the submit button uses the Chinese UI label `提交订单`
await page.getByRole('button', { name: '提交订单' }).click();
```

If knowledge does not contain the selector yet, generate a TODO comment:

```typescript
// TODO: add selector - refer to ui-patterns.md
await page.locator('[data-testid="???"]').click();
```

## Local Execution Command

Run from the project `tests/` directory, where the local `package.json`, `package-lock.json`, and `playwright.config.ts` are authoritative; the project must declare and install the local `@playwright/test` package, which generated specs import directly.

Command priority (use the first that applies):

1. `shell(command="npm run test:{slug}", cwd="tests")` — preferred; uses the focused script the generator wrote.
2. `shell(command="npm test -- generated/{slug}.spec.ts", cwd="tests")` — for any spec without a dedicated script.
3. `shell(command="npm run typecheck", cwd="tests")` — verify TypeScript before running.

Do **not** use bare `npx playwright test`: `playwright` (no `/test`) is a different package that lacks the `test` subcommand.

## Learning Feedback

If the user manually adjusts selectors or flow after generation, extract those edits and update `ui-patterns.md` so the process forms a positive feedback loop.
