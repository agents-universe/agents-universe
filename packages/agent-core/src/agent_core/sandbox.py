"""Sandbox helpers: Python subprocess guard env + shared shell command validator.

Used by the shell tool (PR-2) and later by code_executor's bash mode (PR-3).
Path containment itself lives in agent_core.paths (PR-1).
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import tempfile
from pathlib import Path

from .paths import is_within

GUARD_DIR = Path(__file__).parent / "sandbox_guard"

_ROOT_ENV = "_AGENT_PROJECT_ROOT"
_BLOCK_SUBPROCESS_ENV = "_AGENT_BLOCK_SUBPROCESS"


def spawn_in_new_session(*, posix_only: bool = True) -> dict[str, bool]:
    """kwargs helper for create_subprocess_exec: put the child in its own
    session so terminate_process_tree() can reach every descendant.
    Windows asyncio subprocess does not support start_new_session."""
    if posix_only and os.name != "posix":
        return {}
    return {"start_new_session": True}


def terminate_process_tree(proc) -> None:
    """Kill the whole process tree, not just the direct child.

    A forked/backgrounded grandchild (``sleep 300 &``, ``sh -c '... &'``)
    would otherwise survive proc.kill() and keep running — and writing —
    after the tool returns or times out. The child must have been spawned
    with spawn_in_new_session() so killpg reaches every descendant; on
    Windows (no process groups) fall back to plain kill().
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # already gone
    else:
        proc.kill()


def _guard_pythonpath(project_root: str | Path, inherited: str) -> str:
    """GUARD_DIR followed by inherited PYTHONPATH entries that stay in-project.

    The API server inherits PYTHONPATH=/app/src (framework source, outside
    every project workspace) into every tool-spawned Python. Leaving that
    entry in place is worse than dead: imports silently skip it (the guard's
    PermissionError is swallowed by the import machinery) and resolve to
    whatever wheel is installed instead — a branch checkout's tests would run
    against stale site-packages code — and tools like pip trip the guard while
    scanning sys.path. Relative entries are kept (the child's cwd is always
    inside the project), as are absolute entries inside the project; everything
    else is dropped.
    """
    parts = [str(GUARD_DIR)]
    if not inherited:
        return os.pathsep.join(parts)
    root = Path(project_root).resolve()
    for entry in inherited.split(os.pathsep):
        if not entry:
            parts.append(entry)  # interior empty entry == cwd (inside the project)
            continue
        candidate = Path(entry)
        # Treat a leading '/' (or '\') as absolute even on Windows, where
        # Path.is_absolute() only recognizes drive letters.
        if not candidate.is_absolute() and not entry.startswith(("/", "\\")):
            candidate = root / candidate  # resolve relative entries in-project
        try:
            if is_within(root, candidate):
                parts.append(entry)
        except OSError:
            continue  # unresolvable entry — fail closed
    return os.pathsep.join(parts)


def python_guard_env(project_root: str | Path, *, strict: bool = False) -> dict[str, str]:
    """Env vars that arm the sitecustomize guard in spawned Python subprocesses.

    Prepends GUARD_DIR to PYTHONPATH and sets ``_AGENT_PROJECT_ROOT``; the
    child interpreter then auto-imports ``sandbox_guard/sitecustomize.py``
    which installs the audit hook. The env is inherited by any nested Python
    the command spawns, so the guard propagates. strict=True additionally
    blocks ``import subprocess`` (used by code_executor; the shell tool stays
    non-strict so pytest plugins that need subprocess keep working). Inherited
    PYTHONPATH entries outside the project are dropped (see
    ``_guard_pythonpath``). TEMP/TMP/TMPDIR are deliberately left untouched.
    """
    env = {_ROOT_ENV: str(Path(project_root).resolve())}
    env["PYTHONPATH"] = _guard_pythonpath(project_root, os.environ.get("PYTHONPATH", ""))
    if strict:
        env[_BLOCK_SUBPROCESS_ENV] = "1"
    return env


# ---------------------------------------------------------------------------
# Shared command validator
# ---------------------------------------------------------------------------

# shlex punctuation tokens that separate commands inside one command string.
_SEPARATORS = frozenset({"&&", "||", ";;", ";", "|", "&", "(", ")"})

# Redirections whose following token is a file path that must be validated.
# ">&" with a non-numeric word redirects BOTH stdout and stderr to that file
# (`cat >& ../x`), and "<>" opens a file read-write — both are bash-side
# opens the python audit hook never sees, so their targets are paths, not
# fd numbers. (`2>&1` stays safe: the "1" is simply path-checked as a
# project-relative single-character name.)
_REDIRECT_PATH_OPS = frozenset({">", ">>", ">|", "&>", "&>>", "<", ">&", "<>"})
# Redirections whose following token is NOT a path (heredoc delimiter, fd number).
_REDIRECT_SKIP_OPS = frozenset({"<<", "<<<", "<&"})

_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_PY_RE = re.compile(r"^python3?(?:\.\d+)?$")
_SEP_RE = re.compile(r"[\\/]")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Absolute-path commands admitted by the shell tool's allowlist explicitly.
# semgrep hard-requires mcp 1.29.0 at import time (incompatible with
# agent-core's mcp>=2.0.0 in one site-packages), so it is installed in its
# own venv and invoked via its console script. Only the COMMAND token is
# exempt from the absolute-path rule; its arguments pass every path check.
_ALLOWED_ABSOLUTE_COMMANDS = frozenset({"/opt/semgrep-venv/bin/semgrep"})
# command-substitution payloads inside DATA-command args are
# executed for real — extracted for recursive validation. Single-level;
# nested substitutions keep their outer content validated via the path
# checks on the containing argument.
_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _skip_subst_parens(text: str, start: int) -> int | None:
    """Index of the `)` closing a $(...) region at *start* (points past the
    `$(`). Nested parens count depth; a `)` inside a quoted string does not
    close the region. Returns None when never closed."""
    depth = 1
    n = len(text)
    j = start
    while j < n:
        c = text[j]
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if c in ("'", '"'):
            q = c
            j += 1
            while j < n:
                if q == '"' and text[j] == "\\" and j + 1 < n and text[j + 1] in ('"', "\\"):
                    j += 2
                    continue
                if text[j] == q:
                    break
                j += 1
            j += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _check_all_substitutions(
    text: str, *, cwd: Path, root: Path, single_quotes_literal: bool = True
) -> str | None:
    """Validate command-substitution payloads at EVERY nesting level.

    _SUBST_RE only captures the INNERMOST $(...) — bash executes the full
    nested chain, so `echo "$(cat $(echo /etc/passwd))"` reads host files
    unless each layer is validated on its own. Every layer is validated as
    its own command, with its inner substitutions still in place — the token
    checks then see a substitution in a path position of the outer layer
    (`cat $(echo /etc/passwd)` resolves to `cat $` → rejected).

    Single-quoted regions are literal on the command line (``'$(cat
    /etc/passwd)'`` prints, never executes) and skipped; unquoted heredoc
    bodies keep bash's real semantics — quotes are plain characters there,
    so callers pass single_quotes_literal=False to scan them.
    """
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'" and single_quotes_literal:
            end = text.find("'", i + 1)
            if end == -1:
                break  # unterminated — bash syntax error, fail closed
            i = end + 1
            continue
        if c == "`":
            end = text.find("`", i + 1)
            if end == -1:
                break
            payload = text[i + 1 : end]
            reason = _validate_substitution_payload(payload, cwd=cwd, root=root)
            if reason:
                return reason
            i = end + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            close = _skip_subst_parens(text, i + 2)
            if close is None:
                return "Unclosed $(...); refusing to run"
            payload = text[i + 2 : close]
            reason = _validate_substitution_payload(payload, cwd=cwd, root=root)
            if reason:
                return reason
            i = close + 1
            continue
        i += 1
    return None


