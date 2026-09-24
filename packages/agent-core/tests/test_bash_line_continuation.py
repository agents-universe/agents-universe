"""Bash line-continuation (`\\` + newline) handling in the validators.

Two failure modes share one root cause: the backslash-newline pair was never
removed before tokenization.

* _validate_bash checks each PHYSICAL line — a line ending in `\\` reaches
  validate_command without its continuation partner, shlex raises "No escaped
  character", and EVERY backslash-continued command was refused (false
  positive: `echo a \\` / `  b` is plain bash).
* split_logical_lines merged continuation lines but KEPT the `\\`+newline, so
  shlex glued a literal newline into the token: the path check saw
  "\\n../sibling/.env" — which starts with a newline, so it is neither ".."
  nor absolute — and let `cat \\` + `../sibling/.env` through while bash ran
  `cat ../sibling/.env` (escape).
* Unquoted heredoc bodies: bash joins backslash-newline there too (a
  substitution split across the pair executes), but _validate_bash scanned
  each body line alone, so `$(cat \\` + `/etc/passwd)` looked harmless.
"""
from __future__ import annotations

from agent_core.sandbox import split_logical_lines, validate_command
from agent_core.tools.code_executor import CodeExecutorTool

BS = chr(92)
NL = chr(10)


def test_validate_bash_accepts_benign_continuations(tmp_path):
    cases = [
        "echo hello " + BS + NL + "  world" + NL,
        "npm install " + BS + NL + "  --save-dev" + NL + "  lodash" + NL,
        "cat " + BS + NL + "  notes.txt" + NL,
        "if true; then " + BS + NL + "  echo hi " + BS + NL + "fi" + NL,
        "echo a " + BS + NL + "&& echo b" + NL,
        "X=1 " + BS + NL + "echo done" + NL,
    ]
    for code in cases:
        reason = CodeExecutorTool._validate_bash(code, tmp_path)
        assert reason is None, (code, reason)


def test_validate_bash_continuation_path_checks_still_apply(tmp_path):
    # The continued line's tokens must be validated exactly as bash joins
    # them — not per-physical-line fragments (or a "No escaped character"
    # parse refusal, which says nothing about the path).
    for code in (
        "cat " + BS + NL + "../sibling/.env" + NL,
        "cat " + BS + NL + "/etc/passwd" + NL,
    ):
        reason = CodeExecutorTool._validate_bash(code, tmp_path)
        assert reason is not None, code
        assert "escaped character" not in reason, reason
        assert "parsed safely" not in reason, reason


def test_validate_bash_trailing_backslash_at_eof(tmp_path):
    # bash drops the dangling backslash and runs the line (printf 'echo hi \'
    # | bash prints hi) — so validation must not refuse it.
    assert CodeExecutorTool._validate_bash("echo hi " + BS, tmp_path) is None


def test_validate_command_continuation_cannot_smuggle_escape(tmp_path):
    kw = dict(cwd=tmp_path, project_root=tmp_path, allow_substitution=True)
    assert validate_command(
        "cat " + BS + NL + "../sibling/.env", **kw
    ) is not None, "backslash-newline glued into the token defeated the .. check"
    assert validate_command(
        "cat " + BS + NL + "/etc/passwd", **kw
    ) is not None, "backslash-newline glued into the token defeated the absolute check"
    assert validate_command("cat " + BS + NL + "notes.txt", **kw) is None
    assert validate_command("echo hi " + BS, **kw) is None


def test_split_logical_lines_removes_backslash_newline():
    # bash removes the pair entirely — keeping it made shlex emit a literal
    # "\n" token that started with a newline instead of ".." or "/".
    assert split_logical_lines("ls " + BS + NL + "  -la") == [("ls   -la", False)]


def test_split_logical_lines_single_quote_backslash_stays_literal():
    # Inside single quotes the backslash is data, not a continuation: the
    # newline and backslash must survive into the token (bash keeps them).
    text = "echo 'a" + BS + NL + "b'"
    assert split_logical_lines(text) == [(text, False)]


def test_split_logical_lines_double_quote_continuation_joins(tmp_path):
    lines = split_logical_lines('echo "a' + BS + NL + 'b"')
    assert lines == [('echo "ab"', False)]


def test_validate_bash_heredoc_body_substitution_across_continuation(tmp_path):
    # bash joins backslash-newline in unquoted heredoc bodies, so this runs
    # `cat ../sibling/.env` — each body line alone looks harmless.
    code = (
        "cat <<EOF" + NL
        + "$(cat " + BS + NL + "../sibling/.env)" + NL
        + "EOF" + NL
    )
    assert CodeExecutorTool._validate_bash(code, tmp_path) is not None


def test_validate_bash_heredoc_body_benign_multiline_substitution(tmp_path):
    # A substitution split across continuation lines is legal bash and must
    # be validated joined — scanning each body line alone saw an unclosed
    # `$(...` and refused a harmless script.
    code = (
        "cat <<EOF" + NL
        + "$(echo " + BS + NL
        + "hi)" + NL
        + "EOF" + NL
    )
    assert CodeExecutorTool._validate_bash(code, tmp_path) is None


def test_validate_bash_heredoc_delimiter_consumed_by_continuation(tmp_path):
    # bash: a continuation target line is not delimiter-checked (`hello \`
    # swallows `EOF`), so the substitution below still runs inside the body
    # and must be caught by the body scan — not by lucky early termination.
    code = (
        "cat <<EOF" + NL
        + "hello " + BS + NL
        + "EOF" + NL
        + "$(cat ../sibling/.env)" + NL
        + "EOF" + NL
    )
    assert CodeExecutorTool._validate_bash(code, tmp_path) is not None
