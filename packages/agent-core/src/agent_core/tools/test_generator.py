"""Test generator tool — generate Playwright .spec.ts files from structured test designs."""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.paths import PathEscapeError, resolve_within

from .base import Tool, ToolContext
from ._uploads import normalize_mime_type

_log = logging.getLogger(__name__)

# Generated specs embed upload payloads as base64. The caps keep a spec
# reviewable and the tool-call argument inside model limits; larger assets
# belong in tests/fixtures/ (referenced by path, not inlined).
_MAX_INLINE_UPLOAD_BYTES = 512 * 1024
_MAX_UPLOADS_PER_CASE = 5
_MAX_SPEC_BYTES = 2 * 1024 * 1024

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]*$")
# Words that mark a step as an upload. ASCII keywords need \b so "important"
# or "attachment-free" prose does not turn into a file upload; the CJK terms
# have no word boundaries and are matched as substrings.
_UPLOAD_WORD_RE = re.compile(
    r"\b(upload\w*|attach\w*|import(?:s|ed|ing)?|choose file|select file)\b"
)
_UPLOAD_CJK = ("上传", "导入", "附件", "选择文件")
# Steps that only talk ABOUT an upload (waiting for it, asserting it landed)
# are not upload actions; without this, "Verify the attachment uploaded" emits
# a setInputFiles call. "import" needs the same care for "important".
_STEP_VERBS = ("wait", "verify", "check", "validate", "assert", "expect", "confirm", "ensure", "waiting")
_STEP_VERBS_CJK = ("等待", "验证", "检查", "确认")
# A token that looks like a filename, used to name the fixture a TODO step
# still needs ("Upload orders.csv" → orders.csv).
_FILENAME_RE = re.compile(
    r"[A-Za-z0-9_\-.]+\.(?:csv|xlsx?|json|txt|pdf|png|jpe?g|xml|zip|docx?)[a-z0-9]*\b",
    re.IGNORECASE,
)