def _validate_substitution_payload(payload: str, *, cwd: Path, root: Path) -> str | None:
    """Validate one $(...) payload as its own command."""
    payload = payload.strip()
    if not payload:
        return None
    if payload.startswith("<"):
        return "Command substitution reading a file ($(< ...)) is not allowed"
    # The payload may itself nest further substitutions — validate_command
    # re-enters _check_all_substitutions for those.
    return validate_command(payload, cwd=cwd, project_root=root, allow_substitution=True)

_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "add", "commit", "push", "pull", "fetch",
    "checkout", "switch", "restore", "branch", "tag", "stash", "blame",
    "rev-parse", "ls-files", "merge", "init",
    # Common workflow subcommands — all operate inside the repository; path
    # arguments still go through the per-token path validation below.
    "reset", "rm", "mv", "revert", "rebase", "clean", "cherry-pick", "cherry",
    "apply", "am", "config", "submodule", "worktree", "reflog", "rev-list",
    "describe", "shortlog", "count-objects", "ls-remote", "grep", "show-ref",
    "symbolic-ref", "for-each-ref", "ls-tree", "cat-file", "name-rev",
    "archive", "bisect", "notes", "range-diff", "remote",
})
# git options that redirect the repository/work-tree location or run arbitrary
# external commands — rejected anywhere inside a git invocation.
_GIT_FORBIDDEN_LONG = ("--git-dir", "--work-tree", "--exec-path")
# `git config` scopes that would write outside the project (~/.gitconfig, /etc).
_GIT_CONFIG_FORBIDDEN_SCOPES = frozenset({"--global", "--system"})

# Config keys whose values git EXECUTES later, on a different command than
# the one writing them: core.fsmonitor runs on EVERY status (including the
# git_repo tool's cleanliness check), filter.* on add/checkout, alias.* on
# invocation, core.pager/sshCommand/hooksPath/difftool.*/mergetool.* on their
# triggers, include.path reads other config files. The `git -c` check
# rejects these; the `git config key value` WRITE path must too — the value
# lands in .git/config and fires without this command ever running again.
_GIT_CONFIG_EXECUTABLE_KEY_RE = re.compile(
    r"^(?:alias\.|core\.(?:pager|fsmonitor|sshCommand|hooksPath|editor)|"
    r"credential\.helper|filter\.|difftool\.|mergetool\.|include\.path|"
    r"diff\.(?:external|[A-Za-z0-9_.-]+\.(?:command|textconv)))",
    re.IGNORECASE,
)
_NODE_EVAL_OPTS = frozenset({"-e", "--eval", "-p", "--print"})

# Commands whose arguments are all data, never file paths.
_DATA_COMMANDS = frozenset({"echo", "printf", "pwd", "tr", "true", "false"})

# Commands whose FIRST positional argument is data (a pattern/program) while
# any remaining positionals are file paths (grep <pattern> <files...>).
_FIRST_POS_DATA = frozenset({"grep", "egrep", "fgrep", "jq", "awk", "gawk", "sed"})

# Per-command options whose SEPARATE value token is data, not a path
# (e.g. `git commit -m "mentions ../x"` must not trip the path checks).
_DATA_VALUE_OPTS: dict[str, frozenset[str]] = {
    "git": frozenset({
        "-m", "-b", "-t", "-u", "--message", "--author", "--date",
        "--format", "--pretty", "--abbrev", "--depth", "-j", "--jobs",
        "--grep", "-i", "-e",
    }),
    "grep": frozenset({"-e", "--regexp", "--label"}),
    "find": frozenset({
        "-name", "-iname", "-path", "-ipath", "-regex", "-regextype", "-type",
        "-size", "-user", "-group", "-perm", "-mtime", "-mmin", "-ctime",
        "-cmin", "-atime", "-amin", "-maxdepth", "-mindepth", "-printf",
    }),
    "awk": frozenset({"-F", "-v", "--field-separator", "--assign"}),
    "gawk": frozenset({"-F", "-v", "--field-separator", "--assign"}),
    "sed": frozenset({"-e", "--expression"}),
    "cut": frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields", "--characters", "--bytes"}),
    "sort": frozenset({"-t", "-k", "--field-separator", "--key"}),
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes", "--pid", "-s", "--sleep-interval"}),
    "jq": frozenset({"--arg", "--argjson", "--raw-input", "--indent", "-n"}),
    "python": frozenset({"-c", "-m", "-W", "-X"}),
    "node": frozenset({"-e", "-p", "--eval", "--print"}),  # rejected by the eval scan anyway
}

# Per-command options whose separate value token IS a path to validate.
_PATH_VALUE_OPTS: dict[str, frozenset[str]] = {
    "git": frozenset({"-C", "-F", "--file"}),
    "jq": frozenset({"-f", "--from-file"}),
    "awk": frozenset({"-f"}),
    "gawk": frozenset({"-f"}),
    "sed": frozenset({"-f", "--file"}),
    "grep": frozenset({"-f", "--file"}),
    "sort": frozenset({"-o", "--output"}),
    "find": frozenset({"-newer", "-anewer", "-cnewer"}),
}


def _cmd_key(cmd_base: str) -> str:
    """Normalize a command basename for the option tables (python3.12 -> python)."""
    return "python" if _PY_RE.match(cmd_base) else cmd_base


# git -c values must be inert literals: no whitespace (core.pager='cat ../x'
# would execute via shell word-splitting), no '!' (alias.!cmd), and no shell
# metacharacters (; | & > < ` $). A real config need — user.name, core.autocrlf,
# commit.gpgsign, init.defaultBranch — is always a plain literal.
_SAFE_GIT_C_RE = re.compile(r"^[A-Za-z0-9_.-]+=[A-Za-z0-9_.:/@+=-]*$")

# Config keys whose values may legitimately contain spaces (git -c
# user.name="First Last"). These keys are inert text/booleans — git never
# executes them — so a space-containing value is safe as long as it carries
# no shell metacharacters and no escape path. Execute-prone keys (core.pager,
# alias.*, credential.helper, ...) deliberately stay off this list, so a
# quoted `git -c core.pager='cat ../x'` still falls through to rejection.
_GIT_C_SPACE_OK_KEYS = frozenset({
    "user.name", "user.email", "commit.template",
    "init.defaultbranch", "core.autocrlf", "core.eol", "core.safecrlf",
})
# Shell metacharacters that would survive shlex quote removal and make a
# space-containing -c value dangerous (word-splitting, substitution, aliases).
_GIT_C_DANGEROUS_VALUE_RE = re.compile(r"[;|&><`$!\\'\"]")


