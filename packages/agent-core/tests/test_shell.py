"""Tests for the shell tool env_refs secret injection and output redaction."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools import shell as shell_module


def make_context(db=None, project_fs_path="/tmp/proj") -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path=project_fs_path,
        conversation_id="conv",
        user_id="user-1",
        db_session=db,
    )


def make_proc(out: bytes, err: bytes = b"", returncode: int = 0) -> SimpleNamespace:
    proc = SimpleNamespace(returncode=returncode)
    proc.communicate = AsyncMock(return_value=(out, err))
    return proc


# ---------------------------------------------------------------------------
# redact_secrets unit tests
# ---------------------------------------------------------------------------


def test_redact_secrets_replaces_exact_matches():
    text = "login ok, token hunter2 here, stderr hunter2 again"
    out = shell_module.redact_secrets(text, {"APP_PASSWORD": "hunter2"})
    assert "hunter2" not in out
    assert out.count("[REDACTED:APP_PASSWORD]") == 2


def test_redact_secrets_short_value():
    out = shell_module.redact_secrets("a", {"APP_PASSWORD": "a"})
    assert "a" not in out
    assert out == "[REDACTED:APP_PASSWORD]"


def test_redact_secrets_empty_value_skipped():
    assert shell_module.redact_secrets("nothing", {"APP_PASSWORD": ""}) == "nothing"


# ---------------------------------------------------------------------------
# _resolve_env_refs unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_env_refs_user_scope():
    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="hunter2")) as get_token:
        resolved, missing, error = await shell_module._resolve_env_refs(
            make_context(),
            {"APP_PASSWORD": {"scope": "user", "ref": "qa:login:password"}},
        )
    get_token.assert_awaited_once()
    assert get_token.await_args.args[1] == "qa:login:password"  # first arg is the context
    assert error is None and missing == []
    assert resolved == {"APP_PASSWORD": "hunter2"}


@pytest.mark.asyncio
async def test_resolve_env_refs_missing_returns_keys():
    with patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value=None)):
        resolved, missing, error = await shell_module._resolve_env_refs(
            make_context(),
            {"APP_USERNAME": {"ref": "qa:login:username"}, "APP_PASSWORD": {"ref": "qa:login:password"}},
        )
    assert resolved is None and error is None
    assert missing == ["qa:login:username", "qa:login:password"]


@pytest.mark.asyncio
async def test_resolve_env_refs_invalid_shape():
    resolved, missing, error = await shell_module._resolve_env_refs(
        make_context(), {"APP_PASSWORD": "just-a-string"}
    )
    assert resolved is None
    assert "env_refs['APP_PASSWORD']" in error


@pytest.mark.asyncio
async def test_resolve_env_refs_invalid_scope():
    resolved, missing, error = await shell_module._resolve_env_refs(
        make_context(), {"APP_PASSWORD": {"ref": "x", "scope": "team"}}
    )
    assert resolved is None
    assert "scope must be 'user' or 'project'" in error


@pytest.mark.asyncio
async def test_resolve_env_refs_project_scope_uses_get_secret_optional():
    with (
        patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="proj-secret")) as get_secret,
        patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="leak")) as get_token,
    ):
        resolved, missing, error = await shell_module._resolve_env_refs(
            make_context(),
            {"APP_PASSWORD": {"scope": "project", "ref": "qa:login:password", "environment": "uat"}},
        )
    get_secret.assert_awaited_once()
    assert get_secret.await_args.kwargs["environment"] == "uat"
    get_token.assert_not_awaited()
    assert error is None and missing == []
    assert resolved == {"APP_PASSWORD": "proj-secret"}


@pytest.mark.asyncio
async def test_resolve_env_refs_db_session_missing_treated_as_missing():
    # Real _auth path: db_session=None raises ToolAuthError inside get_token,
    # which get_token_optional swallows into None -> surfaces as a missing ref.
    resolved, missing, error = await shell_module._resolve_env_refs(
        make_context(db=None),
        {"APP_PASSWORD": {"ref": "qa:login:password"}},
    )
    assert resolved is None and error is None
    assert missing == ["qa:login:password"]


# ---------------------------------------------------------------------------
# _build_env extra merge
# ---------------------------------------------------------------------------


def test_build_env_merges_extra_after_safe_env(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "alice")
    monkeypatch.setenv("APP_PASSWORD", "from-server-env")
    ctx = make_context()
    env = shell_module._build_env(ctx, extra={"APP_PASSWORD": "vault-value"})
    assert env["APP_PASSWORD"] == "vault-value"  # extra wins, safe_env stripped the rest
    assert env["APP_USERNAME"] == "alice"        # non-credential keys pass through
    env_no_extra = shell_module._build_env(ctx)
    assert "APP_PASSWORD" not in env_no_extra     # safe_env strips PASSWORD keys


# ---------------------------------------------------------------------------
# end-to-end execute() with env_refs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_refs_merged_into_subprocess_env_and_output_redacted():
    proc = make_proc(out=b"user hunter2 logged in\n", returncode=0)
    with (
        patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="hunter2")),
        patch("agent_core.tools.shell.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("agent_core.tools.shell.asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)),
    ):
        result = await shell_module.ShellTool().execute(
            {
                "command": "echo hi",
                "env_refs": {"APP_PASSWORD": {"scope": "user", "ref": "qa:login:password"}},
            },
            make_context(),
        )
    assert result["exit_code"] == 0
    assert "hunter2" not in result["stdout"]
    assert "[REDACTED:APP_PASSWORD]" in result["stdout"]


@pytest.mark.asyncio
async def test_env_refs_injected_into_subprocess_env_dict():
    proc = make_proc(out=b"ok", returncode=0)
    exec_mock = AsyncMock(return_value=proc)
    shell_mock = AsyncMock(return_value=proc)
    with (
        patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="hunter2")),
        patch("agent_core.tools.shell.asyncio.create_subprocess_exec", new=exec_mock),
        patch("agent_core.tools.shell.asyncio.create_subprocess_shell", new=shell_mock),
    ):
        await shell_module.ShellTool().execute(
            {
                "command": "echo hi",
                "env_refs": {"APP_PASSWORD": {"ref": "qa:login:password"}},
            },
            make_context(),
        )
    # Windows runs commands through Git Bash (create_subprocess_exec); Linux and
    # macOS use /bin/sh (create_subprocess_shell). Either way the resolved vault
    # value must reach the child process env.
    called = exec_mock.await_args or shell_mock.await_args
    assert called is not None
    assert called.kwargs["env"]["APP_PASSWORD"] == "hunter2"  # subprocess needs the real value


@pytest.mark.asyncio
async def test_env_refs_missing_returns_error_and_does_not_run():
    def fail_subprocess(*args, **kwargs):
        raise AssertionError("subprocess must not run when a ref is missing")

    with (
        patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value=None)),
        patch("agent_core.tools.shell.asyncio.create_subprocess_exec", side_effect=fail_subprocess),
        patch("agent_core.tools.shell.asyncio.create_subprocess_shell", side_effect=fail_subprocess),
    ):
        result = await shell_module.ShellTool().execute(
            {
                "command": "echo hi",
                "env_refs": {"APP_PASSWORD": {"ref": "qa:login:password"}},
            },
            make_context(),
        )
    assert result["missing_service_keys"] == ["qa:login:password"]
    assert "env_refs unresolved" in result["error"]


@pytest.mark.asyncio
async def test_env_refs_invalid_shape_fails_execute():
    proc = make_proc(out=b"nope", returncode=0)
    with (
        patch("agent_core.tools.shell.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("agent_core.tools.shell.asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)),
    ):
        result = await shell_module.ShellTool().execute(
            {"command": "echo hi", "env_refs": {"APP_PASSWORD": "bad-shape"}},
            make_context(),
        )
    assert "env_refs['APP_PASSWORD']" in result["error"]


@pytest.mark.asyncio
async def test_redact_before_truncate_straddling_secret():
    # Build stdout where the secret straddles the _MAX_OUTPUT boundary.
    prefix = b"x" * (shell_module._MAX_OUTPUT - 5)
    secret = b"hunter2"
    proc = make_proc(out=prefix + secret + b"y" * 50, returncode=0)
    with (
        patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="hunter2")),
        patch("agent_core.tools.shell.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("agent_core.tools.shell.asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)),
    ):
        result = await shell_module.ShellTool().execute(
            {
                "command": "echo hi",
                "env_refs": {"APP_PASSWORD": {"ref": "qa:login:password"}},
            },
            make_context(),
        )
    # The placeholder itself may be cut by truncation at the boundary — the
    # security property is that the plaintext never survives, so assert that.
    assert "hunter2" not in result["stdout"]
    assert len(result["stdout"]) <= shell_module._MAX_OUTPUT


@pytest.mark.asyncio
async def test_install_error_output_is_redacted():
    # A command that triggers _ensure_node_deps failure path carries npm stderr
    # fragments; resolved secrets must be redacted there too. Force a missing
    # node_modules with an npm command and a writeable package.json dir.
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pkg_dir = Path(tmp) / "proj"
        (pkg_dir / "tests").mkdir(parents=True)
        (pkg_dir / "tests" / "package.json").write_text(
            '{"name": "p", "scripts": {"test": "playwright test"}}', encoding="utf-8"
        )

        def fake_install(*args, **kwargs):
            proc = make_proc(out=b"", err=b"install failed with hunter2 in stderr", returncode=1)
            return proc

        with (
            patch("agent_core.tools._auth.get_token_optional", new=AsyncMock(return_value="hunter2")),
            patch("agent_core.tools.shell.asyncio.create_subprocess_exec", side_effect=fake_install),
            patch("agent_core.tools.shell.asyncio.create_subprocess_shell", side_effect=fake_install),
        ):
            result = await shell_module.ShellTool().execute(
                {
                    "command": "npm run test:x",
                    "cwd": "tests",
                    "env_refs": {"APP_PASSWORD": {"ref": "qa:login:password"}},
                },
                make_context(project_fs_path=tmp + "/proj"),
            )
        assert "hunter2" not in result["error"]
        assert "install failed" in result["error"]


# ---------------------------------------------------------------------------
# Cross-project isolation: command validation + runtime python guard
# ---------------------------------------------------------------------------


@pytest.fixture
def sibling_projects(tmp_path):
    """proj-a/proj-b sibling workspaces plus a dedicated fake system temp dir.

    The fake temp matters: tempfile.gettempdir() is a write root of the python
    guard, so projects placed under the real system temp would both be inside
    the guard's allowed roots.
    """
    proj_a = tmp_path / "projects" / "proj-a"
    proj_b = tmp_path / "projects" / "proj-b"
    (proj_a / "sub").mkdir(parents=True)
    proj_b.mkdir(parents=True)
    (proj_a / "local.txt").write_text("local-data", encoding="utf-8")
    (proj_b / "secret.txt").write_text("secret-data", encoding="utf-8")
    fake_temp = tmp_path / "fake-temp"
    fake_temp.mkdir()
    return proj_a, proj_b, fake_temp


@pytest.fixture
def guarded_temp(monkeypatch, sibling_projects):
    """Relocate the child interpreter's tempfile.gettempdir() so the sibling
    project stays outside the guard's allowed roots."""
    _, _, fake_temp = sibling_projects
    for var in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(var, str(fake_temp))
    return sibling_projects


