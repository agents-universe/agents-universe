"""Run-scoped results and artifacts for automation-script runs.

A Playwright run reports its outcome through here: the JSON reporter supplies
the counts and the failed cases, and the HTML report plus per-test attachments
(screenshots, videos, traces) are redirected into a per-run directory under the
project workspace. Playwright empties its own output folders at the start of
every run, so anything a past run produced has to live somewhere run-scoped or
it is gone.

Deliberately free of DB and request objects - the router, the scheduled-run
service and the tests all drive the same pure helpers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from api.config import get_settings

_log = logging.getLogger("agents_universe.script_artifacts")

# Run directories kept per project workspace (newest first), and the total size
# ceiling across them. Attachments are videos and traces - megabytes each - and
# nothing else ever prunes them, so each finished run trims the older ones.
KEEP_RUNS = 20
MAX_TOTAL_BYTES = 512 * 1024 * 1024

# Ceilings on what reaches the run row and the API payload: one huge suite must
# not turn a single run into megabytes of JSON on every history request.
MAX_TEST_ROWS = 200
MAX_FAILED_TESTS = 50
MAX_ARTIFACTS = 100
MAX_ERROR_CHARS = 500

# A test runner prints its verdict LAST, which is exactly what a head-capped log
# drops first - hence the tail buffer in agent-core and this window.
LOG_TAIL_CHARS = 20_000

# The report URL is a bearer capability: minted only after a project-access
# check, bound to one run, and short-lived because the token is visible in
# browser history and in the server's access log.
REPORT_TOKEN_TTL_SECONDS = 3600

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|flaky|skipped|did not run|interrupted)\b")
_DURATION_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*(ms|s|m)\)")
_FAILED_CASE_RE = re.compile(r"^\s*\d+\)\s+(.+?)\s*$")
_BOX_CHARS = "─-—| \t"
_COUNT_ALIASES = {"did not run": "skipped", "interrupted": "failed"}
_OUTCOME_STATUS = {
    "expected": "passed",
    "unexpected": "failed",
    "flaky": "flaky",
    "skipped": "skipped",
}
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".zip": "application/zip",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    # The HTML report's own assets (and its trace viewer, when it inlines one).
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".map": "application/json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
}
# Suffixes safe to render on the app origin (and inside the sandboxed report):
# everything else - zips, HTML, SVG, unknown - is forced to download.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VIDEO_SUFFIXES = {".webm", ".mp4"}


def runs_root(project_fs: str | Path) -> Path:
    """Parent of every run directory for one project workspace."""
    return Path(project_fs) / ".tmp" / "script-runs"


def artifacts_dir(project_fs: str | Path, run_id: str) -> Path:
    """Where one run's artifacts live.

    Under .tmp/, which the workspace file tree skips and project deletion
    reclaims along with the rest of the workspace.
    """
    return runs_root(project_fs) / run_id / "artifacts"


# ── Result parsing ───────────────────────────────────────────────────────────


def collect_result(artifacts: Path, log_tail: str) -> dict:
    """Structured result for a finished run.

    The JSON reporter is authoritative when it wrote a report; otherwise fall
    back to parsing the *uncapped* tail the caller captured, and say so in the
    payload so the UI can mark the summary as best-effort.
    """
    for report in _json_candidates(artifacts):
        parsed = parse_json_report(report)
        if parsed is not None:
            return parsed
    return parse_log_summary(log_tail)


def find_json_report(artifacts: Path) -> Path | None:
    """The run's Playwright JSON report, if it wrote one.

    Searched rather than assumed: the env vars that place it are honoured only
    by Playwright versions that know them, and a project config naming its own
    outputFile wins over them.
    """
    for report in _json_candidates(artifacts):
        if parse_json_report(report) is not None:
            return report
    return None


def _json_candidates(artifacts: Path) -> list[Path]:
    """JSON files that could be the report, most likely first."""
    if not artifacts.is_dir():
        return []
    candidates = [artifacts / "results.json"]
    candidates.extend(sorted(artifacts.rglob("*.json")))
    return [c for c in candidates if c.is_file()]


def parse_json_report(path: Path) -> dict | None:
    """Normalize Playwright's JSON reporter output; None when it is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Both keys are always present in a real report, and requiring them keeps
    # unrelated JSON (our own manifest, a hand-written fixture) from parsing as
    # an all-zero result and masking the log fallback.
    if not isinstance(data, dict) or not isinstance(data.get("stats"), dict):
        return None
    if not isinstance(data.get("suites"), list):
        return None

    stats = data["stats"]
    passed = _as_int(stats.get("expected"))
    skipped = _as_int(stats.get("skipped"))
    failed = _as_int(stats.get("unexpected"))
    flaky = _as_int(stats.get("flaky"))
    total = passed + skipped + failed + flaky

    tests: list[dict] = []
    failed_tests: list[dict] = []
    truncated = False
    for spec in _iter_specs(data.get("suites")):
        row = _spec_row(spec)
        if len(tests) < MAX_TEST_ROWS:
            tests.append(row)
        else:
            truncated = True
        if row["status"] in ("failed", "flaky"):
            if len(failed_tests) < MAX_FAILED_TESTS:
                failed_tests.append(
                    {key: row[key] for key in ("title", "file", "line", "error")}
                )
            else:
                truncated = True

    if failed:
        status = "failed"
    elif total:
        status = "passed"
    else:
        status = "unknown"

    return {
        "source": "json",
        "partial": False,
        "status": status,
        "counts": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "flaky": flaky,
            "skipped": skipped,
        },
        "duration_ms": _as_int(stats.get("duration")),
        "failed_tests": failed_tests,
        "tests": tests,
        "truncated": truncated,
    }