def _is_safe_git_c_value(value: str) -> bool:
    # The config VALUE (after the first '=') must not smuggle an executable
    # path: `core.pager=../evil` — git executes the pager relative to the
    # repo root, so '../evil' escapes the project . _check_simple on
    # the full "name=value" string misses it (the '..' is embedded in the
    # name segment); check the value part directly. URL-ish values
    # (remote.origin.url=https://...) keep working — they start with a
    # scheme, not '/', '~' or '..'.
    _name, _eq, val = value.partition("=")
    if not _eq:
        return False
    # diff.external / diff.<driver>.command|textconv run the value with the
    # diffed FILES as arguments — `git config diff.external sh` makes git
    # execute each old/new file as a script. Reject the family outright (even
    # a bare command name is unsafe here); inert diff.renames/color etc. do
    # not match and keep working.
    if re.fullmatch(
        r"diff\.external|diff\.[A-Za-z0-9_.-]+\.(?:command|textconv)", _name,
        re.IGNORECASE,
    ):
        return False
    # Executable keys (filters, pagers, fsmonitor, aliases...) run the value
    # as a COMMAND via the shell — a bare command name (cat) is the only
    # acceptable value. A path separator or leading '.' would resolve to a
    # project script whose content was never validated: `git -c
    # core.fsmonitor=./evil.sh status` executes it on every status, the
    # ./script escape via a git trigger.
    if _GIT_CONFIG_EXECUTABLE_KEY_RE.match(_name):
        return bool(re.fullmatch(r"[A-Za-z0-9_-]+", val)) and _check_simple(val) is None
    if _SAFE_GIT_C_RE.match(value):
        return _check_simple(val) is None
    # Whitespace values (user.name="First Last"): only for keys git never
    # executes, and only when the value is free of shell metacharacters.
    if _name.lower() in _GIT_C_SPACE_OK_KEYS and not _GIT_C_DANGEROUS_VALUE_RE.search(val):
        return _check_simple(val) is None
    return False


def has_unclosed_quote(text: str) -> bool:
    """True when shlex cannot tokenize *text* because a quote is left open.

    Used by code_executor to group physical lines into logical lines before
    validating (a quoted string may legitimately span multiple lines).
    """
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        list(lexer)
        return False
    except ValueError as exc:
        return "quotation" in str(exc)


def validate_command(
    command: str,
    *,
    cwd: Path,
    project_root: Path,
    allow_substitution: bool = False,
    tainted_vars: frozenset[str] | None = None,
    pre_assigned_vars: set[str] | None = None,
) -> str | None:
    """Validate a shell command token-by-token. Returns None to allow, else the reason.

    Validation-only: the original command string is still what gets executed,
    so a parse failure fails closed.  ``cwd`` must already be resolved inside
    ``project_root`` by the caller.

    Command substitution (``$(...)`` / backticks) and variable-expanded paths
    (``$var/x``) stay banned unless *allow_substitution* is True: a nested or
    expanded value could smuggle a path out as DATA (``cat $(printf '../x')``,
    ``p=../proj-b; cat $p/x``), which token-level validation can never see.
    Leading ``VAR=value`` assignments are validated themselves (the value may
    be expanded later), so ``p=../proj-b`` is rejected even though the
    assignment touches no files. code_executor's bash mode allows substitution
    because its _BLOCKED_BASH regex strips the external commands that could
    abuse it, its env vars all point inside the project, and the raw inner
    text is still tokenized (crudely) by the checks below.

    Path checks are command-aware: quoted data strings (commit messages, grep
    patterns, ``-c`` code) are skipped via per-command option tables, while
    real path arguments (positionals of file commands, redirection targets,
    git -C values, ...) are still confined to the project — redirection
    targets may additionally live in the system temp dir.
    """
    if not allow_substitution and ("$(" in command or "`" in command):
        return "Command substitution ($(...) or backticks) is not allowed"

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        return f"Command could not be parsed safely ({exc}); refusing to run"

    cwd = Path(cwd).resolve()
    root = Path(project_root).resolve()
    temp = Path(tempfile.gettempdir()).resolve()

    # Every substitution payload — at every nesting level — must pass
    # validation as its own command. _SUBST_RE alone misses the OUTER layers
    # of nested $(...), which bash still executes (echo "$(cat $(echo
    # /etc/passwd))" reads host files if the outer `cat ...` layer is never
    # checked).
    if allow_substitution and ("$(" in command or "`" in command):
        reason = _check_all_substitutions(command, cwd=cwd, root=root)
        if reason:
            return reason

    # Variable names assigned earlier in this command string. Only these may
    # appear in later path arguments ($f) — their values were validated at the
    # assignment site. Host env vars ($HOME, $PATH) expand to anything.
    # pre_assigned_vars seeds the set from earlier lines of the same script
    # (code_executor's per-line validator): an assignment on line 1 is still
    # in effect on line 3, and its value was checked when the line passed.
    assigned_vars = set(pre_assigned_vars or ())

    for segment in _split_segments(tokens):
        reason = _validate_segment(
            segment, cwd=cwd, root=root, temp=temp,
            allow_substitution=allow_substitution,
            assigned_vars=assigned_vars, tainted_vars=tainted_vars,
        )
        if reason:
            return reason
    return None


