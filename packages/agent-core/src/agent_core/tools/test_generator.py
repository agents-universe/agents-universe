"""Test generator tool — generate Playwright .spec.ts files from structured test designs."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from agent_core.paths import PathEscapeError, resolve_within

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)


class TestGeneratorTool(Tool):
    name = "test_generator"
    prompt_hint = (
        "Turn structured test case designs into executable Playwright .spec.ts files "
        "under tests/generated/ — do not hand-write spec files when this is available."
    )
    description = (
        "Generate Playwright .spec.ts test files from structured test case designs. "
        "Creates executable test scripts in the project's tests/generated/ directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["generate_spec"],
            },
            "issue_key": {
                "type": "string",
                "description": "Jira issue key (used for file naming)",
            },
            "test_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "objective": {"type": "string"},
                        "preconditions": {"type": "array", "items": {"type": "string"}},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "expected_results": {"type": "array", "items": {"type": "string"}},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "steps"],
                },
                "description": "Array of test case definitions",
            },
            "output_dir": {
                "type": "string",
                "description": "Override output directory (relative to project root)",
            },
            "include_login": {
                "type": "boolean",
                "default": True,
                "description": "Include login helper in generated spec",
            },
            "base_url_env": {
                "type": "string",
                "default": "APP_BASE_URL",
                "description": "Env var name for the application base URL",
            },
        },
        "required": ["operation", "issue_key", "test_cases"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]
        if operation != "generate_spec":
            return {"error": f"Unknown operation: {operation}"}

        issue_key = params.get("issue_key", "")
        test_cases = params.get("test_cases", [])
        if not issue_key or not test_cases:
            return {"error": "issue_key and test_cases are required"}

        output_dir = params.get("output_dir", "tests/generated")
        try:
            out_path = resolve_within(context.project_fs_path, output_dir)
        except PathEscapeError:
            return {"error": f"Invalid output_dir: path escapes the project workspace: {output_dir!r}"}
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _log.warning("test_generator: failed to create output dir %s: %s", out_path, e)
            return {"error": f"Failed to create output directory: {e}"}

        # Ensure tests/ has package.json for Playwright execution.
        tests_root = Path(context.project_fs_path) / "tests"
        scaffold_error = _ensure_test_scaffold(tests_root)
        if scaffold_error:
            return {"error": scaffold_error}

        include_login = params.get("include_login", True)
        base_url_env = params.get("base_url_env", "APP_BASE_URL")

        slug = _slugify(issue_key)
        filename = f"{slug}.spec.ts"
        filepath = out_path / filename

        spec_content = _generate_spec(
            issue_key=issue_key,
            test_cases=test_cases,
            include_login=include_login,
            base_url_env=base_url_env,
        )

        if filepath.exists():
            return {"error": f"Spec file already exists and was not overwritten: {filepath}"}
        try:
            filepath.write_text(spec_content, encoding="utf-8")
        except OSError as e:
            _log.warning("test_generator: failed to write %s: %s", filepath, e)
            return {"error": f"Failed to write spec file: {e}"}

        script_error = _ensure_issue_script(tests_root, slug)
        if script_error:
            # The newly-created spec must not be reported as usable when its
            # npm entry point could not be configured.
            try:
                filepath.unlink()
            except OSError:
                _log.warning("test_generator: failed to clean up %s after script setup failure", filepath)
            return {"error": script_error}

        return {
            "success": True,
            "file_path": f"{output_dir}/{filename}",
            "relative_path": f"{output_dir}/{filename}",
            "test_count": len(test_cases),
            "issue_key": issue_key,
        }


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _generate_spec(issue_key: str, test_cases: list[dict], include_login: bool, base_url_env: str) -> str:
    lines = [
        "import { test, expect } from '@playwright/test';",
        "",
    ]

    if include_login:
        lines.extend([
            "async function login(page: any) {",
            f"  const baseUrl = process.env.{base_url_env} || 'http://localhost:3000';",
            "  const loginUrl = process.env.APP_LOGIN_URL || `${baseUrl}/login`;",
            "  await page.goto(loginUrl);",
            "  const username = process.env.APP_USERNAME || '';",
            "  const password = process.env.APP_PASSWORD || '';",
            "  if (username && password) {",
            "    await page.getByLabel(/user|email|account/i).fill(username);",
            "    await page.getByLabel(/pass/i).fill(password);",
            "    await page.getByRole('button', { name: /log|sign|submit/i }).click();",
            "    await page.waitForURL('**/dashboard**', { timeout: 15000 }).catch(() => {});",
            "  }",
            "}",
            "",
        ])

    lines.append(f"test.describe('{_escape_ts(issue_key)}', () => {{")

    if include_login:
        lines.extend([
            "  test.beforeEach(async ({ page }) => {",
            "    await login(page);",
            "  });",
            "",
        ])

    for i, tc in enumerate(test_cases):
        title = tc.get("title", f"Test case {i + 1}")
        objective = tc.get("objective", "")
        steps = tc.get("steps", [])
        expected = tc.get("expected_results", [])

        lines.append(f"  test('{_escape_ts(title)}', async ({{ page }}) => {{")

        if objective:
            lines.append(f"    // Objective: {_escape_ts(objective)}")
        lines.append("")

        for step in steps:
            action = _step_to_action(step)
            lines.append(f"    // Step: {_escape_ts(step)}")
            lines.append(f"    {action}")
            lines.append("")

        if expected:
            for exp in expected:
                assertion = _expected_to_assertion(exp)
                lines.append(f"    // Expected: {_escape_ts(exp)}")
                lines.append(f"    {assertion}")
            lines.append("")

        lines.append(f"    await page.screenshot({{ path: "
                     f"'test-results/{_slugify(issue_key)}-{i}.png' }});")
        lines.append("  });")
        lines.append("")

    lines.append("});")
    lines.append("")

    return "\n".join(lines)


def _escape_ts(s: str) -> str:
    # \r is a JS line terminator too (CRLF-sourced text) - a bare CR inside
    # a string literal breaks the generated .spec.ts at parse time.
    return s.replace("\r", " ").replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")


def _escape_ts_regex(s: str) -> str:
    """Escape for use inside a TS regex literal.

    A raw '/' inside a /.../ literal would terminate the regex and let
    agent-supplied text inject syntax; plain string escaping doesn't cover
    it. The remaining JS regex metacharacters (^ $ \ . * + ? ( ) [ ] { } |)
    must be escaped too, or agent-supplied text like "(submit)" or "[test]"
    silently changes the matched pattern — or, with an unclosed '[', breaks
    the generated .spec.ts at parse time.
    """
    return re.sub(r"[\^$\\\.\*\+\?\(\)\[\]\{\}\|/']", r"\\\g<0>", s.replace("\r", " ").replace("\n", " "))


def _step_to_action(step: str) -> str:
    """Convert a natural-language step into a best-effort Playwright action."""
    s = step.lower().strip()
    if s.startswith("navigate to ") or s.startswith("go to ") or s.startswith("open "):
        url_part = step.split(" ", 2)[-1].strip()
        return f"await page.goto('{_escape_ts(url_part)}');"
    if "click" in s:
        target = step.split("click", 1)[-1].strip().strip('"').strip("'")
        if not target:
            # A bare "click" step must not emit `name: //i` — the empty regex
            # becomes a line comment and breaks the generated .spec.ts syntax.
            return "await page.getByRole('button').first().click();"
        return f"await page.getByRole('button', {{ name: /{_escape_ts_regex(target)}/i }}).click();"
    if "fill" in s or "input" in s or "enter" in s or "type" in s:
        return f"await page.getByRole('textbox').fill('test-value'); // {_escape_ts(step)}"
    if "wait" in s:
        return f"await page.waitForTimeout(2000); // {_escape_ts(step)}"
    if "download" in s:
        target = step.split("download", 1)[-1].strip().strip('"').strip("'").strip(".")
        if target:
            return (
                f"const [download] = await Promise.all([\n"
                f"      page.waitForDownload(),\n"
                f"      page.getByRole('link', {{ name: /{_escape_ts_regex(target)}/i }}).click(),\n"
                f"    ]);\n"
                f"    await download.saveAs('test-results/{_escape_ts(step.split()[0])}-download' + download.suggested_filename());"
            )
        return f"// TODO: Download - use page.waitForDownload() before clicking the download trigger. // {_escape_ts(step)}"
    if "select" in s:
        return f"await page.getByRole('combobox').selectOption({{ index: 0 }}); // {_escape_ts(step)}"
    return f"await page.waitForTimeout(1000); // TODO: {_escape_ts(step)}"


def _expected_to_assertion(expected: str) -> str:
    """Convert an expected result into a best-effort Playwright assertion."""
    e = expected.lower().strip()
    if "visible" in e or "displayed" in e or "shown" in e or "appear" in e:
        text = expected.split("visible")[-1].strip().strip(":").strip()
        if not text:
            text = expected
        return f"await expect(page.getByText(/{_escape_ts_regex(text)}/i)).toBeVisible();"
    if "url" in e or "redirect" in e or "navigate" in e:
        return f"await expect(page).toHaveURL(/{_escape_ts_regex(expected)}/i);"
    if "contain" in e or "text" in e:
        return f"await expect(page.locator('body')).toContainText(/{_escape_ts_regex(expected)}/i);"
    return f"await expect(page.locator('body')).toContainText(/{_escape_ts_regex(expected)}/i);"


_SCAFFOLD_PACKAGE_JSON = """\
{
  "name": "project-tests",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "test": "playwright test",
    "test:ui": "playwright test --ui",
    "typecheck": "tsc --noEmit"
  },
  "devDependencies": {
    "@playwright/test": "^1.46.0",
    "typescript": "^5.4.0"
  }
}
"""

_SCAFFOLD_PLAYWRIGHT_CONFIG = """\
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './generated',
  timeout: 60_000,
  retries: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.APP_BASE_URL || 'http://localhost:3000',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
    acceptDownloads: true,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  outputDir: './test-results',
});
"""

_SCAFFOLD_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["**/*.ts"]
}
"""