class TestGeneratorTool(Tool):
    name = "test_generator"
    prompt_hint = (
        "Turn structured test case designs into executable Playwright .spec.ts files "
        "under tests/generated/ — do not hand-write spec files when this is available."
    )
    description = (
        "Generate Playwright .spec.ts test files from structured test case designs. "
        "Creates executable test scripts in the project's tests/generated/ directory. "
        "File-upload steps get real setInputFiles calls: declare each payload under "
        "the case's uploads array (inline content, or fixture_path into tests/fixtures/)."
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
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "One actionable UI action per entry — navigate / click / "
                                "fill / select / wait / upload / download. Checks belong in "
                                "expected_results: a step about what the page SHOWS cannot be "
                                "automated and is emitted as a TODO instead."
                            ),
                        },
                        "expected_results": {"type": "array", "items": {"type": "string"}},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "uploads": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filename": {
                                        "type": "string",
                                        "description": "Filename the app will see (e.g. orders.csv)",
                                    },
                                    "mime_type": {"type": "string"},
                                    "content": {
                                        "type": "string",
                                        "description": "Inline file content — the test generates its own payload",
                                    },
                                    "content_base64": {
                                        "type": "string",
                                        "description": "Inline binary payload, base64-encoded",
                                    },
                                    "fixture_path": {
                                        "type": "string",
                                        "description": "Path (relative to tests/) of an existing asset, e.g. fixtures/logo.png. Use instead of inlining large files.",
                                    },
                                    "selector": {
                                        "type": "string",
                                        "description": "File input selector (default 'input[type=file]'), or the button that opens the picker when via_chooser is true",
                                    },
                                    "via_chooser": {
                                        "type": "boolean",
                                        "description": "True when the page has no visible input and a button opens the native file chooser",
                                    },
                                    "needs_path": {
                                        "type": "boolean",
                                        "description": "True when a real filesystem path is required — the test writes the payload to its output dir first",
                                    },
                                },
                                "required": ["filename"],
                            },
                            "description": "Files this case uploads; consumed in order by upload steps",
                        },
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
                "description": (
                    "Whether cases sign in. Default true. Where the scaffold already "
                    "signs in once per run the cases inherit that session and only the "
                    "ones that exercise signing in sign in again; false means these "
                    "cases need no session at all. Cases that switch user or check "
                    "permissions always get a signed-out start."
                ),
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
        # The schema declares an array of objects but LLMs sometimes stringify
        # it — a bare string would iterate character-by-character in
        # _generate_spec and crash on .get(). Reject instead of writing a
        # garbage spec.
        if isinstance(test_cases, str) or (
            isinstance(test_cases, list) and any(not isinstance(tc, dict) for tc in test_cases)
        ):
            return {
                "error": (
                    "test_cases must be an array of objects with title/steps/"
                    "expected_results — check the tool's parameter schema."
                )
            }

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
        scaffold = _ensure_test_scaffold(tests_root)
        if scaffold.error:
            return {"error": scaffold.error}

        include_login = params.get("include_login", True)
        base_url_env = params.get("base_url_env", "APP_BASE_URL")

        case_uploads: list[list[_Upload]] = []
        for i, tc in enumerate(test_cases):
            uploads, error = _prepare_case_uploads(tc.get("uploads"), i)
            if error:
                return {"error": error}
            case_uploads.append(uploads)

        slug = _slugify(issue_key)
        filename = f"{slug}.spec.ts"
        filepath = out_path / filename

        spec_content = _generate_spec(
            issue_key=issue_key,
            test_cases=test_cases,
            include_login=include_login,
            base_url_env=base_url_env,
            case_uploads=case_uploads,
            shared_session=scaffold.shared_session,
        )
        spec_bytes = len(spec_content.encode("utf-8"))
        if spec_bytes > _MAX_SPEC_BYTES:
            return {
                "error": (
                    f"Generated spec would be {spec_bytes} bytes, above the "
                    f"{_MAX_SPEC_BYTES // (1024 * 1024)}MB limit — move the upload "
                    "payloads to tests/fixtures/ and reference them with fixture_path."
                )
            }

        if filepath.exists():
            # The hint is here because the two facts meet at this line: a spec
            # generated before the workspace had the setup project still signs
            # in per case, and under the upgraded config that sign-in now races
            # with the session the scaffold hands out. Regenerating fixes it.
            return {
                "error": f"Spec file already exists and was not overwritten: {filepath}",
                "hint": (
                    "Delete it and generate again to pick up the current scaffold "
                    "(one sign-in per run) — a spec written before the workspace was "
                    "upgraded still signs in inside every case."
                ),
            }
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

        result = {
            "success": True,
            "file_path": f"{output_dir}/{filename}",
            "relative_path": f"{output_dir}/{filename}",
            "test_count": len(test_cases),
            "issue_key": issue_key,
        }
        if scaffold.notes:
            # Worth surfacing exactly when something moved under the caller: the
            # spec shape follows the config version, so a workspace that did not
            # upgrade silently keeps producing the slower specs.
            result["scaffold_updates"] = list(scaffold.notes)
        return result


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass(frozen=True)
class _Upload:
    """One file a generated test case uploads.

    Everything is either an inlined base64 payload or a path under ``tests/``;
    the generated spec runs in Node with no access to the agent's workspace,
    so the two forms are the only ones that survive the trip.
    """

    filename: str
    mime_type: str
    selector: str = "input[type=file]"
    via_chooser: bool = False
    needs_path: bool = False
    content_b64: str | None = None
    fixture_path: str | None = None


def _prepare_case_uploads(raw: Any, index: int) -> tuple[list[_Upload], str | None]:
    """Validate a case's ``uploads`` array into _Upload entries."""
    if raw is None:
        return [], None
    if isinstance(raw, str) or not isinstance(raw, list):
        return [], f"test_cases[{index}].uploads must be an array of objects"
    if len(raw) > _MAX_UPLOADS_PER_CASE:
        return [], (
            f"test_cases[{index}].uploads has {len(raw)} entries, above the "
            f"{_MAX_UPLOADS_PER_CASE} per-case limit"
        )

    uploads: list[_Upload] = []
    for position, entry in enumerate(raw):
        where = f"test_cases[{index}].uploads[{position}]"
        if not isinstance(entry, dict):
            return [], f"{where} must be an object"
        filename = _clean_upload_name(entry.get("filename"))
        if not filename:
            return [], f"{where}.filename is required"

        fixture_path = entry.get("fixture_path")
        content = entry.get("content")
        content_b64 = entry.get("content_base64")
        sources = [v is not None for v in (fixture_path, content, content_b64)]
        if sum(sources) != 1:
            return [], (
                f"{where} needs exactly one of fixture_path, content or "
                "content_base64"
            )

        if fixture_path is not None:
            fixture = str(fixture_path).replace("\\", "/").strip().lstrip("/")
            if not fixture or ".." in fixture.split("/"):
                return [], f"{where}.fixture_path must stay under tests/"
            uploads.append(
                _Upload(
                    filename=filename,
                    mime_type=normalize_mime_type(entry.get("mime_type"), filename),
                    selector=_upload_selector(entry),
                    via_chooser=bool(entry.get("via_chooser")),
                    needs_path=bool(entry.get("needs_path")),
                    fixture_path=fixture,
                )
            )
            continue

        if content_b64 is not None:
            if not isinstance(content_b64, str) or not _BASE64_RE.match(content_b64):
                return [], f"{where}.content_base64 is not valid base64"
            try:
                data = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError):
                # Never echo the payload — it is caller content, and a decode
                # error message must stay value-free either way.
                return [], f"{where}.content_base64 is not valid base64"
        else:
            data = str(content).encode("utf-8")
        if len(data) > _MAX_INLINE_UPLOAD_BYTES:
            return [], (
                f"{where} is {len(data)} bytes, above the "
                f"{_MAX_INLINE_UPLOAD_BYTES // 1024}KB inline limit — put the file "
                "under tests/fixtures/ and use fixture_path"
            )

        uploads.append(
            _Upload(
                filename=filename,
                mime_type=normalize_mime_type(
                    entry.get("mime_type"), filename, text_default=content_b64 is None
                ),
                selector=_upload_selector(entry),
                via_chooser=bool(entry.get("via_chooser")),
                needs_path=bool(entry.get("needs_path")),
                content_b64=base64.b64encode(data).decode("ascii"),
            )
        )
    return uploads, None


