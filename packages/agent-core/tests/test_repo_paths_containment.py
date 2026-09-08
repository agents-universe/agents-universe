"""`repos/<name>` must resolve inside the project workspace.

The `repository_path` branch and git_repo._op_clone both contain the resolved
path; the `repository` branch did not — a symlink at repos/<name> made every
git operation (pull/commit/push) read and write a foreign checkout.
"""
from __future__ import annotations

from pathlib import Path

from agent_core.tools._repo_paths import resolve_repo_path


def _patch_resolve(monkeypatch, escaped_name: str, target: Path) -> None:
    """Make Path.resolve() report *escaped_name* as pointing at *target*.

    Stands in for a symlink/junction, which Windows refuses to create
    without elevation.
    """
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if self.name == escaped_name:
            return target
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)


def test_repository_escaping_workspace_is_blocked(monkeypatch, tmp_path):
    base = tmp_path / "proj"
    (base / "repos").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _patch_resolve(monkeypatch, "escape", outside)

    path, error = resolve_repo_path({"repository": "org/escape"}, str(base))

    assert path is None
    assert "traversal" in error["error"].lower()


def test_single_clone_fallback_is_contained(monkeypatch, tmp_path):
    """The lone-clone convenience path must not bypass the same check."""
    base = tmp_path / "proj"
    (base / "repos").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _patch_resolve(monkeypatch, "escape", outside)

    path, error = resolve_repo_path({}, str(base), available=["escape"])

    assert path is None
    assert "traversal" in error["error"].lower()


def test_normal_repository_still_resolves(tmp_path):
    base = tmp_path / "proj"
    (base / "repos" / "demo").mkdir(parents=True)

    path, error = resolve_repo_path({"repository": "org/demo"}, str(base))

    assert error is None
    assert path == (base / "repos" / "demo").resolve()