def parse_log_summary(log_tail: str) -> dict:
    """Best-effort verdict from raw test-runner output.

    Used when no JSON report exists - a project whose Playwright config predates
    the reporter we ask for, or a runner that died before writing one. Callers
    must pass the uncapped in-process tail, never the stored log.
    """
    text = _ANSI_RE.sub("", log_tail or "")[-LOG_TAIL_CHARS:]
    counts = {"total": 0, "passed": 0, "failed": 0, "flaky": 0, "skipped": 0}
    # Last occurrence wins: the per-test progress lines come before the final
    # summary, and it is the summary that counts.
    for raw_count, label in _COUNT_RE.findall(text):
        counts[_COUNT_ALIASES.get(label, label)] = int(raw_count)
    counts["total"] = counts["passed"] + counts["failed"] + counts["flaky"] + counts["skipped"]

    titles: list[str] = []
    for line in text.splitlines():
        match = _FAILED_CASE_RE.match(line)
        if not match:
            continue
        title = match.group(1).strip(_BOX_CHARS).strip()
        if title and title not in titles:
            titles.append(title)

    if counts["failed"]:
        status = "failed"
    elif counts["passed"] or counts["flaky"]:
        status = "passed"
    else:
        status = "unknown"

    return {
        "source": "log",
        "partial": True,
        "status": status,
        "counts": counts,
        "duration_ms": _duration_ms(text),
        "failed_tests": [
            {"title": title, "file": "", "line": 0, "error": ""}
            for title in titles[:MAX_FAILED_TESTS]
        ],
        "tests": [],
        "truncated": len(titles) > MAX_FAILED_TESTS,
    }


def result_summary(result: dict | None) -> dict | None:
    """Compact projection for history rows - counts and verdict, no test list."""
    if not result:
        return None
    return {
        "status": result.get("status"),
        "source": result.get("source"),
        "partial": bool(result.get("partial")),
        "counts": result.get("counts"),
        "duration_ms": result.get("duration_ms"),
        "failed_count": len(result.get("failed_tests") or []),
    }