ESCAPE_COMMANDS = [
    "cat ../proj-b/secret.txt",
    "cat sub/../../proj-b/secret.txt",
    "cat /abs/path",
    "cat ~/x",
    "ls ..",
    "ls ../proj-b",
    "cat x>../proj-b/y",            # redirect without spaces
    "echo hi > ../proj-b/y",
    "cat < ../proj-b/secret.txt",
    "cat $(printf '../proj-b/secret.txt')",
    "cat `printf ../proj-b/secret.txt`",
    "git -C ../proj-b status",
    "git --git-dir=/abs/x status",
    "git config --global user.name x",  # writes ~/.gitconfig, outside the project
    "git clone https://example.com/repo.git",
    'node -e "console.log(1)"',
    "grep --exclude-dir=../x pat .",
    "npm --prefix=../proj-b install",
    "find . -delete",
    "python -S x.py",
    "python -IS x.py",
    'python3 -E -c "print(1)"',
    'cat "unclosed',
]


@pytest.mark.parametrize("command", ESCAPE_COMMANDS)
async def test_escape_commands_are_rejected(command, sibling_projects):
    proj_a, _, _ = sibling_projects
    result = await shell_module.ShellTool().execute(
        {"command": command}, make_context(project_fs_path=str(proj_a))
    )
    assert "error" in result, f"expected rejection for: {command}"