# Characters that may combine into one shlex token yet still act as
# separators — shlex glues `);` into a single token, and treating it as an
# argument would merge the following command into the previous segment's
# data-argument list, skipping its path checks entirely.
_SEPARATOR_CHARS = frozenset(";&|()")


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """Split the token stream on command separators (pipes, &&, ;, ...)."""
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS or (
            tok and all(c in _SEPARATOR_CHARS for c in tok)
        ):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _validate_segment(
    tokens: list[str], *, cwd: Path, root: Path, temp: Path,
    allow_substitution: bool, assigned_vars: set[str],
    tainted_vars: frozenset[str] | None = None,
) -> str | None:
    """Validate one command segment (command + its arguments/redirections)."""
    i = 0
    # Leading VAR=value assignments are environment setup, not paths — but the
    # value may later be expanded through $VAR, so it must not be an escape
    # path itself (e.g. `p=../proj-b; cat $p/x`).
    while i < len(tokens) and _ASSIGN_RE.match(tokens[i]):
        reason = _check_assignment(tokens[i], allow_dollar=allow_substitution,
                                   assigned_vars=assigned_vars)
        if reason:
            return reason
        # Only names with a NON-EMPTY value get "assigned" status: `x=`
        # expands to nothing at runtime, so `cat $x/etc/hostname` silently
        # becomes `cat /etc/hostname` — an absolute path — past the checks.
        if tokens[i].split("=", 1)[1]:
            assigned_vars.add(tokens[i].partition("=")[0])
        i += 1
    if i >= len(tokens):
        return None

    cmd = tokens[i]
    # A ./-prefixed command executes an in-project script whose CONTENT was
    # never validated — `echo 'cat /etc/passwd' > run; chmod +x run; ./run`
    # smuggles an arbitrary host-read past every check (the `. run` dot-form
    # is blocked by code_executor's _BLOCKED_BASH; a bare `./run` validates
    # as a plain in-project path, then executes unguarded in bash mode). Only
    # the ./mvnw ./gradlew build wrappers keep executing.
    if cmd.startswith("./") and cmd not in ("./mvnw", "./gradlew"):
        return (
            f"Command {cmd!r}: executing in-project scripts is not allowed; "
            "the shell tool's allowlist covers script execution"
        )
    if cmd not in _ALLOWED_ABSOLUTE_COMMANDS:
        reason = _check_path(cmd, cwd=cwd, root=root, allow_dollar=allow_substitution,
                             assigned_vars=assigned_vars, tainted_vars=tainted_vars)
        if reason:
            return reason
    cmd_base = cmd.replace("\\", "/").rsplit("/", 1)[-1]  # ./mvnw -> mvnw
    key = _cmd_key(cmd_base)
    is_awk_sed = cmd_base in ("awk", "gawk", "sed")
    is_pattern_cmd = cmd_base in ("grep", "egrep", "fgrep")
    is_jq = cmd_base == "jq"
    data_opts = _DATA_VALUE_OPTS.get(key, frozenset())
    path_opts = _PATH_VALUE_OPTS.get(key, frozenset())
    is_python = bool(_PY_RE.match(cmd_base))
    is_node = cmd_base == "node"
    is_git = cmd_base == "git"
    is_find = cmd_base == "find"
    pattern_opts: frozenset[str] = frozenset()
    if is_pattern_cmd:
        pattern_opts = frozenset({"-e", "--regexp", "-f", "--file"})
    elif is_jq:
        pattern_opts = frozenset({"-f", "--from-file"})
    i += 1

    if cmd_base in ("export", "local", "declare", "typeset", "readonly"):
        # `export NAME=value` (and the declare family) is the same assignment
        # as a leading VAR=value prefix — `export PATH=/tmp/evil` must not
        # slip past as a path argument (the PATH-hijack of allowlisted
        # commands: PATH=./evil makes the next `git` run an in-project script
        # whose content was never validated). Values are validated like any
        # assignment; a name is tracked as assigned only when its value is a
        # plain literal — a `$`-containing value is tokenized into fragments
        # (`x=$(echo ...)` -> `x=$` `(` ...) that would otherwise dodge the
        # substitution guards. Bare names (`export FOO`) are inert: the value
        # is the current env's, so the name is NOT tracked and a later $FOO
        # in a path position is still rejected. Redirections after the names
        # are validated by the argument loop below.
        while i < len(tokens) and _ASSIGN_RE.match(tokens[i]):
            reason = _check_assignment(tokens[i], allow_dollar=allow_substitution,
                                       assigned_vars=assigned_vars)
            if reason:
                return reason
            value = tokens[i].split("=", 1)[1]
            if value and "$" not in value:
                assigned_vars.add(tokens[i].partition("=")[0])
            i += 1

    skip_next = False        # previous option consumes the next token as data
    first_pos_seen = False   # for _FIRST_POS_DATA commands
    # For awk/sed: an expression came from -e/--expression/-f/--file, so the
    # first positional arg is a FILE OPERAND, not the program string. Without
    # this, `sed -e 's/^/x/' /etc/passwd` would scan "/etc/passwd" as the
    # (empty-checked) program and never run path validation on it — arbitrary
    # file reads.
    program_seen = False
    # For grep/egrep/fgrep/jq: the pattern/program came from an OPTION
    # (-e/--regexp/-f/--file/--from-file), so the first positional is a FILE
    # OPERAND, not data — without this `grep -e foo ../sibling/secret` read
    # outside the project .
    pattern_seen = False
    git_subcommand: str | None = None
    git_config_key: str | None = None  # first positional of `git config`
    pip_seen = False  # `python3 -m pip` (or attached `-mpip`) — gate on the subcommand below

    while i < len(tokens):
        tok = tokens[i]

        if tok in _REDIRECT_SKIP_OPS:
            i += 2  # heredoc delimiter / fd number — not a path
            continue
        if tok in _REDIRECT_PATH_OPS:
            if i + 1 >= len(tokens):
                return f"Redirection {tok!r} without a target"
            reason = _check_redirect(
                tokens[i + 1], cwd=cwd, root=root, temp=temp,
                allow_dollar=allow_substitution, assigned_vars=assigned_vars, tainted_vars=tainted_vars,
            )
            if reason:
                return reason
            i += 2
            continue

        if skip_next:
            skip_next = False
            i += 1
            continue

        if tok.startswith("-") and tok != "-":
            opt_name = tok.split("=", 1)[0]
            # `python3 -m pip ...` (incl. the attached `-mpip` form) — the
            # module is consumed by data-option skip_next below, so record it
            # here for the pip-subcommand gate at the first positional.
            if is_python and opt_name.startswith("-m"):
                module = tokens[i + 1] if opt_name == "-m" and i + 1 < len(tokens) else opt_name[2:]
                if module == "pip":
                    pip_seen = True
            # find -exec/-execdir/-ok/-okdir execute arbitrary
            # commands via the shell. The path checks below only cover find's
            # own arguments — the executed command's args are never seen. This
            # is the only gate for these flags (there is no raw-string
            # blocklist: grep patterns may legitimately mention "-exec").
            if is_find and opt_name in ("-exec", "-execdir", "-ok", "-okdir", "-delete"):
                return (
                    f"find {opt_name} is not allowed: it would execute "
                    "arbitrary commands or recursively delete the project "
                    "workspace on the server"
                )
            if is_awk_sed and opt_name in _PATH_VALUE_OPTS.get(key, frozenset()):
                return (
                    f"{cmd_base} {opt_name} is not allowed: a script-file program "
                    "would bypass the program-string escape scan"
                )
            if is_awk_sed and "=" not in tok:
                # Attached option forms: shlex merges `-e'1p'` into the single
                # token `-e1p`, whose option name matches no table — the value
                # was then silently skipped and the NEXT positional was scanned
                # as an (escape-checked) program string instead of a path:
                # `sed -e'1p' /etc/passwd` read any file . Attached
                # -f/--file programs are rejected like their separated forms;
                # attached -e/--expression/--source programs are scanned for
                # escapes and set program_seen like the separated forms.
                if opt_name.startswith("-f") or opt_name.startswith("--file"):
                    return (
                        f"{cmd_base} attached {opt_name!r} is not allowed: a "
                        "script-file program would bypass the program-string "
                        "escape scan"
                    )
                for expr_opt in _EXPRESSION_VALUE_OPTS.get(key, frozenset()):
                    if opt_name.startswith(expr_opt) and len(opt_name) > len(expr_opt):
                        reason = _check_program_string(tok[len(expr_opt):], cmd_base)
                        if reason:
                            return reason
                        program_seen = True
                        break
            if is_pattern_cmd or is_jq:
                # The pattern/program comes from an option: `grep -e foo FILE`,
                # `jq -f prog.jq FILE` — the first positional is then a FILE
                # OPERAND that must pass path checks instead of being skipped
                # as data (`grep -e foo ../sibling/secret` bypassed).
                # Attached forms (-efoo, -fFILE, --regexp'foo') glue the value
                # onto the option token; separated forms are consumed by the
                # data/path option tables below.
                for pat_opt in pattern_opts:
                    if opt_name == pat_opt:
                        pattern_seen = True
                        break
                    if "=" not in tok and opt_name.startswith(pat_opt):
                        attached_val = tok[len(pat_opt):]
                        if pat_opt in ("-f", "--file", "--from-file"):
                            reason = _check_path(attached_val, cwd=cwd, root=root,
                                                 allow_dollar=allow_substitution,
                                                 assigned_vars=assigned_vars, tainted_vars=tainted_vars)
                            if reason:
                                return reason
                        pattern_seen = True
                        break
            # `-c` may appear as `-c name=value` or the attached `-cname=value`
            # (accepted by older git builds). The attached form splits as
            # opt_name="-cname", so a plain `== "-c"` check lets it fall into
            # the generic "=" branch whose value scan only rejects paths —
            # `git -calias.x='!cat ../sibling/.env' x` would then pass.
            # The check only applies BEFORE the subcommand: `git commit -c
            # HEAD~1`, `git grep -c foo`, `git log -c`, `git branch -c old
            # new` are the SUBCOMMAND's own options, not configs .
            if is_git and git_subcommand is None and (opt_name == "-c" or opt_name.startswith("-c")):
                # `git -c name=value` sets one config for this invocation. Most
                # values are inert literals, but alias.!/pager/filter/credential
                # values are executed via the shell — treating them as plain
                # data would let `git -c alias.x='!cat ../sibling/.env' x`
                # escape the project. Only accept plain literals.
                if tok.startswith("-c") and "=" in tok:
                    # Attached form `-ccore.autocrlf=true`: the whole suffix is
                    # the config string — splitting on the first '=' would tear
                    # the value away from its name and reject every attached
                    # form .
                    value = tok[2:]
                elif "=" in tok:
                    value = tok.split("=", 1)[1]
                elif i + 1 < len(tokens):
                    value = tokens[i + 1]
                    i += 1
                else:
                    return "git -c requires a value"
                if not _is_safe_git_c_value(value):
                    return (
                        "git -c value must be a plain literal (name=value with "
                        "no spaces, quotes or shell metacharacters); use repo "
                        "config instead"
                    )
                i += 1
                continue
            if "=" in tok:
                value = tok.split("=", 1)[1]
                if is_awk_sed and opt_name in _EXPRESSION_VALUE_OPTS.get(key, frozenset()):
                    reason = _check_program_string(value, cmd_base)
                    if reason:
                        return reason
                    program_seen = True
                elif opt_name in path_opts:
                    reason = _check_path(value, cwd=cwd, root=root,
                                         allow_dollar=allow_substitution,
                                         assigned_vars=assigned_vars, tainted_vars=tainted_vars)
                    if reason:
                        return reason
                elif opt_name not in data_opts:
                    if not allow_substitution and "$" in value:
                        vars_used = _dollar_vars(value)
                        if not vars_used or not all(v in assigned_vars for v in vars_used):
                            return (
                                f"Option {opt_name!r} value {value!r} uses a "
                                "variable that is not assigned in this command; "
                                "assign it first or use a literal path"
                            )
                    reason = _check_simple(value)
                    if reason:
                        return reason
            elif opt_name in path_opts:
                if i + 1 >= len(tokens):
                    return f"Option {opt_name!r} without a value"
                reason = _check_path(tokens[i + 1], cwd=cwd, root=root,
                                     allow_dollar=allow_substitution,
                                     assigned_vars=assigned_vars, tainted_vars=tainted_vars)
                if reason:
                    return reason
                i += 1  # consume the path value
                if is_awk_sed:
                    # awk/sed -f/--file supplies the program file — the next
                    # positional arg is a file operand (see program_seen).
                    program_seen = True
            elif is_awk_sed and opt_name in _EXPRESSION_VALUE_OPTS.get(key, frozenset()):
                if i + 1 >= len(tokens):
                    return f"Option {opt_name!r} without a value"
                reason = _check_program_string(tokens[i + 1], cmd_base)
                if reason:
                    return reason
                i += 1  # consume the program value
                program_seen = True
            elif opt_name in data_opts:
                skip_next = True
            if is_git and opt_name.startswith(_GIT_FORBIDDEN_LONG):
                return (
                    f"git option {opt_name!r} is not allowed: it would point git at "
                    "a repository outside the project"
                )
            if is_git and git_subcommand == "config" and opt_name in _GIT_CONFIG_FORBIDDEN_SCOPES:
                return f"git config {opt_name} writes outside the project; use repo-local config"
            if is_python and not opt_name.startswith("--"):
                # -S/-I/-E (incl. clusters like -IS) skip sitecustomize or
                # ignore PYTHONPATH, disarming the runtime file guard.
                if set(opt_name[1:]) & set("SIE"):
                    return (
                        f"{cmd_base} with -S/-I/-E is not allowed: it would bypass "
                        "the Python runtime file guard"
                    )
            if is_node and opt_name in _NODE_EVAL_OPTS:
                return (
                    f"node {opt_name} is not allowed; "
                    "write a script file and run it instead"
                )
            i += 1
            continue

        # --- positional token ---
        if is_git and git_subcommand is None:
            git_subcommand = tok
            reason = _check_git_subcommand(tok)
            if reason:
                return reason
            i += 1
            continue
        if is_git and git_subcommand == "config":
            # `git config key value` WRITES .git/config — executable keys
            # (core.fsmonitor etc.) fire on LATER commands, including the
            # git_repo tool's own status checks; the `git -c` check rejects
            # them, the write path must too. Remaining values must be inert
            # literals like the -c check requires. Read/delete forms
            # (`--get/--unset/--remove-section key`) carry no value.
            if git_config_key is None:
                git_config_key = tok
            else:
                if _GIT_CONFIG_EXECUTABLE_KEY_RE.match(git_config_key):
                    return (
                        f"git config {git_config_key!r} is not allowed: git "
                        "executes this key on later commands; keep the "
                        "setting in a repo file instead"
                    )
                if not _is_safe_git_c_value(f"{git_config_key}={tok}"):
                    return (
                        f"git config {git_config_key!r} value {tok!r} must "
                        "be a plain literal; keep it in a repo file instead"
                    )
            i += 1
            continue
        if is_python or is_node:
            # First positional is the script file — validated as a path.
            # Everything after it is the script's own argv (runtime guard
            # confines actual file access) — EXCEPT redirections: bash
            # performs them itself before the child runs, so the runtime
            # guard never sees those file opens. `python script.py >
            # ../sibling/out` wrote outside the project unchallenged.
            reason = _check_path(tok, cwd=cwd, root=root,
                                 allow_dollar=allow_substitution,
                                 assigned_vars=assigned_vars, tainted_vars=tainted_vars)
            if reason:
                return reason
            # `python3 -m pip install` mutates the interpreter's
            # site-packages; --dry-run is the accepted dependency-RESOLUTION
            # channel (pip-audit shells out to exactly this internally) and
            # writes nothing. argv after the module is the script's own, so
            # the pip subcommand is the only thing worth checking here.
            if is_python and (pip_seen or tok == "pip"):
                # With `-m pip` the module was consumed by skip_next, so the
                # first positional IS the subcommand; without `-m`, `pip` is
                # the positional and the subcommand follows it.
                rest = tokens[i + 1:] if tok == "pip" else tokens[i:]
                first_word = next(
                    (t for t in rest if not t.startswith("-") and not _ASSIGN_RE.match(t)),
                    None,
                )
                if first_word == "install" and "--dry-run" not in rest:
                    return (
                        "pip install is not allowed: it mutates the Python "
                        "environment; use 'python3 -m pip install --dry-run' "
                        "for dependency resolution or python3 -m pip_audit "
                        "for CVE scans"
                    )
            j = i + 1
            while j < len(tokens):
                rest = tokens[j]
                if rest in _REDIRECT_PATH_OPS:
                    if j + 1 >= len(tokens):
                        return f"Redirection {rest!r} without a target"
                    reason = _check_redirect(
                        tokens[j + 1], cwd=cwd, root=root, temp=temp,
                        allow_dollar=allow_substitution,
                        assigned_vars=assigned_vars, tainted_vars=tainted_vars,
                    )
                    if reason:
                        return reason
                    j += 1
                j += 1
            return None
        if cmd_base in _DATA_COMMANDS:
            # Data args are inert text — EXCEPT command substitutions, which
            # bash executes for real. echo "$(cat /etc/passwd)"
            # skipped every check (allow_substitution=True) and read host
            # files unchallenged. Substitution payloads are validated for the
            # WHOLE command by _check_all_substitutions at the top of
            # validate_command — on the raw text, so single quotes (literal)
            # and escaped `\$(` are honored. No re-check here: the
            # shlex-joined token stream has already lost the quoting
            # information (`echo '$(cat /etc/passwd)'` re-joins without
            # quotes and would be flagged as a live substitution).
            i += 1
            continue
        if cmd_base in _FIRST_POS_DATA and not first_pos_seen:
            first_pos_seen = True
            if is_awk_sed:
                if program_seen:
                    # The program came from -e/--expression/-f/--file — this
                    # positional is a FILE OPERAND and must pass path checks
                    # (e.g. `sed -e 's/^/x/' /etc/passwd`).
                    reason = _check_path(tok, cwd=cwd, root=root,
                                         allow_dollar=allow_substitution,
                                         assigned_vars=assigned_vars, tainted_vars=tainted_vars)
                else:
                    # The program string is skipped by the path checks — scan it
                    # for escape primitives instead (awk system()/getline, sed e).
                    reason = _check_program_string(tok, cmd_base)
            elif pattern_seen:
                # The pattern/program came from an option (-e/-f/--regexp/...)
                # — this positional is a FILE OPERAND and must pass path checks
                # (`grep -e foo ../sibling/secret`).
                reason = _check_path(tok, cwd=cwd, root=root,
                                     allow_dollar=allow_substitution,
                                     assigned_vars=assigned_vars, tainted_vars=tainted_vars)
            if reason:
                return reason
            i += 1
            continue
        if cmd_base in ("cp", "mv"):
            # cp/mv write to their positional targets — `cp hook.sh
            # .git/hooks/pre-commit` installs a hook past the redirect check
            # (`cp -t dir src` and multi-source forms make "the second
            # positional" ambiguous, so every positional is checked; reading
            # FROM .git has no legitimate project use — fail-closed).
            reason = _git_internal_path(tok)
            if reason:
                return reason
        reason = _check_path(tok, cwd=cwd, root=root,
                             allow_dollar=allow_substitution,
                             assigned_vars=assigned_vars, tainted_vars=tainted_vars)
        if reason:
            return reason
        i += 1

    return None


