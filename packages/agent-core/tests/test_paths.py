"""Tests for agent_core.paths — canonical path containment helpers."""
from __future__ import annotations

import os

import pytest

from agent_core.paths import PathEscapeError, canonical, is_within, resolve_within


def test_resolve_within_normal(tmp_path):
    base = tmp_path / "proj-a"
    result = resolve_within(base, "sub/dir/file.md")
    assert result == canonical(base / "sub" / "dir" / "file.md")
    assert is_within(base, result)


def test_resolve_within_base_itself(tmp_path):
    assert resolve_within(tmp_path, ".") == canonical(tmp_path)


def test_resolve_within_trailing_separator(tmp_path):
    base = tmp_path / "proj-a"
    result = resolve_within(str(base) + os.sep, "x.md")
    assert result == canonical(base / "x.md")


@pytest.mark.parametrize("rel", ["../proj-b/x.md", "sub/../../proj-b/x.md"])
def test_resolve_within_escape_rejected(tmp_path, rel):
    with pytest.raises(PathEscapeError):
        resolve_within(tmp_path / "proj-a", rel)


@pytest.mark.skipif(os.name != "nt", reason="backslash is a separator only on Windows")
def test_resolve_within_backslash_escape_rejected(tmp_path):
    with pytest.raises(PathEscapeError):
        resolve_within(tmp_path / "proj-a", "..\\proj-b\\x.md")


def test_resolve_within_absolute_rejected(tmp_path):
    target = tmp_path / "proj-b" / "x.md"
    with pytest.raises(PathEscapeError):
        resolve_within(tmp_path / "proj-a", target)


def test_is_within_sibling_prefix_not_confused(tmp_path):
    # "proj-b" shares a string prefix with "proj" — containment must be by path segment.
    base = tmp_path / "proj"
    assert not is_within(base, tmp_path / "proj-b" / "x.md")
    assert is_within(base, tmp_path / "proj" / "x.md")


def test_is_within_base_inclusive(tmp_path):
    assert is_within(tmp_path, tmp_path)


def test_canonical_case_insensitive_simulated(monkeypatch, tmp_path):
    """Simulate Windows normcase semantics on any platform."""
    monkeypatch.setattr(os.path, "normcase", lambda s: s.lower())
    monkeypatch.setattr(os.path, "realpath", lambda s: os.path.abspath(s))
    base = tmp_path / "Proj-A"
    target = str(base / "Sub" / "X.md").upper()
    assert is_within(base, target)


@pytest.mark.skipif(os.name != "nt", reason="drive-letter casing is Windows-specific")
def test_canonical_drive_letter_case(tmp_path):
    base = str(tmp_path)
    swapped = base[0].swapcase() + base[1:]
    assert canonical(base) == canonical(swapped)