async def test_absolute_path_to_sibling_rejected(sibling_projects):
    proj_a, proj_b, _ = sibling_projects
    secret = (proj_b / "secret.txt").as_posix()  # forward slashes for bash parity
    result = await shell_module.ShellTool().execute(
        {"command": f"cat {secret}"}, make_context(project_fs_path=str(proj_a))
    )
    assert "error" in result


async def test_empty_assignment_path_prefix_rejected(sibling_projects):
    """`x=; cat $x/etc/hostname` expands to an absolute path at runtime —
    the empty assignment must not grant "assigned" status (the shell tool
    has no python audit-hook fallback; validate_command is its only gate)."""
    proj_a, _, _ = sibling_projects
    result = await shell_module.ShellTool().execute(
        {"command": "x=; cat $x/etc/hostname"}, make_context(project_fs_path=str(proj_a))
    )
    assert "error" in result


async def test_nonempty_assignment_path_prefix_allowed(sibling_projects):
    """A non-empty assignment still validates as a path prefix."""
    proj_a, _, _ = sibling_projects
    result = await shell_module.ShellTool().execute(
        {"command": "x=local.txt; cat $x"}, make_context(project_fs_path=str(proj_a))
    )
    assert result.get("exit_code") == 0, result
    assert "local-data" in result["stdout"]


async def test_env_interpreter_escape_rejected(sibling_projects):
    """`env sh -c 'cat /etc/passwd'` passed the allowlist (env is a listed
    command) while its arguments — an interpreter + arbitrary payload —
    were validated as plain data tokens, then executed for real. env was
    removed from the allowlist: `FOO=bar cmd` covers assignment use."""
    proj_a, _, _ = sibling_projects
    for cmd in (
        "env sh -c 'cat /etc/passwd'",
        "env bash -c 'cat /etc/passwd'",
        "env -i sh -c 'cat /etc/passwd'",
    ):
        result = await shell_module.ShellTool().execute(
            {"command": cmd}, make_context(project_fs_path=str(proj_a))
        )
        assert "error" in result, cmd


async def test_var_prefix_still_allowed(sibling_projects):
    """VAR=value prefixes (the env replacement) keep working."""
    proj_a, _, _ = sibling_projects
    result = await shell_module.ShellTool().execute(
        {"command": "X=hello; echo $X"}, make_context(project_fs_path=str(proj_a))
    )
    assert result.get("exit_code") == 0, result
    assert "hello" in result["stdout"]


async def test_git_config_executable_key_write_rejected(sibling_projects):
    """`git config key value` WRITES .git/config — executable keys fire on
    LATER commands (core.fsmonitor runs on every status, including the
    git_repo tool's checks). The write path must reject them like `git -c`
    does. Inert keys (user.name, core.autocrlf) and read forms stay fine."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    for cmd in (
        "git config core.fsmonitor 'cat /etc/passwd'",
        "git config alias.x '!cat /etc/passwd'",
        "git config credential.helper '!/tmp/evil'",
        "git config filter.pwn.clean 'cat /etc/passwd'",
    ):
        result = await tool.execute({"command": cmd}, ctx)
        assert "error" in result, f"{cmd!r} must be rejected, got {result}"
    # Inert keys keep working
    for cmd in (
        "git config user.name 'First Last'",
        "git config core.autocrlf true",
    ):
        result = await tool.execute({"command": cmd}, ctx)
        assert "error" not in result, f"{cmd!r} must be allowed, got {result}"


async def test_git_c_executable_key_relative_script_rejected(sibling_projects):
    """`git -c core.pager=./evil.sh log` — the value is a RELATIVE script
    path git resolves and executes against the repo root (the ./script
    escape via a git trigger). Executable keys only accept bare command
    names; any value with a path separator or a leading dot is rejected."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    for cmd in (
        "git -c core.pager=./evil.sh log",
        "git -c core.fsmonitor=scripts/watch.sh status",
        "git -c filter.pwn.clean=./clean.sh checkout .",
        "git -c credential.helper=./hook.sh fetch",
    ):
        result = await tool.execute({"command": cmd}, ctx)
        assert "error" in result, f"{cmd!r} must be rejected, got {result}"
    # Bare command names (resolved via PATH, not the repo) keep working
    for cmd in (
        "git -c core.pager=less log",
        "git -c core.autocrlf=true status",
        "git -c remote.origin.url=https://github.com/a/b.git status",
    ):
        result = await tool.execute({"command": cmd}, ctx)
        assert "error" not in result, f"{cmd!r} must be allowed, got {result}"