def _check_git_subcommand(subcommand: str) -> str | None:
    if subcommand not in _GIT_SUBCOMMANDS:
        return (
            f"git subcommand {subcommand!r} is not allowed. "
            f"Supported: {sorted(_GIT_SUBCOMMANDS)}"
        )
    return None


def _git_internal_path(tok: str) -> str | None:
    """Return a reason string if *tok* targets anything inside ``.git/``.

    ``.git/hooks/*`` installs a hook git executes unvalidated on later
    commands (the ./script escape via git's own trigger); ``.git/config``
    can repoint ``hooksPath`` at any directory; nested repos hide hooks at
    ``subdir/.git/hooks/`` and submodules at ``.git/modules/<name>/hooks/``.
    Git itself manages .git — no project operation needs to write there.
    """
    rel = tok.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    if ".git" in rel.split("/"):
        return (
            f"Path {tok!r} writes inside .git — hooks and config are git "
            "internals that execute unvalidated code or corrupt the repo; "
            "keep scripts in the project and run them explicitly"
        )
    return None


def _check_redirect(
    tok: str, *, cwd: Path, root: Path, temp: Path, allow_dollar: bool,
    assigned_vars: set[str], tainted_vars: frozenset[str] | None = None,
) -> str | None:
    """Redirection targets must stay in the project or the system temp dir."""
    reason = _git_internal_path(tok)
    if reason:
        return reason
    reason = _check_path(tok, cwd=cwd, root=root, allow_dollar=allow_dollar,
                         assigned_vars=assigned_vars, tainted_vars=tainted_vars)
    if reason is None:
        return None
    # Absolute paths inside the system temp dir (e.g. > /tmp/sorted.txt) are
    # fine — the temp dir is ephemeral and already a write root of the Python
    # runtime guard.
    if tok.startswith("/") or _DRIVE_RE.match(tok):
        try:
            if is_within(temp, Path(tok).resolve()):
                return None
        except OSError:
            pass
    return reason