def _upload_selector(entry: dict) -> str:
    selector = entry.get("selector")
    if isinstance(selector, str) and selector.strip():
        return selector.strip()
    return "button:has-text('Upload')" if entry.get("via_chooser") else "input[type=file]"


def _clean_upload_name(raw: Any) -> str:
    """Filename for the generated spec — no path decoration, no line breaks."""
    text = str(raw or "").replace("\\", "/")
    return re.sub(r"[\x00-\x1f\x7f]", "", text.rsplit("/", 1)[-1]).strip()[:255]


# Wording that means the case is about the session itself: signing in or out,
# acting as someone else, or lacking rights. These cases must not inherit the
# run's shared session — that is the very thing they exercise. Matching in the
# title/steps rather than a caller-declared flag is a deliberate bias toward
# over-applying: a case that did not need a fresh session only pays a login it
# would have paid before, while one that did need it would otherwise pass
# without ever reaching the login form.
_SESSION_STEP_RE = re.compile(
    r"log ?in|log ?out|sign ?in|sign ?out|switch (?:user|account|role|tenant)|"
    r"permission|unauthori[sz]ed|without a valid account|"
    r"登录|登陆|退出登录|切换用户|切换账号|切换角色|权限|未授权",
    re.IGNORECASE,
)


def _needs_fresh_session(tc: dict) -> bool:
    """Whether *tc* has to start from a signed-out browser."""
    parts = [str(tc.get("title", "")), str(tc.get("objective", ""))]
    for key in ("steps", "expected_results"):
        value = tc.get(key) or []
        # A stringified array would otherwise iterate character by character.
        parts.extend([value] if isinstance(value, str) else [str(v) for v in value])
    return any(_SESSION_STEP_RE.search(part) for part in parts)


