"""Tests for the code_executor tool: sandboxing, path isolation, and bash validation."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.code_executor import CodeExecutorTool


def make_context(project_fs_path: str, *, settings: dict[str, str] | None = None) -> ToolContext:
    ctx = ToolContext(
        project_id="proj",
        project_fs_path=project_fs_path,
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
    )
    if settings:
        ctx.integration_settings = settings
    return ctx


# ---------------------------------------------------------------------------
# Fixtures - mirror test_shell.py's sibling / guarded-temp pattern
# ---------------------------------------------------------------------------


@pytest.fixture
def sibling_projects(tmp_path):
    """proj-a/proj-b sibling workspaces + a dedicated fake system temp dir."""
    proj_a = tmp_path / "projects" / "proj-a"
    proj_b = tmp_path / "projects" / "proj-b"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    (proj_a / "local.txt").write_text("local-data", encoding="utf-8")
    (proj_b / "secret.txt").write_text("secret-data", encoding="utf-8")
    fake_temp = tmp_path / "fake-temp"
    fake_temp.mkdir()
    return proj_a, proj_b, fake_temp


@pytest.fixture
def guarded_temp(monkeypatch, sibling_projects):
    """Relocate tempfile.gettempdir() so sibling projects stay outside the guard roots."""
    _, _, fake_temp = sibling_projects
    for var in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(var, str(fake_temp))
    return sibling_projects


# ---------------------------------------------------------------------------
# Python guard: cross-project access blocked
# ---------------------------------------------------------------------------


async def test_python_open_sibling_blocked(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "open('../proj-b/secret.txt').read()", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result["stderr"]


async def test_python_os_listdir_parent_blocked(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; os.listdir('..')", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result["stderr"]


async def test_python_pathlib_rglob_sibling_blocked(sibling_projects, guarded_temp):
    proj_a, proj_b, _ = sibling_projects
    tool = CodeExecutorTool()
    # pathlib.Path.rglob catches OSError (incl. PermissionError) silently,
    # so the guard blocks access but rglob returns an empty list.  The security
    # property is that no sibling files are leaked, not that an error is raised.
    result = await tool.execute(
        {"code": "import pathlib, json; print(json.dumps([str(p) for p in pathlib.Path('../proj-b').rglob('*')]))", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert result["stdout"].strip() == "[]", f"sibling files leaked: {result['stdout']}"
    # And direct os.scandir still raises a clear PermissionError.
    result2 = await tool.execute(
        {"code": "import os; os.scandir('../proj-b')", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result2.get("exit_code", 1) != 0
    assert "agent-guard" in result2["stderr"]


async def test_python_shutil_copy_sibling_blocked(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import shutil; shutil.copy('local.txt', '../proj-b/x')", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result["stderr"]


# ---------------------------------------------------------------------------
# Python guard: in-project operations work
# ---------------------------------------------------------------------------


async def test_python_in_project_read_write(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "data = open('local.txt').read(); open('out.txt','w').write(data); print(data)", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "local-data" in result["stdout"]
    assert (proj_a / "out.txt").read_text(encoding="utf-8") == "local-data"


async def test_python_import_stdlib(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import json, math; print(math.sqrt(16))", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "4.0" in result["stdout"]


async def test_python_output_dir_write(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; open(os.path.join(os.environ['OUTPUT_DIR'],'test_0.png'),'wb').write(b'\\x89PNG')", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    # Image should be moved to media dir under a server-generated name
    media_dir = proj_a / ".tmp" / "media" / "conv"
    moved = list(media_dir.glob("code_*.png"))
    assert len(moved) == 1, f"expected one moved png, got {moved}"
    assert "images" in result and result["images"][0]["url"].endswith(moved[0].name)


async def test_python_output_dir_files_delivered(sibling_projects, guarded_temp):
    """Non-image outputs move into the media dir and surface as downloadable
    file records (name, media_type, size) with a servable URL."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; p=os.path.join(os.environ['OUTPUT_DIR'],'报告 data.csv'); open(p,'w').write('a,b\\n1,2\\n')", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    files = result.get("files", [])
    assert len(files) == 1, result
    rec = files[0]
    assert rec["name"] == "报告 data.csv"  # original name rides in the record
    assert rec["media_type"] == "text/csv"
    # size read from the moved file (avoids platform newline translation skew)
    fname = rec["url"].rsplit("/", 1)[-1]
    assert rec["size"] == (proj_a / ".tmp" / "media" / "conv" / fname).stat().st_size
    # server-generated filename must satisfy the media whitelist
    assert fname.startswith("code_") and fname.endswith(".csv")
    assert (proj_a / ".tmp" / "media" / "conv" / fname).is_file()
    # no images for a non-png output
    assert "images" not in result


