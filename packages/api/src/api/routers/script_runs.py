"""Run-scoped results, artifacts and the sandboxed HTML report.

Three views of one finished script run: the normalized result the UI renders as
a summary card, the attachments (screenshots, videos, traces) that were
previously unreachable from the app, and Playwright's own HTML report - which is
a generated page, so it is served under a capability token and a CSP sandbox
instead of on the app origin with the user's session.

Authorization for all three is the same chain the run listing uses: run ->
script -> project, via authorize_project. The report's *subresources* are the
exception - see serve_report.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.script import AutomationScript, ScriptRun
from api.paths import resolve_project_fs_path
from api.services.script_artifacts import (
    artifacts_dir,
    build_manifest,
    is_inline_safe,
    load_result,
    mime_for_path,
    mint_report_token,
    read_manifest,
    report_index,
    run_summary_payload,
    sandboxed_report_html,
    trace_viewer_notice_html,
    verify_report_token,
)

_log = logging.getLogger("agents_universe.script_runs")

router = APIRouter(prefix="/api/scripts/runs", tags=["script-runs"])

# Permissive enough for a uuid4 (and for hand-made ids in fixtures), strict
# enough that nothing here can walk a path - the artifacts route's containment
# check is the real guard, this is just cheap input hygiene.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_REPORT_SUBDIR = "html-report"

_HTML_SUFFIXES = (".html", ".htm")

# The report's own trace viewer, entry document relative to the report root.
_TRACE_VIEWER_ENTRY = "trace/index.html"

# Host characters only: this value is reflected into a CSP header, and a
# header-injection attempt must not be able to widen the policy.
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,255}$")


def _check_run_id(run_id: str) -> None:
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run id")


async def _load_run(db: AsyncSession, run_id: str) -> tuple[ScriptRun, AutomationScript]:
    """The run plus the script anchoring it; project authorization is the
    caller's next step."""
    row = (await db.execute(
        select(ScriptRun, AutomationScript)
        .join(AutomationScript, AutomationScript.script_id == ScriptRun.script_id)
        .where(ScriptRun.run_id == run_id)
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return row[0], row[1]


async def _authorized_run(
    db: AsyncSession, run_id: str, current_user: UserInfo,
) -> tuple[ScriptRun, AutomationScript]:
    _check_run_id(run_id)
    run, script = await _load_run(db, run_id)
    await authorize_project(str(script.project_id), db, current_user)
    return run, script


async def _run_artifacts_dir(db: AsyncSession, script: AutomationScript, run_id: str) -> Path:
    project_fs = await resolve_project_fs_path(str(script.project_id), db)
    return artifacts_dir(project_fs, run_id)


def _contained_file(base: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``base`` or 404.

    The same inline containment the media router uses - resolve() collapses
    `..`, symlinks and Windows path separators before the prefix check, so a
    traversal or an absolute path lands outside and is refused.
    """
    if not relative or "\x00" in relative:
        raise HTTPException(status_code=400, detail="Invalid path")
    try:
        base_resolved = base.resolve()
        target = (base_resolved / relative).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.is_relative_to(base_resolved) or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Everything the result card, the gallery and the log pane need.

    Project access is the gate rather than run ownership (the stricter check
    the log WebSocket applies): a past run's verdict is project knowledge, and
    teammates can already see each other's runs in the run list. The stored log
    is inlined here because opening a socket for a finished run would be
    overkill.
    """
    run, script = await _authorized_run(db, run_id, current_user)

    artifacts = await _run_artifacts_dir(db, script, run_id)
    # A live run has no manifest yet; scanning on demand keeps the gallery
    # filling in while the tests are still going.
    manifest = read_manifest(artifacts)
    if manifest is None and artifacts.is_dir():
        manifest = build_manifest(artifacts)

    report_url = None
    if report_index(artifacts) is not None:
        token = mint_report_token(run_id)
        # No html-report/ segment: the report endpoint treats that directory as
        # its root, so the path is relative to the report document itself - the
        # same way the report's own data/ and trace/ links resolve.
        report_url = f"/api/scripts/runs/{run_id}/report/{token}/index.html"

    payload = run_summary_payload(run)
    payload.update(
        {
            "result": load_result(run),
            "artifacts": (manifest or {}).get("artifacts", []),
            "artifacts_truncated": bool((manifest or {}).get("truncated")),
            "report_url": report_url,
            "log": (run.stdout_log or "") + (run.stderr_log or ""),
        }
    )
    return payload


@router.get("/{run_id}/artifacts/{artifact_path:path}")
async def serve_artifact(
    run_id: str,
    artifact_path: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """One attachment: screenshot, video, trace archive.

    Inline only for stills and video; everything else downloads - the same
    policy as uploaded media, because a trace or a generated page rendered on
    the app origin would run with the user's session.
    """
    run, script = await _authorized_run(db, run_id, current_user)
    artifacts = await _run_artifacts_dir(db, script, run_id)

    if artifact_path.lstrip("/").startswith(f"{_REPORT_SUBDIR}/"):
        # Report internals belong to the sandboxed report endpoint; serving
        # them here would hand out the same HTML without the CSP.
        raise HTTPException(status_code=404, detail="File not found")
    target = _contained_file(artifacts, artifact_path)

    media_type = mime_for_path(target)
    if is_inline_safe(target):
        return FileResponse(
            str(target),
            media_type=media_type,
            filename=target.name,
            content_disposition_type="inline",
            headers={"X-Content-Type-Options": "nosniff"},
        )
    return FileResponse(
        str(target),
        media_type=media_type,
        filename=target.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _report_csp(request: Request) -> str:
    """Policy for the report page and every subresource it pulls.

    Two jobs. `sandbox` (without allow-same-origin) drops the page into an
    opaque origin: no cookies, no localStorage, and no credentialed call back
    into /api, so a spec that injects markup into test output cannot act as the
    user. The source list then has to name our origin explicitly - in an opaque
    origin `'self'` matches nothing at all, and the report's assets are
    relative URLs pointing right back here. Deliberately no third-party hosts:
    the trace viewer's "open on trace.playwright.dev" hand-off is not available
    here, download the trace from the gallery and open it there instead.
    """
    host = request.headers.get("host") or ""
    origin = f"{request.url.scheme}://{host}" if _HOST_RE.match(host) else ""
    sources = f"{origin} " if origin else ""
    return (
        "sandbox allow-scripts allow-downloads allow-popups; "
        f"script-src 'unsafe-inline' 'unsafe-eval' {sources}blob:; "
        f"style-src 'unsafe-inline' {sources}blob:; "
        f"img-src {sources}data: blob:; "
        f"media-src {sources}data: blob:; "
        f"font-src {sources}data:; "
        f"connect-src {sources}data: blob:; "
        f"worker-src {sources}blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
    )


@router.get("/{run_id}/report/{token}/{report_path:path}")
async def serve_report(
    run_id: str,
    token: str,
    report_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Playwright's HTML report, sandboxed.

    No session dependency on purpose: the report runs in an opaque origin, so
    its subresource requests carry no cookie and could never authenticate. The
    URL itself is the capability - an HMAC over (run_id, expiry), minted only
    by get_run after a project-access check, valid for an hour, and useless for
    any other run.
    """
    _check_run_id(run_id)
    if not verify_report_token(run_id, token):
        # Same answer for a forged signature and an expired one - there is
        # nothing here worth telling an attacker apart.
        raise HTTPException(status_code=403, detail="Report link expired or invalid")

    run, script = await _load_run(db, run_id)
    artifacts = await _run_artifacts_dir(db, script, run_id)
    report_dir = artifacts / _REPORT_SUBDIR
    target = _contained_file(report_dir, report_path or "index.html")

    headers = _report_headers(request)
    if target == (report_dir / _TRACE_VIEWER_ENTRY).resolve():
        # Playwright's "View Trace" link. Its viewer cannot run under the
        # sandbox (see trace_viewer_notice_html), and a blank tab reads as a
        # broken app, so it is answered with an explanation instead.
        return Response(
            content=trace_viewer_notice_html(), media_type="text/html", headers=headers,
        )
    if target.suffix.lower() in _HTML_SUFFIXES:
        # Documents get the storage shim: a sandboxed page cannot read the
        # report's settings out of localStorage, and the unpatched bundle dies
        # on that before rendering anything.
        try:
            html = sandboxed_report_html(target.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
        else:
            return Response(content=html, media_type="text/html", headers=headers)

    return FileResponse(
        str(target),
        media_type=mime_for_path(target),
        filename=target.name,
        content_disposition_type="inline",
        headers=headers,
    )


def _report_headers(request: Request) -> dict[str, str]:
    return {
        "Content-Security-Policy": _report_csp(request),
        # The sandboxed page is cross-origin to us, so its fetches are CORS
        # requests. Credentials are never sent (opaque origin), which is what
        # makes the wildcard safe here.
        "Access-Control-Allow-Origin": "*",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