def _generate_spec(
    issue_key: str,
    test_cases: list[dict],
    include_login: bool,
    base_url_env: str,
    case_uploads: list[list[_Upload]] | None = None,
    shared_session: bool = False,
) -> str:
    lines: list[str] = []
    # Ambient declarations the emitted snippets rely on (Buffer/require are
    # Node globals the scaffold's tsconfig does not load types for; declaring
    # them here keeps `npm run typecheck` green without an @types/node dep).
    declarations: set[str] = set()

    # The scaffold's setup project signs in once per run and every case
    # inherits that session, so only a case that signs in by itself pays for a
    # login. `include_login` keeps its meaning on top of that: skipping it means
    # "don't sign in for these cases at all", which has to be said out loud once
    # a project-level storageState exists.
    fresh = {i for i, tc in enumerate(test_cases) if _needs_fresh_session(tc)}
    # `include_login: false` applies to the file; a session-relevant case opts
    # out on its own describe instead, so the two never both need saying.
    file_opted_out = shared_session and not include_login

    if file_opted_out:
        lines.extend([
            "// Cases that sign in themselves, or that run before any sign-in.",
            "test.use({ storageState: { cookies: [], origins: [] } });",
            "",
        ])

    if include_login and (not shared_session or fresh):
        lines.extend([
            "async function login(page: any) {",
            f"  const baseUrl = process.env.{base_url_env} || 'http://localhost:3000';",
            "  const loginUrl = process.env.APP_LOGIN_URL || `${baseUrl}/login`;",
            "  const username = process.env.APP_USERNAME || '';",
            "  const password = process.env.APP_PASSWORD || '';",
            "  if (!username || !password) {",
            "    test.skip(true, 'APP_USERNAME/APP_PASSWORD not set - this case needs a session');",
            "    return;",
            "  }",
            "  await page.goto(loginUrl, { waitUntil: 'domcontentloaded' });",
            "  await page.getByLabel(/user|email|account/i).fill(username);",
            "  await page.getByLabel(/pass/i).fill(password);",
            "  await page.getByRole('button', { name: /log|sign|submit/i }).click();",
            "  // No .catch() on the wait: a sign-in that quietly gave up left the case",
            "  // running unauthenticated, so its failure showed up at some unrelated",
            "  // assertion instead of here. 'Left the login page' is the check because",
            "  // the post-login route differs per application.",
            "  await page.waitForURL((url: any) => !/\\/login\\b/i.test(url.pathname), { timeout: 30000 });",
            "}",
            "",
        ])

    regular = [(i, tc) for i, tc in enumerate(test_cases) if i not in fresh]
    fresh_cases = [(i, tc) for i, tc in enumerate(test_cases) if i in fresh]

    if regular:
        lines.append(f"test.describe('{_escape_ts(issue_key)}', () => {{")
        if include_login and not shared_session:
            lines.extend([
                "  test.beforeEach(async ({ page }) => {",
                "    await login(page);",
                "  });",
                "",
            ])
        for i, tc in regular:
            lines.extend(_case_lines(issue_key, i, tc, case_uploads, declarations))
        lines.append("});")
        lines.append("")

    if fresh_cases:
        lines.append(
            f"test.describe('{_escape_ts(issue_key)} - session cases', () => {{"
        )
        if shared_session and not file_opted_out:
            # These cases exist to exercise signing in or a different role, so
            # inheriting the shared session would let them pass without ever
            # reaching the login form.
            lines.append("  test.use({ storageState: { cookies: [], origins: [] } });")
        if include_login:
            lines.extend([
                "  test.beforeEach(async ({ page }) => {",
                "    await login(page);",
                "  });",
            ])
            lines.append("")
        for i, tc in fresh_cases:
            lines.extend(_case_lines(issue_key, i, tc, case_uploads, declarations))
        lines.append("});")
        lines.append("")

    header = ["import { test, expect } from '@playwright/test';", ""]
    if declarations:
        header.extend(f"declare const {name}: any;" for name in sorted(declarations))
        header.append("")
    return "\n".join(header + lines)


def _case_lines(
    issue_key: str,
    index: int,
    tc: dict,
    case_uploads: list[list[_Upload]] | None,
    declarations: set[str],
) -> list[str]:
    """One `test(...)` block. Shared by the plain and session-case describes."""
    lines: list[str] = []
    title = tc.get("title", f"Test case {index + 1}")
    objective = tc.get("objective", "")
    steps = tc.get("steps", [])
    expected = tc.get("expected_results", [])
    uploads = list(case_uploads[index]) if case_uploads and index < len(case_uploads) else []

    lines.append(f"  test('{_escape_ts(title)}', async ({{ page }}) => {{")

    if objective:
        lines.append(f"    // Objective: {_escape_ts(objective)}")
        lines.append("")

    for step in steps:
        upload = uploads.pop(0) if uploads and _classify_step(step) == "upload" else None
        action = _step_to_action(step, upload=upload, decls=declarations)
        lines.append(f"    // Step: {_escape_ts(step)}")
        lines.append(f"    {action}")
        lines.append("")

    if uploads:
        # Payloads nothing in the step list claimed — emitting them beats
        # dropping them, but the mismatch is worth a comment: the test
        # still uploads, just not at the step the author pictured.
        for upload in uploads:
            action = _step_to_action(f"upload {upload.filename}", upload=upload, decls=declarations)
            lines.append(f"    // Unmatched upload: {_escape_ts(upload.filename)}")
            lines.append(f"    {action}")
            lines.append("")

    if expected:
        for exp in expected:
            assertion = _expected_to_assertion(exp)
            lines.append(f"    // Expected: {_escape_ts(exp)}")
            lines.append(f"    {assertion}")
        lines.append("")

    lines.append(f"    await page.screenshot({{ path: "
                 f"'test-results/{_slugify(issue_key)}-{index}.png' }});")
    lines.append("  });")
    lines.append("")
    return lines


