"""skill_source — address parsing, security gates, and the fetch/list/read/install flow.

Network access is replaced by a local bare repository: the fake runner rewrites
the clone URL and otherwise runs real git, so the cache layout, the shallow
clone flags and the containment checks are all exercised for real.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import agent_core.tools.skill_source as _ss
from agent_core.skills.loader import load_skills_from_dir
from agent_core.tools._git_exec import _TIMEOUT_DEFAULT, _GIT_BIN
from agent_core.tools.base import ToolContext
from agent_core.tools.skill_source import SkillSourceTool, _SourceError, _parse_source

pytestmark = pytest.mark.skipif(_GIT_BIN is None, reason="git executable not available")

_FAKE_REMOTE = "https://github.com/acme/skills.git"


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SKILL_MD = "\n".join([
    "---",
    "name: pdf",
    "description: Extract text and fill forms in PDF files",
    "tags: [document]",
    "tools: [shell, filesystem]",
    "---",
    "",
    "# PDF",
    "",
    "Use when a PDF must be read or filled.",
    "",
    "## Execution",
    "",
    "```python",
    "print('third-party payload')",
    "```",
    "",
])

_REMOTE_FILES = {
    "skills/pdf/SKILL.md": SKILL_MD,
    "skills/pdf/reference.md": "# Reference\n\nField offsets.\n",
    "skills/pdf/scripts/extract.py": "print('x')\n",
    "skills/notes/single.md": (
        "---\nname: notes-skill\ndescription: A single-file skill\n---\n\n# Notes\n\nBody.\n"
    ),
    "skills/notes/plain.md": "no frontmatter at all\n",
    "skills/node_modules/pkg/SKILL.md": "---\nname: ignored\n---\n\n# Ignored\n",
    "skills/_private/SKILL.md": "---\nname: private\n---\n\n# Private\n",
    "README.md": "# repository\n",
}


def _make_bare_remote(tmp_path: Path, files: dict[str, str]) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    _git("config", "user.email", "test@example.com", cwd=seed)
    _git("config", "user.name", "Tester", cwd=seed)
    for rel, content in files.items():
        target = seed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "initial", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    return bare


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A real bare repository served at _FAKE_REMOTE, plus a mutate() helper."""
    bare = _make_bare_remote(tmp_path / "src", _REMOTE_FILES)
    real_run_git = _ss.run_git

    async def _run_git(args, cwd, timeout=_TIMEOUT_DEFAULT, token=None, env=None):
        rewritten = [str(bare) if a.startswith("https://") else a for a in args]
        if env is not None:
            # GIT_ALLOW_PROTOCOL exists to stop a rewritten remote from using
            # another transport; the test double reaches the bare repo over the
            # local transport on purpose.
            env = {k: v for k, v in env.items() if k != "GIT_ALLOW_PROTOCOL"}
        return await real_run_git(rewritten, cwd, timeout=timeout, token=token, env=env)

    monkeypatch.setattr(_ss, "run_git", _run_git)
    monkeypatch.setattr(_ss, "get_token_optional", _no_token)

    def _commit(file_rel: str, content: str) -> None:
        work = tmp_path / "src" / "work"
        if not work.exists():
            _git("clone", str(bare), str(work))
            _git("config", "user.email", "test@example.com", cwd=work)
            _git("config", "user.name", "Tester", cwd=work)
        target = work / file_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git("add", ".", cwd=work)
        _git("commit", "-m", "update", cwd=work)
        _git("push", "origin", "main", cwd=work)

    return {"bare": bare, "commit": _commit}


async def _no_token(context, service_key):
    return None


