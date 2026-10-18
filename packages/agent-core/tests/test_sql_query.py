"""Unit tests for sql_query comment stripping and validation.

The stripped text is BOTH validated and executed, so stripping must preserve
string literals — a comment marker inside '...' is data, not a comment.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.sql_query import (
    SqlQueryTool,
    _strip_comments,
    _validate_readonly_select,
)


def test_strip_comments_preserves_literal_comment_markers():
    # naive regex stripping truncated/broke these
    assert _strip_comments("SELECT 'a--b' AS x") == "SELECT 'a--b' AS x"
    assert _strip_comments("SELECT 'a/*b*/c'") == "SELECT 'a/*b*/c'"


def test_strip_comments_escaped_quote_inside_literal():
    assert _strip_comments("SELECT 'it''s -- ok'") == "SELECT 'it''s -- ok'"


def test_strip_comments_bracket_identifier_preserved():
    assert _strip_comments("SELECT [a--b] FROM t") == "SELECT [a--b] FROM t"
    assert _strip_comments("SELECT [a]]b] FROM t") == "SELECT [a]]b] FROM t"


def test_strip_comments_double_quoted_identifier_preserved():
    assert _strip_comments('SELECT "a--b" FROM t') == 'SELECT "a--b" FROM t'
    assert _strip_comments('SELECT "a""b" FROM t') == 'SELECT "a""b" FROM t'


def test_double_quoted_apostrophe_cannot_hide_blocked_table():
    # An apostrophe inside a delimited identifier used to open a string
    # literal in both scanners, blanking the rest of the query for every
    # check while the untouched text was executed.
    err, _ = _validate_readonly_select('SELECT 1 AS "x\'y", * FROM user_tokens')
    assert err and "credential table" in err

    err, _ = _validate_readonly_select('SELECT * FROM "user_tokens"')
    assert err and "credential table" in err


def test_double_quoted_apostrophe_cannot_hide_statement_batch():
    err, _ = _validate_readonly_select('SELECT 1 AS "x\'y"; DROP TABLE audit_log')
    assert err == "Multiple SQL statements are not allowed."


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


def test_validate_apostrophe_in_bracket_identifier_cannot_hide_blocked_table():
    """A bracket identifier containing ' desynced the literal masker.

    `[a'b]` opened a string literal that was never closed, so everything after
    it (including FROM user_tokens) was blanked before the blocklist ran.
    """
    err, stripped = _validate_readonly_select("SELECT *, 1 AS [a'b] FROM user_tokens")
    assert err and "blocked" in err
    assert "user_tokens" in stripped


def _ctx(tmp_path, session) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="c1",
        user_id="u1",
        db_session=session,
    )


@pytest.mark.asyncio
async def test_duplicate_column_names_keep_every_value(tmp_path):
    """RowMapping's keyed access resolves same-named columns to one value, so
    `SELECT a.id, b.id` returned the second id twice and dropped the first."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with AsyncSession(engine) as session:
            result = await SqlQueryTool().execute(
                {"query": "SELECT 1 AS a, 2 AS a, 3 AS b"}, _ctx(tmp_path, session)
            )
    finally:
        await engine.dispose()

    assert result["rows"] == [{"a": 1, "a_2": 2, "b": 3}]


@pytest.mark.asyncio
async def test_failed_query_rolls_back_the_shared_session(tmp_path):
    """The session is reused by later tool calls; on Postgres the failed
    statement aborts the transaction and every later use raises
    PendingRollbackError until someone rolls back."""
    session = AsyncMock()
    session.execute.side_effect = RuntimeError("syntax error")

    result = await SqlQueryTool().execute({"query": "SELECT broken"}, _ctx(tmp_path, session))

    assert "Query failed" in result["error"]
    session.rollback.assert_awaited_once()
