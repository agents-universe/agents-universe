"""Code executor tool - sandboxed Python/Bash subprocess with 30s timeout.

Python code runs under a file-access audit-hook guard (agent_core.sandbox_guard)
that restricts reads/writes to the project directory.  Bash code is validated
line-by-line against the same path rules as the shell tool.  The working
directory and scratch files live inside the project (.tmp/work/) so the guard
covers them; no TEMP/TMP/TMPDIR injection.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from ..sandbox import (
    _SAFE_ENV_PATH_VARS,
    _check_all_substitutions,
    _check_assignment,
    _dollar_vars,
    _skip_subst_parens,
    has_unclosed_quote,
    python_guard_env,
    spawn_in_new_session,
    terminate_process_tree,
    validate_command,
)
from ._media import _IMAGE_SUFFIXES, media_type_for, sanitize_suffix
from .base import Tool, ToolContext

_log = logging.getLogger(__name__)
_TIMEOUT = 30
_PW_TIMEOUT = 120
_MAX_OUTPUT = 20_000

# Bash control-structure keywords whose lines are skipped during validation.
_BASH_CONTROL = frozenset({
    "if", "then", "else", "elif", "fi", "for", "do", "done",
    "while", "until", "case", "esac", "function", "select", "in",
    "{", "}", "!",
})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Bash assignment-prefix keywords: `export C=$(...)`, `local x=...`, etc.
# perform the same assignment with command substitution as a bare `C=$(...)`,
# so the validator must strip the prefix before the _ASSIGN_RE match below or
# `export C=$(echo /etc/passwd)` skips the taint branch and `cat $C` expands
# to an unvalidated absolute path. Optional single-letter flags are consumed
# (`declare -p`, `readonly -a`).
_ASSIGN_PREFIX_RE = re.compile(r"^(?:export|local|declare|typeset|readonly)(?:\s+-[A-Za-z]+)*\s+")
# Arithmetic expansion regions ($((...)), $[...], and a leading ((...))
# use << as the bit-shift operator — the scanner must skip them or
# `echo $((1 << 2))` would open a bogus heredoc whose "body" swallows the
# following lines un-validated. Parens nest inside arithmetic, so the region
# closes at the first `))` that empties the depth.
def _skip_arith_parens(line: str, start: int) -> int | None:
    depth = 2
    n = len(line)
    j = start
    while j < n:
        if line[j] == "(":
            depth += 1
        elif line[j] == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _at_command_start(line: str, i: int) -> bool:
    """True when *i* starts a command: line start, after ; & | ( {, or after
    a control keyword (if/while/...)."""
    j = i - 1
    while j >= 0 and line[j] in " \t":
        j -= 1
    if j < 0 or line[j] in ";&|({!":
        return True
    # `if (( 1 << 2 )); then` — the arithmetic follows a control keyword,
    # not a separator. Any other word glued to `((` is a bash syntax error,
    # so the keyword whitelist only narrows the skip.
    if line[j].isalnum() or line[j] == "_":
        start = j
        while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
            start -= 1
        return line[start : j + 1] in _CMD_KEYWORDS
    return False


# Control keywords that introduce a command (see _at_command_start).
_CMD_KEYWORDS = frozenset({"if", "elif", "then", "else", "while", "until", "for", "do"})


def _skip_braced(line: str, start: int) -> int | None:
    """Index of the `}` closing a ${...} region opened at *start* (points
    past the `${`). Nested braces (`${x:-${y}}`) count depth; a `}` inside a
    quoted string does not close the region. `$(...)`, `$((...))` and
    backticks inside the region are skipped whole — bash's parameter parser
    does not treat a `}` inside them as the closing brace (`${x:-$(echo a})}`
    closes at the FINAL `}`). Returns None when never closed — the line is a
    bash syntax error and opens no heredoc.
    """
    depth = 1
    n = len(line)
    j = start
    while j < n:
        c = line[j]
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if c == "$" and j + 1 < n:
            if j + 2 < n and line[j + 1] == "(" and line[j + 2] == "(":
                end = _skip_arith_parens(line, j + 3)
                if end is None:
                    return None
                j = end + 1
                continue
            if line[j + 1] == "(":
                end = _skip_subst_parens(line, j + 2)
                if end is None:
                    return None
                j = end + 1
                continue
        if c == "`":
            end = line.find("`", j + 1)
            if end == -1:
                return None
            j = end + 1
            continue
        if c in ("'", '"'):
            q = c
            j += 1
            while j < n:
                if q == '"' and line[j] == "\\" and j + 1 < n and line[j + 1] in ('"', "\\"):
                    j += 2
                    continue
                if line[j] == q:
                    break
                j += 1
            j += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


# Heredoc opener: `<<EOF` (unquoted → bash performs command substitution in
# the body) vs `<<'EOF'` / `<<"EOF"` (quoted → literal body). Group 1 captures
# the quote character, group 2 the delimiter.
def _find_heredoc_start(line: str) -> tuple[str, bool] | None:
    """Return (delimiter, quoted) when *line* opens a heredoc outside quotes.

    Scans the raw line tracking quote state, so a `<<EOF` inside a quoted
    string (`echo "x <<EOF"`) is not detected and the following lines are
    still validated as commands. Handles `<< EOF`, `<<EOF`, `<<"EOF"`,
    `<<'EOF'` and the `<<-` tab-stripping form; `<<<` herestrings are not
    heredocs. Returns None when the line opens no heredoc.
    """
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                c = line[i]
                # \" does not close a double-quoted string
                if quote == '"' and c == "\\" and i + 1 < n and line[i + 1] in ('"', "\\"):
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2  # an escaped char cannot start a heredoc
            continue
        if ch == "$" and i + 1 < n:
            if line[i + 1] == "{":
                # ${...} may hold arithmetic in array subscripts
                # (`${arr[1 << 2]}`) — a << inside must not open a heredoc.
                end = _skip_braced(line, i + 2)
                if end is None:
                    return None  # unclosed ${ — syntax error, no heredoc
                i = end + 1
                continue
            if line[i + 1] == "[":
                end = line.find("]", i + 2)
                if end == -1:
                    return None  # unclosed $[ — syntax error, no heredoc here
                i = end + 1
                continue
            if line[i + 1] == "(" and i + 2 < n and line[i + 2] == "(":
                end = _skip_arith_parens(line, i + 3)
                if end is None:
                    return None  # unclosed $(( — syntax error, no heredoc here
                i = end + 1
                continue
        if ch == "(" and i + 1 < n and line[i + 1] == "(" and _at_command_start(line, i):
            end = _skip_arith_parens(line, i + 2)
            if end is None:
                return None  # unclosed (( — syntax error, no heredoc here
            i = end + 1
            continue
        if ch == "<" and i + 1 < n and line[i + 1] == "<":
            j = i + 2
            if j < n and line[j] == "<":
                i = j + 1  # <<< herestring — not a heredoc
                continue
            if j < n and line[j] == "-":
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            if j >= n:
                return None
            if line[j] in ("'", '"'):
                q = line[j]
                end = line.find(q, j + 1)
                if end == -1:
                    return None
                # Fail closed: bash performs quote removal on the delimiter,
                # so anything after the closing quote (`<<"EO"F`) makes the
                # real delimiter ambiguous. Treat it as not-a-heredoc and
                # validate the following lines as ordinary commands.
                rest = line[end + 1:].lstrip(" \t")
                if rest and rest[0] not in ";|&<>":
                    return None
                return line[j + 1:end], True
            if line[j] == "\\":
                # \EOF is a quoted delimiter (bash treats the body literally,
                # same as <<'EOF') — return quoted=True so substitution
                # payloads in the body are NOT treated as live commands.
                # Fail closed: bash also performs quote removal on this form
                # (`<<\EO\F` is delimiter EOF), so a delimiter still holding
                # backslashes/quotes after the first one cannot be matched
                # reliably — return None and validate every following line
                # as an ordinary command.
                j += 1
                start = j
                while j < n and line[j] not in " \t;|&<>":
                    j += 1
                delim = line[start:j]
                if j == start or "\\" in delim or "'" in delim or '"' in delim:
                    return None
                return delim, True
            start = j
            while j < n and line[j] not in " \t;|&<>":
                j += 1
            if j == start:
                return None
            return line[start:j], False
        i += 1
    return None
# Command substitution payloads: $(...) or backticks. These execute when bash
# runs the line — they must never appear un-validated in skipped regions
# (unquoted heredoc bodies, assignment values).
_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


class CodeExecutorTool(Tool):
    name = "code_executor"
    prompt_hint = (
        "For computation or data processing that needs real code. File access confined "
        "to the project workspace; network follows the SANDBOX_NETWORK policy "
        "(default: allowed), 30s timeout. Use language=python-playwright for browser "
        "automation (Playwright Python): subprocess + network allowed so the agent can "
        "drive a real browser - downloads, screen recording, screenshots, etc."
    )
    description = (
        "Execute Python, Python-Playwright, or Bash code in a sandboxed subprocess. "
        "Python/Bash: 30s timeout. Network follows the SANDBOX_NETWORK policy "
        "(default: allowed; 'localhost' or 'none' restrict it). "
        "python-playwright: 120s timeout, subprocess + network allowed for browser "
        "automation via the Playwright Python library. "
        "Python code runs under a file-access guard that restricts reads/writes to "
        "the project directory; cross-project access via ../ or absolute paths is blocked. "
        "The working directory is the project root, available as the PROJECT_DIR env var. "
        "Files written to the OUTPUT_DIR env var are delivered to the user "
        "(images appear in chat, other files as downloadable attachments) — "
        "write anything the user should receive there "
        "(e.g., $OUTPUT_DIR/output_0.png, $OUTPUT_DIR/report.csv). "
        "Bash mode is for in-project data processing; use the shell tool for external commands."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Code to execute"},
            "language": {
                "type": "string",
                "enum": ["python", "bash", "python-playwright"],
                "default": "python",
            },
        },
        "required": ["code"],
    }

    # ctypes is on the text blocklist (not the runtime import blocklist —
    # pandas imports it internally); user code naming it explicitly is
    # rejected, while library imports keep working.
    _BLOCKED_IMPORTS = re.compile(
        r"\b(subprocess|os\.system|os\.popen|os\.execv|os\.execl|os\.fork|pty|ctypes|_ctypes|pexpect|fabric|invoke)\b"
    )
    # Playwright mode allows subprocess (browser launch) but still blocks
    # direct os.system/fork/pty/ctypes etc. The audit hook blocks the
    # runtime calls regardless; this regex is the text-level first gate.
    _BLOCKED_IMPORTS_PW = re.compile(
        r"\b(os\.system|os\.popen|os\.execv|os\.execl|os\.fork|pty|ctypes|_ctypes|pexpect|fabric|invoke)\b"
    )
    _BLOCKED_BASH = re.compile(
        # interpreters bypass the python-only audit hook (perl -e runs
        # arbitrary code) and git reaches the network via GIT_SSH_COMMAND /
        # credential helpers / proxies — this regex also gates scripts.py.
        # Database clients (mysql/psql/redis-cli/mongosh/...) reach the
        # compose-internal databases that hold session/secret data; bash mode
        # has no socket audit hook, so they are blocked here like curl/wget.
        r"\b(curl|wget|npx|npm|node|tsx|python3?|ssh|scp|rsync|nc|ncat|telnet|ftp|"
        r"mysql|mariadb|psql|redis-cli|redis-server|mongosh|mongo|"
        r"clickhouse-client|tsql|"
        # shell/meta-programming words run arbitrary code as a prelude to
        # reading host files — `eval "$(echo cat /etc/hostname)"`,
        # `echo 'cat /etc/passwd' > x.sh; . x.sh`, `bash x.sh` all bypass the
        # token checks while executing the payload for real. trap/alias/shopt
        # are shell BUILTINS with the same power (trap 'cat /etc/passwd'
        # EXIT runs at script end; alias + shopt -s expand_aliases runs
        # aliased commands in non-interactive bash); xargs/dd smuggle the
        # command/path construction into data tokens (`echo /etc/passwd |
        # xargs cat`, `dd if=/etc/passwd`) the token checks never see.
        r"bash|sh|zsh|dash|ksh|fish|ash|eval|source|exec|command|builtin|env|"
        r"perl|ruby|php|git|socat|openssl|unset|trap|alias|shopt|xargs|dd)\b|"
        # ANSI-C quoting ($'\x2fetc\x2fpasswd') hides '/' and '..' inside a
        # token shlex never unescapes — bash decodes it into an absolute
        # path at runtime, past every check.
        r"\$\'|"
        # dot command (`. script.sh`) is not a \b word — match it at start /
        # after a separator only, so `./tool.sh` keeps working.
        r"(?:^|[\s;|&])\.(?=\s|$)"
    )

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        code = params["code"]
        language = params.get("language", "python")

        if language == "python" and self._BLOCKED_IMPORTS.search(code):
            return {"error": "Code uses a blocked module (subprocess, os.system, etc.). Use the shell tool for commands."}
        if language == "python-playwright" and self._BLOCKED_IMPORTS_PW.search(code):
            return {"error": "Code uses a blocked module (os.system, pty, ctypes, etc.). Use the shell tool for commands."}
        if language == "bash":
            # ANSI-C quoting ($'\x2fetc\x2fpasswd') hides '/' and '..'
            # inside a token that bash decodes at runtime — the shlex
            # unescape below strips the quotes so the $'-pattern never
            # matches, and the token checks see only decoded content. Check
            # the RAW code; a literal `$'` text in a data echo is a rare
            # false positive, fail-closed.
            if "$'" in code:
                return {"error": "Bash code_executor is for data processing only. ANSI-C quoting ($'...') is not allowed."}
            # \b-word 正则跑在原始代码上可被反斜杠混淆绕过（`c\url ...` 中
            # "curl" 不是连续子串，但 bash 执行时反斜杠转义还原出真正的
            # `curl`）。先按 shlex 还原转义再匹配，堵住该旁路。
            try:
                lexer = shlex.shlex(code, posix=True)
                lexer.whitespace_split = True
                unescaped = " ".join(list(lexer))
            except ValueError:
                unescaped = code
            if self._BLOCKED_BASH.search(unescaped):
                return {"error": "Bash code_executor is for data processing only. Use the shell tool for external commands."}

        project_cwd = Path(context.project_fs_path)

        # Validate bash code for path escapes before spawning.  Command
        # substitution is allowed here (unlike the shell tool) because it is
        # common in data-processing snippets and _BLOCKED_BASH already strips
        # the external commands that could abuse it.
        if language == "bash":
            reason = self._validate_bash(code, project_cwd)
            if reason:
                return {"error": reason}

        # Scratch directory inside the project (.tmp/work/) so the Python
        # file-access guard covers script files and image outputs.  Cleaned up
        # in finally.  No TEMP/TMP/TMPDIR injection - the user explicitly
        # requested the temp dir stay inside the project.
        work_dir = project_cwd / ".tmp" / "work" / f"code-{uuid.uuid4().hex[:8]}"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            output_dir = work_dir / "output"
            output_dir.mkdir(exist_ok=True)

            if language in ("python", "python-playwright"):
                code_file = work_dir / "script.py"
                # newline="" out on Windows: text-mode write_text would
                # translate \n to \r\n, and a bash script read back on Linux
                # (or in Git Bash/WSL) chokes on the trailing \r in command
                # names and file paths.
                code_file.write_text(code, encoding="utf-8", newline="\n")
                cmd = [sys.executable, str(code_file)]
            else:
                # Bash runs from a script FILE (not `bash -s` via stdin) so the
                # script's own stdin stays free: a bare `cat`/`read` gets EOF
                # instead of swallowing the remaining script lines.  The path
                # is relative to the cwd (project root) so it works identically
                # in Linux bash, Git Bash, and WSL — no drive-letter mapping.
                code_file = work_dir / "script.sh"
                code_file.write_text(code, encoding="utf-8", newline="\n")
                cmd = ["bash", code_file.relative_to(project_cwd).as_posix()]

            # safe_env() strips SECRET_KEY, DB connection strings, etc.
            # python_guard_env arms the sitecustomize audit hook in the child
            # (strict=True denies subprocess.Popen calls, matching the
            # _BLOCKED_PYTHON regex above as a second layer; subprocess import
            # stays allowed so matplotlib/pandas keep working).
            extra: dict[str, str] = {
                "OUTPUT_DIR": str(output_dir),
                "PROJECT_DIR": str(project_cwd),
            }
            # Network policy follows the SANDBOX_NETWORK config (see
            # .env.example): unset/empty -> allow all network (Playwright
            # downloads, screen recording, ...); localhost -> loopback only;
            # none -> all sockets blocked. Only a restricting mode needs to
            # be passed down; "all" is the sitecustomize default.
            network_mode = (context.cfg("SANDBOX_NETWORK") or "").strip().lower()
            if language == "python":
                extra["HOME"] = str(work_dir)
                extra.update(python_guard_env(project_cwd, strict=True))
                if network_mode in ("localhost", "none"):
                    extra["_AGENT_NETWORK_MODE"] = network_mode
            elif language == "python-playwright":
                # Keep the real HOME so Playwright finds browser binaries
                # (PLAYWRIGHT_BROWSERS_PATH is inherited from the Docker ENV).
                # Non-strict: allow subprocess.Popen for browser launching.
                # Network follows SANDBOX_NETWORK (default: allow all).
                extra.update(python_guard_env(project_cwd, strict=False))
                if network_mode in ("localhost", "none"):
                    extra["_AGENT_NETWORK_MODE"] = network_mode

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    # DEVNULL stdin: scripts reading stdin get a clean EOF,
                    # matching "run a script file" semantics.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(project_cwd),
                    env=context.safe_env(extra=extra),
                    # Own session on POSIX so a forked/backgrounded
                    # grandchild dies with the tree on timeout/abort.
                    **spawn_in_new_session(),
                )
                timeout = _PW_TIMEOUT if language == "python-playwright" else _TIMEOUT
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                terminate_process_tree(proc)
                await proc.wait()
                _log.warning("code_executor timed out after %ds (language=%s)", timeout, language)
                return {"error": f"Execution timed out after {timeout}s", "exit_code": -1}
            except asyncio.CancelledError:
                # Task cancelled: kill the child before its finally-rmtree of
                # work_dir (a live child would hold the script file open).
                terminate_process_tree(proc)
                await proc.wait()
                raise
            except OSError as exc:
                _log.warning("code_executor failed to start subprocess: %s", exc)
                return {"error": f"Failed to start code execution: {exc}"}

            stdout_text = stdout.decode(errors="replace")[:_MAX_OUTPUT]
            stderr_text = stderr.decode(errors="replace")[:_MAX_OUTPUT]
            if proc.returncode != 0:
                _log.warning("code_executor failed (exit=%d, language=%s)\nstderr: %s",
                             proc.returncode, language, stderr_text[:500])

            # Collect outputs from the isolated output directory: PNGs surface
            # as images, everything else as downloadable attachments. Names
            # are server-generated (the media whitelist rejects spaces and
            # unicode — the sandboxed code can write any filename), the
            # original name rides in the record's `name` field.
            images: list[dict[str, str]] = []
            files: list[dict[str, Any]] = []
            media_path = Path(context.conversation_media_dir)
            media_path.mkdir(parents=True, exist_ok=True)
            for out_file in sorted(output_dir.iterdir()):
                if not out_file.is_file():
                    continue
                suffix = sanitize_suffix(out_file.name)
                dest = media_path / f"code_{uuid.uuid4().hex[:8]}{suffix}"
                try:
                    shutil.move(str(out_file), str(dest))
                except OSError:
                    continue
                rel = f"/api/media/{context.project_id}/{context.conversation_id}/{dest.name}"
                if suffix in _IMAGE_SUFFIXES:
                    images.append({"id": dest.stem, "url": rel, "alt": f"Code output: {out_file.name}", "path": str(dest)})
                else:
                    files.append({
                        "id": dest.stem,
                        "url": rel,
                        "name": out_file.name[:255],
                        "media_type": media_type_for(out_file.name),
                        "size": dest.stat().st_size,
                    })

            result: dict[str, Any] = {
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exit_code": proc.returncode,
            }
            if images:
                result["images"] = images
            if files:
                result["files"] = files
            return result
        except OSError as exc:
            return {"error": f"Failed to create scratch directory inside the project: {exc}"}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Bash validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_bash(code: str, project_root: Path) -> str | None:
        """Best-effort line-by-line path-escape check for bash scripts.

        Skips comments, control-structure keywords, variable assignments, and
        heredoc bodies.  Physical lines whose quotes don't close are joined
        with the following lines first — a quoted string may legitimately
        span multiple lines.  Command substitution is allowed (the
        _BLOCKED_BASH regex already strips external commands that could abuse
        it); the remaining path tokens are checked by validate_command.
        """
        in_heredoc = False
        heredoc_delim: str | None = None
        heredoc_quoted = False
        buffer = ""
        buffer_start = 0
        # Variable names whose values were captured from command substitution
        # (`x=$(echo /etc/passwd)`). Later path positions using them expand to
        # anything at runtime — validate_command rejects those references.
        tainted: set[str] = set()
        # Variable names assigned on earlier lines of this script. Seeded into
        # each per-line validate_command so `d=data` on line 1 still satisfies
        # the assigned-path-prefix rule on line 3 (`cat $d/x`); every value was
        # validated when its assignment line passed (_check_simple rejects
        # `..` and absolute paths), so carrying the name forward is safe.
        assigned: set[str] = set()
        for line_num, line in enumerate(code.splitlines(), 1):
            if in_heredoc:
                if line.strip() == heredoc_delim:
                    in_heredoc = False
                    heredoc_delim = None
                    heredoc_quoted = False
                    continue
                # an UNQUOTED heredoc body runs command substitution
                # when bash executes it — `cat <<EOF` + `$(cat /app/.env)` in
                # the body previously sailed through because body lines were
                # skipped wholesale. Quoted delimiters (<<'EOF') are literal
                # and safe to skip.
                if not heredoc_quoted:
                    # Single quotes are plain characters in a heredoc body —
                    # $(...) inside them still executes, so scan them.
                    reason = CodeExecutorTool._validate_substitutions(
                        line, buffer_start, project_root, single_quotes_literal=False
                    )
                    if reason:
                        return reason
                continue
            candidate = f"{buffer}\n{line}" if buffer else line
            if not buffer:
                buffer_start = line_num
            stripped = candidate.strip()
            if not stripped or stripped.startswith("#"):
                buffer = ""
                continue
            if has_unclosed_quote(candidate):
                # Multi-line quoted string — accumulate and retry with the
                # next physical line before validating.
                buffer = candidate
                continue
            buffer = ""
            # Detect heredoc start to skip body lines on subsequent iterations.
            # A line-wide regex search matched "<<EOF" INSIDE quotes
            # (`echo "x <<EOF"`) and put the following lines in heredoc-skip
            # mode while bash ran them for real — arbitrary commands escaped
            # validation. _find_heredoc_start scans the raw line with quote
            # tracking, and also captures the delimiter's quoted flag: a
            # quoted delimiter (<<'EOF') makes the body literal, so its
            # $(...) must not be treated as executable.
            heredoc = _find_heredoc_start(stripped)
            if heredoc is not None:
                heredoc_delim, heredoc_quoted = heredoc
                in_heredoc = True
            first_word = stripped.split()[0].rstrip(";")
            if first_word in _BASH_CONTROL:
                # Control-structure lines used to skip validation wholesale —
                # but their bodies execute for real: `for i in $(cat
                # ../sibling/.env)` and `do cat ../x; done` both bypassed the
                # path checks (the control keywords themselves validate as
                # plain in-project tokens, so the full-line check is safe).
                reason = validate_command(
                    stripped,
                    cwd=project_root,
                    project_root=project_root,
                    allow_substitution=True,
                    tainted_vars=frozenset(tainted),
                    pre_assigned_vars=assigned,
                )
                if reason:
                    return f"Line {buffer_start}: {reason}\n  -> {stripped}"
                continue
            # `export C=$(...)` / `local x=...` are assignments too — strip the
            # prefix so `export C=$(echo /etc/passwd)` cannot skip the taint
            # branch (its `$C` would then expand past validation).
            assign_body = _ASSIGN_PREFIX_RE.sub("", stripped) if _ASSIGN_PREFIX_RE.match(stripped) else stripped
            if _ASSIGN_RE.match(assign_body):
                # assignment lines are skipped wholesale, but
                # `X=$(cat /app/.env)` performs command substitution — the
                # payload must be validated like any other command. The rest
                # of the line still runs (`x=$(echo /etc/passwd); cat $x`
                # puts the captured value into a path argument), so the whole
                # line goes through validate_command with substitution-captured
                # names tracked as tainted.
                reason = CodeExecutorTool._validate_substitutions(stripped, buffer_start, project_root)
                if reason:
                    return reason
                try:
                    # Quote-aware tokens: `a="x y" b=$t` must yield two
                    # assignments — plain whitespace split would cut at the
                    # space inside the quotes, drop out of the loop at `y"`,
                    # and leave `b` untracked for `cat $b` to slip through.
                    assign_tokens = shlex.split(stripped)
                except ValueError:
                    # Unclosed quote — the line is a bash syntax error and
                    # validate_command rejects it below; nothing to taint.
                    assign_tokens = []
                for tok in assign_tokens:
                    if not _ASSIGN_RE.match(tok):
                        # Not an assignment — a payload word (`hi)` glued by
                        # shlex to `x=$(echo hi)`) or a command token; keep
                        # scanning so a later assignment on the same line is
                        # still tracked (`x=$(echo hi) C=$(echo /etc/passwd)`
                        # must taint C).
                        continue
                    # `export x=/etc/passwd` — the NAME= prefix dissolves the
                    # absolute-path scan when the token validates as a plain
                    # positional (the prefix is the whole token's leading
                    # segment). The VALUE must pass assignment validation:
                    # absolute/tilde/`..` paths and untracked variable
                    # references are rejected, and the name only gets
                    # "assigned" status on a clean value.
                    reason = _check_assignment(tok, allow_dollar=True,
                                               assigned_vars=assigned)
                    if reason:
                        return f"Line {buffer_start}: {reason}\n  -> {stripped}"
                    name, _, value = tok.partition("=")
                    # shlex (no punctuation_chars) glues a command separator
                    # onto the token (`x=;`, `x=a&&b`) — bash parses it as a
                    # boundary, so `x=;` IS an empty assignment and must not
                    # grant "assigned" status.
                    value = value.rstrip(";&|()")
                    refs = set(_dollar_vars(value))
                    # A variable that expands a tainted variable (`y=$x`) is
                    # tainted itself — `y=$x; cat $y` smuggles the captured
                    # path through a relay the same way `x=$(echo /etc/passwd);
                    # cat $x` does. A reference to an UNTRACKED name is
                    # tainted too: host env vars ($SHELL) expand to host
                    # paths, and an unset var expands to EMPTY, collapsing
                    # `$x/etc/hostname` into an absolute path. Only names
                    # assigned earlier on this line/screen and the
                    # server-injected project-internal vars are predictable.
                    untracked = refs - assigned - _SAFE_ENV_PATH_VARS
                    if "$(" in value or "`" in value or (refs & tainted) or untracked:
                        tainted.add(name)
                    # Only non-empty values qualify as "assigned" — `x=`
                    # expands to nothing, so `cat $x/etc/hostname` collapses
                    # to an absolute path past the checks. (validate_command
                    # independently rejects ${x:-...} expansions in values.)
                    if value:
                        assigned.add(name)
                reason = validate_command(
                    stripped,
                    cwd=project_root,
                    project_root=project_root,
                    allow_substitution=True,
                    tainted_vars=frozenset(tainted),
                    pre_assigned_vars=assigned,
                )
                if reason:
                    return f"Line {buffer_start}: {reason}\n  -> {stripped}"
                continue
            reason = validate_command(
                stripped,
                cwd=project_root,
                project_root=project_root,
                allow_substitution=True,
                tainted_vars=frozenset(tainted),
                pre_assigned_vars=assigned,
            )
            if reason:
                return f"Line {buffer_start}: {reason}\n  -> {stripped}"
        if buffer:
            return f"Line {buffer_start}: command could not be parsed safely (unclosed quotation)\n  -> {buffer.strip()}"
        return None

    @staticmethod
    def _validate_substitutions(
        line: str, line_num: int, project_root: Path, *, single_quotes_literal: bool = True
    ) -> str | None:
        """Validate command-substitution payloads in lines that are otherwise
        skipped by _validate_bash (unquoted heredoc bodies, assignment values).

        `X=$(cat /app/.env)` and heredoc bodies with `$(...)` execute payloads
        when bash runs the line; the payload must pass the same path-scoped
        validation as any other command. `$(< file)` (pure file read) is
        rejected outright. _check_all_substitutions walks every nesting level —
        `$(cat $(echo /etc/passwd))` only gets caught when the OUTER layer is
        validated (the inner one is a harmless data command). In an unquoted
        heredoc body quotes are plain characters to bash (substitution still
        runs), so single_quotes_literal=False.
        """
        reason = _check_all_substitutions(
            line,
            cwd=project_root,
            root=project_root,
            single_quotes_literal=single_quotes_literal,
        )
        if reason:
            return f"Line {line_num}: command substitution blocked: {reason}"
        return None