def _ensure_test_scaffold(tests_root: Path) -> str | None:
    """Create missing scaffold files without replacing user-owned files."""
    try:
        tests_root.mkdir(parents=True, exist_ok=True)
        pkg = tests_root / "package.json"
        if not pkg.exists():
            _log.info("Creating test scaffold package.json at %s", pkg)
            pkg.write_text(_SCAFFOLD_PACKAGE_JSON, encoding="utf-8")
        config = tests_root / "playwright.config.ts"
        if not config.exists():
            config.write_text(_SCAFFOLD_PLAYWRIGHT_CONFIG, encoding="utf-8")
        tsconfig = tests_root / "tsconfig.json"
        if not tsconfig.exists():
            tsconfig.write_text(_SCAFFOLD_TSCONFIG, encoding="utf-8")
    except OSError as e:
        _log.warning("test_generator: failed to write scaffold in %s: %s", tests_root, e)
        return f"Failed to create test scaffold: {e}"
    return None


def _ensure_issue_script(tests_root: Path, slug: str) -> str | None:
    """Add a focused npm script while preserving existing package configuration."""
    pkg = tests_root / "package.json"
    try:
        import json
        data = json.loads(pkg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("package.json root must be a JSON object")
        scripts = data.setdefault("scripts", {})
        if not isinstance(scripts, dict):
            raise ValueError("package.json scripts must be an object")
        dev_dependencies = data.setdefault("devDependencies", {})
        if not isinstance(dev_dependencies, dict):
            raise ValueError("package.json devDependencies must be an object")
        changed = False
        required_scripts = {
            "test": "playwright test",
            "test:ui": "playwright test --ui",
            "typecheck": "tsc --noEmit",
            f"test:{slug}": f"playwright test generated/{slug}.spec.ts",
        }
        for name, command in required_scripts.items():
            # Always overwrite generator-managed issue scripts so a stale or
            # incorrect entry from a previous run never silently stays behind.
            if name.startswith("test:") and name not in ("test:ui",):
                if scripts.get(name) != command:
                    scripts[name] = command
                    changed = True
            elif name not in scripts:
                scripts[name] = command
                changed = True
        for name, version in (("@playwright/test", "^1.46.0"), ("typescript", "^5.4.0")):
            if name not in dev_dependencies:
                dev_dependencies[name] = version
                changed = True
        if changed:
            pkg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, AttributeError) as e:
        _log.warning("test_generator: failed to update npm script in %s: %s", pkg, e)
        return f"Failed to configure focused test script: {e}"
    return None