async def test_python_output_dir_png_and_files_split(sibling_projects, guarded_temp):
    """PNG outputs stay images; other outputs go to files — same execution."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; d=os.environ['OUTPUT_DIR']; open(d+'/plot.png','wb').write(b'\\x89PNG'); open(d+'/data.json','w').write('{}')", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert len(result["images"]) == 1
    assert result["images"][0]["alt"].startswith("Code output: plot.png")
    assert len(result["files"]) == 1
    assert result["files"][0]["name"] == "data.json"
    assert result["files"][0]["media_type"] == "application/json"


# ---------------------------------------------------------------------------
# Python guard: blocked imports
# ---------------------------------------------------------------------------


async def test_python_ctypes_blocked_by_guard(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    # User code naming ctypes is rejected at the text level (pandas imports
    # it internally, so the runtime guard must not block the import itself).
    result = await tool.execute(
        {"code": "import ctypes", "language": "python"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_python_subprocess_blocked_by_guard(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    # Naming subprocess in the code text is caught by _BLOCKED_IMPORTS regex
    # (first layer) — a plain `import subprocess` never reaches the runtime.
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import subprocess", "language": "python"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_python_subprocess_popen_blocked_by_guard(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    # Dynamically constructed import (splitting the name defeats the text
    # regex) still must not escape: the strict guard denies the Popen CALL.
    result = await tool.execute(
        {"code": "m = __import__('sub' + 'process'); m.run('echo hi', shell=True)",
         "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result["stderr"]


async def test_python_subprocess_import_allowed(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    # matplotlib/pandas import subprocess internally (font probing, IO);
    # blocking the import broke both, so it must stay allowed.
    result = await tool.execute(
        {"code": "m = __import__('sub' + 'process'); print('ok')",
         "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result


@pytest.mark.skipif(
    importlib.util.find_spec("matplotlib") is None,
    reason="matplotlib not installed",
)
async def test_python_matplotlib_import_works(sibling_projects, guarded_temp):
    """Regression: the strict guard used to block matplotlib (it imports
    subprocess); it must keep working for plotting in code_executor."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import matplotlib", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result.get("stderr", result)