def _has_param_expansion(value: str) -> bool:
    """True when *value* uses ${...} with anything beyond a plain name.

    `${x:-/etc}` / `${x:=y}` / `${x:+y}` / `${x%a}` / `${x#b}` expand to a
    value a later path position would use verbatim — `${x:-/etc}` makes
    `cat $x/passwd` an absolute path. Plain references (`$x`, `${x}`) stay
    traceable: their value was validated at the assignment site.
    """
    for m in re.finditer(r"\$\{([^}]*)\}", value):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)):
            return True
    return False


# Environment variables that change how commands are RESOLVED or EXECUTED:
# assigning one lets a command's arguments become the code that runs
# (PATH=. python executes ./python; GIT_EXTERNAL_DIFF='cat /etc/passwd' runs
# on every diff; LD_PRELOAD loads any shared object; GIT_DIR repoints the
# repo). Their values are never valid project data — refuse the assignment
# by name, regardless of the value.
_EXECUTION_ENV_VARS = frozenset({
    "PATH", "CDPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV",
    "SHELLOPTS", "BASHOPTS",
    "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_PAGER",
    "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_ASKPASS",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
})


def _check_assignment(tok: str, *, allow_dollar: bool,
                      assigned_vars: set[str] | None = None) -> str | None:
    """Validate the value of a leading ``VAR=value`` assignment.

    The value may be expanded through ``$VAR`` in a later argument, so an
    escape path hidden in the value (``p=../proj-b``) must be rejected even
    though the assignment itself touches no files.  ``allow_dollar`` relaxes
    the check for code_executor's bash mode, where the environment variables
    (OUTPUT_DIR, PROJECT_DIR, HOME) all point inside the project — but
    parameter expansions (${x:-...}) still expand to anything and are
    rejected.
    """
    name, eq, value = tok.partition("=")
    if name in _EXECUTION_ENV_VARS:
        return (
            f"Assignment to {name!r} is not allowed: it changes how commands "
            "are resolved or executed (path hijack, external diff/editor, "
            "repo redirection); use allowlisted commands with literal paths"
        )
    if not eq:
        return None
    if not allow_dollar and "$" in value:
        return (
            f"Assigned value {value!r} contains variable expansion, which "
            "cannot be validated; assign a literal path instead"
        )
    if allow_dollar and _has_param_expansion(value):
        return (
            f"Assigned value {value!r} contains a parameter expansion "
            "(${{x:-...}}) whose result could be any path; assign a literal "
            "or a plain variable reference instead"
        )
    if allow_dollar:
        # A plain `$VAR` reference must be TRACEABLE: host env vars ($SHELL,
        # $PATH) expand to host paths, and an unset var expands to EMPTY —
        # `x=$UNSET; cat $x/etc/hostname` collapses to an absolute path, the
        # empty-assignment escape revived with a non-empty value. Only names
        # assigned earlier in this command (or in code_executor, earlier
        # lines) and the server-injected project-internal vars qualify.
        for ref in _dollar_vars(value):
            if ref not in (assigned_vars or ()) and ref not in _SAFE_ENV_PATH_VARS:
                return (
                    f"Assigned value {value!r} references {ref!r}, which is "
                    "neither assigned earlier nor a project-internal env "
                    "var; assign a literal or an earlier-assigned name "
                    "instead"
                )
    return _check_simple(value)