def _escape_ts(s: str) -> str:
    # \r is a JS line terminator too (CRLF-sourced text) - a bare CR inside
    # a string literal breaks the generated .spec.ts at parse time.
    return s.replace("\r", " ").replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")


def _escape_ts_regex(s: str) -> str:
    r"""Escape for use inside a TS regex literal.

    A raw '/' inside a /.../ literal would terminate the regex and let
    agent-supplied text inject syntax; plain string escaping doesn't cover
    it. The remaining JS regex metacharacters (^ $ \ . * + ? ( ) [ ] { } |)
    must be escaped too, or agent-supplied text like "(submit)" or "[test]"
    silently changes the matched pattern — or, with an unclosed '[', breaks
    the generated .spec.ts at parse time.
    """
    return re.sub(r"[\^$\\\.\*\+\?\(\)\[\]\{\}\|/']", r"\\\g<0>", s.replace("\r", " ").replace("\n", " "))


def _is_upload_step(step: str) -> bool:
    """True when the step's action is attaching a file.

    Checked before the fill branch — "Upload the orders.csv file" contains no
    fill keyword, but "Enter the file to upload" does, and filling a textbox
    where an upload was meant is the silent no-coverage failure this whole
    branch exists to remove.
    """
    s = step.lower().strip()
    if s.startswith(_STEP_VERBS) or s.startswith(_STEP_VERBS_CJK):
        return False
    return bool(_UPLOAD_WORD_RE.search(s)) or any(term in s for term in _UPLOAD_CJK)


def _upload_action(upload: _Upload, decls: set[str] | None = None) -> str:
    """The Playwright statements that attach *upload* to the page."""
    used: set[str] = set()
    statements: list[str] = []

    if upload.fixture_path:
        payload = f"'{_escape_ts(upload.fixture_path)}'"
    else:
        used.add("Buffer")
        buffer_expr = f"Buffer.from('{upload.content_b64}', 'base64')"
        payload = (
            "{\n"
            f"      name: '{_escape_ts(upload.filename)}',\n"
            f"      mimeType: '{_escape_ts(upload.mime_type)}',\n"
            f"      buffer: {buffer_expr},\n"
            "    }"
        )
        if upload.needs_path:
            # Some widgets read a real path instead of the File object —
            # materialize the payload in the test's own output dir.
            used.add("require")
            payload = "_uploadFile"
            statements.append(
                "const _uploadFile = test.info().outputPath("
                f"'{_escape_ts(upload.filename)}');\n"
                f"    require('fs').writeFileSync(_uploadFile, {buffer_expr});"
            )

    selector = f"'{_escape_ts(upload.selector)}'"
    if upload.via_chooser:
        # No visible input: the button opens the OS picker, so the setFiles
        # call must be armed before the click — hence Promise.all.
        statements.append(
            "const [fileChooser] = await Promise.all([\n"
            "      page.waitForEvent('filechooser'),\n"
            f"      page.locator({selector}).click(),\n"
            "    ]);\n"
            f"    await fileChooser.setFiles({payload});"
        )
    else:
        statements.append(f"await page.setInputFiles({selector}, {payload});")

    if decls is not None:
        decls |= used
    return "\n    ".join(statements)


def _unmatched_upload_action(step: str) -> str:
    """A real setInputFiles for an upload step with no declared payload.

    The old fallback was `waitForTimeout(1000); // TODO`, which passes
    without ever exercising the upload — a green test that proves nothing.
    A path that does not exist yet fails loudly until the fixture is added.
    """
    match = _FILENAME_RE.search(step)
    name = _clean_upload_name(match.group(0)) if match else "payload.bin"
    return (
        f"await page.setInputFiles('input[type=file]', 'fixtures/{_escape_ts(name)}');"
        " // TODO: create this fixture under tests/fixtures/ (or add it to the "
        "case's uploads)"
    )


def _classify_step(step: str) -> str:
    """Which branch :func:`_step_to_action` takes for *step*.

    The classifier is the single source of truth for the dispatch order
    because callers consume a queued upload based on it: when the two
    disagreed, "Navigate to /upload" matched the upload keywords in the
    caller's check but rendered as a goto, and the payload vanished.
    """
    s = step.lower().strip()
    if s.startswith("navigate to ") or s.startswith("go to ") or s.startswith("open "):
        return "navigate"
    if "click" in s:
        return "click"
    if _is_upload_step(step):
        return "upload"
    if "fill" in s or "input" in s or "enter" in s or "type" in s:
        return "fill"
    if "wait" in s:
        return "wait"
    if "download" in s:
        return "download"
    if "select" in s:
        return "select"
    return "unknown"