@pytest.mark.skipif(
    importlib.util.find_spec("pandas") is None,
    reason="pandas not installed",
)
async def test_python_pandas_import_works(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import pandas", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result.get("stderr", result)


async def test_scratch_dir_creation_failure_returns_error(
    sibling_projects, guarded_temp, monkeypatch,
):
    """A read-only/blocked scratch dir must return an error dict, not crash."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _boom)
    result = await tool.execute(
        {"code": "print(1)", "language": "python"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "scratch" in result["error"]


async def test_python_os_system_blocked_by_guard(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; os.system('echo hi')", "language": "python"},
        make_context(str(proj_a)),
    )
    # _BLOCKED_IMPORTS regex catches "os.system" at the text level first.
    assert "error" in result or result.get("exit_code", 1) != 0


async def test_python_os_fork_blocked_by_guard(sibling_projects, guarded_temp):
    """os.fork duplicates the process — the forked child would survive the
    parent's timeout kill and keep running unguarded. Blocked at the text
    level (the audit hook adds a second layer inside the sandbox)."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; os.fork()", "language": "python"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "blocked" in result["error"]


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------


async def test_env_has_no_projects_root(sibling_projects, guarded_temp, monkeypatch):
    proj_a, _, _ = sibling_projects
    monkeypatch.setenv("PROJECTS_ROOT", str(proj_a.parent))
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; print(os.environ.get('PROJECTS_ROOT'))", "language": "python"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert result["stdout"].strip() == "None"


# ---------------------------------------------------------------------------
# Temp dir inside project + cleanup
# ---------------------------------------------------------------------------


async def test_work_dir_cleaned_up_after_python(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    await tool.execute(
        {"code": "print('hi')", "language": "python"},
        make_context(str(proj_a)),
    )
    work_dir = proj_a / ".tmp" / "work"
    if work_dir.exists():
        remaining = list(work_dir.iterdir())
        assert remaining == [], f"work dir not cleaned up: {remaining}"


async def test_work_dir_cleaned_up_after_bash(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    await tool.execute(
        {"code": "echo hi", "language": "bash"},
        make_context(str(proj_a)),
    )
    work_dir = proj_a / ".tmp" / "work"
    if work_dir.exists():
        remaining = list(work_dir.iterdir())
        assert remaining == [], f"work dir not cleaned up: {remaining}"


# ---------------------------------------------------------------------------
# Bash mode: validation + in-project commands
# ---------------------------------------------------------------------------


async def test_bash_in_project_echo(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "echo hello", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "hello" in result["stdout"]


async def test_bash_in_project_cat(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat local.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "local-data" in result["stdout"]


async def test_bash_cross_project_cat_rejected(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat ../proj-b/secret.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "proj-b" in result["error"] or "escape" in result["error"].lower() or ".." in result["error"]


async def test_bash_cross_project_redirect_rejected(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "echo hi > ../proj-b/x", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_bash_command_substitution_allowed(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "x=$(echo hi); echo $x", "language": "bash"},
        make_context(str(proj_a)),
    )
    # Command substitution is allowed in code_executor bash (unlike shell tool).
    assert result.get("exit_code") == 0, result
    assert "hi" in result["stdout"]


async def test_bash_nested_substitution_host_file_rejected(sibling_projects, guarded_temp):
    """Nested $(...) executes the FULL chain at runtime — the outer layer
    (`cat` of an absolute path) hid behind an innermost-only scan, so
    `echo "$(cat $(echo /etc/passwd))"` read host files unchallenged. Every
    layer must be validated."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        'echo "$(cat $(echo /etc/passwd))"',
        'x="$(cat $(echo /etc/passwd))"',
        'echo "$(cp $(echo /etc/passwd) $(echo /tmp/pwn))"',
        "cat <<EOF\n$(cat $(echo /etc/passwd))\nEOF",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_nested_substitution_safe_data_allowed(sibling_projects, guarded_temp):
    """Data-only nesting (echo of a command output) stays allowed."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": 'echo "$(echo $(date))"', "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result


async def test_bash_heredoc_not_falsely_rejected(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat <<EOF\nhello world\nthis is data\nEOF", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "hello world" in result["stdout"]


async def test_bash_heredoc_space_before_delim(sibling_projects, guarded_temp):
    """`cat << EOF` (whitespace between << and the delimiter) is valid bash
    and must enter heredoc mode, not be validated as commands."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat << EOF\nhello spaced\nEOF", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "hello spaced" in result["stdout"]


async def test_bash_heredoc_quoted_delim_literal_substitution(sibling_projects, guarded_temp):
    """A quoted delimiter (<<'EOF') makes the body literal — $(...) inside is
    text, not an executable substitution to reject."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat <<'EOF'\nliteral $(cat ../proj-b/secret.txt) text\nEOF", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "literal $(cat ../proj-b/secret.txt) text" in result["stdout"]


async def test_bash_heredoc_backslash_quoted_delim_literal_substitution(sibling_projects, guarded_temp):
    """`<<\\EOF` is bash's backslash form of a quoted delimiter — the body is
    literal. A $(...) inside is text, so it must NOT be rejected as a live
    command substitution."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat <<\\EOF\nliteral $(cat ../proj-b/secret.txt) text\nEOF", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "literal $(cat ../proj-b/secret.txt) text" in result["stdout"]


async def test_bash_heredoc_backslash_inside_delim_fail_closed(sibling_projects, guarded_temp):
    """`<<\\EO\\F` — bash performs quote removal on the delimiter too, so the
    real delimiter is `EOF`, not `EO\\F`. The validator cannot match it
    reliably (a never-closed heredoc would let every following line skip
    validation), so it must fail closed: not enter heredoc mode and validate
    the body lines as ordinary commands."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat <<\\EO\\F\n$(cat ../proj-b/secret.txt)\nEOF", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_bash_heredoc_trailing_after_quote_fail_closed(sibling_projects, guarded_temp):
    """`<<"EO"F` — quote removal makes the delimiter `EOF`, but the validator
    cannot resolve the ambiguity; it must not treat the line as a heredoc, so
    the body is validated as ordinary commands (fail closed)."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": 'cat <<"EO"F\n$(cat ../proj-b/secret.txt)\nEOF', "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_bash_heredoc_arithmetic_shift_not_heredoc(sibling_projects, guarded_temp):
    """`$((1 << 2))` — the << is the shift operator, not a heredoc opener.
    If the scanner opened a bogus heredoc there, the following line would be
    swallowed as its body and `cat ../proj-b/secret.txt` would run for real."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "echo $((1 << 2))\ncat ../proj-b/secret.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "secret.txt" in str(result)


async def test_bash_assignment_taint_relay_through_quoted_value(sibling_projects, guarded_temp):
    """`a="x y" b=$t` — the space inside the quotes must not break the
    assignment scan; `b` relays the tainted `t` and `cat $b` must be blocked
    just like `cat $t`."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {
            "code": "t=$(echo /etc/passwd)\na=\"x y\" b=$t\ncat $b",
            "language": "bash",
        },
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "captured from command substitution" in str(result)


async def test_bash_heredoc_arithmetic_after_keyword(sibling_projects, guarded_temp):
    """`if (( 1 << 2 )); then` — the shift inside a control-keyword
    arithmetic must not open a heredoc that swallows the following lines."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "if (( 1 << 2 )); then\ncat ../proj-b/secret.txt\nfi", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "secret.txt" in str(result)


async def test_bash_heredoc_arith_subscript_in_braces(sibling_projects, guarded_temp):
    """`${arr[1 << 2]}` — arithmetic array subscripts are valid bash; the
    << must not open a bogus heredoc either."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "echo ${arr[1 << 2]}\ncat ../proj-b/secret.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "secret.txt" in str(result)


async def test_bash_assignment_taint_after_inline_substitution(sibling_projects, guarded_temp):
    """`x=$(echo hi) C=$(echo /etc/passwd)` — the shlex-glued `hi)` token must
    not stop the taint sweep; the later assignment on the same line is still
    tracked and `cat $C` is blocked."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {
            "code": "x=$(echo hi) C=$(echo /etc/passwd)\ncat $C",
            "language": "bash",
        },
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "captured from command substitution" in str(result)


async def test_bash_export_assignment_taints_variable(sibling_projects, guarded_temp):
    """`export C=$(echo /etc/passwd)` is an assignment too — the prefix must
    not skip the taint branch; `cat $C` expands the captured value and must be
    blocked just like the bare `C=$(...)` form."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {
            "code": "export C=$(echo /etc/passwd)\ncat $C",
            "language": "bash",
        },
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "captured from command substitution" in str(result)


@pytest.mark.parametrize("expr", ["$1", "${1}", "$2", "$@", "$*", "$#"])
async def test_bash_positional_parameter_in_path_rejected(expr, sibling_projects, guarded_temp):
    """`cat $1/etc/passwd` — positional parameters are never set by
    code_executor, so they expand empty at runtime and the path collapses to
    the absolute /etc/passwd past the static check. Must be denied."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": f"cat {expr}/etc/passwd", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    if expr == "$#":
        # shlex splits `$#` into a bare `$` token + `#` comment — rejected by
        # the bare-`$` substitution guard instead of the positional check;
        # either way it is fail-closed.
        pass
    else:
        assert "positional parameter" in str(result)


@pytest.mark.parametrize("expr", ["$y", "${y}"])
async def test_bash_unassigned_var_path_prefix_rejected(expr, sibling_projects, guarded_temp):
    """`cat $y/etc/passwd` with y unassigned expands to `cat /etc/passwd` —
    an absolute path outside the project. A $var/ path prefix must only be
    allowed when the variable is assigned in the same command."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": f"cat {expr}/etc/passwd", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "not assigned" in str(result)


async def test_bash_empty_assignment_prefix_rejected(sibling_projects, guarded_temp):
    """`x=` expands to nothing at runtime, so `cat $x/etc/hostname` silently
    becomes `cat /etc/hostname` — an empty assignment must not grant
    "assigned" status for path-prefix use. Same for ${x:-...} defaults and
    unset (which is blocked outright)."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "x=; cat $x/etc/hostname",
        "x=${x:-/etc}; cat $x/passwd",
        "x=1\nunset x\ncat $x/etc/passwd",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_single_quoted_substitution_is_literal(sibling_projects, guarded_temp):
    """'$(...)' in single quotes is literal text — bash prints it, never
    executes it, so the substitution scanner must not flag the payload."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "echo '$(cat /etc/passwd)'", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "$(cat /etc/passwd)" in result["stdout"]


async def test_bash_dot_slash_script_execution_rejected(sibling_projects, guarded_temp):
    """`./run` executes an in-project script whose CONTENT was never
    validated — `echo 'cat /etc/passwd' > run; chmod +x run; ./run` reads
    host files unguarded (bash mode has no python audit hook). Script
    execution belongs to the shell tool's allowlist."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "echo 'cat /etc/passwd' > run; chmod +x run; ./run",
        "echo 'cat /etc/passwd' > run\nchmod +x run\n./run",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_build_wrappers_still_allowed(sibling_projects, guarded_temp):
    """The ./mvnw ./gradlew build wrappers stay executable."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "./mvnw --version", "language": "bash"},
        make_context(str(proj_a)),
    )
    # Not statically rejected — reaches execution (wrapper absent here, so
    # bash reports the missing file instead of a validation error).
    assert "error" not in result, result
    assert result.get("exit_code") == 127, result


async def test_bash_host_env_assignment_rejected(sibling_projects, guarded_temp):
    """`x=$SHELL` assigns a HOST env var — it expands to a host path at
    runtime (`cat $x` reads /bin/bash). Worse, an UNSET var expands to
    EMPTY, so `x=$UNSET; cat $x/etc/hostname` collapses to an absolute
    path — the empty-assignment escape revived with a non-empty value.
    Only names assigned earlier and the project-internal injected vars
    (HOME/OUTPUT_DIR/PROJECT_DIR/PWD) are traceable."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "x=$SHELL; cat $x",
        "x=$PATH; cat $x/foo",
        "x=$DEFINITELY_UNSET_VAR_9x; cat $x/etc/hostname",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_ansi_c_quote_path_rejected(sibling_projects, guarded_temp):
    """ANSI-C quoting ($'\x2fetc\x2fpasswd') hides '/' and '..' inside a
    token shlex never unescapes — bash decodes it into an absolute path
    (or a ../ escape) at runtime, past every token check."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "cat $'\\x2fetc\\x2fpasswd'",
        "cat $'\\x2e\\x2e/sibling/secret.txt'",
        "echo $'\\x2fetc\\x2fpasswd'",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_trap_alias_xargs_dd_rejected(sibling_projects, guarded_temp):
    """trap/alias+shopt run arbitrary shell payloads (builtins), xargs/dd
    smuggle command construction and paths into data tokens the token
    checks never see."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "trap 'cat /etc/passwd' EXIT",
        "trap 'cat /etc/passwd' ERR",
        "shopt -s expand_aliases\nalias r='cat /etc/passwd'\nr",
        "echo /etc/passwd | xargs cat",
        "dd if=/etc/passwd",
        "dd of=/etc/cron.d/evil",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_assigned_var_of_safe_env_still_allowed(sibling_projects, guarded_temp):
    """Assignments from the server-injected project-internal env vars
    (HOME/OUTPUT_DIR/PROJECT_DIR/PWD) keep working — their values are
    guaranteed inside the project. (On Windows the PROJECT_DIR value is a
    backslash path that bash mis-expands at runtime, so only the static
    validation result is asserted.)"""
    proj_a, _, _ = sibling_projects
    (proj_a / "local.txt").write_text("local-data", encoding="utf-8")
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "d=$PROJECT_DIR; cat $d/local.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    # Not rejected by validation (Windows runtime expansion may fail).
    assert "error" not in result, result


async def test_bash_assigned_var_path_prefix_allowed(sibling_projects, guarded_temp):
    """An assigned variable may still prefix a path: `d=data; cat $d/x` is
    validated at the assignment site and must run."""
    proj_a, _, _ = sibling_projects
    (proj_a / "data").mkdir()
    (proj_a / "data" / "x.txt").write_text("hi", encoding="utf-8")
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "d=data\ncat $d/x.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "hi" in result["stdout"]


async def test_bash_export_absolute_assignment_rejected(sibling_projects, guarded_temp):
    """`export x=/etc/passwd` — the NAME= prefix makes the token look like
    an assignment (skipped by the absolute-path scan), then `cat $x` reads
    the host file at runtime. Assignments must be validated the same way
    as plain path tokens."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "export x=/etc/passwd\ncat $x",
        "export x=/etc/cron.d/evil\ncat $x",
        "declare x=/etc/passwd\ncat $x",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_param_expansion_in_path_rejected(sibling_projects, guarded_temp):
    """`${Y:-/etc}` in a path position — the expansion result can be any
    path (or a ../ escape), so the literal-path check can't vouch for it."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "cat ${Y:-/etc/passwd}",
        "cat ${UNSET:-../../sibling/secret.txt}",
        "echo ${X:-/etc} > ${Y:-/etc/passwd}",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_git_hooks_redirect_rejected(sibling_projects, guarded_temp):
    """Writing .git/hooks/* plants a script git executes unvalidated on a
    later commit/status — the git variant of the ./run escape."""
    proj_a, _, _ = sibling_projects
    (proj_a / ".git").mkdir()
    tool = CodeExecutorTool()
    for code in (
        "echo 'cat /etc/passwd' > .git/hooks/pre-commit",
        "printf '#!/bin/sh\\ncat /etc/passwd\\n' >> .git/hooks/post-checkout",
        "cat /etc/passwd > ./.git/hooks/post-merge",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_git_internal_writes_rejected(sibling_projects, guarded_temp):
    """Deep .git writes — nested repos (subdir/.git/hooks/), submodules
    (.git/modules/<name>/hooks/), .git/config (hooksPath repoint), and
    cp/mv past the redirect check — all install code git executes later."""
    proj_a, _, _ = sibling_projects
    (proj_a / "subdir" / ".git" / "hooks").mkdir(parents=True)
    tool = CodeExecutorTool()
    for code in (
        "printf '#!/bin/sh\\ncat /etc/passwd\\n' > subdir/.git/hooks/pre-commit",
        "echo x > .git/config",
        "echo x > .git/modules/foo/hooks/post-checkout",
        "echo 'cat /etc/passwd' > hook.sh; cp hook.sh .git/hooks/pre-commit",
        "mv hook.sh .git/hooks/pre-commit",
        "cp -t .git/hooks hook.sh",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_find_delete_rejected(sibling_projects, guarded_temp):
    """`find . -delete` recursively wipes the whole project workspace —
    the -delete gate lives in sandbox.py so both tools share it."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "find . -delete",
        "find . -maxdepth 2 -name '*.tmp' -delete",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_execution_env_assignment_rejected(sibling_projects, guarded_temp):
    """PATH=. ls makes bash resolve `ls` through the assigned PATH — a
    project script named ls runs as the command. Same family as the shell
    tool's rejection: names that change command resolution/execution."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "PATH=. ls",
        "PATH=./bin python3 -c 'pass'",
        "LD_PRELOAD=./evil.so ls",
        "GIT_EXTERNAL_DIFF='cat /etc/passwd' git diff",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_db_clients_rejected(sibling_projects, guarded_temp):
    """mysql/psql/redis-cli/mongosh reach the compose-internal databases
    that hold sessions and secrets — blocked like curl/wget in bash mode
    (no socket audit hook there)."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    for code in (
        "redis-cli -h redis keys '*'",
        "mysql -h db -e 'select 1'",
        "psql -h pg -c 'select 1'",
        "mongosh --host mongo --eval 'db.runCommand({ping:1})'",
    ):
        result = await tool.execute(
            {"code": code, "language": "bash"},
            make_context(str(proj_a)),
        )
        assert "error" in result, f"{code!r} must be rejected, got {result}"


async def test_bash_braced_substitution_brace_not_closing(sibling_projects, guarded_temp):
    """`echo ${x:-$(echo aaaa}) <<b}` — the `}` inside $(...) does not close
    the ${...} region, so `<<b` is still INSIDE it and opens no heredoc; the
    following line must stay validated (previously it was swallowed as a
    bogus heredoc body and `cat ../proj-b/secret.txt` ran for real)."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "echo ${x:-$(echo aaaa}) <<b}\ncat ../proj-b/secret.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "secret.txt" in str(result)


async def test_bash_negated_arith_shift_not_heredoc(sibling_projects, guarded_temp):
    """`! (( 1 << 2 ))` — negation of an arithmetic command; the `<<` is the
    shift operator and must not open a bogus heredoc that swallows the
    following lines."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "! (( 1 << 2 ))\ncat ../proj-b/secret.txt", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result
    assert "secret.txt" in str(result)
    """An unquoted heredoc body runs $(...) for real — it must be validated
    and a cross-project read rejected."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat <<EOF\n$(cat ../proj-b/secret.txt)\nEOF", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_bash_heredoc_marker_inside_quotes_not_heredoc(sibling_projects, guarded_temp):
    """`echo "x <<EOF"` must not open heredoc mode — following lines stay
    validated as real commands."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": 'echo "x <<EOF"\ncat ../proj-b/secret.txt', "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_bash_multiline_script_in_project(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    (proj_a / "data.txt").write_text("foo\nbar\nbaz\n", encoding="utf-8")
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "wc -l data.txt | awk '{print $1}'", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "3" in result["stdout"]


async def test_bash_multiline_quoted_string(sibling_projects, guarded_temp):
    """A quoted string spanning physical lines is valid bash and must not be
    rejected by the per-line validator."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": 'echo "line one\nline two"', "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "line one" in result["stdout"] and "line two" in result["stdout"]


async def test_bash_unclosed_quote_still_rejected(sibling_projects, guarded_temp):
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": 'echo "never closed', "language": "bash"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_bash_slash_data_args_allowed(sibling_projects, guarded_temp):
    """awk/cut/tr separators and patterns are data, not paths."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "echo 'a/b/c' | awk -F '/' '{print $2}'", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert result["stdout"].strip() == "b"


async def test_bash_bare_cat_does_not_swallow_script(sibling_projects, guarded_temp):
    """Scripts run from a FILE with DEVNULL stdin: a bare `cat` gets EOF
    instead of consuming the remaining script lines (bash -s regression)."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "cat\necho after", "language": "bash"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "after" in result["stdout"]


def test_bash_validate_allows_temp_redirect(tmp_path):
    import tempfile

    target = Path(tempfile.gettempdir()) / "ce-redirect-test.log"
    assert CodeExecutorTool._validate_bash(f"echo hi > {target.as_posix()}", tmp_path) is None


def test_bash_validate_denies_project_escape_redirect(tmp_path):
    proj = tmp_path / "proj-a"
    proj.mkdir()
    assert CodeExecutorTool._validate_bash("echo hi > ../other/x", proj) is not None


# ---------------------------------------------------------------------------
# python-playwright language mode
# ---------------------------------------------------------------------------


async def test_pw_allows_subprocess_import(sibling_projects, guarded_temp):
    """python-playwright mode allows subprocess in code text (browser launch)."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import subprocess; print('ok')", "language": "python-playwright"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code") == 0, result
    assert "ok" in result["stdout"]


async def test_pw_blocks_dangerous_imports(sibling_projects, guarded_temp):
    """python-playwright mode still blocks os.system, pty, ctypes, etc."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import os; os.system('echo hi')", "language": "python-playwright"},
        make_context(str(proj_a)),
    )
    assert "error" in result


async def test_pw_os_system_blocked_at_runtime(sibling_projects, guarded_temp):
    """os.system is blocked by the audit hook even in non-strict mode."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "m = __import__('os'); m.system('echo hi')", "language": "python-playwright"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result.get("stderr", "")


async def test_pw_file_guard_still_active(sibling_projects, guarded_temp):
    """File-access guard still confines reads to the project workspace."""
    proj_a, proj_b, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": f"open(r'{proj_b / 'secret.txt'}').read()", "language": "python-playwright"},
        make_context(str(proj_a)),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result.get("stderr", "")


async def test_pw_nonlocalhost_socket_blocked(sibling_projects, guarded_temp):
    """With SANDBOX_NETWORK=localhost, non-localhost destinations are still
    blocked in python-playwright mode."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import socket; s=socket.socket(); s.connect(('8.8.8.8', 80))",
         "language": "python-playwright"},
        make_context(str(proj_a), settings={"SANDBOX_NETWORK": "localhost"}),
    )
    assert result.get("exit_code", 1) != 0
    assert "agent-guard" in result.get("stderr", "")


async def test_pw_localhost_socket_allowed(sibling_projects, guarded_temp):
    """SANDBOX_NETWORK=localhost still allows loopback connections in
    python-playwright mode."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    # Connecting to localhost on a port where nothing listens should fail with
    # ConnectionRefused, NOT with agent-guard PermissionError.
    result = await tool.execute(
        {"code": "import socket; s=socket.socket(); s.connect(('127.0.0.1', 1))",
         "language": "python-playwright"},
        make_context(str(proj_a), settings={"SANDBOX_NETWORK": "localhost"}),
    )
    # The connect call itself is allowed (no agent-guard), but it fails at the
    # OS level (connection refused / timeout).
    stderr = result.get("stderr", "")
    assert "agent-guard" not in stderr, stderr


async def test_pw_network_all_by_default(sibling_projects, guarded_temp):
    """Unset SANDBOX_NETWORK defaults to allow-all: python-playwright can use
    the full Playwright stack (downloads, screen recording), so no
    agent-guard denial appears on any socket call."""
    proj_a, _, _ = sibling_projects
    tool = CodeExecutorTool()
    result = await tool.execute(
        {"code": "import socket; socket.getaddrinfo('127.0.0.1', 80); "
                 "s=socket.socket(); s.connect(('127.0.0.1', 1))",
         "language": "python-playwright"},
        make_context(str(proj_a)),
    )
    stderr = result.get("stderr", "")
    assert "agent-guard" not in stderr, stderr