async def test_git_c_diff_external_rejected(sibling_projects):
    """diff.external / diff.<driver>.command|textconv run the value with the
    diffed FILES as arguments — `git config diff.external sh` makes git
    execute old/new files as shell scripts on every diff. The family is
    rejected outright (even a bare interpreter name is unsafe); inert
    diff.renames stays allowed."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    for cmd in (
        "git -c diff.external=./evil.sh diff",
        "git config diff.external sh",
        "git -c diff.pwn.command=cat diff",
        "git config diff.pwn.textconv ./x",
    ):
        result = await tool.execute({"command": cmd}, ctx)
        assert "error" in result, f"{cmd!r} must be rejected, got {result}"
    for cmd in (
        "git -c diff.renames=true status",
        "git config diff.color true",
    ):
        result = await tool.execute({"command": cmd}, ctx)
        assert "error" not in result, f"{cmd!r} must be allowed, got {result}"


async def test_execution_env_assignment_rejected(sibling_projects):
    """VAR=value prefixes skip the path checks (assignment, not a path), but
    variables that change how commands are RESOLVED/EXECUTED let a command's
    arguments become the code that runs: PATH=. python executes ./python,
    GIT_EXTERNAL_DIFF='cat /etc/passwd' runs on every diff, LD_PRELOAD loads
    any shared object. Reject them by name regardless of value."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    for cmd in (
        "PATH=. python -c 'import os; os.chmod(\"python\", 0o755)'",
        "GIT_EXTERNAL_DIFF='cat /etc/passwd' git diff",
        "LD_PRELOAD=./evil.so ls",
        "GIT_DIR=/etc git status",
        "BASH_ENV=./evil.sh ls",
    ):
        result = await tool.execute({"command": cmd}, ctx)
        assert "error" in result, f"{cmd!r} must be rejected, got {result}"
    # Legitimate assignments keep working (GIT_AUTHOR_* is inert identity)
    result = await tool.execute(
        {"command": "GIT_AUTHOR_NAME='Agent U' git commit -m x"}, ctx
    )
    assert "error" not in result, result


# -- regression: normal in-project commands keep working ---------------------


async def test_cat_local_file(sibling_projects):
    proj_a, _, _ = sibling_projects
    result = await shell_module.ShellTool().execute(
        {"command": "cat local.txt"}, make_context(project_fs_path=str(proj_a))
    )
    assert result.get("exit_code") == 0
    assert "local-data" in result["stdout"]


async def test_redirect_within_project(sibling_projects):
    proj_a, _, _ = sibling_projects
    result = await shell_module.ShellTool().execute(
        {"command": "echo hi > local.log"}, make_context(project_fs_path=str(proj_a))
    )
    assert result.get("exit_code") == 0
    assert (proj_a / "local.log").read_text(encoding="utf-8").strip() == "hi"


async def test_git_common_subcommands_allowed(sibling_projects):
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute({"command": "git init"}, ctx)
    assert result.get("exit_code") == 0
    result = await tool.execute({"command": "git status"}, ctx)
    assert result.get("exit_code") == 0
    # Empty repo: git log exits non-zero, but must pass validation (no "error").
    result = await tool.execute({"command": "git log --oneline"}, ctx)
    assert "error" not in result


async def test_node_script_file_allowed(sibling_projects):
    proj_a, _, _ = sibling_projects
    (proj_a / "script.js").write_text("console.log('hi')", encoding="utf-8")
    result = await shell_module.ShellTool().execute(
        {"command": "node script.js"}, make_context(project_fs_path=str(proj_a))
    )
    # node may not be installed in every dev environment; validation is the contract.
    assert "error" not in result


async def test_ls_and_grep_regression(sibling_projects):
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    for command in ("ls", "ls sub", "grep -r local ."):
        result = await tool.execute({"command": command}, ctx)
        assert result.get("exit_code") == 0, f"{command}: {result}"


async def test_python_inline_allowed_and_guarded(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))

    result = await tool.execute({"command": 'python -c "print(1)"'}, ctx)
    assert result.get("exit_code") == 0, result
    assert result["stdout"].strip() == "1"

    result = await tool.execute(
        {"command": 'python -c "import json, math; print(math.sqrt(4))"'}, ctx
    )
    assert result.get("exit_code") == 0, result
    assert "2.0" in result["stdout"]