def _step_to_action(step: str, upload: "_Upload | None" = None, decls: set[str] | None = None) -> str:
    """Convert a natural-language step into a best-effort Playwright action."""
    kind = _classify_step(step)
    s = step.lower().strip()
    if kind == "navigate":
        url_part = step.split(" ", 2)[-1].strip()
        # Explicit 'domcontentloaded': Playwright's default navigation wait is
        # 'load', which blocks every generated case on the slowest subresource
        # (analytics beacon, CDN font). On a poor network that eats the case's
        # whole timeout while the page has been interactive for ages.
        return (
            f"await page.goto('{_escape_ts(url_part)}', "
            "{ waitUntil: 'domcontentloaded' });"
        )
    if kind == "click":
        # Split the ORIGINAL step, not the lowercased copy: `step.split("click")`
        # found nothing in "Click the login button" and returned the whole step
        # as the locator name. Match case-insensitively but keep the original
        # text after the keyword.
        match = re.search(r"click", step, re.IGNORECASE)
        target = step[match.end():].strip().strip('"').strip("'") if match else ""
        if not target:
            # A bare "click" step must not emit `name: //i` — the empty regex
            # becomes a line comment and breaks the generated .spec.ts syntax.
            return "await page.getByRole('button').first().click();"
        return f"await page.getByRole('button', {{ name: /{_escape_ts_regex(target)}/i }}).click();"
    if kind == "upload":
        return _upload_action(upload, decls) if upload is not None else _unmatched_upload_action(step)
    if kind == "fill":
        return f"await page.getByRole('textbox').fill('test-value'); // {_escape_ts(step)}"
    if kind == "wait":
        return f"await page.waitForTimeout(2000); // {_escape_ts(step)}"
    if kind == "download":
        # Same case-insensitive split as the click branch — "Download the
        # report" must yield "the report", not the whole step.
        match = re.search(r"download", step, re.IGNORECASE)
        target = (
            step[match.end():].strip().strip('"').strip("'").strip(".")
            if match else ""
        )
        if target:
            return (
                f"const [download] = await Promise.all([\n"
                # Playwright has no page.waitForDownload() — the download
                # event is awaited through waitForEvent. The generated spec
                # otherwise threw "page.waitForDownload is not a function".
                f"      page.waitForEvent('download'),\n"
                f"      page.getByRole('link', {{ name: /{_escape_ts_regex(target)}/i }}).click(),\n"
                f"    ]);\n"
                f"    await download.saveAs('test-results/{_escape_ts(step.split()[0])}-download' + download.suggested_filename());"
            )
        return f"// TODO: Download - await page.waitForEvent('download') around the click that triggers it. // {_escape_ts(step)}"
    if kind == "select":
        return f"await page.getByRole('combobox').selectOption({{ index: 0 }}); // {_escape_ts(step)}"
    # An unclassified step used to burn a fixed second and then run the rest of
    # the case against whatever the page happened to be showing. Waiting on the
    # load state instead is an anchor rather than a sleep: it returns as soon as
    # there is nothing left to wait for (~0ms on a settled page) and fails fast
    # if the page never settles. The TODO stays because the step is still not
    # exercised — `_STEP_VERBS` shows how often "Verify ..." lands here, and
    # failing the case outright would turn design sloppiness into a red run.
    return (
        "await page.waitForLoadState('domcontentloaded');"
        f" // TODO: unautomated step — {_escape_ts(step)}"
    )


def _expected_to_assertion(expected: str) -> str:
    """Convert an expected result into a best-effort Playwright assertion."""
    e = expected.lower().strip()
    if "visible" in e or "displayed" in e or "shown" in e or "appear" in e:
        # Case-insensitive split (same defect as _step_to_action): "Visible:
        # Welcome" left the whole string as the expected text.
        matches = list(re.finditer(r"visible", expected, re.IGNORECASE))
        text = (
            expected[matches[-1].end():].strip().strip(":").strip()
            if matches else expected.strip()
        )
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

