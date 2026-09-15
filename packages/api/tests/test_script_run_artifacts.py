"""Run results, artifact serving and the sandboxed HTML report.

Two layers: the pure helpers in ``api.services.script_artifacts`` (parsing,
manifest, retention, capability tokens) and the three endpoints that expose
them, including the containment checks that keep a crafted path from reaching
outside a run's artifact directory.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from api.models.script import AutomationScript, ScriptRun
from api.paths import PROJECTS_ROOT
from api.services import script_artifacts as sa

# Trimmed from a real `--reporter json` run of @playwright/test 1.61.1: the
# keys matter (a report test entry carries its verdict as `status`, the result
# enum), so the fixture mirrors the file instead of a convenient invention.
_REPORT = {
    "config": {"rootDir": "/srv/project/tests"},
    "suites": [
        {
            "title": "login.spec.ts",
            "file": "tests/generated/login.spec.ts",
            "column": 0,
            "line": 0,
            "specs": [
                {
                    "title": "signs in",
                    "ok": True,
                    "tags": [],
                    "id": "a1b2",
                    "file": "tests/generated/login.spec.ts",
                    "line": 4,
                    "column": 5,
                    "tests": [
                        {
                            "timeout": 30000,
                            "expectedStatus": "passed",
                            "status": "expected",
                            "results": [{"status": "passed", "duration": 120, "attachments": []}],
                        }
                    ],
                },
                {
                    "title": "rejects a bad password",
                    "ok": False,
                    "tags": [],
                    "id": "c3d4",
                    "file": "tests/generated/login.spec.ts",
                    "line": 12,
                    "column": 5,
                    "tests": [
                        {
                            "timeout": 30000,
                            "expectedStatus": "passed",
                            "status": "unexpected",
                            "results": [
                                {
                                    "status": "failed",
                                    "duration": 340,
                                    "errors": [{"message": "Error: expected 200, got 401"}],
                                    "attachments": [
                                        {
                                            "name": "screenshot",
                                            "path": "test-results/login-fails/test-failed-1.png",
                                            "contentType": "image/png",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    ],
    "errors": [],
    "stats": {"expected": 1, "skipped": 0, "unexpected": 1, "flaky": 0, "duration": 460},
}


def _report_with_attachment(path: str, content_type: str = "image/png") -> dict:
    """The fixture report, pointing its screenshot at ``path``.

    Playwright records attachments as absolute ``testInfo.outputPath()`` values;
    a relative one is what a hand-written ``testInfo.attach({path})`` leaves
    behind, so both shapes need covering.
    """
    report = json.loads(json.dumps(_REPORT))
    attachments = report["suites"][0]["specs"][1]["tests"][0]["results"][0]["attachments"]
    attachments[0]["path"] = path
    attachments[0]["contentType"] = content_type
    return report


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parse_json_report_normalizes_counts_and_failures(tmp_path: Path):
    report = tmp_path / "results.json"
    report.write_text(json.dumps(_REPORT), encoding="utf-8")

    parsed = sa.parse_json_report(report)

    assert parsed is not None
    assert parsed["source"] == "json" and parsed["partial"] is False
    assert parsed["status"] == "failed"
    assert parsed["counts"] == {"total": 2, "passed": 1, "failed": 1, "flaky": 0, "skipped": 0}
    assert parsed["duration_ms"] == 460
    assert [t["title"] for t in parsed["failed_tests"]] == ["rejects a bad password"]
    assert parsed["failed_tests"][0]["error"] == "Error: expected 200, got 401"
    assert parsed["failed_tests"][0]["file"] == "tests/generated/login.spec.ts"
    assert [t["status"] for t in parsed["tests"]] == ["passed", "failed"]


def test_parse_json_report_rejects_unrelated_json(tmp_path: Path):
    """A manifest or a fixture must not parse as an all-zero result."""
    other = tmp_path / "manifest.json"
    other.write_text(json.dumps({"generated_at": "x", "artifacts": []}), encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    assert sa.parse_json_report(other) is None
    assert sa.parse_json_report(broken) is None


def test_parse_log_summary_reads_the_final_counts(tmp_path: Path):
    """The verdict is the LAST thing a test runner prints - and the stored log
    is head-capped, so this parses the caller's uncapped tail."""
    log = (
        "\x1b[32m1 passed\x1b[0m (1.2s)\n"   # per-test progress line: must lose
        "  1) login.spec.ts:12:3 › rejects a bad password ───\n"
        "    Error: expect(received).toBe(expected)\n"
        "\x1b[31m1 failed\x1b[0m\n"
        "\x1b[32m2 passed\x1b[0m (4.6s)\n"
    )

    parsed = sa.parse_log_summary(log)

    assert parsed["source"] == "log" and parsed["partial"] is True
    assert parsed["counts"] == {"total": 3, "passed": 2, "failed": 1, "flaky": 0, "skipped": 0}
    assert parsed["duration_ms"] == 4600
    assert parsed["status"] == "failed"
    assert parsed["failed_tests"][0]["title"].startswith("login.spec.ts:12:3")


