"""Tests for agent_core.sandbox: python_guard_env + validate_command + the
sitecustomize runtime guard (via real Python subprocesses)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.sandbox import (
    GUARD_DIR,
    python_guard_env,
    spawn_in_new_session,
    terminate_process_tree,
    validate_command,
)


@pytest.fixture
def sandbox_dirs(tmp_path):
    """Sibling proj-a/proj-b workspaces plus a dedicated fake system temp dir.

    The fake temp dir matters: tempfile.gettempdir() is a guard write root, so
    projects placed under the real system temp would all be readable/writable.
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


def run_python(code: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )


def guarded_env(project_root: Path, fake_temp: Path, *, strict: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("_AGENT_PROJECT_ROOT", None)
    env.pop("_AGENT_BLOCK_SUBPROCESS", None)
    env.pop("_AGENT_NETWORK_MODE", None)
    env.pop("_AGENT_EXEC_ALLOWLIST", None)
    env.update(python_guard_env(project_root, strict=strict))
    # The guard's network default is now "all" (SANDBOX_NETWORK unset). These
    # tests assert the blocking behavior, so pin the child to the old default.
    env["_AGENT_NETWORK_MODE"] = "none"
    # Relocate the child's tempfile.gettempdir() so sandbox_dirs stay outside it.
    for var in ("TEMP", "TMP", "TMPDIR"):
        env[var] = str(fake_temp)
    return env


# ---------------------------------------------------------------------------
# python_guard_env
# ---------------------------------------------------------------------------


def test_guard_env_sets_project_root_resolved(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = python_guard_env(tmp_path / "proj")
    assert env["_AGENT_PROJECT_ROOT"] == str((tmp_path / "proj").resolve())
    assert env["PYTHONPATH"] == str(GUARD_DIR)
    assert "_AGENT_BLOCK_SUBPROCESS" not in env


def test_guard_env_drops_outside_pythonpath_entries(tmp_path, monkeypatch):
    """Inherited PYTHONPATH entries outside the project (e.g. /app/src in the
    container) are dropped: imports would silently skip them and resolve to
    stale wheels instead, and pip-style sys.path scans trip the guard."""
    inside = (tmp_path / "proj" / "src").resolve()
    inside.mkdir(parents=True)
    inherited = os.pathsep.join(["/outside/lib", "rel-src", str(inside), ""])
    monkeypatch.setenv("PYTHONPATH", inherited)
    env = python_guard_env(tmp_path / "proj")
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(GUARD_DIR)
    assert "rel-src" in parts              # relative entries kept (cwd is in-project)
    assert str(inside) in parts            # absolute in-project entries kept
    assert "" in parts                     # empty entry == cwd, kept
    assert "/outside/lib" not in parts     # absolute outside entries dropped


def test_guard_env_keeps_only_in_project_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "sibling"))
    env = python_guard_env(tmp_path / "proj")
    assert env["PYTHONPATH"] == str(GUARD_DIR)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["../escape", str(tmp_path / "proj")]))
    env = python_guard_env(tmp_path / "proj")
    assert env["PYTHONPATH"] == str(GUARD_DIR) + os.pathsep + str((tmp_path / "proj").resolve())


def test_guard_env_strict_blocks_subprocess(tmp_path):
    assert python_guard_env(tmp_path, strict=True)["_AGENT_BLOCK_SUBPROCESS"] == "1"


def test_guard_env_sets_exec_allowlist(tmp_path):
    """The non-strict guard env carries the os.exec allowlist (the semgrep
    venv binaries); the strict env must not - an exec there would swap the
    guarded interpreter for an unguarded binary past the Popen block."""
    from agent_core.sandbox import _EXEC_ALLOW_DIRS

    env = python_guard_env(tmp_path / "proj")
    assert env["_AGENT_EXEC_ALLOWLIST"] == os.pathsep.join(_EXEC_ALLOW_DIRS)
    assert "_AGENT_EXEC_ALLOWLIST" not in python_guard_env(tmp_path / "proj", strict=True)


def test_guard_dir_contains_sitecustomize():
    assert (GUARD_DIR / "sitecustomize.py").is_file()


# ---------------------------------------------------------------------------
# validate_command — pure function matrix (no subprocess)
# ---------------------------------------------------------------------------

