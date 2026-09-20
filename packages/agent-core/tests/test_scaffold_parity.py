"""The test scaffold exists in two places and they must not drift.

`scaffold/tests/*` is what `POST /api/projects` copies into a new workspace;
`_SCAFFOLD_*` in `test_generator` is what `_ensure_test_scaffold` writes when a
file is missing. Two spellings of the same file is a standing invitation to edit
one and forget the other — the config had already drifted once (the embedded
copy carried `acceptDownloads`, the copied one did not), which is why this test
exists rather than a comment asking people to remember.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_core.tools import test_generator as tg

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAFFOLD_DIR = REPO_ROOT / "scaffold" / "tests"

# package.json is left out on purpose: the scaffold copy declares
# `"license": "Apache-2.0"` and the embedded fallback does not, so they differ
# today. Unifying them means deciding whether a user's project workspace should
# inherit the framework's license declaration — not a call to make as a side
# effect of a parity test.
PARITY = {
    "playwright.config.ts": tg._SCAFFOLD_PLAYWRIGHT_CONFIG,
    "auth.setup.ts": tg._SCAFFOLD_AUTH_SETUP,
    "tsconfig.json": tg._SCAFFOLD_TSCONFIG,
    "fixtures/README.md": tg._SCAFFOLD_FIXTURES_README,
}


@pytest.mark.parametrize("rel", sorted(PARITY))
def test_embedded_constant_matches_the_scaffold_file(rel: str):
    path = SCAFFOLD_DIR / rel
    assert path.is_file(), f"{path} is missing — new projects would get a bare tests/ dir"

    assert PARITY[rel] == path.read_text(encoding="utf-8"), (
        f"{rel}: the embedded constant in test_generator.py and scaffold/tests/{rel} "
        "have diverged. A project created through the API and one repaired by "
        "_ensure_test_scaffold would then get different files."
    )


def test_the_config_reads_the_state_file_the_setup_writes():
    """Two files, one path: a mismatch is silent — Playwright would fail every
    case with 'Error reading storage state', which reads as a broken suite."""
    in_config = re.search(
        r"storageState:\s*'([^']+)'", tg._SCAFFOLD_PLAYWRIGHT_CONFIG
    )
    in_setup = re.search(r"STATE_PATH = '([^']+)'", tg._SCAFFOLD_AUTH_SETUP)

    assert in_config and in_setup, (tg._SCAFFOLD_PLAYWRIGHT_CONFIG, tg._SCAFFOLD_AUTH_SETUP)
    assert in_config.group(1) == in_setup.group(1), (
        "the config hands out a session from a file the setup never writes"
    )


def test_the_setup_project_is_a_dependency_of_the_specs():
    """`dependencies: ['setup']` is what makes the one-login-per-run shape work
    under the file-filtered invocation the npm script uses; without it the
    session file would simply be missing."""
    assert "dependencies: ['setup']" in tg._SCAFFOLD_PLAYWRIGHT_CONFIG
    assert "testMatch: /auth\\.setup\\.ts/" in tg._SCAFFOLD_PLAYWRIGHT_CONFIG


def test_the_current_config_is_not_listed_as_an_upgrade_baseline():
    """The baseline set is for configs already superseded. Listing the current
    one would make an upgrade a no-op rewrite and, worse, hide a stale file."""
    assert tg._scaffold_digest(tg._SCAFFOLD_PLAYWRIGHT_CONFIG) not in (
        tg._SCAFFOLD_CONFIG_BASELINES
    )