def load_result(run) -> dict | None:
    """Parse a run row's stored result; None when there is none to show."""
    raw = getattr(run, "result_json", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_summary_payload(run) -> dict:
    """History-row projection of a run: identity, verdict, compact summary.

    Shared by the run-detail and the per-spec history endpoints so a row means
    the same thing in both. No log, no artifact list, no per-test rows - a
    history of twenty runs must not carry twenty log bodies.
    """
    return {
        "run_id": str(run.run_id),
        "script_id": str(run.script_id),
        "spec_slug": run.spec_slug,
        "status": run.status,
        "exit_code": run.exit_code,
        "triggered_by": run.triggered_by,
        "started_at": _isoformat(run.started_at),
        "completed_at": _isoformat(run.completed_at),
        "created_at": _isoformat(run.created_at),
        "summary": result_summary(load_result(run)),
    }


def _isoformat(value) -> str | None:
    return value.isoformat() if value else None


def _iter_specs(suites, file: str | None = None) -> Iterator[dict]:
    """Depth-first walk of the report's suite tree, inheriting the file path."""
    for node in suites or []:
        if not isinstance(node, dict):
            continue
        node_file = node.get("file") or file
        for spec in node.get("specs") or []:
            if isinstance(spec, dict):
                yield {**spec, "file": spec.get("file") or node_file}
        yield from _iter_specs(node.get("suites"), node_file)


def _spec_row(spec: dict) -> dict:
    # `status` on a report test entry is Playwright's outcome enum
    # (expected/unexpected/flaky/skipped), not the result's run state.
    outcome = "skipped"
    duration = 0
    retries = 0
    error = ""
    for test in spec.get("tests") or []:
        if not isinstance(test, dict):
            continue
        outcome = str(test.get("status") or outcome)
        results = [r for r in (test.get("results") or []) if isinstance(r, dict)]
        retries = max(retries, len(results) - 1)
        for result in results:
            duration += _as_int(result.get("duration"))
            error = error or _result_error(result)
    return {
        "title": _ANSI_RE.sub("", str(spec.get("title") or "")),
        "file": str(spec.get("file") or ""),
        "line": _as_int(spec.get("line")),
        "status": _OUTCOME_STATUS.get(outcome, outcome),
        "duration_ms": duration,
        "retries": retries,
        "error": error,
    }


def _result_error(result: dict) -> str:
    for key in ("errors", "error"):
        value = result.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            message = str(value.get("message") or "")
            if message:
                return _ANSI_RE.sub("", message)[:MAX_ERROR_CHARS]
    return ""


def _duration_ms(text: str) -> int:
    matches = _DURATION_RE.findall(text)
    if not matches:
        return 0
    value, unit = matches[-1]
    return int(float(value) * {"ms": 1, "s": 1000, "m": 60_000}[unit])


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── Artifact manifest ────────────────────────────────────────────────────────


def build_manifest(artifacts: Path, report_path: Path | None = None) -> dict:
    """Gallery index for one run.

    The report's own attachment list comes first because it is the only source
    that knows which test each file belongs to; a disk scan then picks up files
    the report never tracked (a spec's manual `saveAs` download).
    """
    entries: list[dict] = []
    seen: set[str] = set()

    if report_path is not None:
        for entry in _report_attachments(report_path, artifacts):
            if entry["rel_path"] not in seen:
                seen.add(entry["rel_path"])
                entries.append(entry)

    for path in sorted(artifacts.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(artifacts).as_posix()
        if rel_path in seen or _is_internal(rel_path) or _is_hidden(rel_path):
            continue
        seen.add(rel_path)
        entries.append(_entry(path, rel_path, name=path.name, test_title=""))

    if (artifacts / "html-report" / "index.html").is_file():
        entries.append(
            _entry(
                artifacts / "html-report" / "index.html",
                "html-report/index.html",
                name="playwright-report",
                test_title="",
                kind="report",
            )
        )

    truncated = len(entries) > MAX_ARTIFACTS
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "truncated": truncated,
        "artifacts": entries[:MAX_ARTIFACTS],
    }


def write_manifest(artifacts: Path, manifest: dict) -> None:
    """Persist the manifest beside the artifacts it describes."""
    try:
        (artifacts / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        _log.warning("Could not write the artifact manifest under %s", artifacts)


def read_manifest(artifacts: Path) -> dict | None:
    """Manifest for a past run; None when the run produced no artifacts."""
    try:
        data = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def report_index(artifacts: Path) -> Path | None:
    """The run's HTML report entry point, when it wrote one."""
    index = artifacts / "html-report" / "index.html"
    return index if index.is_file() else None


def mime_for_path(path: str | Path) -> str:
    """Content type for a served artifact, by suffix."""
    return _MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "application/octet-stream")


def is_inline_safe(path: str | Path) -> bool:
    """May this file render in place?

    Only stills and video. An artifact's provenance is a generated spec and a
    live page, so `.html`/`.svg`/unknown suffixes are download-only exactly
    like the media router's uploads.
    """
    suffix = Path(path).suffix.lower()
    return suffix in _IMAGE_SUFFIXES or suffix in _VIDEO_SUFFIXES


# Playwright's report reads localStorage at startup for its settings store, and
# a `sandbox`ed document has none: reading it throws, the bundle dies before it
# renders, and the tab is blank. This in-memory stand-in keeps the sandbox -
# i.e. the opaque origin that makes a page built out of test output safe to
# serve - while letting the report boot. Injected into every HTML document
# under the report, because the trace viewer is a document of its own.
_REPORT_STORAGE_SHIM = """<script>(function(){
  function memoryStorage(){
    var data = new Map();
    var methods = {
      getItem: function(k){ k = String(k); return data.has(k) ? data.get(k) : null },
      setItem: function(k, v){ data.set(String(k), String(v)) },
      removeItem: function(k){ data.delete(String(k)) },
      clear: function(){ data.clear() },
      key: function(i){ var keys = Array.from(data.keys()); return i < keys.length ? keys[i] : null }
    };
    return new Proxy(methods, {
      get: function(t, p){
        if (p === 'length') return data.size;
        if (Object.prototype.hasOwnProperty.call(t, p)) return t[p];
        var v = data.get(String(p));
        return v === undefined ? undefined : v;
      },
      set: function(t, p, v){
        if (Object.prototype.hasOwnProperty.call(t, p)) { t[p] = v }
        else { data.set(String(p), String(v)) }
        return true;
      },
      has: function(t, p){ return Object.prototype.hasOwnProperty.call(t, p) || data.has(String(p)) },
      deleteProperty: function(t, p){ data.delete(String(p)); return true },
      ownKeys: function(){ return Array.from(data.keys()) },
      getOwnPropertyDescriptor: function(t, p){
        return data.has(String(p))
          ? { value: data.get(String(p)), enumerable: true, configurable: true, writable: true }
          : undefined;
      }
    });
  }
  ['localStorage', 'sessionStorage'].forEach(function(name){
    try { void window[name] } catch (e) {
      try { Object.defineProperty(window, name, { value: memoryStorage(), configurable: true }) } catch (e2) {}
    }
  });
})()</script>"""


def sandboxed_report_html(source: str) -> str:
    """The report document with the storage shim spliced into its head."""
    marker = "<head>"
    at = source.find(marker)
    if at < 0:
        return _REPORT_STORAGE_SHIM + source
    cut = at + len(marker)
    return source[:cut] + _REPORT_STORAGE_SHIM + source[cut:]


# The report's "View Trace" link opens the trace viewer, which is a separate
# single-page app whose bootstrap waits for a service worker to take control
# (`navigator.serviceWorker.controller || await new Promise(...)` in its entry
# bundle) and whose data layer is that worker intercepting its own URLs. A
# sandboxed document has an opaque origin, and no browser will register a
# worker for one - so the viewer can never render here, whatever shim we add,
# and the alternative (dropping the sandbox for this one document) would hand
# test output the session. The viewer entry gets this notice instead of a
# blank tab: the trace itself is intact, it just opens elsewhere.
def trace_viewer_notice_html() -> str:
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Trace</title>
<style>
  body { margin: 0; padding: 40px; font: 15px/1.7 system-ui, -apple-system, "Segoe UI", sans-serif;
         color: #e6e6e6; background: #1c1c1f; }
  code { background: #2c2c31; padding: 1px 5px; border-radius: 4px; }
  .muted { color: #a0a0a8; }
</style></head>
<body>
  <h1>Trace 需要单独打开</h1>
  <p>本报告运行在沙箱（opaque origin）里，浏览器不会为这类页面注册 Service Worker，
     而 Playwright 的 trace 查看器依赖它加载 trace，因此无法内嵌显示。</p>
  <p class="muted">Open the trace separately: download <code>trace.zip</code> from the run's
     artifacts panel and drop it on <code>trace.playwright.dev</code> (or run
     <code>npx playwright show-trace trace.zip</code>).</p>
</body></html>"""


def _report_attachments(report_path: Path, artifacts: Path) -> list[dict]:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    base = artifacts.resolve()
    entries: list[dict] = []
    for spec in _iter_specs(data.get("suites")):
        title = str(spec.get("title") or "")
        for test in spec.get("tests") or []:
            if not isinstance(test, dict):
                continue
            for result in test.get("results") or []:
                if not isinstance(result, dict):
                    continue
                for attachment in result.get("attachments") or []:
                    if not isinstance(attachment, dict) or not attachment.get("path"):
                        continue
                    resolved = _contained_attachment(str(attachment["path"]), base)
                    if resolved is None:
                        continue
                    path, rel_path = resolved
                    entries.append(
                        _entry(
                            path,
                            rel_path,
                            name=str(attachment.get("name") or path.name),
                            test_title=title,
                            content_type=str(attachment.get("contentType") or ""),
                        )
                    )
    return entries


def _contained_attachment(raw: str, base: Path) -> tuple[Path, str] | None:
    """Resolve a report-declared attachment path to (path, rel_path) inside base.

    Playwright writes absolute paths (testInfo.outputPath), but a hand-written
    `testInfo.attach({path})` can be relative to the runner's cwd - try the run
    directory as a base too. Anything that resolves outside is dropped rather
    than served.
    """
    for candidate in (Path(raw), base / raw, base.parent / raw):
        try:
            resolved = candidate.resolve()
            return resolved, resolved.relative_to(base).as_posix()
        except (OSError, ValueError):
            continue
    return None


def _entry(
    path: Path,
    rel_path: str,
    *,
    name: str,
    test_title: str,
    content_type: str = "",
    kind: str | None = None,
) -> dict:
    suffix = Path(rel_path).suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "name": name,
        "rel_path": rel_path,
        "kind": kind or _kind(suffix, content_type),
        "mime": _MIME_BY_SUFFIX.get(suffix, content_type or "application/octet-stream"),
        "size_bytes": size,
        "test_title": test_title,
    }


def _kind(suffix: str, content_type: str) -> str:
    if content_type.startswith("image/") or suffix in _IMAGE_SUFFIXES:
        return "screenshot"
    if content_type.startswith("video/") or suffix in _VIDEO_SUFFIXES:
        return "video"
    if suffix == ".zip":
        return "trace"
    if suffix in (".html", ".htm"):
        return "report"
    return "file"


def _is_internal(rel_path: str) -> bool:
    """Report internals and our own files are not gallery entries."""
    return rel_path.startswith("html-report/") or rel_path in (
        "results.json",
        "manifest.json",
    )


def _is_hidden(rel_path: str) -> bool:
    """Dotfiles are the runner's bookkeeping (.last-run.json), not artifacts.

    Only the disk scan is filtered - an attachment the report names explicitly
    is shown whatever it is called.
    """
    return any(part.startswith(".") for part in rel_path.split("/"))


# ── Retention ────────────────────────────────────────────────────────────────


def prune_run_artifacts(
    project_fs: str | Path,
    current_run_id: str,
    keep_runs: int = KEEP_RUNS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> None:
    """Trim a project's run directories to the newest few within a size budget.

    Called after a run finishes, and never removes the run that just produced
    artifacts - history is the point of keeping them at all.
    """
    root = runs_root(project_fs)
    try:
        candidates = [
            (entry, entry.stat().st_mtime, _dir_size(entry))
            for entry in root.iterdir()
            if entry.is_dir()
        ]
    except OSError:
        return

    candidates.sort(key=lambda item: item[1], reverse=True)
    total = 0
    for index, (path, _mtime, size) in enumerate(candidates):
        total += size
        if path.name == current_run_id or (index < keep_runs and total <= max_total_bytes):
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            _log.warning("Could not prune script-run artifacts at %s", path)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


# ── Report capability tokens ─────────────────────────────────────────────────


def mint_report_token(run_id: str, ttl_seconds: int = REPORT_TOKEN_TTL_SECONDS) -> str:
    """Capability URL component for the sandboxed HTML report.

    The report page runs on an opaque origin, so it cannot authenticate with the
    session cookie; the token travels in the URL path instead, which also covers
    the report's relative subresources.
    """
    expires = int(time.time()) + ttl_seconds
    return f"{expires}.{_report_signature(run_id, expires)}"


def verify_report_token(run_id: str, token: str) -> bool:
    expires_raw, _, signature = (token or "").partition(".")
    if not expires_raw.isdigit() or not signature:
        return False
    expires = int(expires_raw)
    if expires < time.time():
        return False
    return hmac.compare_digest(_report_signature(run_id, expires), signature)


def _report_signature(run_id: str, expires: int) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    message = f"script-run-report:{run_id}:{expires}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()
