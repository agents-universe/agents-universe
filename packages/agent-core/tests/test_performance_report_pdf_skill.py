"""End-to-end test for the performance report Markdown -> PDF converter.

Extracts the Converter Script from ``agents/skills/performance/perf-report-pdf.md``
(the single fenced python block the agent pastes into ``code_executor``), runs it
against a fixture stress-test report with a real evidence PNG, and asserts the
produced PDF via pypdf: page count, section-heading extraction, the p95 token,
CJK text (CID font path), embedded image XObject, and a TOC entry.
"""
from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = REPO_ROOT / "agents" / "skills" / "performance" / "perf-report-pdf.md"

FIXTURE_MD = """# Performance Test Report - Order API

Classification: CONFIDENTIAL
Project: Order Platform
Environment tier: dev
Test date: 2026-11-24
Report version: 1.0
Generator: Python asyncio+httpx via script_writer

## Executive Summary

SLA: met on read paths, missed on checkout write. Headline p95 480 ms against
the 500 ms target; error rate 0.4%. 结论：读接口达标，写接口在高并发下出现排队。

## Scope & Methodology

Targets: https://app-dev.example.com (declared scope, ceiling 50 VUs).
Methodology stance: black-box API load, knowledge-informed design.
Channel: script_writer executor, profiles bounded to 240 s.

## Test Profile

Profile: baseline - step ramp, 30 s ramp to 50 VUs, 120 s steady, 30 s cooldown.
Data set: self-created order fixtures (create-once recipe).

## SLA & Targets

| Endpoint | p95 target | Error rate | Status |
|---|---|---|---|
| GET /api/orders | 500 ms | <=1% | confirmed |
| POST /api/orders | 500 ms | <=1% | proposal |

## Results

| Endpoint | p50 | p95 | p99 | RPS | Errors |
|---|---|---|---|---|---|
| GET /api/orders | 45 ms | 180 ms | 320 ms | 412 | 0.1% |
| POST /api/orders | 90 ms | 480 ms | 910 ms | 188 | 0.8% |

Result: GET passes the confirmed SLA; POST p95 480 ms is inside the proposal
target but the p99 tail (910 ms) shows queueing above 40 VUs.

Evidence figures:
  ![](performance/chart.png)

## Bottleneck Analysis

Bottleneck: POST latency grows super-linearly above 40 VUs while GET stays flat -
evidence: performance/results/order-api-baseline-20261124.json.

## Recommendations

Recommendation: add connection pooling on the checkout write path (quick win);
re-run the baseline profile after the change and compare p95 within noise bands.

## Appendix

Evidence: performance/results/order-api-baseline-20261124.json
Limitation: single segment; soak-lite not run.
Script: load-order-api-baseline (script_writer run).
"""


def _extract_script() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    m = re.search(r"##\s+Converter Script\s*\n+```python\n(.*?)```", text, re.DOTALL)
    assert m, "Converter Script block not found in perf-report-pdf skill"
    return m.group(1)


def _make_fixture(tmp_path: Path) -> tuple[Path, Path]:
    from PIL import Image

    md_path = tmp_path / "performance" / "perf-report-20261124.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(FIXTURE_MD, encoding="utf-8")

    img = Image.new("RGB", (60, 40), (30, 90, 180))
    img.save(tmp_path / "performance" / "chart.png")
    return md_path, tmp_path / "performance" / "perf-report-20261124.pdf"


def _patch_config(script: str, md_path: Path, out_path: Path) -> str:
    # Lambda replacements: a plain re.sub template would interpret backslashes
    # in Windows paths as escape sequences and corrupt the repr.
    script, n1 = re.subn(
        r"^MD_PATH = .*$", lambda _: "MD_PATH = %r" % str(md_path), script,
        count=1, flags=re.M,
    )
    script, n2 = re.subn(
        r"^OUT_PATH = .*$", lambda _: "OUT_PATH = %r" % str(out_path), script,
        count=1, flags=re.M,
    )
    assert n1 == 1 and n2 == 1, "CONFIG assignment lines not found for substitution"
    return script


def _has_image_xobject(page) -> bool:
    resources = page.get("/Resources")
    if not resources:
        return False
    xobj = resources.get("/XObject")
    if not xobj:
        return False
    xobj = xobj.get_object()
    return any(o.get_object().get("/Subtype") == "/Image" for o in xobj.values())


def test_converter_renders_fixture_report_to_verified_pdf(tmp_path, monkeypatch, capsys):
    md_path, out_path = _make_fixture(tmp_path)
    script = _patch_config(_extract_script(), md_path, out_path)
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))

    exec(compile(script, "<perf-report-pdf>", "exec"), {"__name__": "__main__"})

    captured = capsys.readouterr()
    assert "pages:" in captured.out, captured.out
    assert "pdf written:" in captured.out, captured.out
    # Evidence figure resolved — the converter must not degrade here.
    assert "WARN: evidence missing" not in captured.out, captured.out

    assert out_path.is_file() and out_path.stat().st_size > 0
    reader = PdfReader(str(out_path))
    # cover + TOC + at least one content page
    assert len(reader.pages) >= 4, f"expected >=4 pages, got {len(reader.pages)}"

    extracted = "\n".join((p.extract_text() or "") for p in reader.pages)
    # Section headings survive extraction (the converter asserts this itself,
    # but re-check here so a weakened script fails this test too).
    assert "Executive Summary" in extracted  # TOC entry
    assert "SLA & Targets" in extracted
    assert "Bottleneck Analysis" in extracted
    # The headline percentile token must be present.
    assert "p95" in extracted
    # CJK body text exercises the STSong-Light CID path end to end. Whitespace
    # is stripped first: extraction may wrap between two CJK glyphs ("结\n论").
    assert "结论" in re.sub(r"\s+", "", extracted), "CJK text missing from PDF extraction"
    # Evidence chart embedded as an image XObject.
    assert any(_has_image_xobject(p) for p in reader.pages), "no embedded evidence image"


def test_converter_script_is_single_block_and_fence_free():
    """The skill must ship exactly one self-contained script block.

    A stray triple backtick inside the script would split the fenced block in
    the skill markdown, breaking both the agent's copy-paste and extraction.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    assert len(blocks) == 1
    assert blocks[0].count("```") == 0
