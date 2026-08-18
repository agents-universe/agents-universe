"""Unit tests for sql_query comment stripping and validation.

The stripped text is BOTH validated and executed, so stripping must preserve
string literals — a comment marker inside '...' is data, not a comment.
"""
from __future__ import annotations

from agent_core.tools.sql_query import _strip_comments, _validate_readonly_select


def test_strip_comments_preserves_literal_comment_markers():
    # naive regex stripping truncated/broke these
    assert _strip_comments("SELECT 'a--b' AS x") == "SELECT 'a--b' AS x"
    assert _strip_comments("SELECT 'a/*b*/c'") == "SELECT 'a/*b*/c'"


def test_strip_comments_escaped_quote_inside_literal():
    assert _strip_comments("SELECT 'it''s -- ok'") == "SELECT 'it''s -- ok'"


def test_strip_comments_bracket_identifier_preserved():
    assert _strip_comments("SELECT [a--b] FROM t") == "SELECT [a--b] FROM t"
    assert _strip_comments("SELECT [a]]b] FROM t") == "SELECT [a]]b] FROM t"


def test_strip_comments_removes_real_comments():
    # a comment collapses to a single space, joining the surrounding spaces
    assert _strip_comments("SELECT 1 -- drop stuff") == "SELECT 1  "
    assert _strip_comments("SELECT /* x */ 1") == "SELECT   1"
    assert _strip_comments("-- lead\nSELECT 2") == " \nSELECT 2"


def test_validate_keyword_inside_literal_allowed():
    # The keyword check runs against the literal-masked text — a keyword
    # inside '...' is data, not a statement, and a legit read
    # (`SELECT 'DROP TABLE' AS note`) must not be rejected. The literal
    # itself still arrives intact (stripping never touches it).
    err, stripped = _validate_readonly_select("SELECT 'DROP TABLE x' AS note")
    assert err is None
    assert stripped == "SELECT 'DROP TABLE x' AS note"


def test_validate_keyword_outside_literal_still_rejected():
    err, _ = _validate_readonly_select("SELECT 1 INTO #tmp")
    assert err == "Forbidden keyword in read-only query: INTO"
    err, _ = _validate_readonly_select("SELECT 'x' FROM t WHERE n='sp_help'")
    assert err is None
    err, _ = _validate_readonly_select("SELECT 'x' FROM sp_help")
    assert err and "Forbidden keyword" in err


def test_validate_keyword_only_in_comment_removed_by_strip():
    # the executed (stripped) text no longer contains the keyword — safe
    err, stripped = _validate_readonly_select("SELECT 1 -- DROP TABLE x")
    assert err is None
    assert "DROP" not in stripped


def test_validate_comment_split_identifier_cannot_smuggle_blocked_table():
    # user/*c*/_tokens must not reach the DB as the single identifier
    # user_tokens: the executed text splits it, so the check cannot be fooled
    err, stripped = _validate_readonly_select("SELECT * FROM user/*c*/_tokens")
    assert err is None
    assert "user_tokens" not in stripped


def test_validate_blocked_table_still_rejected():
    err, _ = _validate_readonly_select("SELECT * FROM user_tokens")
    assert err and "blocked" in err