ALLOWED = [
    "cat local.txt",
    "echo hi > local.log",
    "echo hi 2>/dev/null",
    "echo hi >> local.log",
    "git status",
    "git log --oneline",
    "git commit -m \"fix: message with / slash\"",
    "git remote get-url origin",
    "git remote -v",
    "git remote",
    "node script.js",
    "node --test",
    "node --test tests/",
    "python -c \"print(1)\"",
    "python -m pytest",
    "python3.12 -m pytest tests/",
    "ls",
    "ls src",
    "ls .",
    "grep -r pat .",
    "npm test",
    "npm run build",
    "npm install",
    "npx vitest run",
    "npx tsc",
    "npx vite build",
    "npx playwright test",
    "cat a && cat b",
    "echo x | grep x",
    "mkdir -p a/b",
    "cat <<EOF",
    "./mvnw -q",
    # Data strings must not trip the path checks (regression matrix):
    "git commit -m \"revert ../../x change\"",
    "git tag -a v1 -m \"~/plan\"",
    "grep -rn \"~/.config\" src/",
    "python -c \"print('a/../b')\"",
    # git workflow subcommands and repo-local config:
    "git config user.email a@b.c",
    "git -c user.email=a@b.c commit -m x",
    "git -c user.name=\"First Last\" commit -m x",
    "git -c core.autocrlf=true status",
    "git -c remote.origin.url=https://x/y.git status",
    "git -C sub status",
    "git remote add origin url",
    "git reset --hard HEAD~1",
    "git rebase main",
    "git rm old.txt",
    "git mv a.txt b.txt",
    "git revert HEAD",
    "git cherry-pick abc123",
    "git clean -fd",
    "git submodule update --init",
    "git stash push -m \"wip\"",
    # python/node script argv belongs to the script, not the interpreter:
    "python script.py -S fast",
    "python tool.py -I input -E extra",
    "echo python -I hello",
    # Data-processing tools: pattern/program args and option values are data:
    "awk -F \"/\" \"{print $2}\" paths.txt",
    "cut -d / -f 2 data.txt",
    "tr '/' '-' names.txt",
    "jq --arg name value data.json",
    "git commit -F message.txt",
    # Legitimate sed programs must not trip the attached-target scans:
    "sed 's/x/w/g' f.txt",
    "sed 's/foo/r/g' f.txt",
    "sed '1,10p' f.txt",
    "sed 's/x/y/w out.txt' f.txt",
    # "Compute first, then use" — variables assigned earlier in the command
    # string may appear in paths (their values were validated):
    "f=notes.md; cat $f",
    "f=notes.md; wc $f",
    "out=build/out.txt; mv $out final.txt",
    "d=sub; grep -rn pat $d",
    "x=42; echo $x",
    "echo $PATH",                      # data command: args are never paths
    "git -m \"see $f in notes\" commit",  # data value, not a path
    # sed text-command payloads embed bare ` e` / trailing `e` — the program
    # scan must not mistake them for the `e` command or the s///e flag:
    "sed 'a\\hello e' f.txt",
    "sed 'c\\line e' f.txt",
    "sed 'c\\done' f.txt",
    "sed '1a\\hello' f.txt",
    # s///w with a bare-letter filename (we) — `e` is the filename, not the
    # e flag; only `ew` (e flag + w flag) executes the shell:
    "sed 's/foo/bar/we' f.txt",
]

