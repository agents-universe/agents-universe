"""Shared path-safety helpers for cross-project isolation.

Zero-dependency module imported by both agent-core and the api package.
All comparisons go through canonical() so Windows drive-letter casing and
8.3 short names cannot fool a bare resolve()-based check.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Knowledge slug validation, shared by agent_core.tools.knowledge_rw and the
# API knowledge router. Hierarchical slugs with uppercase letters and dots
# (e.g. "API-Gateway", "notes.v2") are legitimate — the indexer derives slugs
# verbatim from filenames. Rejected: "." / ".." segments anywhere (traversal),
# backslashes (Windows separator variants), and leading non-alphanumerics.
KNOWLEDGE_SLUG_RE = re.compile(r"^(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9][A-Za-z0-9_./-]*$")


class PathEscapeError(ValueError):
    """Raised when a resolved path escapes its allowed base directory."""


def canonical(p: str | Path) -> Path:
    """Return a cross-platform comparable canonical form of *p*.

    realpath resolves symlinks and normalizes the path; normcase folds
    drive-letter/path casing on Windows so comparisons are reliable.
    """
    return Path(os.path.normcase(os.path.realpath(os.fspath(p))))


def is_within(base: str | Path, target: str | Path) -> bool:
    """True if canonical(target) lies inside canonical(base), base included."""
    base_c = canonical(base)
    target_c = canonical(target)
    return target_c == base_c or base_c in target_c.parents


def resolve_within(base: str | Path, rel: str | Path) -> Path:
    """Join *rel* onto *base* and require the result to stay within *base*.

    Raises PathEscapeError if the normalized path escapes the base
    (e.g. via ``..`` segments or an absolute *rel*).
    """
    rel_s = os.fspath(rel)
    # Drive-letter ("C:\evil") / UNC ("\\server", "//server") prefixes are
    # absolute on Windows but a mere relative directory on POSIX — an
    # attempted escape would silently resolve into a folder named "C:" and
    # come back 404 instead of 400. Reject the prefix on every host so the
    # behavior does not depend on the OS.
    if re.match(r"^[A-Za-z]:[\\/]", rel_s) or rel_s.startswith(("\\", "//")):
        raise PathEscapeError(f"Path escapes allowed base {base}: {rel!r}")
    base_c = canonical(base)
    candidate = canonical(base_c / rel)
    if candidate != base_c and base_c not in candidate.parents:
        raise PathEscapeError(f"Path escapes allowed base {base_c}: {rel!r}")
    return candidate