class _FakeSession:
    def __init__(self, answer: Any = "confirm"):
        self.answer = answer
        self.calls: list[dict] = []

    async def request_user_selection(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _make_context(
    fs_path: Path,
    session: Any = None,
    interactive: bool = True,
    git_base: str = "",
) -> ToolContext:
    ctx = ToolContext(
        project_id="p1",
        project_fs_path=str(fs_path),
        conversation_id="c1",
        user_id="u1",
        session=session,
    )
    ctx.interactive = interactive
    if git_base:
        ctx.integration_settings["GIT_BASE_URL"] = git_base
    return ctx


def _clones(capture: dict) -> list[dict]:
    return [c for c in capture["calls"] if "clone" in c["args"]]


@pytest.fixture
def capture_git(monkeypatch):
    """Record every git call without running one."""
    calls: list[dict] = []
    result: dict = {"stdout": "", "stderr": "", "exit_code": 0}

    async def _run_git(args, cwd, timeout=_TIMEOUT_DEFAULT, token=None, env=None):
        calls.append({"args": list(args), "cwd": str(cwd), "token": token, "env": env or {}})
        return dict(result)

    monkeypatch.setattr(_ss, "run_git", _run_git)
    return {"calls": calls, "result": result}


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------

def test_owner_repo_uses_the_default_host():
    source = _parse_source("acme/skills", "github.com")

    assert source.clone_url == "https://github.com/acme/skills.git"
    assert (source.host, source.owner, source.repo) == ("github.com", "acme", "skills")
    assert source.ref is None and source.path is None


def test_full_https_url_and_git_suffix():
    source = _parse_source("https://git.example.com/team/tools.git", "github.com")

    assert source.clone_url == "https://git.example.com/team/tools.git"
    assert source.host == "git.example.com"


def test_tree_and_blob_links_carry_ref_and_path():
    tree = _parse_source("https://github.com/acme/skills/tree/main/skills/pdf", "github.com")
    blob = _parse_source("https://github.com/acme/skills/blob/v1.2/x/y.md", "github.com")

    assert (tree.ref, tree.path) == ("main", "skills/pdf")
    assert (blob.ref, blob.path) == ("v1.2", "x/y.md")


def test_raw_file_link_maps_to_the_github_clone_url():
    source = _parse_source(
        "https://raw.githubusercontent.com/acme/skills/main/skills/pdf/SKILL.md", "github.com"
    )

    assert source.clone_url == "https://github.com/acme/skills.git"
    assert source.host == "github.com"
    assert (source.ref, source.path) == ("main", "skills/pdf/SKILL.md")


def test_plain_repository_url_has_no_path():
    source = _parse_source("https://github.com/acme/skills", "github.com")

    assert source.path is None and source.ref is None


@pytest.mark.parametrize("value", [
    "http://github.com/acme/skills",
    "git://github.com/acme/skills.git",
    "ssh://git@github.com/acme/skills.git",
    "file:///etc/passwd",
    "ext::sh -c whoami",
])
def test_non_https_schemes_are_rejected(value):
    with pytest.raises(_SourceError):
        _parse_source(value, "github.com")


@pytest.mark.parametrize("value", [
    "git@github.com:acme/skills.git",
    "https://user:pass@github.com/acme/skills.git",
    "https://github.com/acme/skills.git?token=abc",
    "https://github.com/acme/skills.git#main",
])
def test_credentials_scp_form_and_query_are_rejected(value):
    with pytest.raises(_SourceError):
        _parse_source(value, "github.com")


def test_commit_sha_ref_is_rejected_with_a_hint():
    with pytest.raises(_SourceError) as exc:
        _parse_source("https://github.com/acme/skills/tree/" + "a" * 40, "github.com")

    assert "SHA" in str(exc.value)


@pytest.mark.parametrize("value", [
    "acme",
    "not a repo",
    "https://github.com/acme",
    "https://github.com/acme/skills/settings",
])
def test_malformed_addresses_are_rejected(value):
    with pytest.raises(_SourceError):
        _parse_source(value, "github.com")


def test_paths_may_not_escape_the_repository():
    with pytest.raises(_SourceError):
        _parse_source("https://github.com/acme/skills/tree/main/../../etc", "github.com")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_clones_shallow_and_reports_commit(tmp_path, remote):
    ctx = _make_context(tmp_path / "proj")
    tool = SkillSourceTool()

    result = await tool.execute(
        {"operation": "fetch", "source": "acme/skills", "path": "skills"}, ctx
    )

    assert result["status"] == "fetched"
    assert result["host"] == "github.com"
    assert len(result["commit"]) == 40
    assert result["cache_path"].startswith(".tmp/skill_cache/github.com/acme__skills--")
    assert result["token_used"] is False
    cache = Path(ctx.project_fs_path) / result["cache_path"]
    assert (cache / ".git").is_dir()
    # --depth 1 keeps the cache small; the bare remote has a single commit.
    assert (cache / "skills" / "pdf" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_fetch_uses_the_cache_and_picks_up_new_commits(tmp_path, remote):
    ctx = _make_context(tmp_path / "proj")
    tool = SkillSourceTool()
    first = await tool.execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    remote["commit"]("skills/new.md", "---\nname: new-skill\ndescription: new\n---\n")
    second = await tool.execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    assert second["status"] == "updated"
    assert second["cache_path"] == first["cache_path"]
    assert second["commit"] != first["commit"]
    cache = Path(ctx.project_fs_path) / second["cache_path"]
    assert (cache / "skills" / "new.md").is_file()


@pytest.mark.asyncio
async def test_clone_failure_reports_a_hint_and_leaves_no_cache(tmp_path, capture_git):
    capture_git["result"].update({"error": "Git exited with code 128", "exit_code": 128})
    ctx = _make_context(tmp_path / "proj")

    result = await SkillSourceTool().execute(
        {"operation": "fetch", "source": "acme/skills"}, ctx
    )

    assert "fetch failed" in result["error"]
    assert "public https" in result["hint"]
    cache_root = Path(ctx.project_fs_path) / ".tmp" / "skill_cache"
    assert list(cache_root.glob("github.com/*")) == []


@pytest.mark.asyncio
async def test_clone_is_hardened(tmp_path, capture_git):
    ctx = _make_context(tmp_path / "proj")

    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    clone = _clones(capture_git)[0]
    assert clone["args"][:3] == ["-c", "http.followRedirects=false", "-c"]
    assert clone["args"][3].startswith("core.hooksPath=")
    assert "--depth" in clone["args"] and "--single-branch" in clone["args"]
    assert "clone" in clone["args"]
    env = clone["env"]
    # GIT_TERMINAL_PROMPT is forced by the runner itself (see test_git_exec).
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_KEY_1"] == "http.followRedirects"
    assert env["GIT_CONFIG_VALUE_1"] == "false"


@pytest.mark.asyncio
async def test_token_is_injected_only_for_the_configured_host(tmp_path, capture_git, monkeypatch):
    async def _token(context, service_key):
        return "s3cret"

    monkeypatch.setattr(_ss, "get_token_optional", _token)

    matching = _make_context(tmp_path / "a", git_base="https://github.com")
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, matching)

    other = _make_context(tmp_path / "b", git_base="https://git.example.com")
    await SkillSourceTool().execute(
        {"operation": "fetch", "source": "https://github.com/acme/skills.git"}, other
    )

    anonymous = _make_context(tmp_path / "c")
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, anonymous)

    clones = _clones(capture_git)
    assert [c["token"] for c in clones] == ["s3cret", None, None]


