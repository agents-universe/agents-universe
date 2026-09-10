"""Tests for test_generator output_dir confinement."""
from __future__ import annotations

from agent_core.tools.base import ToolContext
from agent_core.tools.test_generator import TestGeneratorTool


def _context(project_fs_path) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(project_fs_path),
        conversation_id="c1",
        user_id="u1",
    )


def _params(**overrides) -> dict:
    base = {
        "operation": "generate_spec",
        "issue_key": "PROJ-1",
        "test_cases": [{"title": "t", "steps": ["wait"]}],
    }
    base.update(overrides)
    return base


async def test_output_dir_escape_rejected(tmp_path):
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    tool = TestGeneratorTool()
    result = await tool.execute(_params(output_dir="../proj-b/x"), _context(proj_a))
    assert "error" in result
    assert not (proj_b / "x").exists()


async def test_default_output_dir_unaffected(tmp_path):
    proj_a = tmp_path / "proj-a"
    proj_a.mkdir()
    tool = TestGeneratorTool()
    result = await tool.execute(_params(), _context(proj_a))
    assert result.get("success") is True
    assert (proj_a / "tests" / "generated" / "proj-1.spec.ts").exists()


async def test_stringified_test_cases_rejected(tmp_path):
    """A string test_cases must be rejected instead of generating a garbage
    spec or crashing on character iteration."""
    proj = tmp_path / "proj"
    proj.mkdir()
    tool = TestGeneratorTool()
    result = await tool.execute(_params(test_cases="just one case"), _context(proj))
    assert "error" in result
    assert "array" in result["error"].lower()


# ---------------------------------------------------------------------------
# Step/expected conversion (pure helpers)
# ---------------------------------------------------------------------------


def test_click_step_splits_case_insensitively():
    """`step.split("click")` on the original text found nothing in a
    capitalized step, so the whole sentence became the locator name."""
    from agent_core.tools.test_generator import _step_to_action

    action = _step_to_action("Click the login button")
    assert "name: /the login button/i" in action, action


def test_download_step_uses_wait_for_event():
    """Playwright has no page.waitForDownload() — the generated spec threw
    "page.waitForDownload is not a function"."""
    from agent_core.tools.test_generator import _step_to_action

    action = _step_to_action("Download the report")
    assert "page.waitForEvent('download')" in action, action
    assert "waitForDownload" not in action
    assert "name: /the report/i" in action, action


def test_expected_visible_splits_case_insensitively():
    from agent_core.tools.test_generator import _expected_to_assertion

    assertion = _expected_to_assertion("Visible: Welcome back")
    assert "toContainText" not in assertion  # visible → toBeVisible
    assert "Welcome back" in assertion, assertion


# ---------------------------------------------------------------------------
# Upload scenarios
# ---------------------------------------------------------------------------


def _upload_case(**overrides) -> dict:
    case = {
        "title": "upload a file",
        "steps": ["Navigate to /upload", "Upload the orders file"],
        "uploads": [
            {"filename": "orders.csv", "mime_type": "text/csv", "content": "id,total\n1,42\n"}
        ],
    }
    case.update(overrides)
    return case


async def _generate(tmp_path, case: dict) -> str:
    result = await _generate_result(tmp_path, case)
    assert result.get("success") is True, result
    spec_file = tmp_path / "proj" / "tests" / "generated" / "proj-1.spec.ts"
    return spec_file.read_text(encoding="utf-8")


async def _generate_result(tmp_path, case: dict) -> dict:
    (tmp_path / "proj").mkdir(exist_ok=True)
    return await TestGeneratorTool().execute(
        _params(test_cases=[case]), _context(tmp_path / "proj")
    )


async def test_upload_step_emits_set_input_files(tmp_path):
    """The old behavior degraded upload steps to waitForTimeout(1000) — a
    green test that never uploaded anything."""
    spec = await _generate(tmp_path, _upload_case())

    assert "setInputFiles('input[type=file]'" in spec, spec
    assert "waitForTimeout" not in spec, spec
    assert "buffer: Buffer.from('aWQsdG90YWwKMSw0Mgo=', 'base64')" in spec, spec
    assert "name: 'orders.csv'" in spec and "mimeType: 'text/csv'" in spec
    # Buffer is a Node global the scaffold's tsconfig has no types for.
    assert "declare const Buffer: any;" in spec, spec


async def test_upload_step_precedes_the_fill_branch(tmp_path):
    """"Enter the file to upload" contains a fill keyword; without the upload
    check running first it became a textbox fill."""
    spec = await _generate(
        tmp_path, _upload_case(steps=["Enter the report to upload"])
    )
    assert "setInputFiles" in spec, spec
    assert "getByRole('textbox')" not in spec, spec


async def test_cjk_upload_step_is_detected(tmp_path):
    spec = await _generate(tmp_path, _upload_case(steps=["上传订单文件"]))
    assert "setInputFiles" in spec, spec
    assert "waitForTimeout" not in spec, spec


async def test_binary_upload_uses_the_given_base64(tmp_path):
    """A generated PNG byte-for-byte (newlines and all) without a decode/re-encode
    round trip that could differ from the caller's intent."""
    spec = await _generate(
        tmp_path,
        _upload_case(
            uploads=[
                {
                    "filename": "logo.png",
                    "content_base64": "iVBORw0KGgo=",
                    "mime_type": "image/png",
                }
            ]
        ),
    )
    assert "Buffer.from('iVBORw0KGgo=', 'base64')" in spec, spec
    assert "mimeType: 'image/png'" in spec, spec