async def test_python_guard_denies_cross_project_read(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    result = await shell_module.ShellTool().execute(
        {"command": 'python -c "open(\'../proj-b/secret.txt\').read()"'},
        make_context(project_fs_path=str(proj_a)),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result["stderr"]


async def test_python_guard_blocks_os_system(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    # ctypes import stays allowed (pandas needs it internally) — the guard
    # blocks the dangerous CALL paths (os.system/os.exec), not library imports.
    result = await tool.execute({"command": 'python -c "import ctypes; print(\'ok\')"'}, ctx)
    assert result.get("exit_code") == 0, result
    result = await tool.execute(
        {"command": 'python -c "import os; os.system(\'echo hi\')"'}, ctx
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result["stderr"]


async def test_python_subprocess_env_has_no_projects_root(
    sibling_projects, guarded_temp, monkeypatch
):
    proj_a, _, _ = sibling_projects
    monkeypatch.setenv("PROJECTS_ROOT", str(proj_a.parent))
    result = await shell_module.ShellTool().execute(
        {"command": 'python -c "import os; print(os.environ.get(\'PROJECTS_ROOT\'))"'},
        make_context(project_fs_path=str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert result["stdout"].strip() == "None"


async def test_python_guard_keeps_pytest_working(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    (proj_a / "test_trivial.py").write_text(
        "def test_ok(tmp_path):\n"
        "    (tmp_path / 'f.txt').write_text('x')\n"
        "    assert (tmp_path / 'f.txt').read_text() == 'x'\n",
        encoding="utf-8",
    )
    result = await shell_module.ShellTool().execute(
        {"command": "python -m pytest test_trivial.py -q", "timeout_seconds": 180},
        make_context(project_fs_path=str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "1 passed" in result["stdout"]


# ---------------------------------------------------------------------------
# Git identity injection + git workflow regression
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_repo_missing_identity_returns_defaults(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    env = shell_module._repo_missing_git_identity(str(repo))
    assert env["GIT_AUTHOR_NAME"] == "Agents Universe"
    assert env["GIT_AUTHOR_EMAIL"] == "agents-universe@localhost"
    assert env["GIT_COMMITTER_NAME"] == "Agents Universe"
    assert env["GIT_COMMITTER_EMAIL"] == "agents-universe@localhost"


def test_repo_with_identity_returns_nothing(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "config", "user.name", "Repo Alice"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "alice@repo.com"], cwd=repo, check=True)
    env = shell_module._repo_missing_git_identity(str(repo))
    assert env == {}


def test_repo_with_partial_identity_fills_only_gaps(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "config", "user.name", "Repo Alice"], cwd=repo, check=True)
    env = shell_module._repo_missing_git_identity(str(repo))
    assert env == {
        "GIT_AUTHOR_EMAIL": "agents-universe@localhost",
        "GIT_COMMITTER_EMAIL": "agents-universe@localhost",
    }
    assert "GIT_AUTHOR_NAME" not in env  # repo identity must win


def test_build_env_does_not_inject_git_identity(monkeypatch):
    """_build_env never injects git identity itself — execute() probes the repo
    and passes only the missing fields via git_identity."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    env = shell_module._build_env(make_context())
    assert "GIT_AUTHOR_NAME" not in env
    assert "GIT_COMMITTER_EMAIL" not in env


async def test_git_commit_works_out_of_the_box(sibling_projects, guarded_temp, monkeypatch):
    """Stock containers have no git identity; the probed default must make
    `git commit` work without any `git config` call. Also covers repo-local
    config, -c overrides, -C into a subdir, and data strings with ../."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))

    result = await tool.execute({"command": "git init"}, ctx)
    assert result.get("exit_code") == 0, result
    result = await tool.execute({"command": "git add local.txt"}, ctx)
    assert "error" not in result, result
    # Commit message containing a ../ data string must not trip path checks.
    result = await tool.execute({"command": 'git commit -m "revert ../../x regression"'}, ctx)
    assert result.get("exit_code") == 0, result

    # Repo-local config and -c overrides are allowed.
    result = await tool.execute({"command": "git config user.name Tester"}, ctx)
    assert "error" not in result, result
    result = await tool.execute({"command": "git -c user.name=Other log --oneline"}, ctx)
    assert "error" not in result, result
    # -C into an in-project subdirectory is allowed.
    result = await tool.execute({"command": "git -C sub rev-parse --show-toplevel"}, ctx)
    assert "error" not in result, result
    # reset/rebase-class subcommands pass validation.
    result = await tool.execute({"command": "git reset --hard HEAD"}, ctx)
    assert "error" not in result, result


async def test_git_commit_respects_repo_identity(sibling_projects, guarded_temp, monkeypatch):
    """A repo with its own user.name/user.email must keep it — the synthetic
    env identity must never override repo-local git config."""
    import subprocess
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))

    result = await tool.execute({"command": "git init"}, ctx)
    assert result.get("exit_code") == 0, result
    result = await tool.execute({"command": "git config user.name \"Repo Alice\""}, ctx)
    assert "error" not in result, result
    result = await tool.execute({"command": "git config user.email alice@repo.com"}, ctx)
    assert "error" not in result, result
    result = await tool.execute({"command": "git add local.txt"}, ctx)
    assert "error" not in result, result
    result = await tool.execute({"command": "git commit -m first"}, ctx)
    assert result.get("exit_code") == 0, result

    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=str(proj_a), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert author == "Repo Alice <alice@repo.com>", author


# ---------------------------------------------------------------------------
# Per-segment allowlist checking for compound commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_commands_pass_allowlist(sibling_projects):
    """Commands added to the allowlist (which, sed, awk, etc.) should pass."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    for cmd in ("which python", "sed --version", "awk --version", "cut --version",
                "tr --version", "printf 'hi'", "touch newfile", "stat local.txt",
                "date", "basename local.txt", "dirname sub", "whoami", "uname",
                "printenv PATH", "test -f local.txt",
                "HOME=.tmp/pentest/home /opt/semgrep-venv/bin/semgrep --version"):
        result = await tool.execute({"command": cmd}, ctx)
        # "error" key only appears on rejection; exit_code may be non-zero
        # (e.g. sed --version not installed) but that's not an allowlist block.
        assert "not in allowlist" not in result.get("error", ""), f"{cmd}: {result.get('error')}"


@pytest.mark.asyncio
async def test_compound_command_all_segments_allowed(sibling_projects):
    """Compound commands where every segment is allowed should pass allowlist."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute(
        {"command": "which python; echo '---'; python --version"}, ctx
    )
    # Should not be an allowlist rejection (may fail at runtime if which missing)
    assert "not in allowlist" not in result.get("error", ""), result


@pytest.mark.asyncio
async def test_compound_command_second_segment_not_in_allowlist(sibling_projects):
    """A non-allowed command in a later segment must be caught."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute(
        {"command": "echo hi; curl http://evil.com"}, ctx
    )
    assert "error" in result
    # curl is caught by blocklist first, so test with a non-blocked, non-allowed cmd
    result = await tool.execute(
        {"command": "echo hi; dd if=/dev/zero of=x bs=1 count=1"}, ctx
    )
    assert "error" in result
    assert "not in allowlist" in result["error"]


@pytest.mark.asyncio
async def test_compound_command_first_segment_not_in_allowlist(sibling_projects):
    """A non-allowed command in the first segment must be caught (same as before)."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute(
        {"command": "dd if=/dev/zero of=x bs=1 count=1; echo hi"}, ctx
    )
    assert "error" in result
    assert "not in allowlist" in result["error"]


@pytest.mark.asyncio
async def test_pipe_segments_all_checked(sibling_projects):
    """Piped commands must have every segment in the allowlist."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    # Both echo and head are allowed - should pass
    result = await tool.execute({"command": "echo hello | head -1"}, ctx)
    assert "not in allowlist" not in result.get("error", ""), result
    # echo allowed, dd not - should fail
    result = await tool.execute({"command": "echo hello | dd of=x"}, ctx)
    assert "error" in result
    assert "not in allowlist" in result["error"]


@pytest.mark.asyncio
async def test_var_assignment_then_allowed_command(sibling_projects):
    """Leading VAR=value assignments should be skipped to find the real command."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute({"command": "FOO=bar echo $FOO"}, ctx)
    assert "not in allowlist" not in result.get("error", ""), result


@pytest.mark.asyncio
async def test_find_with_grouped_parens_passes_allowlist(sibling_projects):
    r"""find \( -name x -o -name y \) must not be split on parens."""
    proj_a, _, _ = sibling_projects
    (proj_a / "pytest.ini").write_text("[pytest]", encoding="utf-8")
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute(
        {"command": r'find . -maxdepth 1 \( -name "pytest.ini" -o -name "setup.cfg" \) 2>/dev/null | sort'},
        ctx,
    )
    assert "not in allowlist" not in result.get("error", ""), result


@pytest.mark.asyncio
async def test_compound_with_find_parens_allows_full_pipeline(sibling_projects):
    """The original failing command: ls + find with \\( \\) + echo + find."""
    proj_a, _, _ = sibling_projects
    (proj_a / "pytest.ini").write_text("[pytest]", encoding="utf-8")
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    cmd = (
        'ls -la; echo "---TESTS---"; '
        r'find . -maxdepth 2 -type d -name "test*" 2>/dev/null | sort; '
        'echo "---CONFIG---"; '
        r'find . -maxdepth 2 \( -name "pytest.ini" -o -name "pyproject.toml" \) 2>/dev/null | sort'
    )
    result = await tool.execute({"command": cmd}, ctx)
    assert "not in allowlist" not in result.get("error", ""), result


def test_strip_env_prefix():
    tool = shell_module.ShellTool()
    assert tool._strip_env_prefix("npm run build") == "npm run build"
    assert tool._strip_env_prefix("NODE_ENV=production npm run build") == "npm run build"
    assert tool._strip_env_prefix("A=1 B=2 npx tsc") == "npx tsc"
    # A word that merely LOOKS like an assignment must not be stripped
    assert tool._strip_env_prefix("echo A=1") == "echo A=1"


@pytest.mark.asyncio
async def test_export_home_override_passes(sibling_projects):
    """The toolchain's HOME override works in its `export` form too — the
    natural way an agent writes it (`export HOME=...; cmd`)."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute({"command": "export HOME=.tmp/pentest/home; echo done"}, ctx)
    assert "error" not in result, result


@pytest.mark.asyncio
async def test_export_path_hijack_rejected(sibling_projects):
    """`export PATH=./evil` must stay refused — PATH-hijacking an allowlisted
    command (the next `git`/`ls` resolves through PATH to an in-project
    script whose content was never validated)."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute({"command": "export PATH=./evil; git status"}, ctx)
    assert "error" in result
    assert "PATH" in result["error"]


@pytest.mark.asyncio
async def test_grep_pip_install_pattern_not_blocked(sibling_projects):
    """grep for 'pip install' in a Dockerfile is a legitimate dependency-
    pinning audit — the pattern text must not be mistaken for a pip install
    command (no raw-string blocklist)."""
    proj_a, _, _ = sibling_projects
    (proj_a / "Dockerfile").write_text("RUN pip install flask\n", encoding="utf-8")
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute({"command": 'grep -n "pip install" Dockerfile'}, ctx)
    assert "error" not in result, result
    assert "pip install" in result.get("stdout", "")


@pytest.mark.asyncio
async def test_pip_dry_run_allowed_real_install_rejected(sibling_projects):
    """`python3 -m pip install --dry-run` is the dependency-resolution
    channel (pip_audit's degradation path) and must not be blocked; a real
    `pip install` stays rejected."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute(
        {
            "command": "python3 -m pip install --dry-run --ignore-installed "
            "--report .tmp/pentest/resolve.json -r .tmp/pentest/req.txt",
            "timeout_seconds": 60,
        },
        ctx,
    )
    # May fail at runtime (no python3 / missing req.txt) — the point is the
    # sandbox accepts it.
    assert "pip install is not allowed" not in result.get("error", ""), result
    assert "not in allowlist" not in result.get("error", ""), result
    result = await tool.execute({"command": "python3 -m pip install requests"}, ctx)
    assert "error" in result
    assert "pip install is not allowed" in result["error"]


@pytest.mark.asyncio
async def test_semgrep_absolute_command_runs(sibling_projects):
    """The allowlisted semgrep console script passes the validator end-to-end
    (the sandbox carve-out for its absolute path); on a machine without the
    binary the command fails at spawn, not at validation."""
    proj_a, _, _ = sibling_projects
    tool = shell_module.ShellTool()
    ctx = make_context(project_fs_path=str(proj_a))
    result = await tool.execute(
        {"command": "HOME=.tmp/pentest/home /opt/semgrep-venv/bin/semgrep --version"}, ctx
    )
    assert "not in allowlist" not in result.get("error", ""), result
    assert "Absolute path" not in result.get("error", ""), result


def test_inject_npm_cache_env_recognizes_env_prefix(tmp_path):
    """FOO=bar npm ... must be recognized as an npm command even though the
    first token is an assignment. The cache-dir injection itself is
    Linux-only (win32 keeps the user's own npm cache)."""
    import re
    import sys

    tool = shell_module.ShellTool()
    stripped = tool._strip_env_prefix("NODE_ENV=production npm run build")
    assert re.match(r"^(npm|npx)\b", stripped)
    if sys.platform != "win32":
        command = tool._inject_npm_cache_env(
            "NODE_ENV=production npm run build", str(tmp_path), str(tmp_path)
        )
        assert command.startswith("npm_config_cache=")
        assert command.endswith("NODE_ENV=production npm run build")


@pytest.mark.asyncio
async def test_ensure_node_deps_matches_env_prefixed_npm(tmp_path, monkeypatch):
    """_ensure_node_deps must treat VAR=value npm as an npm command (previously
    skipped the deps check, letting installs run against an unwritable cache)."""
    tool = shell_module.ShellTool()
    # Force a package.json to exist so the early-return branches don't fire
    (tmp_path / "package.json").write_text("{}")
    ctx = make_context(project_fs_path=str(tmp_path))

    installs: list[str] = []

    def fake_install(*args, **kwargs):
        installs.append(str(args[-1] if args else kwargs))
        return make_proc(out=b"", err=b"", returncode=0)

    with (
        patch("agent_core.tools.shell.asyncio.create_subprocess_exec", side_effect=fake_install),
        patch("agent_core.tools.shell.asyncio.create_subprocess_shell", side_effect=fake_install),
    ):
        result = await tool._ensure_node_deps("NODE_ENV=test npm install", str(tmp_path), ctx)
    # A clean install returns None; what matters is that the deps machinery
    # ran at all - the VAR= prefix must not hide npm from the check.
    assert result is None
    assert installs, "npm install was never run - the env prefix hid npm from the deps check"


# ---------------------------------------------------------------------------
# npm cache resolution (EACCES regression on the shared /tmp/npm-cache)
# ---------------------------------------------------------------------------

_skip_win32 = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only cache resolution")


@_skip_win32
def test_resolve_npm_cache_dir_prefers_project_internal_over_unwritable_shared(tmp_path, monkeypatch):
    """A root-owned shared /tmp/npm-cache must not block installs: the
    project-internal .npm-cache is tried before it and wins whenever the
    shared cache is unusable."""
    monkeypatch.setattr(shell_module, "_NPM_CACHE_FALLBACK", "/definitely/not/writable")
    resolved = shell_module._resolve_npm_cache_dir(tmp_path, "npm run test:sys-101")
    assert resolved == str(tmp_path / ".npm-cache")
    assert (tmp_path / ".npm-cache").is_dir()


@_skip_win32
def test_resolve_npm_cache_dir_command_env_prefix_wins(tmp_path):
    """NPM_CONFIG_CACHE= (either case) in the command itself is honored for
    the install phase too - the original bug ran the install against the
    hardcoded /tmp cache even when the command named another cache."""
    custom = tmp_path / "custom-cache"
    resolved = shell_module._resolve_npm_cache_dir(
        tmp_path, f"NPM_CONFIG_CACHE={custom} npm run typecheck"
    )
    assert resolved == str(custom)
    assert custom.is_dir()


@_skip_win32
def test_resolve_npm_cache_dir_npmrc_wins_over_project_default(tmp_path):
    npmrc_cache = tmp_path / "from-npmrc"
    (tmp_path / ".npmrc").write_text(f"cache={npmrc_cache}\nfund=false\naudit=false\n", encoding="utf-8")
    resolved = shell_module._resolve_npm_cache_dir(tmp_path, "npm run typecheck")
    assert resolved == str(npmrc_cache)


@_skip_win32
def test_resolve_npm_cache_dir_relative_npmrc_value_resolved_against_pkg_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_module, "_NPM_CACHE_FALLBACK", "/definitely/not/writable")
    (tmp_path / ".npmrc").write_text("cache=.npm-cache\n", encoding="utf-8")
    resolved = shell_module._resolve_npm_cache_dir(tmp_path, "npm run typecheck")
    assert resolved == str(tmp_path / ".npm-cache")


@_skip_win32
def test_resolve_npm_cache_dir_home_value_expanded(tmp_path, monkeypatch):
    """npm expands ~/ in .npmrc values; the tool must agree with npm instead
    of creating a literal '~' directory inside the project."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".npmrc").write_text("cache=~/.npm-home-cache\n", encoding="utf-8")
    resolved = shell_module._resolve_npm_cache_dir(tmp_path, "npm run typecheck")
    assert resolved == str(tmp_path / ".npm-home-cache")


@_skip_win32
def test_resolve_npm_cache_dir_unwritable_candidates_fall_through(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_module, "_NPM_CACHE_FALLBACK", "/definitely/not/writable")
    resolved = shell_module._resolve_npm_cache_dir(
        tmp_path, "NPM_CONFIG_CACHE=/proc/no-such-cache-possible npm run test"
    )
    assert resolved == str(tmp_path / ".npm-cache")


@_skip_win32
def test_resolve_npm_cache_dir_returns_none_when_nothing_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_module, "_ensure_writable_cache", lambda d: False)
    assert shell_module._resolve_npm_cache_dir(tmp_path, "npm run test") is None


@_skip_win32
def test_inject_npm_cache_env_uses_same_resolution_as_install(tmp_path, monkeypatch):
    """The real command and the dependency-install phase must agree on the
    cache: the injected prefix points at the resolved cache dir, not at the
    legacy hardcoded /tmp/npm-cache."""
    tool = shell_module.ShellTool()
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shell_module, "_NPM_CACHE_FALLBACK", "/definitely/not/writable")
    command = tool._inject_npm_cache_env("npm run test:sys-101", str(tmp_path), str(tmp_path))
    assert command.startswith(f"npm_config_cache={tmp_path / '.npm-cache'} npm ")


@_skip_win32
def test_inject_npm_cache_env_leaves_explicit_cache_untouched(tmp_path):
    tool = shell_module.ShellTool()
    cmd = "NPM_CONFIG_CACHE=.npm-cache npm run typecheck"
    assert tool._inject_npm_cache_env(cmd, str(tmp_path), str(tmp_path)) == cmd


def test_inject_npm_cache_env_ignores_non_npm_commands(tmp_path):
    tool = shell_module.ShellTool()
    assert tool._inject_npm_cache_env("git status", str(tmp_path), str(tmp_path)) == "git status"


@_skip_win32
@pytest.mark.asyncio
async def test_ensure_node_deps_install_uses_resolved_cache(tmp_path, monkeypatch):
    """End-to-end regression for the QA blocker: the dependency-install phase
    hardcoded /tmp/npm-cache, so every npm/playwright command died with EACCES
    before the user command even started. The install must now run with the
    resolved (writable) cache dir."""
    tool = shell_module.ShellTool()
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shell_module, "_NPM_CACHE_FALLBACK", "/definitely/not/writable")
    ctx = make_context(project_fs_path=str(tmp_path))

    installs: list[str] = []

    def fake_install(*args, **kwargs):
        installs.append(str(args[-1] if args else kwargs))
        return make_proc(out=b"", err=b"", returncode=0)

    with (
        patch("agent_core.tools.shell.asyncio.create_subprocess_exec", side_effect=fake_install),
        patch("agent_core.tools.shell.asyncio.create_subprocess_shell", side_effect=fake_install),
    ):
        result = await tool._ensure_node_deps("npm run test:sys-101", str(tmp_path), ctx)

    assert result is None
    assert installs, "install phase did not run"
    assert installs[0].startswith(f"npm_config_cache={tmp_path / '.npm-cache'} npm ")
    assert "/definitely/not/writable" not in installs[0]



def test_description_commands_all_in_allowlist():
    """description/prompt_hint 宣称可用的命令必须真的被 _ALLOWED_CMDS 接受——
    allowlist 移除命令时描述漏改，会让 LLM 按描述调用然后被拒（env 被移出
    allowlist 但描述仍列出的同类 bug）。"""
    import re

    def _listing_segment(text: str) -> str:
        # The comma-separated command listing starts after its header colon
        # ("Allowed:" / "Allowed commands:") and ends at './gradlew' — prose
        # on either side (intro, path rules, git subcommands, ...) is not
        # part of the claim.
        start = text.find(":")
        end = text.rfind("./gradlew")
        if start == -1 or end == -1 or end <= start:
            return ""
        return text[start + 1 : end + len("./gradlew")]

    tool = shell_module.ShellTool()
    # "python3?" in the pattern yields "python3" — the description lists the
    # bare name; normalize trailing digits so both sides match.
    allowed_names = {
        re.sub(r"\d+$", "", n)
        for n in re.findall(r"[a-z][a-z0-9]*", shell_module._ALLOWED_CMDS.pattern)
    }
    allowed_names |= {"mvnw", "gradlew"}  # _WRAPPER_COMMAND
    for text in (tool.description, tool.prompt_hint):
        for name in re.findall(r"[a-z][a-z0-9]*", _listing_segment(text)):
            assert name in allowed_names, (
                f"description/prompt_hint lists {name!r} but _ALLOWED_CMDS does not allow it"
            )