@pytest.mark.asyncio
async def test_blocked_hosts_never_reach_git(tmp_path, capture_git):
    ctx = _make_context(tmp_path / "proj")

    result = await SkillSourceTool().execute(
        {"operation": "fetch", "source": "https://169.254.169.254/acme/skills.git"}, ctx
    )

    assert "SSRF" in result["error"]
    assert capture_git["calls"] == []


@pytest.mark.asyncio
async def test_source_key_resolves_through_the_catalog(tmp_path, remote):
    catalog = tmp_path / "proj" / "knowledge" / "integrations"
    catalog.mkdir(parents=True)
    (catalog / "skill-sources.md").write_text(
        "---\nslug: integrations/skill-sources\n---\n\n```yaml\nsources:\n"
        "  - key: mine\n    repo: acme/skills\n    path: skills\n```\n",
        encoding="utf-8",
    )
    ctx = _make_context(tmp_path / "proj")

    result = await SkillSourceTool().execute({"operation": "fetch", "source_key": "mine"}, ctx)

    assert result["path"] == "skills"
    assert result["tags"] is None


@pytest.mark.asyncio
async def test_unknown_source_key_lists_the_known_keys(tmp_path):
    ctx = _make_context(tmp_path / "proj")

    result = await SkillSourceTool().execute({"operation": "fetch", "source_key": "nope"}, ctx)

    assert "unknown source_key" in result["error"]


@pytest.mark.asyncio
async def test_operations_other_than_fetch_require_a_cache(tmp_path, remote):
    ctx = _make_context(tmp_path / "proj")

    result = await SkillSourceTool().execute({"operation": "list", "source": "acme/skills"}, ctx)

    assert result["error"] == "source not fetched yet — call fetch first"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