async def test_fixture_path_reference(tmp_path):
    spec = await _generate(
        tmp_path,
        _upload_case(uploads=[{"filename": "big.zip", "fixture_path": "fixtures/big.zip"}]),
    )
    assert "setInputFiles('input[type=file]', 'fixtures/big.zip')" in spec, spec
    assert "declare const Buffer" not in spec, spec


async def test_via_chooser_arms_the_file_chooser(tmp_path):
    spec = await _generate(
        tmp_path,
        _upload_case(
            uploads=[{"filename": "a.txt", "content": "hi", "via_chooser": True}]
        ),
    )
    assert "page.waitForEvent('filechooser')" in spec, spec
    assert "fileChooser.setFiles(" in spec, spec
    assert "page.locator(" in spec, spec
    assert "Promise.all" in spec, spec


async def test_needs_path_materializes_the_payload(tmp_path):
    spec = await _generate(
        tmp_path,
        _upload_case(uploads=[{"filename": "a.txt", "content": "hi", "needs_path": True}]),
    )
    assert "test.info().outputPath('a.txt')" in spec, spec
    assert "require('fs').writeFileSync" in spec, spec
    assert "setInputFiles('input[type=file]', _uploadFile)" in spec, spec
    assert "declare const require: any;" in spec, spec


async def test_upload_step_without_a_payload_still_uploads(tmp_path):
    """No declared payload → a real setInputFiles against a fixture path that
    does not exist yet, so the test fails loudly instead of passing empty."""
    spec = await _generate(
        tmp_path, _upload_case(uploads=[], steps=["Upload the orders.csv file"])
    )
    assert "setInputFiles('input[type=file]', 'fixtures/orders.csv')" in spec, spec
    assert "waitForTimeout" not in spec, spec
    assert "TODO: create this fixture" in spec, spec


async def test_unconsumed_payloads_are_still_emitted(tmp_path):
    spec = await _generate(
        tmp_path,
        _upload_case(
            steps=["Navigate to /upload"],
            uploads=[
                {"filename": "a.txt", "content": "one"},
                {"filename": "b.txt", "content": "two"},
            ],
        ),
    )
    assert "Unmatched upload: a.txt" in spec, spec
    assert "Unmatched upload: b.txt" in spec, spec
    assert spec.count("setInputFiles") == 2, spec


async def test_upload_filename_does_not_escape_its_literal(tmp_path):
    """Filenames are caller-controlled text landing inside a TS string literal
    in a generated, then executed, file."""
    spec = await _generate(
        tmp_path,
        _upload_case(uploads=[{"filename": "it's.csv", "content": "x"}]),
    )
    assert "name: 'it\\'s.csv'" in spec, spec


async def test_upload_filename_path_decoration_is_stripped(tmp_path):
    """A filename carrying a path (or a predicate that looks like one) is
    reduced to its last segment — that is what a browser would show."""
    spec = await _generate(
        tmp_path,
        _upload_case(
            uploads=[
                {
                    "filename": "'); await page.goto('http://evil.example.com'); //.txt",
                    "content": "x",
                }
            ]
        ),
    )
    body = spec.split("test.describe")[1]
    assert "evil.example.com" not in body, spec
    assert "name: '.txt'" in body, spec


def test_upload_step_detection_keywords():
    from agent_core.tools.test_generator import _is_upload_step

    assert _is_upload_step("Upload the CSV")
    assert _is_upload_step("attach the signed contract")
    assert _is_upload_step("Select file from disk")
    assert _is_upload_step("导入客户数据")
    # "important" must not read as import; a click on an Upload button is a
    # click step, not an upload.
    assert not _is_upload_step("This is important")
    assert not _is_upload_step("Verify the page title")


async def test_oversized_inline_payload_is_refused(tmp_path):
    result = await _generate_result(
        tmp_path,
        _upload_case(
            uploads=[{"filename": "big.bin", "content": "x" * (512 * 1024 + 1)}]
        ),
    )
    assert "inline limit" in result["error"], result
    assert "fixture_path" in result["error"], result


async def test_too_many_uploads_are_refused(tmp_path):
    result = await _generate_result(
        tmp_path,
        _upload_case(
            uploads=[{"filename": f"f{i}.txt", "content": "x"} for i in range(6)]
        ),
    )
    assert "per-case limit" in result["error"], result


async def test_invalid_base64_is_refused_without_echoing_it(tmp_path):
    result = await _generate_result(
        tmp_path,
        _upload_case(
            uploads=[{"filename": "a.bin", "content_base64": "sk-live-SUPERSECRET!"}]
        ),
    )
    assert "not valid base64" in result["error"], result
    assert "SUPERSECRET" not in result["error"], result


async def test_conflicting_payload_sources_are_refused(tmp_path):
    result = await _generate_result(
        tmp_path,
        _upload_case(
            uploads=[{"filename": "a.txt", "content": "x", "fixture_path": "fixtures/a.txt"}]
        ),
    )
    assert "exactly one of" in result["error"], result


async def test_fixture_path_cannot_escape_tests(tmp_path):
    result = await _generate_result(
        tmp_path,
        _upload_case(uploads=[{"filename": "a.txt", "fixture_path": "../../etc/passwd"}]),
    )
    assert "must stay under tests/" in result["error"], result


async def test_scaffold_creates_fixtures_dir_and_readme(tmp_path):
    await _generate(tmp_path, _upload_case())
    fixtures = tmp_path / "proj" / "tests" / "fixtures"
    assert fixtures.is_dir(), fixtures
    assert "fixtures/<name>" in (fixtures / "README.md").read_text(encoding="utf-8")