def test_collect_result_prefers_the_json_report(tmp_path: Path):
    (tmp_path / "results.json").write_text(json.dumps(_REPORT), encoding="utf-8")

    result = sa.collect_result(tmp_path, "1 failed\n")

    assert result["source"] == "json" and result["status"] == "failed"


def test_run_summary_payload_projects_the_row():
    run = ScriptRun(
        script_id="s1",
        status="completed",
        exit_code=1,
        spec_slug="login-1",
        triggered_by="u1",
        result_json=json.dumps(sa.parse_log_summary("2 passed (1.0s)\n")),
    )
    payload = sa.run_summary_payload(run)

    assert payload["spec_slug"] == "login-1"
    assert payload["summary"]["status"] == "passed"
    assert payload["summary"]["counts"]["passed"] == 2
    assert "result_json" not in payload and "log" not in payload


# ── Manifest ─────────────────────────────────────────────────────────────────

def test_build_manifest_maps_attachments_and_skips_internals(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    (artifacts / "test-results" / "login-fails").mkdir(parents=True)
    shot = artifacts / "test-results" / "login-fails" / "test-failed-1.png"
    shot.write_bytes(b"\x89PNG")
    (artifacts / "html-report").mkdir()
    (artifacts / "html-report" / "index.html").write_text("<html>", encoding="utf-8")
    (artifacts / "html-report" / "report.js").write_text("//", encoding="utf-8")
    report = artifacts / "results.json"
    report.write_text(json.dumps(_report_with_attachment(str(shot))), encoding="utf-8")

    manifest = sa.build_manifest(artifacts, report)

    by_path = {a["rel_path"]: a for a in manifest["artifacts"]}
    # Report-declared attachment first, with the test it belongs to.
    assert manifest["artifacts"][0]["rel_path"] == "test-results/login-fails/test-failed-1.png"
    assert manifest["artifacts"][0]["test_title"] == "rejects a bad password"
    assert manifest["artifacts"][0]["kind"] == "screenshot"
    # Report internals and our own files never become gallery entries.
    assert not any(p.startswith("html-report/") and p != "html-report/index.html" for p in by_path)
    assert "results.json" not in by_path and "manifest.json" not in by_path
    assert by_path["html-report/index.html"]["kind"] == "report"


def test_build_manifest_resolves_relative_attachment_paths(tmp_path: Path):
    """Attachments the JSON reporter could not resolve still map to the file
    they name - the run directory is the runner's cwd."""
    artifacts = tmp_path / "artifacts"
    (artifacts / "downloads").mkdir(parents=True)
    (artifacts / "downloads" / "invoice.pdf").write_bytes(b"%PDF")
    report = artifacts / "results.json"
    report.write_text(
        json.dumps(
            _report_with_attachment("downloads/invoice.pdf", "application/pdf")
        ),
        encoding="utf-8",
    )

    manifest = sa.build_manifest(artifacts, report)

    by_path = {a["rel_path"]: a for a in manifest["artifacts"]}
    assert by_path["downloads/invoice.pdf"]["kind"] == "file"
    assert by_path["downloads/invoice.pdf"]["test_title"] == "rejects a bad password"


def test_build_manifest_drops_attachments_outside_the_run(tmp_path: Path):
    """A crafted report must not turn the gallery into a file browser."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    report = artifacts / "results.json"
    report.write_text(
        json.dumps(_report_with_attachment(str(secret))), encoding="utf-8"
    )

    manifest = sa.build_manifest(artifacts, report)

    assert manifest["artifacts"] == []


def test_build_manifest_caps_entries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sa, "MAX_ARTIFACTS", 3)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for index in range(5):
        (artifacts / f"shot-{index}.png").write_bytes(b"x")

    manifest = sa.build_manifest(artifacts)

    assert len(manifest["artifacts"]) == 3
    assert manifest["truncated"] is True


def test_read_manifest_roundtrip(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "shot.png").write_bytes(b"x")

    sa.write_manifest(artifacts, sa.build_manifest(artifacts))

    assert sa.read_manifest(artifacts)["artifacts"][0]["rel_path"] == "shot.png"
    assert sa.read_manifest(tmp_path / "elsewhere") is None


# ── Retention ────────────────────────────────────────────────────────────────

def _fake_run_dir(root: Path, name: str, age_seconds: int, size: int = 0) -> Path:
    run_dir = root / name
    (run_dir / "artifacts").mkdir(parents=True)
    if size:
        (run_dir / "artifacts" / "video.webm").write_bytes(b"x" * size)
    stamp = time.time() - age_seconds
    os.utime(run_dir, (stamp, stamp))
    return run_dir


def test_prune_keeps_the_newest_runs(tmp_path: Path):
    root = sa.runs_root(tmp_path)
    root.mkdir(parents=True)
    for index in range(5):
        _fake_run_dir(root, f"run-{index}", age_seconds=index * 60)

    sa.prune_run_artifacts(tmp_path, "run-0", keep_runs=2)

    assert sorted(p.name for p in root.iterdir()) == ["run-0", "run-1"]


def test_prune_never_removes_the_current_run(tmp_path: Path):
    """History is the point of keeping artifacts: an ancient run that just
    finished must survive its own prune."""
    root = sa.runs_root(tmp_path)
    root.mkdir(parents=True)
    _fake_run_dir(root, "ancient", age_seconds=10_000)
    for index in range(3):
        _fake_run_dir(root, f"recent-{index}", age_seconds=index)

    sa.prune_run_artifacts(tmp_path, "ancient", keep_runs=1)

    assert (root / "ancient").is_dir()
    assert (root / "recent-0").is_dir()
    assert not (root / "recent-1").exists()


def test_prune_enforces_the_size_budget(tmp_path: Path):
    """Newest-first, keep what fits: the third run overflows the budget, so it
    goes even though the run count is nowhere near the cap."""
    root = sa.runs_root(tmp_path)
    root.mkdir(parents=True)
    for index in range(3):
        _fake_run_dir(root, f"run-{index}", age_seconds=index * 60, size=512)

    sa.prune_run_artifacts(tmp_path, "current", keep_runs=10, max_total_bytes=1200)

    assert sorted(p.name for p in root.iterdir()) == ["run-0", "run-1"]


# ── Capability tokens ────────────────────────────────────────────────────────

def test_sandboxed_report_html_splices_the_storage_shim():
    """The shim must run before the report's own bundle, i.e. inside <head>."""
    patched = sa.sandboxed_report_html(
        "<html><head><meta charset='utf-8'><title>t</title></head><body>x</body></html>"
    )

    assert patched.index("localStorage") < patched.index("<meta")
    assert patched.rstrip().endswith("</html>")

    # A document with no head at all is still patched rather than left blank.
    headless = sa.sandboxed_report_html("<pre>x</pre>")
    assert headless.endswith("<pre>x</pre>") and "localStorage" in headless


def test_report_token_roundtrip_and_rejection():
    token = sa.mint_report_token("run-1")

    assert sa.verify_report_token("run-1", token) is True
    # Bound to one run, unforgeable without the app secret, and time-limited.
    assert sa.verify_report_token("run-2", token) is False
    assert sa.verify_report_token("run-1", token[:-1] + ("0" if token[-1] != "0" else "1")) is False
    assert sa.verify_report_token("run-1", "not-a-token") is False
    assert sa.verify_report_token("run-1", "") is False
    assert sa.verify_report_token("run-1", sa.mint_report_token("run-1", ttl_seconds=-5)) is False


# ── Endpoints ────────────────────────────────────────────────────────────────

async def _seed_run(
    db, project, *, spec_slug: str | None = None, with_artifacts: bool = True,
    result: dict | None = None,
) -> ScriptRun:
    # Every Playwright run in a project hangs off one hidden anchor script, so
    # telling runs apart is spec_slug's job - as in production.
    anchor = (await db.execute(
        select(AutomationScript).where(AutomationScript.project_id == str(project.project_id))
    )).scalars().first()
    if anchor is None:
        anchor = AutomationScript(
            project_id=str(project.project_id),
            name="__playwright__",
            script_type="playwright",
            content="",
        )
        db.add(anchor)
        await db.commit()
        await db.refresh(anchor)

    run = ScriptRun(
        script_id=anchor.script_id,
        triggered_by="test-user",
        status="completed",
        exit_code=1,
        spec_slug=spec_slug,
        stdout_log="Running 2 tests\n",
        result_json=json.dumps(result) if result else None,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    if with_artifacts:
        artifacts = sa.artifacts_dir(PROJECTS_ROOT / project.slug, str(run.run_id))
        (artifacts / "test-results" / "login-fails").mkdir(parents=True)
        (artifacts / "test-results" / "login-fails" / "test-failed-1.png").write_bytes(
            b"\x89PNG\r\n\x1a\n"
        )
        (artifacts / "trace.zip").write_bytes(b"PK\x03\x04")
        (artifacts / "html-report").mkdir()
        (artifacts / "html-report" / "index.html").write_text(
            "<html><body>playwright report</body></html>", encoding="utf-8"
        )
        (artifacts / "html-report" / "trace").mkdir()
        (artifacts / "html-report" / "trace" / "index.html").write_text(
            "<html><body>trace viewer</body></html>", encoding="utf-8"
        )
        (artifacts / "results.json").write_text(json.dumps(_REPORT), encoding="utf-8")
        if result is None:
            # Mirrors the execution path, which stores what collect_result read
            # off the disk - the detail endpoint never re-parses artifacts.
            run.result_json = json.dumps(sa.collect_result(artifacts, ""), ensure_ascii=False)
            await db.commit()
    return run


@pytest.mark.asyncio
async def test_get_run_returns_result_and_artifacts(client, db, make_project):
    project = await make_project("runs-detail")
    run = await _seed_run(db, project, spec_slug="login-1")

    resp = await client.get(f"/api/scripts/runs/{run.run_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["spec_slug"] == "login-1"
    assert body["result"]["counts"]["failed"] == 1
    assert body["status"] == "completed"
    assert body["log"] == "Running 2 tests\n"
    kinds = {a["rel_path"]: a["kind"] for a in body["artifacts"]}
    assert kinds["test-results/login-fails/test-failed-1.png"] == "screenshot"
    assert kinds["trace.zip"] == "trace"
    assert kinds["html-report/index.html"] == "report"
    # The report is reachable only through the sandboxed endpoint, and the URL
    # this endpoint hands out has to be the one the browser can actually open -
    # a mismatch between the two is a 404 nobody sees until a run finishes.
    assert body["report_url"].startswith(f"/api/scripts/runs/{run.run_id}/report/")
    assert sa.verify_report_token(str(run.run_id), body["report_url"].split("/report/")[1].split("/")[0])
    served = await client.get(body["report_url"])
    assert served.status_code == 200 and "sandbox" in served.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_get_run_without_artifacts(client, db, make_project):
    project = await make_project("runs-bare")
    run = await _seed_run(db, project, with_artifacts=False)

    resp = await client.get(f"/api/scripts/runs/{run.run_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["artifacts"] == []
    assert body["report_url"] is None
    assert body["result"] is None


@pytest.mark.asyncio
async def test_get_run_404s_for_unknown_and_malformed_ids(client, db, make_project):
    project = await make_project("runs-missing")
    run = await _seed_run(db, project, with_artifacts=False)

    assert (await client.get("/api/scripts/runs/does-not-exist")).status_code == 404
    # The encoded traversal is decoded before routing, so it lands either on the
    # id-shape guard (400) or on a route that never matches (404).
    assert (await client.get("/api/scripts/runs/..%2f..%2fetc")).status_code in (400, 404)
    assert (await client.get(f"/api/scripts/runs/{run.run_id}")).status_code == 200


@pytest.mark.asyncio
async def test_run_endpoints_respect_project_privacy(client, db, make_project, as_user):
    project = await make_project("runs-private", visibility="private")
    run = await _seed_run(db, project, with_artifacts=False)

    async with as_user("intruder"):
        assert (await client.get(f"/api/scripts/runs/{run.run_id}")).status_code == 403
        assert (await client.get(
            f"/api/scripts/runs/{run.run_id}/artifacts/test-results/login-fails/test-failed-1.png"
        )).status_code == 403


@pytest.mark.asyncio
async def test_artifact_endpoint_sets_inline_and_download_policies(client, db, make_project):
    project = await make_project("runs-artifacts")
    run = await _seed_run(db, project)

    shot = await client.get(
        f"/api/scripts/runs/{run.run_id}/artifacts/test-results/login-fails/test-failed-1.png"
    )
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/png"
    assert shot.headers["content-disposition"].startswith("inline")
    assert shot.headers["x-content-type-options"] == "nosniff"

    trace = await client.get(f"/api/scripts/runs/{run.run_id}/artifacts/trace.zip")
    assert trace.status_code == 200
    assert trace.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_artifact_endpoint_refuses_escapes_and_report_internals(client, db, make_project):
    project = await make_project("runs-containment")
    run = await _seed_run(db, project)
    outside = PROJECTS_ROOT / project.slug / "tests"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "secret.txt").write_text("not yours", encoding="utf-8")

    for path in (
        "../tests/secret.txt",
        "%2e%2e%2ftests%2fsecret.txt",
        "/etc/passwd",
    ):
        resp = await client.get(f"/api/scripts/runs/{run.run_id}/artifacts/{path}")
        assert resp.status_code in (400, 404), path
        assert "not yours" not in resp.text

    # Report internals belong to the sandboxed report endpoint, not to a plain
    # download of the same HTML.
    internal = await client.get(
        f"/api/scripts/runs/{run.run_id}/artifacts/html-report/index.html"
    )
    assert internal.status_code == 404


def test_contained_file_rejects_traversal_and_absolute_paths(tmp_path: Path):
    from api.routers.script_runs import _contained_file

    base = tmp_path / "artifacts"
    base.mkdir()
    (base / "ok.png").write_bytes(b"x")

    assert _contained_file(base, "ok.png").name == "ok.png"
    for bad in ("../ok.png", "..", "/etc/passwd", "sub/../../ok.png", "a\x00b", ""):
        with pytest.raises(HTTPException):
            _contained_file(base, bad)


@pytest.mark.asyncio
async def test_report_endpoint_requires_a_valid_token(client, db, make_project):
    project = await make_project("runs-report")
    run = await _seed_run(db, project)

    assert (await client.get(f"/api/scripts/runs/{run.run_id}/report/bogus/index.html")).status_code == 403
    assert (await client.get(
        f"/api/scripts/runs/{run.run_id}/report/{sa.mint_report_token(str(run.run_id), ttl_seconds=-5)}/index.html"
    )).status_code == 403


@pytest.mark.asyncio
async def test_report_endpoint_serves_sandboxed_html(client, db, make_project):
    """No session cookie on this request: the report, and every subresource it
    pulls from an opaque origin, authenticates with the URL alone."""
    project = await make_project("runs-report-ok")
    run = await _seed_run(db, project)
    token = sa.mint_report_token(str(run.run_id))

    resp = await client.get(f"/api/scripts/runs/{run.run_id}/report/{token}/index.html")

    assert resp.status_code == 200
    assert "playwright report" in resp.text
    # The report reads localStorage while booting; a sandboxed document has
    # none, so the served document carries the in-memory stand-in.
    assert "localStorage" in resp.text
    assert resp.headers["content-type"].startswith("text/html")
    csp = resp.headers["content-security-policy"]
    assert "sandbox" in csp
    assert "allow-same-origin" not in csp
    assert "http://test " in csp  # this request's own origin, spelled out
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_report_endpoint_answers_the_trace_viewer_with_a_notice(client, db, make_project):
    """The report's "View Trace" link opens a viewer that waits for a service
    worker before rendering, which no sandboxed document can have. Better an
    explanation than the blank tab Playwright's own page would produce."""
    project = await make_project("runs-trace-notice")
    run = await _seed_run(db, project)
    token = sa.mint_report_token(str(run.run_id))

    resp = await client.get(
        f"/api/scripts/runs/{run.run_id}/report/{token}/trace/index.html"
    )

    assert resp.status_code == 200
    assert "trace viewer" not in resp.text
    assert "trace.playwright.dev" in resp.text
    assert "sandbox" in resp.headers["content-security-policy"]
    # Only that one document: the rest of the report still comes off the disk.
    assert "playwright report" in (
        await client.get(f"/api/scripts/runs/{run.run_id}/report/{token}/index.html")
    ).text


@pytest.mark.asyncio
async def test_spec_history_filters_by_slug_without_creating_an_anchor(client, db, make_project):
    project = await make_project("runs-history")
    url = f"/api/projects/{project.project_id}/playwright/specs/login-1/runs"

    # No anchor yet: an empty history must not write one as a side effect.
    assert (await client.get(url)).json() == []
    anchors = (await db.execute(
        select(AutomationScript).where(AutomationScript.project_id == str(project.project_id))
    )).scalars().all()
    assert anchors == []

    first = await _seed_run(db, project, spec_slug="login-1", with_artifacts=False)
    await _seed_run(db, project, spec_slug="checkout-2", with_artifacts=False)
    second = await _seed_run(db, project, spec_slug="login-1", with_artifacts=False)

    rows = (await client.get(url)).json()
    assert [r["run_id"] for r in rows] == [str(second.run_id), str(first.run_id)]
    assert rows[0]["spec_slug"] == "login-1"

    invalid = await client.get(
        f"/api/projects/{project.project_id}/playwright/specs/UPPER/runs"
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_script_run_list_carries_the_compact_summary(client, db, make_project):
    project = await make_project("runs-list")
    result = sa.parse_log_summary("2 passed (1.0s)\n")
    run = await _seed_run(db, project, spec_slug="login-1", with_artifacts=False, result=result)

    resp = await client.get(f"/api/scripts/{run.script_id}/runs")

    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["run_id"] == str(run.run_id)
    assert row["summary"]["counts"]["passed"] == 2
    assert "result" not in row and "log" not in row