_SCAFFOLD_PLAYWRIGHT_CONFIG = r"""import { defineConfig, devices } from '@playwright/test';

// Run shape is a tunable, not policy. The knobs exist because the same config
// has to serve a laptop, a shared test environment and a CI box:
//   PW_SERIAL=1    run one case at a time (cases that share mutable state)
//   PW_WORKERS=2   cap browser processes (small boxes, shared environments)
//   PW_RETRIES=1   retry a failure (a known-flaky target; off by default)
const serial = process.env.PW_SERIAL === '1';

export default defineConfig({
  testDir: './generated',
  timeout: 60_000,
  // Retries off by default: a retry doubles the wall clock of every failing
  // case, and the retried attempt overwrites the first one's video/trace — the
  // evidence is then of a different run than the failure that was reported.
  retries: process.env.PW_RETRIES ? Number(process.env.PW_RETRIES) : 0,
  // Cases inside one spec file run in parallel — a card whose cases each set up
  // their own data pays for one browser at a time otherwise. Cases that depend
  // on each other's effects must be merged or wrapped in
  // `test.describe.configure({ mode: 'serial' })`; PW_SERIAL=1 turns the whole
  // run serial without editing a spec.
  fullyParallel: !serial,
  workers: serial ? 1 : undefined,
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
      // Signs in once per run and hands the session to the chromium project, so
      // a spec no longer logs in in every case. Runs even when a single spec
      // file is selected (`playwright test generated/x.spec.ts`): Playwright
      // keeps a selected project's dependencies.
      name: 'setup',
      testDir: '.',
      testMatch: /auth\.setup\.ts/,
    },
    {
      name: 'chromium',
      dependencies: ['setup'],
      testMatch: /\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        // Must stay the path auth.setup.ts writes. A case that must start
        // signed out (sign-in, role/permission, sign-out) opts out with
        // `test.use({ storageState: { cookies: [], origins: [] } })`.
        storageState: '.auth/state.json',
      },
    },
  ],
  outputDir: './test-results',
});
"""

# Written by _ensure_test_scaffold, so it must stay byte-identical to
# scaffold/tests/auth.setup.ts — test_scaffold_parity.py fails otherwise.
_SCAFFOLD_AUTH_SETUP = r"""import { test } from '@playwright/test';
import fs from 'fs';
import path from 'path';

// Playwright resolves a relative `storageState` path (see playwright.config.ts)
// and this script's writes against the working directory, which is `tests/`
// for every run the platform starts. Keep the two spellings in step if either
// side ever moves.
const STATE_PATH = '.auth/state.json';
const EMPTY_STATE = JSON.stringify({ cookies: [], origins: [] });

test('authenticate', async ({ page }) => {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });

  const baseUrl = process.env.APP_BASE_URL || 'http://localhost:3000';
  const loginUrl = process.env.APP_LOGIN_URL || `${baseUrl}/login`;
  const username = process.env.APP_USERNAME || '';
  const password = process.env.APP_PASSWORD || '';

  if (!username || !password) {
    // Unauthenticated coverage is a legitimate run, so this is a skip rather
    // than a failure — but the empty state file is written first, because a
    // skip that left the file absent would fail every case that names it.
    fs.writeFileSync(STATE_PATH, EMPTY_STATE);
    test.skip(true, 'APP_USERNAME/APP_PASSWORD not set - running without a session');
    return;
  }

  await page.goto(loginUrl, { waitUntil: 'domcontentloaded' });
  await page.getByLabel(/user|email|account/i).fill(username);
  await page.getByLabel(/pass/i).fill(password);
  await page.getByRole('button', { name: /log|sign|submit/i }).click();

  // Deliberately no `.catch()`: a sign-in that quietly gave up would surface as
  // one confusing failure per case instead of one clear failure here, and it
  // would be attempted once per case rather than once per run. "Left the login
  // page" is the assertion because the post-login route differs per app.
  await page.waitForURL((url) => !/\/login\b/i.test(url.pathname), { timeout: 30_000 });

  await page.context().storageState({ path: STATE_PATH });
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


# Digests of every config this generator has shipped, newline-normalised. A
# workspace file hashing to one of these is generator output nobody edited, so
# replacing it with the current version is safe — and it is the only way an
# existing project learns about the setup project and parallel cases, because
# scaffold files are otherwise created once and left alone. Add the outgoing
# digest here whenever _SCAFFOLD_PLAYWRIGHT_CONFIG changes.
_SCAFFOLD_CONFIG_BASELINES = frozenset({
    # v1 — one chromium project, per-case UI sign-in, retries: 1. Two spellings
    # reached workspaces: the embedded constant, and the older scaffold/ file it
    # had drifted from (no acceptDownloads).
    "c6392165abcf64b3621af5d127a9bffcb036a8ab3a53f51a2fcd60c2648c8e9e",
    "4e7654c4e84add2757aa67cddbbf302a456eded0e5422f28e6225b84acf4494b",
})

_STORAGE_STATE_RE = re.compile(r"\bstorageState\s*:")

_SCAFFOLD_FIXTURES_README = """\
# Test fixtures

