"""Guards for the `template_words` baseline on knowledge templates.

`content_depth` subtracts `template_words` from the file's meaningful words, so
a baseline that no longer matches the body silently reports missing content as
present (body grew — baseline too low) or real content as missing (body shrank
— baseline too high). The scaffolding is otherwise invisible: a template's own
prompt text counts as project content the moment a placeholder form is one that
`scorer._PLACEHOLDER_PATTERNS` does not recognize.

Policy, matching how templates have been maintained: every template named here
must be EXACTLY calibrated (baseline == the body's measured meaningful words,
so an unfilled file scores content_depth 0 and any added content still counts).
Templates outside this list predate the policy and are known-inconsistent —
several declare no baseline at all, because the text they ship is real guidance
rather than scaffolding. Extend the list as templates are recalibrated; do not
silently add a new template to the repo without calibrating it.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from agent_core.knowledge import scorer

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = REPO_ROOT / "knowledge" / "_template"

# Templates held to exact calibration. `test-patterns` is here because it is the
# file QA design leans on and its baseline was re-derived after a Data Setup
# edit; `test-data-setup` is the recipe file added with the QA speedup work.
CALIBRATED = ["test-data-setup", "test-patterns"]

# `_EXPECTED_WORDS[category]` drives the curve; an unknown category falls back to
# a default, so a template's own category must be one the scorer knows.
KNOWN_CATEGORIES = set(scorer._EXPECTED_WORDS) | {"system"}


def _measured_words(path: Path) -> int:
    body = frontmatter.loads(path.read_text(encoding="utf-8")).content
    return sum(
        len(line.strip().split())
        for line in body.splitlines()
        if scorer._is_line_meaningful(line.strip())
    )


def _declared_words(path: Path) -> int:
    return int(frontmatter.loads(path.read_text(encoding="utf-8")).metadata.get("template_words") or 0)


@pytest.mark.parametrize("stem", CALIBRATED)
def test_calibrated_template_scores_empty_until_filled(stem: str):
    path = TEMPLATE_DIR / f"{stem}.md"
    assert path.exists(), f"{path} is listed as calibrated but does not exist"

    components = scorer.compute_completeness(str(path), days_since_update=0.0)

    assert components.content_depth == 0, (
        f"{stem}.md: an unfilled template scores content_depth "
        f"{components.content_depth:.1f}. template_words is {_declared_words(path)} but the body "
        f"measures {_measured_words(path)} meaningful words."
    )


@pytest.mark.parametrize("stem", CALIBRATED)
def test_calibrated_template_words_is_exact(stem: str):
    path = TEMPLATE_DIR / f"{stem}.md"
    declared, measured = _declared_words(path), _measured_words(path)

    assert declared == measured, (
        f"{stem}.md: template_words must equal the body's meaningful word count so the "
        f"baseline cancels exactly (declared {declared}, measured {measured})"
    )


@pytest.mark.parametrize("stem", CALIBRATED)
def test_calibrated_template_declares_a_scorable_category(stem: str):
    metadata = frontmatter.loads((TEMPLATE_DIR / f"{stem}.md").read_text(encoding="utf-8")).metadata

    assert metadata.get("slug"), f"{stem}.md: missing slug"
    assert metadata.get("category") in KNOWN_CATEGORIES, (
        f"{stem}.md: category {metadata.get('category')!r} has no word target in "
        f"scorer._EXPECTED_WORDS — its content_depth would use the default curve"
    )


def test_added_content_still_counts():
    """The baseline cancels the scaffolding only — real content must raise the
    score, so an over-large baseline cannot mask an unfilled project."""
    path = TEMPLATE_DIR / "test-data-setup.md"
    filled = (
        path.read_text(encoding="utf-8")
        + "\n\n### setup-1 — a verified customer account\n\n"
        "Create the account through the test-support API, then read it back once to confirm it "
        "exists before the case that depends on it runs.\n"
    )

    components = scorer.compute_completeness(str(path), days_since_update=0.0, content=filled)

    assert components.content_depth > 0
    assert components.final_score > 0