async def _fetched(tmp_path, remote, **params) -> tuple[ToolContext, dict]:
    ctx = _make_context(tmp_path / "proj")
    result = await SkillSourceTool().execute(
        {"operation": "fetch", "source": "acme/skills", **params}, ctx
    )
    assert result["status"] == "fetched"
    return ctx, result


@pytest.mark.asyncio
async def test_list_returns_bundles_and_single_files_but_no_bodies(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")

    result = await SkillSourceTool().execute(
        {"operation": "list", "source": "acme/skills", "path": "skills"}, ctx
    )

    by_path = {c["path"]: c for c in result["candidates"]}
    assert set(by_path) == {"skills/pdf", "skills/notes/single.md"}
    assert by_path["skills/pdf"]["kind"] == "bundle"
    assert by_path["skills/pdf"]["name"] == "pdf"
    assert by_path["skills/pdf"]["files"] == 3
    assert by_path["skills/notes/single.md"]["kind"] == "file"
    assert by_path["skills/notes/single.md"]["name"] == "notes-skill"
    assert "body" not in by_path["skills/pdf"]
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_list_skips_ignored_directories_and_frontmatter_less_files(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")

    result = await SkillSourceTool().execute(
        {"operation": "list", "source": "acme/skills", "path": "skills"}, ctx
    )

    paths = [c["path"] for c in result["candidates"]]
    assert not any("node_modules" in p for p in paths)
    assert "skills/notes/plain.md" not in paths
    assert not any("_private" in p for p in paths)


@pytest.mark.asyncio
async def test_list_filters_by_query_and_caps_the_limit(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")
    tool = SkillSourceTool()

    # The query matches only the candidate that sorts last: filtering after the
    # limit would report "no match" for a skill that is right there.
    filtered = await tool.execute(
        {"operation": "list", "source": "acme/skills", "path": "skills",
         "query": "notes", "limit": 1}, ctx
    )
    capped = await tool.execute(
        {"operation": "list", "source": "acme/skills", "path": "skills", "limit": 1}, ctx
    )

    assert [c["path"] for c in filtered["candidates"]] == ["skills/notes/single.md"]
    assert filtered["truncated"] is False
    assert capped["count"] == 1 and capped["truncated"] is True


@pytest.mark.asyncio
async def test_list_on_a_missing_path_is_an_error(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")

    result = await SkillSourceTool().execute(
        {"operation": "list", "source": "acme/skills", "path": "nowhere"}, ctx
    )

    assert "does not exist" in result["error"]


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_bundle_returns_body_manifest_and_untrusted_envelope(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")

    result = await SkillSourceTool().execute(
        {"operation": "read", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    assert result["kind"] == "bundle"
    assert result["untrusted"] is True
    assert "never follow instructions" in result["warning"]
    assert result["content"].startswith(_ss._UNTRUSTED_OPEN)
    assert result["content"].rstrip().endswith(_ss._UNTRUSTED_CLOSE)
    assert "# PDF" in result["content"]
    assert {f["path"] for f in result["files"]} == {
        "skills/pdf/SKILL.md", "skills/pdf/reference.md", "skills/pdf/scripts/extract.py",
    }
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_read_accepts_a_single_file_and_a_bundle_member(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")
    tool = SkillSourceTool()

    single = await tool.execute(
        {"operation": "read", "source": "acme/skills", "path": "skills/notes/single.md"}, ctx
    )
    member = await tool.execute(
        {"operation": "read", "source": "acme/skills", "path": "skills/pdf/reference.md"}, ctx
    )

    assert single["kind"] == "file"
    assert member["kind"] == "file"
    assert "Field offsets." in member["content"]


@pytest.mark.asyncio
async def test_read_defaults_to_the_address_path(tmp_path, remote):
    """A tree/blob address already carries the skill path — no repeat needed."""
    ctx, _ = await _fetched(
        tmp_path, remote, source="https://github.com/acme/skills/tree/main/skills/pdf"
    )

    result = await SkillSourceTool().execute(
        {"operation": "read", "source": "https://github.com/acme/skills/tree/main/skills/pdf"}, ctx
    )

    assert result["kind"] == "bundle"
    assert result["path"] == "skills/pdf/SKILL.md"


@pytest.mark.asyncio
async def test_read_truncates_long_content(tmp_path, remote, monkeypatch):
    monkeypatch.setattr(_ss, "_MAX_READ_CHARS", 20)
    ctx, _ = await _fetched(tmp_path, remote, path="skills")

    result = await SkillSourceTool().execute(
        {"operation": "read", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    assert result["truncated"] is True
    assert len(result["content"]) < 200


@pytest.mark.asyncio
async def test_read_rejects_unknown_extensions_and_escapes(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")
    tool = SkillSourceTool()

    escape = await tool.execute(
        {"operation": "read", "source": "acme/skills", "path": "../../../etc/passwd"}, ctx
    )
    missing_dir = await tool.execute(
        {"operation": "read", "source": "acme/skills", "path": "skills/notes"}, ctx
    )

    assert "invalid path" in escape["error"]
    assert "without a SKILL.md" in missing_dir["error"]


@pytest.mark.asyncio
async def test_symlinks_are_never_followed_out_of_the_cache(tmp_path, remote):
    ctx, _ = await _fetched(tmp_path, remote, path="skills")
    cache = _ss._cache_dir(ctx, _ss._parse_source("acme/skills", "github.com"))
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    link = cache / "skills" / "pdf" / "link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    result = await SkillSourceTool().execute(
        {"operation": "read", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    assert "skills/pdf/link.md" not in {f["path"] for f in result["files"]}
    with pytest.raises(_SourceError):
        _ss._resolve_in_cache(cache, "skills/pdf/link.md")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_bundle_writes_the_registered_file_and_the_assets(tmp_path, remote):
    session = _FakeSession("confirm")
    ctx = _make_context(tmp_path / "proj", session=session)
    await SkillSourceTool().execute(
        {"operation": "fetch", "source": "acme/skills", "path": "skills"}, ctx
    )

    result = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    project = Path(ctx.project_fs_path)
    assert result["status"] == "installed"
    assert result["slug"] == "imported/pdf"
    assert result["triggers"] == []
    assert (project / "skills" / "imported" / "pdf.md").is_file()
    assert (project / "skills" / "imported" / "_pdf" / "SKILL.md").is_file()
    assert (project / "skills" / "imported" / "_pdf" / "scripts" / "extract.py").is_file()
    assert result["files_copied"] == 3

    installed = load_skills_from_dir(project / "skills")
    assert [s.slug for s in installed] == ["imported/pdf"]
    skill = installed[0]
    assert skill.skill_type == "guidance"
    assert skill.triggers == []
    assert skill.tools == []
    # `## Execution` from the upstream body must not become execution_code.
    assert skill.execution_code is None
    assert "_pdf/reference.md" in skill.body
    assert "不执行" in skill.body


@pytest.mark.asyncio
async def test_install_records_provenance_in_the_frontmatter(tmp_path, remote):
    session = _FakeSession("confirm")
    ctx = _make_context(tmp_path / "proj", session=session)
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    text = (Path(ctx.project_fs_path) / "skills" / "imported" / "pdf.md").read_text(encoding="utf-8")
    header = yaml.safe_load(text.split("---")[1])
    assert header["source"].startswith(_FAKE_REMOTE + "@")
    assert header["source_path"] == "skills/pdf"
    assert header["tags"] == ["document"]
    assert header["description"].startswith("Extract text")


@pytest.mark.asyncio
async def test_install_asks_before_writing_and_a_cancel_writes_nothing(tmp_path, remote):
    session = _FakeSession("cancel")
    ctx = _make_context(tmp_path / "proj", session=session)
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    result = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    assert result["status"] == "cancelled"
    assert not (Path(ctx.project_fs_path) / "skills").exists()
    call = session.calls[0]
    assert call["kind"] == "selection"
    assert call["allow_other"] is False
    assert [o["label"] for o in call["options"]] == ["安装", "取消"]
    assert _FAKE_REMOTE in call["question"]


@pytest.mark.asyncio
async def test_install_existing_target_offers_overwrite(tmp_path, remote):
    session = _FakeSession("confirm")
    ctx = _make_context(tmp_path / "proj", session=session)
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, ctx)
    target = Path(ctx.project_fs_path) / "skills" / "imported" / "pdf.md"
    target.parent.mkdir(parents=True)
    target.write_text("stale\n", encoding="utf-8")

    result = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    assert result["status"] == "installed"
    assert [o["label"] for o in session.calls[0]["options"]] == ["覆盖", "取消"]
    assert "覆盖" in session.calls[0]["question"]
    assert "stale" not in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_install_is_fail_closed_without_a_session_or_when_headless(tmp_path, remote):
    headless = _make_context(tmp_path / "proj", session=_FakeSession(), interactive=False)
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, headless)
    detached = _make_context(tmp_path / "proj2")
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, detached)

    blocked = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf"}, headless
    )
    failed = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf"}, detached
    )

    assert "interactive" in blocked["error"]
    assert "session" in failed["error"]
    assert not (Path(headless.project_fs_path) / "skills").exists()


@pytest.mark.asyncio
async def test_install_survives_a_confirmation_timeout(tmp_path, remote):
    session = _FakeSession(RuntimeError("user_selection timed out"))
    ctx = _make_context(tmp_path / "proj", session=session)
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    result = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf"}, ctx
    )

    assert result["status"] == "unavailable"
    assert not (Path(ctx.project_fs_path) / "skills").exists()


@pytest.mark.asyncio
async def test_install_accepts_an_explicit_name(tmp_path, remote):
    ctx = _make_context(tmp_path / "proj", session=_FakeSession("confirm"))
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    result = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf", "name": "My PDF Skill"},
        ctx,
    )

    assert result["slug"] == "imported/my-pdf-skill"
    assert (Path(ctx.project_fs_path) / "skills" / "imported" / "my-pdf-skill.md").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["CON", "nul", "...", "!!!"])
async def test_install_rejects_unusable_names(tmp_path, remote, name):
    ctx = _make_context(tmp_path / "proj", session=_FakeSession("confirm"))
    await SkillSourceTool().execute({"operation": "fetch", "source": "acme/skills"}, ctx)

    result = await SkillSourceTool().execute(
        {"operation": "install", "source": "acme/skills", "path": "skills/pdf", "name": name}, ctx
    )

    assert "valid skill name" in result["error"]


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_removes_one_source_or_everything(tmp_path, remote):
    ctx, first = await _fetched(tmp_path, remote)
    other = await SkillSourceTool().execute(
        {"operation": "fetch", "source": "https://github.com/acme/other.git"}, ctx
    )
    root = Path(ctx.project_fs_path) / ".tmp" / "skill_cache"
    assert (Path(ctx.project_fs_path) / other["cache_path"]).exists()

    one = await SkillSourceTool().execute({"operation": "cleanup", "source": "acme/skills"}, ctx)
    rest = await SkillSourceTool().execute({"operation": "cleanup", "all": True}, ctx)

    assert one["status"] == "cleaned"
    assert not (Path(ctx.project_fs_path) / first["cache_path"]).exists()
    assert rest["status"] == "cleaned"
    assert not root.exists()
    assert other["status"] == "fetched"


@pytest.mark.asyncio
async def test_cleanup_of_an_unfetched_source_is_a_no_op(tmp_path, remote):
    ctx = _make_context(tmp_path / "proj")

    result = await SkillSourceTool().execute({"operation": "cleanup", "source": "acme/skills"}, ctx)

    assert result["status"] == "nothing_to_clean"


@pytest.mark.asyncio
async def test_operations_need_a_project_workspace(tmp_path, capture_git):
    """No project selected: never resolve the cache relative to the process cwd."""
    ctx = _make_context(tmp_path)
    ctx.project_fs_path = ""
    ctx.framework_root = str(Path(__file__).resolve().parents[3])

    fetched = await SkillSourceTool().execute(
        {"operation": "fetch", "source": "acme/skills"}, ctx
    )
    cleaned = await SkillSourceTool().execute({"operation": "cleanup", "all": True}, ctx)
    catalog = await SkillSourceTool().execute({"operation": "list_sources"}, ctx)

    assert "project workspace" in fetched["error"]
    assert "project workspace" in cleaned["error"]
    assert capture_git["calls"] == []
    # The catalog is project-optional: it falls back to the framework template.
    assert catalog["sources"]