Upload assets referenced by generated specs live here. `playwright test` runs
with `tests/` as the working directory, so a fixture is addressed as
`fixtures/<name>` — never with `__dirname` (the specs are ESM, where it does
not exist).

Prefer generating the payload inline in the spec (`content` / `content_base64`
on the case's `uploads` entry) when it is small and synthetic: the test then
carries its own data and cannot rot when a file is moved. Put a file here when
it is large (over ~512KB), shared by several cases, or must be byte-identical
to a real-world sample.

Fixtures are inputs, not results — Playwright writes screenshots, videos and
traces to `test-results/`.
"""


@dataclass(frozen=True)
class _Scaffold:
    """What ensuring the test scaffold found and did.

    ``shared_session`` is the part the spec generator needs: whether the config
    that will run the spec signs in for it (see `_ensure_test_scaffold`).
    """

    error: str | None = None
    notes: tuple[str, ...] = ()
    shared_session: bool = False


def _scaffold_digest(text: str) -> str:
    """Digest of scaffold text, newline-normalised.

    The repo's config may reach a workspace as LF or CRLF depending on how the
    checkout that copied it was configured, and the two must not read as
    different files.
    """
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _upgrade_config_if_pristine(config: Path, current: str) -> bool:
    """Swap in the current config when the file is still an earlier one of ours.

    Scaffold files are created once and otherwise left alone, which is right for
    user-owned files and wrong for this one: a workspace built before the setup
    project existed would keep signing in per case forever, and the spec shape
    follows the config. The digest check is what makes the rewrite safe — any
    edit at all moves the file off the baseline and it is left untouched.
    """
    if _scaffold_digest(current) not in _SCAFFOLD_CONFIG_BASELINES:
        return False
    try:
        config.write_text(_SCAFFOLD_PLAYWRIGHT_CONFIG, encoding="utf-8")
    except OSError as e:
        _log.warning("test_generator: failed to upgrade %s: %s", config, e)
        return False
    _log.info("test_generator: upgraded %s to the shared-session layout", config)
    return True


def _ensure_test_scaffold(tests_root: Path) -> _Scaffold:
    """Create missing scaffold files; upgrade untouched generator-owned ones."""
    notes: list[str] = []
    shared_session = False
    try:
        tests_root.mkdir(parents=True, exist_ok=True)
        pkg = tests_root / "package.json"
        if not pkg.exists():
            _log.info("Creating test scaffold package.json at %s", pkg)
            pkg.write_text(_SCAFFOLD_PACKAGE_JSON, encoding="utf-8")
        config = tests_root / "playwright.config.ts"
        config_text: str
        if not config.exists():
            config_text = _SCAFFOLD_PLAYWRIGHT_CONFIG
            config.write_text(config_text, encoding="utf-8")
        else:
            config_text = config.read_text(encoding="utf-8")
            if _upgrade_config_if_pristine(config, config_text):
                config_text = _SCAFFOLD_PLAYWRIGHT_CONFIG
                notes.append(
                    "playwright.config.ts upgraded — one sign-in per run, cases in a "
                    "file run in parallel (PW_SERIAL=1 restores one-at-a-time)"
                )
        # The generated spec's shape depends on the config it will run under: a
        # config that hands out a session lets the spec stop signing in per
        # case, and one that does not must keep the per-case login. Matched as a
        # property assignment so a config that merely mentions storageState in a
        # comment is not read as handing one out.
        shared_session = _STORAGE_STATE_RE.search(config_text) is not None
        setup = tests_root / "auth.setup.ts"
        if shared_session and not setup.exists():
            setup.write_text(_SCAFFOLD_AUTH_SETUP, encoding="utf-8")
            notes.append("auth.setup.ts created — signs in once per run")
        tsconfig = tests_root / "tsconfig.json"
        if not tsconfig.exists():
            tsconfig.write_text(_SCAFFOLD_TSCONFIG, encoding="utf-8")
        fixtures = tests_root / "fixtures"
        fixtures.mkdir(exist_ok=True)
        readme = fixtures / "README.md"
        if not readme.exists():
            readme.write_text(_SCAFFOLD_FIXTURES_README, encoding="utf-8")
    except OSError as e:
        _log.warning("test_generator: failed to write scaffold in %s: %s", tests_root, e)
        return _Scaffold(error=f"Failed to create test scaffold: {e}")
    return _Scaffold(notes=tuple(notes), shared_session=shared_session)


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