DENIED = [
    "cat ../proj-b/secret.txt",
    "cat sub/../../proj-b/secret.txt",
    "cat /abs/path",
    "cat ~/x",
    "ls ..",
    "ls ../proj-b",
    "cat x>../proj-b/y",          # redirect without spaces
    "echo hi > ../proj-b/y",
    "echo hi >| ../proj-b/y",     # noclobber override — same open, same check
    "echo hi &>> ../proj-b/y",    # append stdout+stderr
    "cat < ../proj-b/secret.txt",
    "cat $(printf '../proj-b/secret.txt')",
    "cat `printf ../proj-b/secret.txt`",
    # Unquoted $() splits into a bare `$` token that ends up in a path
    # position (`cat $(echo /etc/passwd)` → cat $ ( echo ... )):
    "cat $(echo /etc/passwd)",
    "echo hi > $(echo /etc/passwd)",
    "rm -rf $(echo /etc/passwd)",
    "for f in $(echo /etc/passwd); do cat $f; done",
    "git -C ../proj-b status",
    "git --git-dir=/abs/x status",
    "git --git-dir=rel/x status",
    "git --work-tree=../x status",
    "git config --global user.name x",   # would write ~/.gitconfig
    "git config --system core.x y",
    "git clone https://example.com/repo.git",
    # git -c values git EXECUTES (pager/alias) must stay blocked even when
    # quoted — the space-allowed keys are inert text only:
    "git -c alias.x='!cat ../proj-b/secret.txt' x",
    "git -c core.pager='cat ../proj-b/secret.txt' status",
    "git -c user.name=../evil status",
    "git -c user.name='x; rm -rf /' status",
    # sed's `e` command executes the rest of the line via the shell — the
    # delimiter/whitespace variants below all must stay blocked:
    "sed '1e' f.txt",
    "sed '{e}' f.txt",
    "sed '/./e ' f.txt",
    "sed '\\#HOST#e' f.txt",
    "sed 's|x|echo HOST3|e ' f.txt",
    "sed '1 e' f.txt",                 # address, whitespace, then e
    "sed '1\ne cat /etc/passwd'",      # multi-line script: newline is whitespace
    # `e` executes everything after the command letter — a glued-on payload
    # must not slip past the old "only after whitespace" boundary:
    "sed '1ewhoami' f.txt",
    "sed 'ecat /etc/passwd' f.txt",    # program starting with e (no address)
    "sed '\\#HOST#ecat /etc/passwd'",  # escaped-delimiter address, glued text
    "sed 's/x/y/\ne cat /etc/passwd'", # line-start e on a later script line
    "sed '1!eid' f.txt",               # negated address before e
    "sed '\\#HOST# eid' f.txt",        # escaped-delimiter address + whitespace + e
    "sed ':lbl eid' f.txt",            # label definition, whitespace, then e
    "sed 's|x|y|e2' f.txt",            # e flag with a count suffix
    # s///e with a non-standard delimiter — GNU sed accepts any separator,
    # the e flag still executes the replacement via the shell:
    "sed 's#a#b#e' f.txt",
    "sed 's%a%b%ge' f.txt",
    "sed 's/a/b/ew' f.txt",            # e flag before the w flag
    "sed 's/a/b/eg' f.txt",            # e flag before g
    "sed 's/a\\/b/c/e' f.txt",         # escaped delimiter inside content
    # awk one-way pipe to a command string — `| "cmd"` hands the string to
    # the shell (only `|&` and system() used to be covered):
    "awk 'BEGIN{print \"x\" | \"cat secretfile.txt\"}'",
    "awk 'BEGIN{print \"x\" | \"rm -rf /tmp\"}'",
    # sed attached write targets (s///w flag, address forms) must not smuggle
    # a filename outside the project:
    "sed 's/x/y/w/tmp/out' f.txt",
    "sed '1,10w/tmp/x' f.txt",
    "sed '2,5r/etc/passwd' f.txt",
    "sed 's/x/y/gw/tmp/out' f.txt",
    "sed '1w/tmp/out' f.txt",
    "sed 's#a#b#w/tmp/x' f.txt",       # non-standard delimiter w target
    "node -e \"console.log(1)\"",
    "node --eval=console.log(1)",
    "node -p \"1+1\"",
    "grep --exclude-dir=../x pat .",
    "npm --prefix=../proj-b install",
    "python -S x.py",
    "python -IS x.py",
    "python3 -E -c \"print(1)\"",
    "python3.12 -I x.py",
    "jq -f ../prog.jq data.json",        # path-valued options stay checked
    "awk -f ../prog.awk data.txt",
    "sort -o ../out.txt data.txt",
    # Variable-expansion smuggling: $VAR in a path argument can only be
    # trusted when assigned earlier in this command (values are validated);
    # host env vars and unassigned vars are rejected outright.
    "p=../proj-b; cat $p/secret.txt",
    "p=../proj-b; cat ${p}/secret.txt",
    "p=../proj-b; cat $p",
    "p=../proj-b; git -C $p status",
    "p=../proj-b; echo x > $p/out.txt",
    "p=$HOME/x; cat $p",                  # assignment value itself expands
    "cat $HOME/secret.txt",               # env var path — not assigned here
    "git -C $HOME status",
    "ls $HOME",
    "cat $1",
    "cat $$/x",
    "cat \"unclosed",
    # Command substitution in a PATH position expands at runtime — the
    # payload is a data command (`echo`) with no leading /, so it is only
    # the runtime expansion that reveals the escape:
    "cat \"$(echo ../proj-b/secret.txt)\"",
    "cat `echo ../proj-b/secret.txt`",
    # Variable captured from command substitution then used as a path:
    "x=$(echo ../proj-b/secret.txt); cat $x",
    "x=$(echo ../proj-b/secret.txt)\ncat $x",
    # Tainted-ness relays through assignments: `y=$x` copies the captured
    # value, so `cat $y` smuggles the same path as `cat $x`:
    "x=$(echo ../proj-b/secret.txt); y=$x; cat $y",
    "x=$(echo ../proj-b/secret.txt); y=$x; z=$y; cat $z",
    # Directory-attached substitution (`cat dir/$(echo x)`) splits into a
    # `dir/` token plus a bare `$` — the bare form is unambiguous:
    "cat dir/$(echo x)",
    # shlex glues `);` into one token — it must still separate commands, or
    # the following command's path checks are skipped (merged into the
    # previous segment's data-argument list):
    "(echo hi); cat ../proj-b/secret.txt",
    "(echo hi);cat ../proj-b/secret.txt",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_validate_command_allows(command, tmp_path):
    assert validate_command(command, cwd=tmp_path, project_root=tmp_path) is None


@pytest.mark.parametrize("command", DENIED)
def test_validate_command_denies(command, tmp_path):
    assert validate_command(command, cwd=tmp_path, project_root=tmp_path) is not None


def test_validate_command_denies_absolute_path_to_sibling(tmp_path):
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    secret = (proj_b / "secret.txt").as_posix()  # forward slashes for bash parity
    reason = validate_command(f"cat {secret}", cwd=proj_a, project_root=proj_a)
    assert reason is not None


def test_validate_command_substitution_literal_dollar_filename(tmp_path):
    """`cat file$` — a trailing $ is a literal filename character, not
    command substitution (only a bare `$` or directory-attached `/$` is).
    In substitution-allowed mode it passes; `cat dir/$(echo x)` still splits
    into a directory token plus a bare `$` and must be rejected."""
    assert validate_command("cat file$", cwd=tmp_path, project_root=tmp_path,
                            allow_substitution=True) is None
    assert validate_command("cat dir/$(echo x)", cwd=tmp_path, project_root=tmp_path,
                            allow_substitution=True) is not None


def test_validate_command_allows_in_project_subdir(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src" / "pkg").mkdir(parents=True)
    assert validate_command("cat src/pkg/mod.py", cwd=proj, project_root=proj) is None
    assert validate_command("cat pkg/mod.py", cwd=proj / "src", project_root=proj) is None


def test_validate_command_allows_redirect_into_system_temp(tmp_path):
    import tempfile

    target = Path(tempfile.gettempdir()) / "sandbox-redirect-test.log"
    command = f"echo hi > {target.as_posix()}"
    assert validate_command(command, cwd=tmp_path, project_root=tmp_path) is None


def test_validate_command_denies_redirect_outside_project_and_temp(tmp_path):
    assert validate_command("echo hi > /etc/x", cwd=tmp_path, project_root=tmp_path) is not None


# ---------------------------------------------------------------------------
# allowlisted absolute command (semgrep venv console script) + pentest channels
# ---------------------------------------------------------------------------


def test_validate_command_allows_semgrep_absolute_command(tmp_path):
    """/opt/semgrep-venv/bin/semgrep is the allowlisted venv console script
    (semgrep hard-requires mcp 1.29.0, incompatible with agent-core's
    mcp>=2.0.0 in one site-packages). As the COMMAND token it is exempt from
    the absolute-path rule; as an ARGUMENT it is still an absolute path and
    must stay rejected."""
    assert validate_command(
        "HOME=.tmp/pentest/home /opt/semgrep-venv/bin/semgrep "
        "--config auto --json --output=security/evidence/semgrep.json src",
        cwd=tmp_path, project_root=tmp_path,
    ) is None
    assert validate_command(
        "/opt/semgrep-venv/bin/semgrep --version",
        cwd=tmp_path, project_root=tmp_path,
    ) is None
    assert validate_command(
        "cat /opt/semgrep-venv/bin/semgrep",
        cwd=tmp_path, project_root=tmp_path,
    ) is not None


def test_validate_command_pip_install_gate(tmp_path):
    """`python3 -m pip install` mutates site-packages — blocked. --dry-run is
    the accepted dependency-resolution channel (pip-audit shells out to
    exactly this internally); pip_audit itself stays untouched."""
    assert validate_command(
        "python3 -m pip install requests",
        cwd=tmp_path, project_root=tmp_path,
    ) is not None
    assert validate_command(
        "python3 -m pip install --dry-run --ignore-installed "
        "--report .tmp/pentest/resolve.json -r .tmp/pentest/req.txt",
        cwd=tmp_path, project_root=tmp_path,
    ) is None
    assert validate_command(
        "python3 -m pip_audit -r requirements.txt",
        cwd=tmp_path, project_root=tmp_path,
    ) is None


def test_validate_command_export_assignment_validation(tmp_path):
    """`export NAME=value` is the same assignment as a leading VAR=value
    prefix — PATH/LD_PRELOAD stays refused (the PATH-hijack of allowlisted
    commands), absolute values stay refused, and $ values (which tokenize
    into substitution fragments) stay refused."""
    assert validate_command(
        "export HOME=.tmp/pentest/home; python3 -m bandit -r src",
        cwd=tmp_path, project_root=tmp_path,
    ) is None
    assert validate_command(
        "export PATH=./evil; git status",
        cwd=tmp_path, project_root=tmp_path,
    ) is not None
    assert validate_command(
        "export HOME=/root; ls",
        cwd=tmp_path, project_root=tmp_path,
    ) is not None
    assert validate_command(
        "export HOME=$UNSET; ls",
        cwd=tmp_path, project_root=tmp_path,
    ) is not None
    assert validate_command(
        "echo hi; export HOME=x; echo $HOME",
        cwd=tmp_path, project_root=tmp_path,
    ) is None


# ---------------------------------------------------------------------------
# sitecustomize runtime guard — real subprocesses
# ---------------------------------------------------------------------------


def test_guard_allows_project_and_temp_access(sandbox_dirs):
    proj_a, _, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    r = run_python(
        "import tempfile, pathlib;"
        "print(pathlib.Path('local.txt').read_text());"
        "f = tempfile.NamedTemporaryFile(delete=False); f.write(b'x'); f.close();"
        "print('tmp-ok')",
        cwd=proj_a, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "local-data" in r.stdout and "tmp-ok" in r.stdout


def test_guard_denies_read_outside_project(sandbox_dirs):
    proj_a, _, fake_temp = sandbox_dirs
    r = run_python("open('../proj-b/secret.txt').read()", cwd=proj_a, env=guarded_env(proj_a, fake_temp))
    assert r.returncode != 0
    assert "agent-guard" in r.stderr and "PermissionError" in r.stderr


def test_guard_denies_write_outside_project(sandbox_dirs):
    proj_a, proj_b, fake_temp = sandbox_dirs
    r = run_python("open('../proj-b/evil.txt', 'w').write('x')", cwd=proj_a, env=guarded_env(proj_a, fake_temp))
    assert r.returncode != 0
    assert "agent-guard" in r.stderr
    assert not (proj_b / "evil.txt").exists()


def test_guard_denies_listdir_outside(sandbox_dirs):
    proj_a, _, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    r = run_python("import os; os.listdir('..')", cwd=proj_a, env=env)
    assert r.returncode != 0 and "agent-guard" in r.stderr
    # NOTE: os.stat emits no audit event in CPython 3.12, so metadata probing
    # is not blocked; content reads are (see test_guard_denies_read_outside_project).


def test_guard_denies_rename_across_projects(sandbox_dirs):
    proj_a, proj_b, fake_temp = sandbox_dirs
    r = run_python(
        "import os; os.rename('local.txt', '../proj-b/moved.txt')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp),
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    assert not (proj_b / "moved.txt").exists()


def test_guard_denies_copyfile_outside(sandbox_dirs):
    proj_a, proj_b, fake_temp = sandbox_dirs
    r = run_python(
        "import shutil; shutil.copyfile('local.txt', '../proj-b/copy.txt')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp),
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    assert not (proj_b / "copy.txt").exists()


def test_guard_blocks_pty_and_os_system(sandbox_dirs):
    proj_a, _, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    # ctypes import stays allowed (pandas imports it at module level); the
    # tools' text-level regexes reject user code that names it explicitly.
    r = run_python("import ctypes", cwd=proj_a, env=env)
    assert r.returncode == 0, r.stderr
    r = run_python("import pty", cwd=proj_a, env=env)
    assert r.returncode != 0 and "agent-guard" in r.stderr
    r = run_python("import os; os.system('echo hi')", cwd=proj_a, env=env)
    assert r.returncode != 0 and "agent-guard" in r.stderr
    r = run_python("import os; os.execvp('echo', ['echo'])", cwd=proj_a, env=env)
    assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_allows_exec_into_allowlisted_dir(sandbox_dirs, tmp_path):
    """os.exec into a _AGENT_EXEC_ALLOWLIST directory passes the audit hook
    (non-strict only): the semgrep console script execvp's the native
    osemgrep binary from its venv this way, and the hook must step aside or
    the scan dies at startup. The target here does not exist, so a pass
    surfaces as FileNotFoundError from the OS rather than an [agent-guard]
    denial - asserting the hook's decision without depending on real exec
    semantics (which differ between POSIX and Windows)."""
    proj_a, _, fake_temp = sandbox_dirs
    allow = tmp_path / "allowbin"
    allow.mkdir()
    env = guarded_env(proj_a, fake_temp)
    env["_AGENT_EXEC_ALLOWLIST"] = str(allow)
    target = str(allow / "no-such-binary")
    r = run_python(
        f"import os; os.execvp({target!r}, [{target!r}])", cwd=proj_a, env=env,
    )
    assert r.returncode != 0
    assert "agent-guard" not in r.stderr
    assert "FileNotFoundError" in r.stderr


def test_guard_blocks_exec_outside_allowlist(sandbox_dirs, tmp_path):
    """Setting the allowlist admits only targets inside it; a sibling
    directory - and a PATH-resolved bare name, which carries no directory to
    check - stays denied."""
    proj_a, _, fake_temp = sandbox_dirs
    allow = tmp_path / "allowbin"
    allow.mkdir()
    env = guarded_env(proj_a, fake_temp)
    env["_AGENT_EXEC_ALLOWLIST"] = str(allow)
    target = str(tmp_path / "elsewhere" / "no-such-binary")
    r = run_python(
        f"import os; os.execvp({target!r}, [{target!r}])", cwd=proj_a, env=env,
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_strict_blocks_exec_even_in_allowlist(sandbox_dirs, tmp_path):
    """Strict mode ignores the exec allowlist entirely: code_executor scripts
    must not swap the guarded interpreter out from under the Popen block."""
    proj_a, _, fake_temp = sandbox_dirs
    allow = tmp_path / "allowbin"
    allow.mkdir()
    env = guarded_env(proj_a, fake_temp, strict=True)
    env["_AGENT_EXEC_ALLOWLIST"] = str(allow)
    target = str(allow / "no-such-binary")
    r = run_python(
        f"import os; os.execvp({target!r}, [{target!r}])", cwd=proj_a, env=env,
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_strict_blocks_subprocess_popen(sandbox_dirs):
    proj_a, _, fake_temp = sandbox_dirs
    # The import itself stays allowed (matplotlib/pandas need it); the Popen
    # CALL is what strict mode denies.
    r = run_python(
        "import subprocess; subprocess.run(['echo', 'hi'], capture_output=True)",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp, strict=True),
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    r = run_python(
        "import subprocess; print('ok')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp, strict=True),
    )
    assert r.returncode == 0, r.stderr
    # Non-strict keeps subprocess fully usable (pytest plugins may need it).
    r = run_python(
        "import subprocess; subprocess.run(['echo', 'hi'], capture_output=True); print('ok')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp),
    )
    assert r.returncode == 0, r.stderr


def test_guard_strict_blocks_spawn_family(sandbox_dirs):
    """os.spawn* and os.posix_spawn run an UNGUARDED child (cat /etc/passwd
    etc.) — strict mode must deny both: the os.spawn audit event, and the
    event-free os.posix_spawn via the startup monkeypatch."""
    proj_a, _, fake_temp = sandbox_dirs
    r = run_python(
        "import os; os.spawnl(os.P_WAIT, 'echo', 'echo', 'hi')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp, strict=True),
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    # os.posix_spawn exists only on POSIX — on Windows there is no such
    # function, hence no escape surface to test.
    r = run_python(
        "import os; assert hasattr(os, 'posix_spawn')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp, strict=True),
    )
    if r.returncode == 0:
        r = run_python(
            "import os; os.posix_spawn('/bin/echo', ['echo', 'hi'], {})",
            cwd=proj_a, env=guarded_env(proj_a, fake_temp, strict=True),
        )
        assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_blocks_direct_fork_outside_strict(sandbox_dirs):
    """A direct os.fork() is denied in non-strict mode too: the forked child
    could setsid() out of the shell tool's kill tree and keep running. Only
    multiprocessing's own worker fork is admitted (see
    test_guard_non_strict_allows_multiprocessing_pool). os.fork does not
    exist on Windows — nothing to test there."""
    proj_a, _, fake_temp = sandbox_dirs
    r = run_python(
        "import os; assert hasattr(os, 'fork')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp),
    )
    if r.returncode == 0:
        r = run_python(
            "import os; os.fork()",
            cwd=proj_a, env=guarded_env(proj_a, fake_temp),
        )
        assert r.returncode != 0 and "agent-guard" in r.stderr


@pytest.mark.skipif(os.name != "posix", reason="multiprocessing fork start method is POSIX-only")
def test_guard_non_strict_allows_multiprocessing_pool(sandbox_dirs):
    """multiprocessing.Pool forks its workers from popen_fork._launch — the
    one fork call site the non-strict guard admits (detect-secrets' scan
    command parallelizes through exactly this path). The workers inherit the
    audit hook, so the file guard stays armed inside them: a worker reading
    a sibling project is denied, not silently unguarded."""
    proj_a, proj_b, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    ok = (
        "import multiprocessing as mp\n"
        "def probe(path):\n"
        "    return len(open(path).read())\n"
        "with mp.Pool(2) as p:\n"
        "    assert p.map(probe, ['local.txt', 'local.txt']) == [10, 10]\n"
        "    print('pool-ok')\n"
    )
    r = run_python(ok, cwd=proj_a, env=env)
    assert r.returncode == 0 and "pool-ok" in r.stdout
    escape = (
        "import multiprocessing as mp\n"
        "def probe(path):\n"
        "    return len(open(path).read())\n"
        "with mp.Pool(2) as p:\n"
        f"    p.map(probe, [{str(proj_b / 'secret.txt')!r}])\n"
    )
    r = run_python(escape, cwd=proj_a, env=env)
    assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_strict_blocks_fork(sandbox_dirs):
    """os.fork duplicates the process — a forked child would survive the
    parent's kill (kill reaches only the direct child) and keep running
    unguarded (CPU + writes). The os.fork audit event (also fired by
    os.forkpty) is denied in strict mode. os.fork does not exist on
    Windows — nothing to test there."""
    proj_a, _, fake_temp = sandbox_dirs
    r = run_python(
        "import os; assert hasattr(os, 'fork')",
        cwd=proj_a, env=guarded_env(proj_a, fake_temp, strict=True),
    )
    if r.returncode == 0:
        r = run_python(
            "import os; os.fork()",
            cwd=proj_a, env=guarded_env(proj_a, fake_temp, strict=True),
        )
        assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_blocks_network_access(sandbox_dirs):
    """Sandboxed python must not reach the network (Redis/MSSQL/API on the
    compose network): socket creation, connect, and DNS resolution are all
    denied by the audit hook."""
    proj_a, _, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    # Constructor and resolution are denied independently of any real peer.
    r = run_python(
        "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
        cwd=proj_a, env=env,
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    r = run_python(
        "import socket; socket.getaddrinfo('127.0.0.1', 80)",
        cwd=proj_a, env=env,
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    # create_connection routes through getaddrinfo first — same denial.
    r = run_python(
        "import socket; socket.create_connection(('127.0.0.1', 1))",
        cwd=proj_a, env=env,
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    # The import itself stays allowed; only the network calls are blocked.
    r = run_python("import socket; print('ok')", cwd=proj_a, env=env)
    assert r.returncode == 0 and "ok" in r.stdout


def test_guard_allows_socketpair_self_pipe(sandbox_dirs):
    """socket.socketpair() must work under the guard — asyncio's event loop
    builds its self-pipe from it, so blocking its internal socket()/connect
    calls would break every sandboxed `asyncio.run()`. The pair's endpoints
    connect only to each other, so no network escape opens; a DIRECT unix or
    inet socket stays blocked (see test_guard_blocks_network_access)."""
    proj_a, _, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    r = run_python(
        "import socket; a, b = socket.socketpair(); "
        "a.sendall(b'ping'); assert b.recv(4) == b'ping'; "
        "a.close(); b.close(); print('pair-ok')",
        cwd=proj_a, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "pair-ok" in r.stdout
    # asyncio needs the self-pipe at event-loop startup.
    r = run_python(
        "import asyncio; asyncio.run(asyncio.sleep(0)); print('asyncio-ok')",
        cwd=proj_a, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "asyncio-ok" in r.stdout
    # A direct AF_UNIX socket is NOT a socketpair endpoint — still denied
    # (where the platform exposes AF_UNIX at all).
    import socket as _socket

    if hasattr(_socket, "AF_UNIX"):
        r = run_python(
            "import socket; socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)",
            cwd=proj_a, env=env,
        )
        assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_allows_network_by_default(sandbox_dirs):
    """Without _AGENT_NETWORK_MODE the guard intercepts nothing — the
    SANDBOX_NETWORK=unset default gives agent code full Playwright
    functionality (downloads, screen recording, outbound APIs)."""
    proj_a, _, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    env.pop("_AGENT_NETWORK_MODE", None)
    r = run_python(
        "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
        "socket.getaddrinfo('127.0.0.1', 80); print('net-ok')",
        cwd=proj_a, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "net-ok" in r.stdout


def test_guard_localhost_mode_blocks_nonloopback(sandbox_dirs):
    """SANDBOX_NETWORK=localhost keeps the loopback (Playwright's WebSocket
    to its browser subprocess) but blocks any other destination."""
    proj_a, _, fake_temp = sandbox_dirs
    env = guarded_env(proj_a, fake_temp)
    env["_AGENT_NETWORK_MODE"] = "localhost"
    r = run_python(
        "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
        "socket.getaddrinfo('127.0.0.1', 80); print('loopback-ok')",
        cwd=proj_a, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "loopback-ok" in r.stdout
    r = run_python(
        "import socket; socket.getaddrinfo('example.com', 80)",
        cwd=proj_a, env=env,
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr


def test_guard_sets_dont_write_bytecode(sandbox_dirs):
    proj_a, _, fake_temp = sandbox_dirs
    r = run_python("import sys; print(sys.dont_write_bytecode)", cwd=proj_a, env=guarded_env(proj_a, fake_temp))
    assert r.returncode == 0 and "True" in r.stdout


def test_guard_noop_without_project_root(sandbox_dirs):
    """Without _AGENT_PROJECT_ROOT the sitecustomize import is a no-op."""
    proj_a, proj_b, fake_temp = sandbox_dirs
    env = dict(os.environ)
    env.pop("_AGENT_PROJECT_ROOT", None)
    env.pop("_AGENT_BLOCK_SUBPROCESS", None)
    env["PYTHONPATH"] = str(GUARD_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    r = run_python(
        "import sys; print(sys.dont_write_bytecode); print(open('../proj-b/secret.txt').read())",
        cwd=proj_a, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "False" in r.stdout and "secret-data" in r.stdout


def test_guard_keeps_pytest_working(sandbox_dirs):
    """python -m pytest must run under the guard (config/cache in project,
    tmp_path in the temp write root, stdlib/site-packages readable)."""
    proj_a, _, fake_temp = sandbox_dirs
    (proj_a / "test_trivial.py").write_text(
        "def test_ok(tmp_path):\n"
        "    (tmp_path / 'f.txt').write_text('x')\n"
        "    assert (tmp_path / 'f.txt').read_text() == 'x'\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "test_trivial.py", "-q"],
        cwd=proj_a, env=guarded_env(proj_a, fake_temp),
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert "1 passed" in r.stdout


def test_guard_allows_home_tool_caches(sandbox_dirs, tmp_path):
    """node-gyp/matplotlib-style cache writes under $HOME must pass the guard
    (npm native builds spawn Python via the shell tool), while writing to
    $HOME itself stays denied."""
    proj_a, _, fake_temp = sandbox_dirs
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    env = guarded_env(proj_a, fake_temp)
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)  # Windows expanduser fallback
    r = run_python(
        "import os; p = os.path.expanduser('~/.cache/node-gyp');"
        "os.makedirs(p, exist_ok=True);"
        "open(os.path.join(p, 'x'), 'w').write('y'); print('cache-ok')",
        cwd=proj_a, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "cache-ok" in r.stdout

    r = run_python(
        "import os; open(os.path.expanduser('~/evil.txt'), 'w').write('x')",
        cwd=proj_a, env=env,
    )
    assert r.returncode != 0 and "agent-guard" in r.stderr
    assert not (fake_home / "evil.txt").exists()


def test_guard_chains_shadowed_sitecustomize(sandbox_dirs):
    """A pre-existing sitecustomize on PYTHONPATH (e.g. a corporate SSL shim)
    must still load even though GUARD_DIR shadows it."""
    proj_a, _, fake_temp = sandbox_dirs
    # Put the shim inside fake_temp so the guard's read roots allow the exec.
    shim_dir = fake_temp / "shim"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text("print('SHIM-LOADED')", encoding="utf-8")
    env = guarded_env(proj_a, fake_temp)
    env["PYTHONPATH"] = env["PYTHONPATH"] + os.pathsep + str(shim_dir)
    r = run_python("print('main-ok')", cwd=proj_a, env=env)
    assert r.returncode == 0, r.stderr
    assert "SHIM-LOADED" in r.stdout and "main-ok" in r.stdout


def test_guard_stays_armed_when_command_overrides_pythonpath(sandbox_dirs, tmp_path):
    """A command that assigns PYTHONPATH itself (e.g. `PYTHONPATH=packages/src
    python -m pytest`) replaces the tool-injected guard dir. The container
    image also ships sitecustomize.py in site-packages (Dockerfile) so the
    guard must still load from a site.py-searched directory; the user-site
    directory simulates that channel here."""
    proj_a, proj_b, fake_temp = sandbox_dirs
    (proj_b / "secret.txt").write_text("secret-data", encoding="utf-8")
    if sys.platform == "win32":
        appdata = tmp_path / "appdata"
        site_dir = appdata / "Python" / f"Python{sys.version_info.major}{sys.version_info.minor}" / "site-packages"
    else:
        home = tmp_path / "home"
        site_dir = home / ".local" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site_dir.mkdir(parents=True)
    shutil.copy2(GUARD_DIR / "sitecustomize.py", site_dir / "sitecustomize.py")

    env = guarded_env(proj_a, fake_temp)
    # Simulate the agent's leading assignment overriding the tool-injected
    # PYTHONPATH entirely (guard dir absent).
    env["PYTHONPATH"] = "src"
    if sys.platform == "win32":
        env["APPDATA"] = str(appdata)
    else:
        env["HOME"] = str(home)
    r = run_python("print(open('../proj-b/secret.txt').read())", cwd=proj_a, env=env)
    assert r.returncode != 0, r.stdout
    assert "agent-guard" in r.stderr and "PermissionError" in r.stderr
    # Sanity: the guard only works because sitecustomize loads at startup —
    # with site processing skipped (-S) the same env is genuinely unguarded.
    # -S also keeps this hermetic on machines whose real site-packages ships
    # the guard copy (e.g. the container image).
    r = subprocess.run(
        [sys.executable, "-S", "-c", "print(open('../proj-b/secret.txt').read())"],
        cwd=proj_a, env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "secret-data" in r.stdout


# ---------------------------------------------------------------------------
# browser tool SSRF gate — port policy follows the SSRF_ENABLED env flag
# ---------------------------------------------------------------------------


def _check_browser_url(url: str) -> None:
    from agent_core.tools.browser_playwright import _check_browser_url as fn

    return fn(url)


def test_browser_url_gate_allows_any_port_when_ssrf_disabled(tmp_path, monkeypatch):
    """With SSRF_ENABLED off (the default), the port allowlist must not apply
    — dev frontends/backends live on arbitrary ports (5173, 8081, ...)."""
    monkeypatch.delenv("SSRF_ENABLED", raising=False)
    _check_browser_url("http://localhost:8081/page")
    _check_browser_url("http://localhost:5173/")
    # Literal scheme/metadata checks still apply without the env gate.
    with pytest.raises(Exception):
        _check_browser_url("ftp://localhost/x")
    with pytest.raises(Exception):
        _check_browser_url("http://169.254.169.254/latest")


def test_browser_url_gate_enforces_port_allowlist_when_ssrf_enabled(tmp_path, monkeypatch):
    """With SSRF_ENABLED=1 the port allowlist applies to every navigation."""
    monkeypatch.setenv("SSRF_ENABLED", "true")
    with pytest.raises(Exception):
        _check_browser_url("http://localhost:8081/page")
    # Allowlisted port on a public IP passes (localhost would be DNS-blocked
    # under SSRF_ENABLED regardless of port; 8.8.8.8 resolves numerically).
    _check_browser_url("http://8.8.8.8:5173/")


def test_inet_aton_mapper():
    """The alternate numeric forms ipaddress rejects still map to real IPs
    via glibc inet_aton (verified against a python:3.12-slim container) —
    the mapper must reproduce exactly what getaddrinfo would connect to."""
    from agent_core.tools._ssrf import _inet_aton_mapped

    assert _inet_aton_mapped("2130706433") == "127.0.0.1"
    assert _inet_aton_mapped("0x7f000001") == "127.0.0.1"
    assert _inet_aton_mapped("2852039166") == "169.254.169.254"
    assert _inet_aton_mapped("127.1") == "127.0.0.1"
    assert _inet_aton_mapped("017700000001") == "127.0.0.1"
    assert _inet_aton_mapped("0x7f.0.0.1") == "127.0.0.1"
    assert _inet_aton_mapped("010.0.0.1") == "8.0.0.1"
    # Canonical forms round-trip unchanged.
    assert _inet_aton_mapped("127.0.0.1") == "127.0.0.1"
    assert _inet_aton_mapped("8.8.8.8") == "8.8.8.8"
    assert _inet_aton_mapped("1.2.3.4") == "1.2.3.4"
    # Not inet_aton forms — real hostnames / inert strings.
    assert _inet_aton_mapped("example.com") is None
    assert _inet_aton_mapped("x1.2.3.4") is None
    assert _inet_aton_mapped("999.1.2.3") is None
    assert _inet_aton_mapped("127.0.0.1.") is None
    assert _inet_aton_mapped("08.0.0.1") is None
    assert _inet_aton_mapped("1.2.3.4.5") is None
    assert _inet_aton_mapped("0x") is None
    assert _inet_aton_mapped("") is None


def test_browser_url_gate_blocks_inet_aton_alternate_ip_forms(tmp_path, monkeypatch):
    """The always-on literal-IP guard must also catch the inet_aton alternate
    forms ipaddress rejects — with SSRF_ENABLED off (the default) glibc's
    getaddrinfo would connect them to loopback/metadata unblocked."""
    monkeypatch.delenv("SSRF_ENABLED", raising=False)
    for url in (
        "http://2130706433/",  # 127.0.0.1
        "http://0x7f000001:8080/",  # 127.0.0.1, non-allowlisted port
        "http://2852039166/latest/meta-data/",  # 169.254.169.254
        "http://127.1/",  # 127.0.0.1
        "http://017700000001/",  # 127.0.0.1 (octal)
        "http://0x7f.0.0.1/",  # 127.0.0.1 (hex component)
    ):
        with pytest.raises(Exception, match="Blocked IP"):
            _check_browser_url(url)
    # Public mapped forms and real hostnames are unaffected.
    _check_browser_url("http://010.0.0.1/")  # 8.0.0.1 — public
    _check_browser_url("http://8.8.8.8/")
    _check_browser_url("http://example.com/")


def test_api_request_rejects_inet_aton_alternate_ip_forms(monkeypatch):
    """api_request's own literal check (independent of SSRF_ENABLED) rejects
    the same alternate forms."""
    from agent_core.tools.api_request import _validate_url_safety

    monkeypatch.delenv("SSRF_ENABLED", raising=False)
    for url in (
        "http://2130706433:8000/health",
        "http://2852039166/latest/meta-data/iam/security-credentials/",
        "http://127.1/x",
    ):
        assert _validate_url_safety(url, None) is not None
    assert _validate_url_safety("http://8.8.8.8/x", None) is None
    assert _validate_url_safety("http://example.com/x", None) is None


def test_spawn_in_new_session_platform_kwargs():
    """POSIX spawns go into their own session so terminate_process_tree can
    reach grandchildren; Windows asyncio subprocess has no session support."""
    kwargs = spawn_in_new_session()
    if os.name == "posix":
        assert kwargs == {"start_new_session": True}
    else:
        assert kwargs == {}


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
async def test_terminate_process_tree_kills_grandchild():
    """proc.kill() alone leaves a backgrounded grandchild (`sleep 300 &`)
    running — killpg on the child's session must take the whole tree down."""
    import asyncio
    import signal

    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", "sleep 30 & wait",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **spawn_in_new_session(),
    )
    await asyncio.sleep(0.3)  # let the grandchild fork into the session
    terminate_process_tree(proc)
    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode == -signal.SIGKILL