def _check_simple(value: str) -> str | None:
    """Absolute/tilde paths and '..' segments are never allowed."""
    if value in ("", "-", ".", "/dev/null"):
        return None
    if value.startswith("/") or value.startswith("~"):
        return (
            f"Absolute path {value!r} is not allowed. "
            "Use paths relative to the project directory."
        )
    if any(seg == ".." for seg in _SEP_RE.split(value)):
        return f"'..' path segments are not allowed (in {value!r}); stay inside the project directory"
    return None


# awk/gawk/sed commands whose separate-value option is a program string that
# gets scanned for escapes (sed -e '...', gawk -e/--source '...'). awk's own
# program is only ever the first positional.
_EXPRESSION_VALUE_OPTS: dict[str, frozenset[str]] = {
    "gawk": frozenset({"-e", "--source"}),
    "sed": frozenset({"-e", "--expression"}),
}

# Escape primitives inside awk/gawk/sed program strings. The program string is
# skipped by the token-level path checks (it is data, not a path), so this scan
# is the only gate between a pattern and awk system()/getline or sed's `e` —
# each of which executes arbitrary shell commands or reads/writes files outside
# the project. Deliberately narrow so legitimate programs ({print $1},
# s/foo/bar/g) pass. Double-quoted segments are masked before scanning so
# string literals an agent might legitimately print never trip the patterns.
_AWK_PROGRAM_ESCAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsystem\s*\("),   # executes a shell command
    re.compile(r"\bpopen\s*\("),
    re.compile(r"\|&"),             # two-way pipe — spawns a process
    re.compile(r"\|\s*\""),         # one-way pipe to a command string — shell
    re.compile(r"\bgetline\b"),     # reads arbitrary files via < "path"
)
_SED_PROGRAM_ESCAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsystem\s*\("),
    re.compile(r"\bpopen\s*\("),
    # sed's standalone `e` command — pipes the rest of the line to the shell.
    # The preceding character may also be the address delimiter (`/./e`,
    # `\#HOST#e` uses a backslash-escaped delimiter as the address, `!` the
    # negated address). The command letter must follow an
    # address/separator character, optionally across whitespace (`1 e`); a
    # plain space before `e` is NOT enough — text commands like `a\hello e`
    # embed the letter in literal text. No trailing constraint after `e`:
    # `1ewhoami` and `ecat /etc/passwd` execute the rest of the line
    # verbatim. `\n` joins the preceding class so a line-start `e` in a
    # multi-line script (`s/x/y/\ne cat ...`) is hit too; the rare false
    # positive this allows (`a\` continuation text starting with `e`) is
    # accepted — fail-closed direction.
    re.compile(r"(?:^|[$0-9,;{}/\\\n!]\s*)e"),
    # Backslash-escaped delimiter addresses (`\#HOST#e`) — \x ... x e with the
    # same delimiter both opening and closing the address; the command may be
    # separated by whitespace (`\#HOST# eid`). No trailing constraint either
    # (`\#HOST#ecat /etc/passwd` runs `cat /etc/passwd`).
    re.compile(r"\\(.)[^\\\n]*\1\s*e"),
    # `:label e` — a label definition followed by the `e` command; the label
    # is a single word and `e` follows after whitespace. A rare false
    # positive (label text containing ` e`) is accepted, fail-closed.
    re.compile(r"(?:^|[;:\s])[A-Za-z_][A-Za-z0-9_]*[ \t]+e"),
    # s///e / y///e flag — GNU sed executes the replacement via the shell.
    # The delimiter is any non-blank character (GNU sed has no fixed
    # separator set), and `e` is a flag unless it sits in `w`'s attached
    # filename (`s/x/y/we` writes file `e`, `s/x/y/ew` still runs the shell).
    # The s/y prefix keeps text-command payloads (`c\done`, `a\hello`) from
    # being scanned as substitution commands. The trailing class admits the
    # count forms (`s|x|y|e2` still executes the replacement via the shell).
    re.compile(r"(?:s|y)([^\\\n\s])(?:\\.|[^\\\n])*?\1(?:\\.|[^\\\n])*?\1(?:(?!w)[A-Za-z])*?e(?=(?:(?!w)[A-Za-z0-9])*$|w|[; \t}\n0-9]|$)"),
)

# Attached file targets on sed r/w/R/W commands (`1r/etc/passwd`,
# `$w out.txt`) — the filename is glued to the command token and a whole
# token's _check_simple never sees it (verified GNU sed reads the
# attached target). Extracted and checked like any other path token.
_SED_ATTACHED_TARGET_RE = re.compile(r"(?:^|[;}\s])(?:\d+|\$)?[rwRW]\s*([^;\s}\n]+)")
# Numeric-address attached forms (`1,10w/tmp/x`, `2,5r/etc/passwd`) — the
# address digits/comma precede the command letter, which the pattern above
# (single digit or $ only) cannot consume.
_SED_ADDR_TARGET_RE = re.compile(r"(?:^|[;}\s])(?:\d+(?:,\d+)?|\$)[rwRW]\s*([^;\s}\n]+)")
# s///w / s///gw flags with the filename glued on (`s/x/y/w/tmp/out`) — GNU
# sed takes everything after the w flag as the filename. The delimiter is any
# non-blank character (matching the s///e pattern above).
_SED_SFLAG_W_TARGET_RE = re.compile(r"s([^\\\n\s])[^\\\n]*?\1[^\\\n]*?\1(?:[A-Za-z]*w)\s*([^;\s}\n]*)")


def _check_program_string(program: str, cmd: str) -> str | None:
    """Scan an awk/gawk/sed program string for sandbox-escape primitives.

    Token-level path checks skip these quoted strings, so a pattern like
    ``awk 'system("curl ...")'`` or ``sed '1e'`` would otherwise escape the
    sandbox. Also checks filenames embedded in the program (awk string
    literals, sed's bare r/w targets) with the same absolute/tilde/dotdot
    rules as the token checks.
    """
    masked = re.sub(r'"(?:[^"\\]|\\.)*"', '""', program)
    patterns = _SED_PROGRAM_ESCAPES if cmd == "sed" else _AWK_PROGRAM_ESCAPES
    for pattern in patterns:
        if pattern.search(masked):
            return f"{cmd} program {program!r} contains an escape primitive ({pattern.pattern!r}); not allowed"
    # Filenames inside the program: awk double-quoted strings, sed's bare r/w
    # targets — reject absolute/tilde/dotdot paths like the token checks do.
    if cmd == "sed":
        try:
            lexer = shlex.shlex(program, posix=True)
            lexer.whitespace_split = True
            # sed has no `#` comment syntax (unlike sh/awk) — `#` is a legal
            # substitution delimiter (`s#a#b#w/tmp/x`), so it must not be
            # stripped here or the glued-on w target is never extracted.
            lexer.commenters = ""
            candidates = list(lexer)
        except ValueError:
            candidates = program.split()
        for tok in candidates[1:]:
            reason = _check_simple(tok)
            if reason:
                return f"{cmd} program {program!r}: {reason}"
        # Attached r/w/R/W targets glue onto ANY program token — including the
        # first (`1r/etc/passwd`), which the [1:] slice above deliberately
        # skips (a leading address regex like `/^$/d` must not be rejected as
        # an absolute path).
        for tok in candidates:
            for pattern in (_SED_ATTACHED_TARGET_RE, _SED_ADDR_TARGET_RE, _SED_SFLAG_W_TARGET_RE):
                for target in pattern.findall(tok):
                    # _SED_SFLAG_W_TARGET_RE captures the delimiter (for its
                    # \1 backreference) alongside the filename — take the
                    # last group.
                    if isinstance(target, tuple):
                        target = target[-1]
                    reason = _check_simple(target)
                    if reason:
                        return f"{cmd} program {program!r}: {reason}"
    else:
        for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', program):
            reason = _check_simple(m.group(1))
            if reason:
                return f"{cmd} program {program!r}: {reason}"
    return None


_DOLLAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Server-injected env vars whose values are guaranteed project-internal paths
# (code_executor's safe_env sets HOME/OUTPUT_DIR/PROJECT_DIR; PWD is bash's
# own, anchored at the project cwd). These are the only host env vars allowed
# in path positions — $SHELL/$PATH/$USER etc. expand to anything at runtime.
_SAFE_ENV_PATH_VARS = frozenset({"HOME", "OUTPUT_DIR", "PROJECT_DIR", "PWD"})


def _dollar_vars(tok: str) -> list[str]:
    """Variable names referenced by $var / ${var} expansions in *tok*."""
    return [m.group(1) or m.group(2) for m in _DOLLAR_RE.finditer(tok)]


def _check_path(
    tok: str, *, cwd: Path, root: Path, allow_dollar: bool, assigned_vars: set[str],
    tainted_vars: frozenset[str] | None = None,
) -> str | None:
    reason = _check_simple(tok)
    if reason:
        return reason
    # Non-path pseudo-arguments (stdin marker, current dir, null device).
    if tok in ("-", ".", "/dev/null"):
        return None
    # Command substitution in a path position is executed for real at runtime —
    # `cat "$(echo /etc/passwd)"` hides the payload from static checks (the
    # inner command is a data command with no leading /). Reject it outright;
    # data commands (`echo "$(date)"`) never reach _check_path. Backticks are
    # plain characters to shlex (not quotes), so a raw `\`x\`` token shows up
    # verbatim.
    if "$(" in tok or "`" in tok or tok == "$" or tok.endswith("/$"):
        # `cat $(echo /etc/passwd)` reaches here as a bare `$` token (shlex
        # splits `$(` into `$` + `(`) and `cat dir/$(echo x)` as `dir/$` — the
        # same substitution-in-a-path hole as the quoted form above. A literal
        # `file$` (a file whose name ends in $) stays allowed: only a bare or
        # directory-attached `$` is unambiguous substitution.
        return f"Command substitution is not allowed in a path argument: {tok!r}"
    # Positional parameters ($1, ${2}, $@, $*, $#) are never set by
    # code_executor — they expand to empty at runtime, so `cat $1/etc/passwd`
    # silently becomes `cat /etc/passwd` (an absolute path) past the static
    # checks below. Reject them in path positions outright.
    if re.search(r"\$\{[0-9]+\}|\$[0-9]|\$[@*#]", tok):
        return (
            f"Path {tok!r} uses a positional parameter, which is never set "
            "here (it would expand empty); use a literal path inside the project"
        )
    # Variables whose value was captured from command substitution
    # (`x=$(echo /etc/passwd); cat $x`) resolve to anything at runtime —
    # reject them in path positions regardless of allow_dollar.
    if tainted_vars:
        used = set(_dollar_vars(tok))
        if used & tainted_vars:
            return (
                f"Path {tok!r} expands a variable captured from command "
                "substitution; use a literal in-project path instead"
            )
    if allow_dollar:
        # `${Y:-/etc}` / `${Y:=x}` / `${Y%pat}` / `${Y#pat}` / `${Y/old/new}`
        # expand to anything at runtime — `cat ${Y:-/etc}/passwd` becomes
        # `cat /etc/passwd`, and _dollar_vars never sees the variable (its
        # regex demands `}` right after the name). Reject parameter
        # expansions in path positions outright.
        if _has_param_expansion(tok):
            return (
                f"Path {tok!r} uses a parameter expansion (${{x:-...}} etc.), "
                "whose result could be any path; use a literal path inside "
                "the project"
            )
        # allow_dollar trusts $var in path positions only when the variable's
        # value is known to stay inside the project: assigned in this command
        # (validated at the assignment site) or one of the server-injected env
        # vars whose value is a project-internal path (HOME, OUTPUT_DIR,
        # PROJECT_DIR, PWD). Host env vars ($SHELL, $PATH, $USER) expand to
        # anything at runtime — `rm $SHELL` would delete a file outside the
        # project and `cat $PATH` would read one.
        allowed = assigned_vars | _SAFE_ENV_PATH_VARS
        vars_used = _dollar_vars(tok)
        if vars_used and not all(v in allowed for v in vars_used):
            return (
                f"Path {tok!r} uses variable {vars_used!r} that is not assigned "
                "in this command or a project-internal env var; assign it first "
                "or use a literal path inside the project"
            )
        # A `$var/` or `${var}/` PREFIX collapses to an absolute path when the
        # variable is empty (`cat $y/etc/passwd` with y unset runs `cat
        # /etc/passwd`) — prefix variables must likewise be assigned or a
        # project-internal env var (the injected ones are always non-empty).
        prefix_names = [
            m.group(1) or m.group(2)
            for m in re.finditer(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}/|\$([A-Za-z_][A-Za-z0-9_]*)/", tok
            )
        ]
        if prefix_names and not all(n in allowed for n in prefix_names):
            return (
                f"Path {tok!r} uses variable {prefix_names!r} as a path prefix "
                "but it is not assigned in this command; assign it first or "
                "use a literal path inside the project"
            )
    if not allow_dollar and "$" in tok:
        # Only variables assigned earlier in THIS command string may appear in
        # path arguments — their values were validated at the assignment site.
        # Host env vars ($HOME, $PATH) or positional markers ($1, $@) expand
        # to anything and would defeat the token-level path checks.
        vars_used = _dollar_vars(tok)
        if not vars_used or not all(v in assigned_vars for v in vars_used):
            return (
                f"Path {tok!r} uses a variable that is not assigned in this "
                "command; assign it first (VAR=value) or use a literal path "
                "inside the project"
            )
    # bare filenames are resolved too. A symlink inside the project
    # (git checkouts materialize committed symlinks; `x -> /etc/passwd` or
    # `x -> ../../sibling/.env`) makes a bare `cat x` read OUTSIDE the
    # project — resolve() follows the link, so the is_within check catches it.
    try:
        resolved = (cwd / tok).resolve()
    except OSError:
        return f"Path {tok!r} could not be resolved; refusing to run"
    if not is_within(root, resolved):
        return f"Path {tok!r} escapes the project directory"
    return None
