"""Single-file tree-sitter parsing -> per-file parse results.

One generic recursive walker driven by ``LanguageSpec``: no .scm query files,
so nothing depends on the tree-sitter query API. tree-sitter is
error-tolerant by design (syntax errors produce ERROR nodes, the parse still
succeeds), so a broken file yields whatever symbols are recoverable and is
never fatal.
"""
from __future__ import annotations

import bisect
import codecs
import functools
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .languages import LanguageSpec, detect_language, get_grammar, language_spec, max_file_bytes

_log = logging.getLogger(__name__)

# <script> / <script lang="ts"> block inside a .vue file. The vue grammar in
# the language pack treats embedded script as raw text, so the fragment is
# extracted and parsed with the TS/JS grammar, with line numbers shifted by
# the lines before the match.
_VUE_SCRIPT_RE = re.compile(rb"<script\b[^>]*>([\s\S]*?)</script>")
_VUE_LANG_RE = re.compile(rb"<script\b[^>]*\blang=['\"]?(\w+)")

_SELF_PREFIXES = frozenset({"self", "cls", "this"})


@dataclass
class FileParseResult:
    sha256: str
    lang: str
    symbols: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    imports: list[dict[str, str | None]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    lines: int = 0               # file line count (file nodes only)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_file(path: Path, rel_path: str) -> FileParseResult | None:
    """Parse one repo-relative file; None for unsupported/unreadable files.

    Never raises for syntax errors — tree-sitter is error-tolerant. OSError
    (Windows file locks, deleted mid-scan) is caught per file.
    """
    lang = detect_language(rel_path)
    if lang is None:
        return None
    try:
        size = path.stat().st_size
        if over_size_limit(path, size, lang):
            _log.debug("graph parse skipped %s: %d bytes > %d", rel_path, size, max_file_bytes(lang))
            return None
        if lang == "sql":
            return _parse_sql_file(path)
        data = path.read_bytes()
    except OSError:
        return None
    return parse_bytes(data, rel_path, lang)


def over_size_limit(path: Path, size: int, lang: str | None) -> bool:
    """True when *size* exceeds the parse cap for *lang*.

    sql is measured in decoded bytes: a UTF-16 dump stores two bytes per
    character, so its raw stat would count twice against a cap that exists
    to catch pathological files, not to punish an encoding. Only files
    already over the raw cap pay the 4KB head read that settles encoding.
    """
    limit = max_file_bytes(lang)
    if size <= limit:
        return False
    if lang != "sql":
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(_SQL_DETECT_BYTES)
    except OSError:
        return True  # vanished/locked mid-scan: skip, same as an unreadable file
    codec = _detect_sql_encoding(head)
    divisor = 4 if codec == "utf-32" else 2 if codec in _UTF16_CODECS else 1
    return size // divisor > limit


def parse_bytes(data: bytes, rel_path: str, lang: str) -> FileParseResult:
    """Pure parse entry point (also used by tests)."""
    started = time.perf_counter()
    sha256 = hashlib.sha256(data).hexdigest()  # identity stays over the raw bytes
    if lang == "sql":
        data = _sql_to_utf8(data)
    result = FileParseResult(
        sha256=sha256,
        lang=lang,
        stats={"parse_ms": 0.0, "error": None},
        lines=data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
    )
    if lang == "vue":
        _parse_vue(data, rel_path, result)
    elif lang == "sql":
        _parse_sql(data, rel_path, result)
    else:
        _parse_lang(data, lang, result)
    result.stats["parse_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _parse_vue(data: bytes, rel_path: str, result: FileParseResult) -> None:
    match = _VUE_SCRIPT_RE.search(data)
    if not match:
        return
    fragment = match.group(1)
    line_offset = data.count(b"\n", 0, match.start())
    lang_match = _VUE_LANG_RE.search(data, 0, match.end())
    inner = "typescript" if lang_match and lang_match.group(1) in (b"ts", b"tsx") else "javascript"
    inner_result = FileParseResult(sha256=result.sha256, lang=inner)
    _parse_lang(fragment, inner, inner_result)
    for symbol in inner_result.symbols:
        symbol["line"] = symbol.get("line", 0) + line_offset
    result.symbols = inner_result.symbols
    result.edges = inner_result.edges
    result.imports = inner_result.imports


# --- sql ------------------------------------------------------------------
# Region-based regex extraction over comment/string-sanitized text. The
# language pack's sql grammar breaks on dominant real-world T-SQL (bracketed
# identifiers, proc parameter lists, EXEC, CREATE TRIGGER, GO batches), so an
# AST is deliberately not used — Vue's script-block path is the precedent for
# a language that bypasses the generic walker.
#
# Large files stream: bytes are sanitized incrementally into a sliding
# window and scanned in rounds over [end - SQL_SPAN_MAX, end), so a
# multi-hundred-MB dump never lands in memory whole. Every match shorter
# than SQL_SPAN_MAX is discovered complete in the round its last byte
# arrives; rounds re-cover the retained tail, which is safe because every
# emission deduplicates (sym_seen / edge_seen) and every attribution
# (region owner, PK/FK table) is a pure function of the declaration index.
# Forward state — sanitizer, column bodies, trigger headers, line cursors —
# consumes each byte exactly once. A match longer than SQL_SPAN_MAX that
# straddles a round boundary is lost: a documented pathological bound (a
# constraint list or dotted chain over 1 MiB), never a real schema.
# Known quirks deliberately preserved for byte-parity with the old
# whole-buffer parse (fixing either requires a CACHE_VERSION bump):
#   * attr_table freezes at the first non-table declaration (a CREATE VIEW
#     between two tables pins later FKs to the first table);
#   * a match cut by a region boundary in the old slice-based scan may
#     normalize differently — only reachable via a dangling dotted target
#     immediately before CREATE.

# An identifier: [bracketed] | `backticked` | bare, optionally schema-qualified.
_SQL_IDENT = (
    rb"(?:\[[^\]\r\n]+\]|`[^`\r\n]+`|[A-Za-z_#][\w$#@]*)"
    rb"(?:\s*\.\s*(?:\[[^\]\r\n]+\]|`[^`\r\n]+`|[A-Za-z_#][\w$#@]*))*"
)
_SQL_IDENT_ONLY = rb"(?:\[[^\]\r\n]+\]|`[^`\r\n]+`|[A-Za-z_#][\w$#@]*)"
# At least two segments — the qualified-call scan must not fire on plain
# keyword-paren shapes like `IN (...)` or `VALUES (...)`.
_SQL_QUALIFIED = (
    rb"(?:\[[^\]\r\n]+\]|`[^`\r\n]+`|[A-Za-z_#][\w$#@]*)"
    rb"(?:\s*\.\s*(?:\[[^\]\r\n]+\]|`[^`\r\n]+`|[A-Za-z_#][\w$#@]*))+"
)

_SQL_DECL_RE = re.compile(
    # T-SQL scripts modules with CREATE OR ALTER; MySQL/Oracle use OR REPLACE.
    rb"CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:TEMP(?:ORARY)?\s+)?"
    rb"(TABLE|VIEW|PROC|PROCEDURE|FUNCTION|TRIGGER)\s+(" + _SQL_IDENT + rb")",
    re.IGNORECASE,
)
_SQL_ALTER_RE = re.compile(rb"\bALTER\s+TABLE\s+(" + _SQL_IDENT + rb")", re.IGNORECASE)
_SQL_PK_INLINE_RE = re.compile(rb"\bPRIMARY\s+KEY\b(?!\s*\()", re.IGNORECASE)
_SQL_PK_RE = re.compile(rb"\bPRIMARY\s+KEY\s*\(([^)]*)\)", re.IGNORECASE)
_SQL_FK_RE = re.compile(
    rb"\bFOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s+(" + _SQL_IDENT + rb")", re.IGNORECASE
)
_SQL_REF_RE = re.compile(rb"\bREFERENCES\s+(" + _SQL_IDENT + rb")", re.IGNORECASE)
# T-SQL accepts `TOP (n) [PERCENT]` on every DML verb, not just UPDATE.
_SQL_TOP = rb"(?:TOP\s*\([^)]*\)\s+(?:PERCENT\s+)?)?"
_SQL_WRITE_RES = (
    re.compile(rb"\bINSERT\s+" + _SQL_TOP + rb"(?:INTO\s+)?(" + _SQL_IDENT + rb")", re.IGNORECASE),
    re.compile(rb"\bUPDATE\s+" + _SQL_TOP + rb"(" + _SQL_IDENT + rb")", re.IGNORECASE),
    re.compile(rb"\bDELETE\s+" + _SQL_TOP + rb"(?:FROM\s+)?(" + _SQL_IDENT + rb")", re.IGNORECASE),
)
_SQL_READ_RE = re.compile(rb"\b(?:FROM|JOIN)\s+(" + _SQL_IDENT + rb")", re.IGNORECASE)
_SQL_EXEC_RE = re.compile(
    rb"\bEXEC(?:UTE)?\s+(?:TOP\s*\([^)]*\)\s+)?(" + _SQL_IDENT + rb")", re.IGNORECASE
)
_SQL_QUAL_CALL_RE = re.compile(rb"(" + _SQL_QUALIFIED + rb")\s*\(", re.IGNORECASE)
_SQL_TRIGGER_ON_RE = re.compile(rb"\bON\s+(" + _SQL_IDENT + rb")", re.IGNORECASE)
_SQL_AS_RE = re.compile(rb"\bAS\b", re.IGNORECASE)
_SQL_BODY_STOP_RE = re.compile(
    rb";|\bGO\b|\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bEXEC(?:UTE)?\b",
    re.IGNORECASE,
)
_SQL_CONSTRAINT_LEAD = frozenset({
    "constraint", "primary", "foreign", "unique", "check", "key", "index",
})
# Bytes that can immediately precede another identifier segment — a qual-call
# match starting right after one is a mid-identifier fragment, not a real call.
_SQL_WORD_CHAR = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    b"_$#@].`"
)

# Words that look like identifiers after a keyword but can never be a table.
_SQL_NOT_TABLE = frozenset({
    "as", "begin", "end", "select", "insert", "update", "delete", "set",
    "values", "from", "join", "on", "into", "where", "and", "or", "declare",
    "exec", "execute", "if", "else", "return", "top", "distinct", "all",
    "with", "group", "order", "by", "having", "union", "case", "when",
    "then", "left", "right", "inner", "outer", "cross", "full", "of",
    "instead", "after", "truncate", "merge", "output", "exists", "between",
    "like", "in", "is", "not", "null", "default", "cascade", "restrict",
})
# DML pseudo-targets that would fabricate nodes if indexed as tables.
_SQL_PSEUDO_TABLES = frozenset({"inserted", "deleted"})

# Last word before a qualified `x.y (` match that proves the paren is a
# column/constraint list, not a call: CREATE TABLE x ( / REFERENCES x ( /
# INSERT INTO x ( all take parentheses but never invoke anything.
_SQL_QUAL_CALL_NO_PRECEDE = frozenset({
    "table", "view", "proc", "procedure", "function", "trigger",
    "into", "references", "alter", "index", "constraint", "key", "merge",
    "update", "delete", "from", "join", "values",
})

_SQL_DECL_KIND = {
    "TABLE": "table",
    "VIEW": "view",
    "PROC": "procedure",
    "PROCEDURE": "procedure",
    "FUNCTION": "function",
    "TRIGGER": "trigger",
}


# Streaming window constants (see the section header for the invariants):
# retention behind the scan start is SQL_SPAN_MAX + SQL_LOOKBACK bytes, which
# keeps the ALTER window (200) and the qual-call precede-24 byte-exact — a
# pure byte slice of the same text, never a collapsed approximation.
SQL_WINDOW = 4 * 1024 * 1024        # buffered bytes before a scan round
SQL_SPAN_MAX = 1024 * 1024          # longest match still discoverable across rounds
SQL_LOOKBACK = 200                  # bytes retained before each round's scan start
SQL_READ_CHUNK = 1024 * 1024        # raw read size in the streaming file path
SQL_MAX_DECLS = 50_000              # declaration-index quota (pathological files)
_SQL_PRE_CAP = 1024 * 1024          # CREATE-name -> '(' search prefix cap
_SQL_PART_CAP = 64 * 1024           # one column-part buffer quota
_SQL_HEAD_CAP = 400                 # trigger header bytes kept for the no-AS fallback


def _blank_non_nl(chunk: bytes) -> bytes:
    """Blank *chunk* to spaces, preserving newlines (line numbers stay exact)."""
    if not chunk:
        return b""
    if 0x0A not in chunk:
        return b" " * len(chunk)
    return b"\n".join(b" " * len(part) for part in chunk.split(b"\n"))


def _at_scan_edge(buf: bytes, m: "re.Match[bytes]", end_i: int) -> bool:
    """Could *m* grow if more bytes followed the scan edge?

    Given an endpos cut, the regex engine backtracks an identifier to a
    shorter form that still fits: ``dbo.Custo|mers`` ends exactly at the
    edge, and ``dbo.|Customers`` backtracks to ``dbo`` leaving the dot
    unconsumed. Either truncated form would poison sym_seen (the offset is
    deduped) so it must never be emitted before the full text is resident.
    A whitespace-then-dot lookahead detects both; deferred matches are
    re-scanned next round from an earlier offset with more bytes available.
    """
    if m.end() >= end_i:
        return True
    j = m.end()
    while j < end_i and buf[j] in b" \t\r\n":
        j += 1
    return j >= end_i or buf[j] == 0x2E  # '.' — a pending qualifier


# Next sanitizer state-machine marker in NORMAL mode. One search per segment:
# three separate finds for `-`/`/`/`'` would each scan to the end of the
# chunk whenever that byte is absent (an INSERT-heavy dump has one `'` per
# line and no dashes — 2 full-chunk scans per string).
_SQL_SAN_MARK_RE = re.compile(rb"[-/']")


class _SqlSanitizer:
    """Incremental comment/string blanking — byte-identical to one-shot scanning.

    Fed arbitrarily split chunks: ``feed()`` returns the sanitized prefix of
    everything received so far and holds back a trailing byte that could
    still complete a ``--`` / ``/*`` / ``*/`` / ``''`` marker split across a
    read boundary. ``finish()`` applies end-of-input semantics: an
    unterminated block comment or string blanks to the end (matching the old
    whole-buffer loop), a dangling ``-`` or ``/`` is plain text. Newlines
    survive everywhere so line numbers computed on the result match the
    original file, and a CREATE TABLE inside a comment or string provably
    never becomes a node. Double quotes are left alone — in T-SQL they are
    usually identifiers, rarely literals in DDL.
    """

    _NORMAL, _LINE, _BLOCK, _STRING = range(4)

    def __init__(self) -> None:
        self._mode = self._NORMAL
        self._pending = b""  # unclassifiable tail held for the next feed

    def feed(self, raw: bytes) -> bytes:
        data = self._pending + raw
        self._pending = b""
        out = bytearray()
        i, n = 0, len(data)
        mode = self._mode
        while i < n:
            if mode == self._NORMAL:
                mark = _SQL_SAN_MARK_RE.search(data, i)
                if mark is None:
                    out += data[i:]
                    break
                j = mark.start()
                out += data[i:j]
                if data[j] == 0x27:  # 'literal' with '' escape
                    mode = self._STRING
                    out.append(0x20)
                    i = j + 1
                    continue
                if j + 1 >= n:  # marker at the very end: hold for the next feed
                    self._pending = data[j:]
                    break
                if data[j] == 0x2D and data[j + 1] == 0x2D:  # -- line comment
                    mode = self._LINE
                    out += b"  "
                    i = j + 2
                    continue
                if data[j] == 0x2F and data[j + 1] == 0x2A:  # /* block */
                    mode = self._BLOCK
                    out += b"  "
                    i = j + 2
                    continue
                out.append(data[j])
                i = j + 1
                continue
            if mode == self._LINE:
                k = data.find(b"\n", i)
                if k == -1:
                    out += _blank_non_nl(data[i:])
                    break
                out += _blank_non_nl(data[i:k])
                out.append(0x0A)
                i = k + 1
                mode = self._NORMAL
                continue
            if mode == self._BLOCK:
                k = data.find(b"*/", i)
                if k == -1:
                    if n > i and data[n - 1] == 0x2A:  # could start '*/'
                        out += _blank_non_nl(data[i:n - 1])
                        self._pending = b"*"
                    else:
                        out += _blank_non_nl(data[i:])
                    break
                out += _blank_non_nl(data[i:k + 2])
                i = k + 2
                mode = self._NORMAL
                continue
            # mode == _STRING: blank until the closing quote ('' escapes both)
            k = data.find(b"'", i)
            if k == -1:
                out += _blank_non_nl(data[i:])
                break
            if k + 1 >= n:  # closing-or-escape quote split at the boundary
                out += _blank_non_nl(data[i:k])
                self._pending = b"'"
                break
            out += _blank_non_nl(data[i:k + 1])
            if data[k + 1] == 0x27:
                out.append(0x20)
                i = k + 2
                continue
            i = k + 1
            mode = self._NORMAL
            continue
        self._mode = mode
        return bytes(out)

    def finish(self) -> bytes:
        """End-of-input flush: pending markers resolve as plain text or blank."""
        pending, self._pending = self._pending, b""
        if not pending:
            return b""
        if self._mode in (self._BLOCK, self._STRING):  # unterminated -> blank to EOF
            return _blank_non_nl(pending)
        return pending


@functools.lru_cache(maxsize=8192)
def _sql_norm_name(raw: bytes) -> str:
    """`[dbo].[Orders]` / ``dbo . Orders`` -> canonical ``dbo.Orders``."""
    text = raw.decode("utf-8", "replace")
    parts = [p.strip() for p in re.split(r"\s*\.\s*", text)]
    cleaned = []
    for part in parts:
        if len(part) >= 2 and (part[0], part[-1]) in (("[", "]"), ("`", "`")):
            part = part[1:-1]
        cleaned.append(part)
    return ".".join(cleaned)


def _sql_valid_table(name: str, raw: bytes | None = None) -> bool:
    if not name or name.startswith("#"):  # temp tables are session-local
        return False
    # A quoted identifier (`[Order]`) is unambiguously a name, and a qualified
    # one (`[dbo].[Order]` -> dbo.Order) cannot be a bare keyword the regex
    # mistook for a target. Only an unquoted single segment can be a misfire
    # (`DELETE FROM` with an unparseable body matches "FROM" as the target).
    quoted = raw is not None and (b"[" in raw or b"`" in raw)
    lowered = name.lower()
    if not quoted and lowered in _SQL_PSEUDO_TABLES:
        return False
    if "." in lowered or quoted:
        return True
    return lowered not in _SQL_NOT_TABLE


def _re_ident_start(part: bytes):
    return re.match(rb"^\s*(" + _SQL_IDENT_ONLY + rb")", part)


class _LineCursor:
    """Forward line counter for one symbol stream (decls or columns).

    Queries arrive in ascending order; the delta between two resident
    positions is a plain newline count. When the cursor has fallen behind
    the window base the caller re-anchors it (see _SqlStreamParser._line_at).
    """

    __slots__ = ("pos", "line")

    def __init__(self) -> None:
        self.pos = 0
        self.line = 1


class _TableBody:
    """Streaming replica of the old _sql_split_columns: pre-body search, then parts.

    Phase ``pre`` hunts the body's opening ``(`` (stopping at the first
    statement keyword so `CREATE TABLE x AS SELECT ...` can never mistake a
    following statement's argument list for its own); phase ``body`` splits
    depth-0 commas exactly like the old inline loop. Region close (next
    declaration or EOF) finalizes the pending part — the old parse clipped
    the body at region_end the same way.
    """

    __slots__ = ("pos", "start", "qname", "phase", "prefix", "part",
                 "part_abs", "part_dropped", "depth")

    def __init__(self, start: int, qname: str) -> None:
        self.pos = start           # next absolute byte to consume
        self.start = start         # decl.end — anchor for prefix offsets
        self.qname = qname
        self.phase = "pre"
        self.prefix = bytearray()
        self.part = bytearray()
        self.part_abs = 0
        self.part_dropped = False
        self.depth = 0

    def feed(self, parser: "_SqlStreamParser", chunk: bytes, chunk_abs: int,
             final: bool = False) -> None:
        if self.phase == "done" or not chunk:
            return
        if self.phase == "pre":
            self.prefix += chunk
            stop = _SQL_BODY_STOP_RE.search(self.prefix)
            if stop is not None and not final and stop.end() == len(self.prefix):
                # Keyword touching the fed boundary may be a backtracked
                # shorter form — defer it to the next feed/region close.
                stop = None
            limit = stop.start() if stop is not None else len(self.prefix)
            open_at = self.prefix.find(b"(", 0, limit)
            if open_at == -1:
                if stop is not None or len(self.prefix) > _SQL_PRE_CAP:
                    self.phase = "done"
                    self.prefix.clear()
                return
            # Body opens: switch to part mode over everything after the '('.
            self.phase = "body"
            body = bytes(self.prefix[open_at + 1:])
            self.prefix.clear()
            self.depth = 0
            self.part_abs = self.start + open_at + 1
            self._scan_body(parser, body, self.part_abs)
            return
        self._scan_body(parser, chunk, chunk_abs)

    def _scan_body(self, parser: "_SqlStreamParser", data: bytes, data_abs: int) -> None:
        i, n = 0, len(data)
        depth = self.depth
        while i < n:
            j, tag = -1, 0
            for pos, t in ((data.find(b"(", i), 40), (data.find(b")", i), 41),
                           (data.find(b",", i), 44)):
                if pos != -1 and (j == -1 or pos < j):
                    j, tag = pos, t
            if j == -1:
                self._append_part(data[i:])
                break
            self._append_part(data[i:j])
            if tag == 40:
                depth += 1
                self._append_part(b"(")
            elif tag == 41:
                if depth == 0:      # body closes — old parse stopped at body_end
                    self._emit_part(parser)
                    self.depth = 0
                    self.phase = "done"
                    return
                depth -= 1
                self._append_part(b")")
            elif depth == 0:        # depth-0 comma: one column part ends
                self._emit_part(parser)
                self.part_abs = data_abs + j + 1
            else:
                self._append_part(b",")
            i = j + 1
        self.depth = depth

    def _append_part(self, data: bytes) -> None:
        if not data or self.part_dropped:
            return
        if len(self.part) + len(data) > _SQL_PART_CAP:
            self.part_dropped = True  # pathological part: skip its symbol only
            return
        self.part += data

    def _emit_part(self, parser: "_SqlStreamParser") -> None:
        if not self.part_dropped:
            m = _re_ident_start(bytes(self.part))
            if m is not None:
                first = _sql_norm_name(m.group(1)).lower()
                if first not in _SQL_CONSTRAINT_LEAD:
                    # table-level constraint, handled by the global PK/FK pass
                    col = _sql_norm_name(m.group(1))
                    name = f"{self.qname}.{col}"
                    parser._emit_symbol(name, "column", parser._line_at(parser.col_cursor, self.part_abs))
                    if _SQL_PK_INLINE_RE.search(bytes(self.part)):
                        parser._emit_edge("primary_key", name, self.qname)
        self.part.clear()
        self.part_dropped = False

    def finalize(self, parser: "_SqlStreamParser") -> None:
        if self.phase == "body":
            self._emit_part(parser)  # unclosed body: old parse kept [part_start, region_end)
        self.phase = "done"


class _TriggerHeader:
    """First-AS / ON-target tracking for CREATE TRIGGER across feeds.

    The old parse searched the whole header once the region was known; here
    the first ``AS`` is found incrementally (with a SQL_SPAN_MAX overlap so
    a boundary-split match is still seen), the first 400 header bytes are
    retained for the no-AS fallback, and the ON target is resolved once —
    when AS is found, over the still-resident prefix, exactly matching the
    old ``header[:as]`` substring search. Realistic headers settle in the
    first feed; a header whose prefix has already left the window is
    pathological and degrades to a search from the window base.
    """

    __slots__ = ("pos", "start", "qname", "as_pos", "as_from", "head", "on")

    def __init__(self, start: int, qname: str) -> None:
        self.pos = start
        self.start = start
        self.qname = qname
        self.as_pos: int | None = None
        self.as_from = start
        self.head = bytearray()
        self.on: str | None = None

    def feed(self, parser: "_SqlStreamParser", chunk: bytes, chunk_abs: int,
             final: bool = False) -> None:
        if len(self.head) < _SQL_HEAD_CAP:
            self.head += chunk[: _SQL_HEAD_CAP - len(self.head)]
        if self.as_pos is not None or not chunk:
            return
        hi = chunk_abs + len(chunk)
        lo = max(self.start, self.as_from, parser.base)
        if hi > lo:
            m = _SQL_AS_RE.search(parser.buf, lo - parser.base, hi - parser.base)
            if m is not None:
                # `AS` ending exactly at the fed boundary may be backtracked —
                # wait for more bytes; the overlap in as_from re-finds it.
                if not final and parser.base + m.end() == hi:
                    pass
                else:
                    self.as_pos = parser.base + m.start()
                    self._resolve_on(parser)
        self.as_from = max(self.start, hi - parser.span)

    def _resolve_on(self, parser: "_SqlStreamParser") -> None:
        if self.on is not None or self.as_pos is None:
            return
        lo = max(self.start, parser.base)
        hi = self.as_pos
        if hi > lo:
            m = _SQL_TRIGGER_ON_RE.search(parser.buf, lo - parser.base, hi - parser.base)
            if m is not None:
                self.on = _sql_norm_name(m.group(1))

    def finalize(self, parser: "_SqlStreamParser") -> None:
        target = self.on
        if self.as_pos is None:
            # no AS in the header: old parse searched its first 400 bytes
            m = _SQL_TRIGGER_ON_RE.search(self.head)
            target = _sql_norm_name(m.group(1)) if m is not None else None
        if target is not None:
            parser._emit_edge("reads", self.qname, target)


class _SqlStreamParser:
    """Sliding-window regex extraction over a sanitized SQL byte stream.

    See the section header for the invariants. In short: rounds scan
    ``[scan_from, end)`` where ``scan_from`` trails ``end`` by SQL_SPAN_MAX
    (SQL_LOOKBACK bytes retained behind it), so any match up to SQL_SPAN_MAX
    is discovered complete; re-scans of the retained tail are idempotent
    thanks to sym_seen/edge_seen dedup and pure-function attribution.
    """

    def __init__(self, result: FileParseResult,
                 window: int = SQL_WINDOW, span: int = SQL_SPAN_MAX) -> None:
        self.result = result
        self.window = window
        self.span = span
        self.san = _SqlSanitizer()
        self.buf = bytearray()       # sanitized text; absolute pos = base + index
        self.base = 0                # absolute offset of buf[0]
        self.scan_from = 0           # next round's scan start (absolute)
        self._nl_before_base = 0     # newlines in [0, base) — re-anchor source
        # Declaration index (pure functions of it back every attribution).
        self.decls: list[dict[str, Any]] = []
        self.decl_offs: list[int] = []
        self.decl_seen: set[int] = set()
        self.first_non_table: int | None = None
        self._decl_frozen = False
        self.machine: _TableBody | _TriggerHeader | None = None
        self.sym_seen: set[str] = set()
        self.edge_seen: set[tuple[str, str | None, str]] = set()
        # Write spans that may still cover a read in a later round (positions
        # are absolute; pruned once they fall behind the next scan start).
        self.write_spans: list[tuple[int, int]] = []
        self.decl_cursor = _LineCursor()
        self.col_cursor = _LineCursor()

    # --- public entries ---

    def feed(self, raw: bytes) -> None:
        out = self.san.feed(raw)
        if out:
            self.buf += out
        if len(self.buf) >= self.window:
            self._round()

    def finish(self) -> None:
        out = self.san.finish()
        if out:
            self.buf += out
        self._round(final=True)

    # --- emission / lines ---

    def _emit_symbol(self, name: str, graph_type: str, line: int) -> None:
        if not name or name in self.sym_seen:
            return
        self.sym_seen.add(name)
        self.result.symbols.append({"name": name, "type": graph_type, "line": line})

    def _emit_edge(
        self, etype: str, src: str | None, target: str, raw: bytes | None = None
    ) -> None:
        # Declared symbols are already known to be real names, whatever the
        # keyword filter would say about a table called `[Order]`.
        if target not in self.sym_seen and not _sql_valid_table(target, raw):
            return
        key = (etype, src, target)
        if key in self.edge_seen:
            return
        self.edge_seen.add(key)
        self.result.edges.append({
            "type": etype,
            "target": target,
            "target_kind": "sql",
            "from": src,
        })

    def _line_at(self, cursor: _LineCursor, abs_pos: int) -> int:
        """1-based line of *abs_pos* on the sanitized text (resident only)."""
        if cursor.pos <= abs_pos and cursor.pos >= self.base:
            cursor.line += self.buf.count(b"\n", cursor.pos - self.base, abs_pos - self.base)
            cursor.pos = abs_pos
            return cursor.line
        # Stale cursor (its anchor left the window) or an out-of-order query:
        # re-anchor from the pop checkpoint. abs_pos < base is only reachable
        # with span < the column-part quota (test configs); production span
        # (1MiB) dwarfs the 64KiB part cap, so the count starts at base.
        end_i = max(0, abs_pos - self.base)
        cursor.line = 1 + self._nl_before_base + self.buf.count(b"\n", 0, end_i)
        cursor.pos = abs_pos
        return cursor.line

    # --- round machinery ---

    def _round(self, final: bool = False) -> None:
        end = self.base + len(self.buf)
        scan_from = self.scan_from
        if end > scan_from:
            # 1. declarations first: region owners and machines need the index
            new_decls = self._scan_decls(scan_from, end, final)
            for decl in new_decls:
                self._close_machine(decl["offset"])   # prior region ends here
                self._emit_symbol(decl["qname"], decl["kind"], decl["line"])
                self._open_machine(decl)
            if self.machine is not None:
                self._feed_machine(end, final)        # last region stays open
            # 2. per-region DML (writes-first), then the table-level PK/FK pass
            self._dml_round(scan_from, end, final)
            self._pkf_round(scan_from, end, final)
            # 3. pop: retain SPAN_MAX + LOOKBACK behind the next scan start
            self.scan_from = max(0, end - self.span)
            # A span ending at/before the next scan start cannot cover any
            # read the next round will look at.
            self.write_spans = [sp for sp in self.write_spans if sp[1] > self.scan_from]
            keep_from = self.scan_from - SQL_LOOKBACK
            if keep_from > self.base:
                drop = keep_from - self.base
                self._nl_before_base += self.buf.count(b"\n", 0, drop)
                del self.buf[:drop]
                self.base = keep_from
        if final and self.machine is not None:
            self._close_machine(end)

    def _scan_decls(self, scan_from: int, end: int, final: bool) -> list[dict[str, Any]]:
        if self._decl_frozen:
            return []
        new: list[dict[str, Any]] = []
        end_i = end - self.base
        for m in _SQL_DECL_RE.finditer(self.buf, scan_from - self.base, end_i):
            if not final and _at_scan_edge(self.buf, m, end_i):
                continue  # backtracked identifier; next round re-finds it
            offset = self.base + m.start()
            if offset in self.decl_seen:
                continue
            kind = _SQL_DECL_KIND[m.group(1).upper().decode()]
            qname = _sql_norm_name(m.group(2))
            if not qname or qname.startswith("#"):
                continue  # temp/empty names never become nodes or region owners
            if len(self.decls) >= SQL_MAX_DECLS:
                self._decl_frozen = True  # pathological quota: freeze the index
                break
            decl = {"offset": offset, "end": self.base + m.end(), "kind": kind,
                    "qname": qname, "line": self._line_at(self.decl_cursor, offset)}
            self.decl_seen.add(offset)
            self.decls.append(decl)
            self.decl_offs.append(offset)
            if kind != "table" and self.first_non_table is None:
                self.first_non_table = len(self.decls) - 1
            new.append(decl)
        return new

    def _open_machine(self, decl: dict[str, Any]) -> None:
        if decl["kind"] == "table":
            self.machine = _TableBody(decl["end"], decl["qname"])
        elif decl["kind"] == "trigger":
            self.machine = _TriggerHeader(decl["end"], decl["qname"])
        else:
            self.machine = None  # procs/views have no column body or header

    def _feed_machine(self, end: int, final: bool = False) -> None:
        m = self.machine
        if m is None or m.pos >= end:
            return
        lo = max(0, m.pos - self.base)  # pos >= base by construction; clamped anyway
        chunk = bytes(self.buf[lo:end - self.base])
        m.feed(self, chunk, self.base + lo, final)
        m.pos = end

    def _close_machine(self, at: int) -> None:
        m = self.machine
        self.machine = None
        if m is None:
            return
        if m.pos < at:
            lo = max(0, m.pos - self.base)
            chunk = bytes(self.buf[lo:at - self.base])
            # final=True: `at` is a real region boundary (next declaration or
            # EOF) — the old parse searched the region slice with the same
            # end-of-slice keyword semantics.
            m.feed(self, chunk, self.base + lo, True)
            m.pos = at
        m.finalize(self)

    def _dml_round(self, scan_from: int, end: int, final: bool) -> None:
        # Regions follow the old layout exactly: [0, first decl) has no
        # owner, each declaration owns [offset, next offset), the last runs
        # to EOF. Only the region containing scan_from is clipped on the left.
        pos = scan_from
        while pos < end:
            i = bisect.bisect_right(self.decl_offs, pos) - 1
            if i < 0:
                decl = None
                nxt = self.decl_offs[0] if self.decl_offs else None
            else:
                decl = self.decls[i]
                nxt = self.decl_offs[i + 1] if i + 1 < len(self.decl_offs) else None
            hi = end if nxt is None else min(nxt, end)
            if hi > pos:
                # A region cut at the round end (not at a declaration) is an
                # artificial boundary: matches touching it are deferred so a
                # backtracked shorter form is never emitted. A region cut at
                # the next declaration keeps the old slice semantics.
                self._scan_region(pos, hi, decl,
                                  defer_edge=not final and hi == end)
            if nxt is None or nxt >= end:
                break
            pos = nxt

    def _scan_region(self, lo: int, hi: int, decl: dict[str, Any] | None,
                     defer_edge: bool) -> None:
        src: str | None = None
        if decl is not None and decl["kind"] in ("procedure", "function", "view", "trigger"):
            src = decl["qname"]
        lo_i, hi_i = lo - self.base, hi - self.base
        # Writes first, so `DELETE FROM x`'s FROM never double-counts as a
        # read. Spans from the previous round are carried in too: a statement
        # whose DELETE/UPDATE keyword sits just before this round's scan start
        # is not re-found by the anchored write regex, but its FROM/INTO part
        # is, and without the span it would be emitted as a spurious read.
        spans: list[tuple[int, int]] = [
            sp for sp in self.write_spans if sp[1] > lo and sp[0] < hi
        ]
        for rx in _SQL_WRITE_RES:
            for m in rx.finditer(self.buf, lo_i, hi_i):
                if defer_edge and _at_scan_edge(self.buf, m, hi_i):
                    continue  # backtracked identifier; next round re-finds it
                self._emit_edge("writes", src, _sql_norm_name(m.group(1)), m.group(1))
                span = (self.base + m.start(), self.base + m.end())
                spans.append(span)
                self.write_spans.append(span)
        # Containment filter: exists span with s <= p and q <= e. Sorted by
        # start, that is a prefix max on e — same predicate as the old any().
        starts: list[int] = []
        max_e: list[int] = []
        if spans:
            spans.sort()
            running = -1
            for s, e in spans:
                starts.append(s)
                if e > running:
                    running = e
                max_e.append(running)

        def covered(p: int, q: int) -> bool:
            if not starts:
                return False
            i = bisect.bisect_right(starts, p) - 1
            return i >= 0 and max_e[i] >= q

        for m in _SQL_READ_RE.finditer(self.buf, lo_i, hi_i):
            if defer_edge and _at_scan_edge(self.buf, m, hi_i):
                continue
            p, q = self.base + m.start(), self.base + m.end()
            if covered(p, q):
                continue
            self._emit_edge("reads", src, _sql_norm_name(m.group(1)), m.group(1))
        for m in _SQL_EXEC_RE.finditer(self.buf, lo_i, hi_i):
            if defer_edge and _at_scan_edge(self.buf, m, hi_i):
                continue
            target = _sql_norm_name(m.group(1))
            if not target.startswith("@"):
                self._emit_edge("calls", src, target, m.group(1))
        for m in _SQL_QUAL_CALL_RE.finditer(self.buf, lo_i, hi_i):
            if defer_edge and _at_scan_edge(self.buf, m, hi_i):
                continue
            if m.start() == lo_i and lo_i > 0:
                # Unlike every other pass this one has no \b keyword anchor,
                # so a scan start landing mid-identifier would match a
                # fragment (dbo.Orders| -> bo.Orders). The full match was
                # already discovered by the round in which its '(' arrived
                # (that round's scan_from cannot trail past the match start).
                prev = self.buf[lo_i - 1]
                if prev in _SQL_WORD_CHAR:
                    continue
            p, q = self.base + m.start(), self.base + m.end()
            if covered(p, q):
                continue
            before = self.buf[max(0, m.start() - 24):m.start()]
            words = re.findall(rb"[A-Za-z_]+", bytes(before))
            if words and words[-1].decode("ascii", "ignore").lower() in _SQL_QUAL_CALL_NO_PRECEDE:
                continue
            self._emit_edge("calls", src, _sql_norm_name(m.group(1)), m.group(1))

    # Table-level PK/FK (also covers inline REFERENCES): attributed to the
    # closest preceding CREATE TABLE, unless an ALTER TABLE sits after it and
    # within reach — then the ALTER names the table being constrained.
    def _attr_table(self, p: int) -> str | None:
        i = bisect.bisect_left(self.decl_offs, p)   # decls[i:] start at/after p
        j = self.first_non_table if self.first_non_table is not None else len(self.decls)
        # Frozen-run quirk, preserved for parity: the old walk broke at the
        # first non-table declaration, so a CREATE VIEW between two tables
        # pins later attributions to the last table before the view. Fixing
        # this requires a CACHE_VERSION bump.
        create = self.decls[min(i, j) - 1] if min(i, j) > 0 else None
        alter = None
        for m in _SQL_ALTER_RE.finditer(self.buf, max(0, p - SQL_LOOKBACK) - self.base,
                                        p - self.base):
            alter = m  # last one wins
        if alter is not None and (create is None or self.base + alter.start() > create["offset"]):
            return _sql_norm_name(alter.group(1))
        return create["qname"] if create else None

    def _pkf_round(self, scan_from: int, end: int, final: bool) -> None:
        lo_i, hi_i = scan_from - self.base, end - self.base
        defer = not final
        for m in _SQL_PK_RE.finditer(self.buf, lo_i, hi_i):
            if defer and _at_scan_edge(self.buf, m, hi_i):
                continue  # backtracked identifier; next round re-finds it
            table = self._attr_table(self.base + m.start())
            if table is None:
                continue
            for raw_col in m.group(1).split(b","):
                col = _sql_norm_name(raw_col)
                if col:
                    self._emit_edge("primary_key", f"{table}.{col}", table)
        for rx in (_SQL_FK_RE, _SQL_REF_RE):
            for m in rx.finditer(self.buf, lo_i, hi_i):
                if defer and _at_scan_edge(self.buf, m, hi_i):
                    continue
                table = self._attr_table(self.base + m.start())
                if table is None:
                    continue
                self._emit_edge(
                    "foreign_key", table, _sql_norm_name(m.group(1)), m.group(1)
                )


def _parse_sql(data: bytes, rel_path: str, result: FileParseResult) -> None:
    """Single-shot entry (parse_bytes): one chunk through the stream parser."""
    parser = _SqlStreamParser(result)
    parser.feed(data)
    parser.finish()


# --- sql encoding -------------------------------------------------------
# SQL Server tooling writes dumps as UTF-16 (SSMS "Unicode", sqlcmd -u),
# always BOM'd; scripts converted elsewhere may be BOM-less. The scanner's
# regexes are ASCII, so UTF-16 bytes (C\0R\0E\0T\0E\0 \0T\0A\0B...) match
# nothing: the file "parses" with zero symbols and no error, and the graph
# silently loses the whole schema. Every entry point therefore transcodes to
# UTF-8 before scanning; file identity (sha256) still covers the raw bytes,
# and over_size_limit() measures the cap in decoded bytes.
_UTF16_CODECS = frozenset({"utf-16", "utf-16-le", "utf-16-be"})
_SQL_BOMS = (
    # utf-32 first: UTF-32LE's BOM starts with UTF-16LE's (FF FE 00 00).
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_SQL_DETECT_BYTES = 4096   # head sample for BOM-less sniffing and the cap
_SQL_MIN_SNIFF = 8         # bytes before NUL-parity sniffing trusts itself


def _detect_sql_encoding(head: bytes) -> str | None:
    """Codec for a non-UTF-8 SQL byte stream; None means "already UTF-8".

    BOM-less UTF-16 is recognized by NUL parity: ASCII text encoded UTF-16LE
    is ``x\0x\0``, so NULs pile onto one parity of byte offsets. UTF-8 text
    contains no NUL at all — that is what makes the density test safe: a
    UTF-8 file can only trip it by actually carrying NUL bytes, and random
    binary sits far below the threshold at either parity.
    """
    for bom, codec in _SQL_BOMS:
        if head.startswith(bom):
            return codec
    sample = head[:_SQL_DETECT_BYTES]
    n = len(sample) - (len(sample) % 2)
    if n < _SQL_MIN_SNIFF:
        return None
    even = sum(1 for i in range(0, n, 2) if sample[i] == 0)
    odd = sum(1 for i in range(1, n, 2) if sample[i] == 0)
    # One parity must hold >= 12.5% of ALL bytes while the other stays rare.
    # ASCII-heavy SQL clears that easily even with CJK comments mixed in.
    if odd * 8 >= n and odd > even * 3:
        return "utf-16-le"
    if even * 8 >= n and even > odd * 3:
        return "utf-16-be"
    return None


def _sql_to_utf8(data: bytes) -> bytes:
    """Whole-buffer transcode for the single-shot sql path; UTF-8 is a no-op."""
    codec = _detect_sql_encoding(data)
    if codec is None:
        return data
    # "utf-16" (BOM'd) strips the BOM; the bare -le/-be codecs never see one.
    return data.decode(codec, errors="replace").encode("utf-8")


def _parse_sql_file(path: Path) -> FileParseResult | None:
    """Streaming SQL file entry: chunked reads, incremental sha and line count.

    No whole-file read_bytes() — a multi-hundred-MB dump is sanitized and
    scanned in a bounded window, and a UTF-16 dump is transcoded chunk by
    chunk (the encoding is sniffed once, from the first read). OSError and
    a decode failure both return None like the generic path.
    """
    started = time.perf_counter()
    digest = hashlib.sha256()
    total = nl = 0
    last = b""
    result = FileParseResult(
        sha256="", lang="sql", stats={"parse_ms": 0.0, "error": None}, lines=0,
    )
    parser = _SqlStreamParser(result)
    decoder: codecs.IncrementalDecoder | None = None
    sniffed = False
    try:
        with open(path, "rb") as fh:
            while True:
                raw = fh.read(SQL_READ_CHUNK)
                if not raw:
                    break
                digest.update(raw)  # identity covers the file as stored
                if not sniffed:
                    sniffed = True  # a regular file's first read is the head
                    codec = _detect_sql_encoding(raw)
                    if codec is not None:
                        decoder = codecs.getincrementaldecoder(codec)(
                            errors="replace"
                        )
                chunk = raw if decoder is None else decoder.decode(raw).encode("utf-8")
                total += len(chunk)  # line accounting runs over decoded bytes
                nl += chunk.count(b"\n")
                last = chunk[-1:]
                parser.feed(chunk)
            if decoder is not None:
                tail = decoder.decode(b"", final=True).encode("utf-8")
                if tail:
                    total += len(tail)
                    nl += tail.count(b"\n")
                    last = tail[-1:]
                    parser.feed(tail)
        parser.finish()
    except (OSError, UnicodeError):
        return None
    result.sha256 = digest.hexdigest()
    result.lines = nl + (1 if total and last != b"\n" else 0)
    result.stats["parse_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _parse_lang(data: bytes, lang: str, result: FileParseResult) -> None:
    grammar = get_grammar(lang)
    if grammar is None:
        result.stats["error"] = f"grammar unavailable: {lang}"
        return
    try:
        from tree_sitter import Parser
        parser = Parser()
        parser.language = grammar
        tree = parser.parse(data)
    except Exception as exc:  # grammar/API mismatch — degrade, never raise
        result.stats["error"] = str(exc)
        return

    ctx = _Ctx(spec=language_spec(lang), lang=lang, result=result)
    _walk(tree.root_node, ctx)

    # A file that yields nothing AND has syntax errors is genuinely broken —
    # tree-sitter recovers symbols from partially-broken files, so this only
    # trips on unparseable garbage. Counted as failed in build stats.
    root = tree.root_node
    if not result.symbols and getattr(root, "has_error", False):
        result.stats["error"] = "syntax"


class _Ctx:
    __slots__ = ("spec", "lang", "result", "class_stack", "current_qname",
                 "inherits_seen")

    def __init__(self, spec: LanguageSpec, lang: str, result: FileParseResult) -> None:
        self.spec = spec
        self.lang = lang
        self.result = result
        self.class_stack: list[str] = []
        self.current_qname: str | None = None
        # (from, target) pairs already emitted as inherits edges. Sealed
        # permits and the subtype's own extends/implements clause can both
        # state the same parent relation from different class nodes, so
        # dedup must live at file level, not per declaration.
        self.inherits_seen: set[tuple[str, str]] = set()


def _node_text(node) -> str:
    try:
        return node.text.decode("utf-8", "replace")
    except Exception:
        return ""


def _chain_text(node) -> str | None:
    """Dotted chain text for identifier/attribute/member_expression nodes."""
    if node is None:
        return None
    typ = node.type
    if typ in ("identifier", "property_identifier", "type_identifier", "nested_type_identifier"):
        return _node_text(node)
    if typ == "attribute":  # python: obj.attr
        obj = _chain_text(node.child_by_field_name("object"))
        attr = _chain_text(node.child_by_field_name("attribute"))
        return f"{obj}.{attr}" if obj and attr else None
    if typ == "member_expression":  # ts/js: obj.prop
        obj = _chain_text(node.child_by_field_name("object"))
        prop = _chain_text(node.child_by_field_name("property"))
        return f"{obj}.{prop}" if obj and prop else None
    if typ == "field_access":  # java: obj.field (static or instance)
        obj = _chain_text(node.child_by_field_name("object"))
        field = _chain_text(node.child_by_field_name("field"))
        return f"{obj}.{field}" if obj and field else None
    if typ in ("scoped_identifier", "dotted_name"):  # python/java dotted paths
        parts = [_node_text(child) for child in node.children if child.type == "identifier"]
        return ".".join(parts) if parts else None
    if typ == "method_invocation":
        # java: chained calls (getService().fetch()) — only the name and
        # object fields; arguments must not leak into the chain.
        name = _node_text(node.child_by_field_name("name"))
        if not name:
            return None
        obj = _chain_text(node.child_by_field_name("object"))
        return f"{obj}.{name}" if obj else name
    if typ == "object_creation_expression":  # java/csharp: `new Foo()` in a chain
        type_node = node.child_by_field_name("type")
        return _node_text(type_node) if type_node is not None else None
    if typ == "member_access_expression":  # csharp: obj.Member (fields: expression, name)
        expr = _chain_text(node.child_by_field_name("expression"))
        name = _chain_text(node.child_by_field_name("name"))
        return f"{expr}.{name}" if expr and name else None
    if typ == "qualified_name":  # csharp: A.B (fields: qualifier, name)
        q = _chain_text(node.child_by_field_name("qualifier"))
        n = _chain_text(node.child_by_field_name("name"))
        return f"{q}.{n}" if q and n else None
    if typ == "generic_name":  # csharp: IRepo<Thing> -> IRepo (drop type args)
        for child in node.named_children:
            if child.type != "type_argument_list":
                return _chain_text(child)
        return None
    if typ == "invocation_expression":  # csharp: chain through Foo().Bar
        return _chain_text(node.child_by_field_name("function"))
    if typ == "this":  # csharp: this.Bar -> stripped by _strip_self_chain
        return _node_text(node)
    return None


def _strip_self_chain(chain: str) -> str | None:
    parts = chain.split(".")
    if parts and parts[0] in _SELF_PREFIXES:
        parts = parts[1:]
    return ".".join(parts) if parts else None


def _qualified(ctx: _Ctx, name: str) -> str:
    return f"{'.'.join(ctx.class_stack)}.{name}" if ctx.class_stack else name


def _record_symbol(ctx: _Ctx, qname: str, graph_type: str, node) -> None:
    # qname arrives already class-qualified from the walker — store verbatim.
    ctx.result.symbols.append({
        "name": qname,
        "type": graph_type,
        "line": node.start_point[0] + 1,
    })


def _record_call(ctx: _Ctx, chain: str) -> None:
    cleaned = _strip_self_chain(chain)
    if not cleaned:
        return
    if cleaned.split(".")[0] in ctx.spec.builtins:
        return
    ctx.result.edges.append({
        "type": "calls",
        "target": cleaned,
        "target_kind": "callee",
        "from": ctx.current_qname,
    })


def _record_inherits(ctx: _Ctx, chain: str, class_qname: str) -> None:
    cleaned = _strip_self_chain(chain)
    if not cleaned:
        return
    # File-level dedup: sealed permits and the subtype's own
    # extends/implements clause can both state the same parent relation from
    # different class nodes (sealed interface X permits A {} + A implements
    # X). Also protects against duplicate targets within one declaration.
    key = (class_qname, cleaned)
    if key in ctx.inherits_seen:
        return
    ctx.inherits_seen.add(key)
    ctx.result.edges.append({
        "type": "inherits",
        "target": cleaned,
        "target_kind": "inherits",
        "from": class_qname,
    })


def _java_type_targets(node) -> list[str]:
    """Simple type names under a Java type-list-ish node, recursively.

    Catches both shapes seen in the grammar: a field holding a bare
    ``type_identifier`` (superclass) and a ``type_list`` wrapper whose
    children are comma-separated ``type_identifier`` / ``nested_type_identifier``
    nodes (implements, interface extends, sealed permits). Nested generics
    (``List<Foo>``) resolve to the outermost type name.
    """
    if node is None:
        return []
    typ = node.type
    if typ in ("type_identifier", "nested_type_identifier", "scoped_type_identifier"):
        return [_node_text(node)]
    if typ == "generic_type":
        child = node.child_by_field_name("name") or next(
            (c for c in node.children if c.type in ("type_identifier", "scoped_type_identifier")),
            None,
        )
        return [_node_text(child)] if child is not None else []
    out: list[str] = []
    for child in node.children:
        out.extend(_java_type_targets(child))
    return out


def _extract_inheritance(ctx: _Ctx, class_node, class_qname: str) -> None:
    spec = ctx.spec
    if spec.is_python():
        superclasses = class_node.child_by_field_name(spec.inherited_field)  # type: ignore[arg-type]
        if superclasses is not None:
            for child in superclasses.children:
                chain = _chain_text(child)
                if chain:
                    _record_inherits(ctx, chain, class_qname)
        return
    if spec.is_java():
        # java: collect every inheritance target — superclass (extends),
        # implements clause, interface extends clause, and sealed permits —
        # then emit deduplicated inherits edges (same (from, target) pair can
        # legitimately appear twice, e.g. `sealed interface X permits A {}`
        # where A also `implements X`).
        pairs: list[tuple[str, str]] = []

        def _add(from_qname: str, target: str | None) -> None:
            cleaned = _strip_self_chain(target) if target else None
            if cleaned:
                pairs.append((from_qname, cleaned))

        # class/record `extends Base` — superclass field wraps a type node.
        superclass = class_node.child_by_field_name("superclass")
        for target in _java_type_targets(superclass):
            _add(class_qname, target)
        # class/record `implements A, B` — interfaces field (its value node
        # is a `super_interfaces` node) wraps a type_list.
        interfaces = class_node.child_by_field_name("interfaces")
        for target in _java_type_targets(interfaces):
            _add(class_qname, target)
        # interface `extends P, Q` — grammar keeps this as a plain child
        # (``extends_interfaces``), not a named field.
        for child in class_node.children:
            if child.type == "extends_interfaces":
                for target in _java_type_targets(child):
                    _add(class_qname, target)
        # sealed `permits A, B` — the sealed parent declares its permitted
        # subtypes; edges point from each subtype to the sealed parent
        # (same direction as extends/implements: from=subtype, target=parent).
        # Only subtypes declared in this same file qualify: a permitted
        # subtype in another file establishes the relation through its own
        # extends/implements clause (Java requires it), and emitting the edge
        # here would attach it to the wrong node (this file).
        program = class_node.parent
        local_types: set[str] = set()
        if program is not None:
            for decl in program.children:
                if decl.type in (
                    "class_declaration", "interface_declaration",
                    "enum_declaration", "record_declaration",
                ):
                    name_node = decl.child_by_field_name("name")
                    if name_node is not None:
                        local_types.add(_node_text(name_node))
        for child in class_node.children:
            if child.type != "permits":
                continue
            for target in _java_type_targets(child):
                if target in local_types:
                    _add(target, class_qname)
        for from_qname, target in pairs:
            _record_inherits(ctx, target, from_qname)
        return
    if spec.is_csharp():
        # csharp: bases are comma-separated children of a plain base_list
        # child (identifier | qualified_name | generic_name) — covers class,
        # record, struct and interface declarations alike.
        for child in class_node.children:
            if child.type != "base_list":
                continue
            for base in child.named_children:
                chain = _chain_text(base)
                if chain:
                    _record_inherits(ctx, chain, class_qname)
        return
    # ts/js: class_heritage -> extends_clause / implements_clause -> expressions
    for child in class_node.children:
        if child.type == "extends_type_clause":
            # An interface's `extends` list is a direct child of the
            # interface_declaration (there is no class_heritage wrapper).
            for expr in child.named_children:
                chain = _chain_text(expr)
                if chain:
                    _record_inherits(ctx, chain, class_qname)
            continue
        if child.type != "class_heritage":
            continue
        for clause in child.children:
            if clause.type not in ("extends_clause", "implements_clause"):
                continue
            for expr in clause.children:
                chain = _chain_text(expr)
                if chain:
                    _record_inherits(ctx, chain, class_qname)


def _flatten_items(node):
    """Field node that is a single item or an implicit list of items."""
    if node is None:
        return []
    if node.type in ("dotted_name", "aliased_import", "import_specifier", "identifier"):
        return [node]
    return [child for child in node.children if child.type != ","]


def _dotted(node) -> str:
    return _chain_text(node) or _node_text(node).replace(" ", "")


def _import_items(node) -> list:
    """Name nodes of a Python import statement.

    tree-sitter fields only the FIRST name, so `import os, sys` must be read
    from the child list — child_by_field_name("name") returned just `os`.
    In a from-import the module sits before the `import` keyword, which
    separates it from the imported names.
    """
    children = node.children
    start = 0
    for i, child in enumerate(children):
        if child.type == "import":
            start = i + 1
            break
    return [
        child for child in children[start:]
        if child.type in ("dotted_name", "aliased_import")
    ]


def _parse_imports_python(ctx: _Ctx, node) -> None:
    imports = ctx.result.imports
    if node.type == "import_statement":
        for item in _import_items(node):
            if item.type == "aliased_import":
                module = _dotted(item.child_by_field_name("name"))
                alias_node = item.child_by_field_name("alias")
                imports.append({"module": module, "name": None, "alias": _node_text(alias_node) if alias_node else None})
            elif item.type == "dotted_name":
                imports.append({"module": _dotted(item), "name": None, "alias": None})
        return
    # import_from_statement
    module_node = node.child_by_field_name("module_name")
    module = _dotted(module_node) if module_node is not None else ""
    for item in _import_items(node):
        if item.type == "aliased_import":
            name = _dotted(item.child_by_field_name("name"))
            alias_node = item.child_by_field_name("alias")
            imports.append({"module": module, "name": name, "alias": _node_text(alias_node) if alias_node else None})
        elif item.type == "dotted_name":
            name = _dotted(item)
            imports.append({"module": module, "name": name, "alias": None})


def _string_content(node) -> str | None:
    text = _node_text(node)
    if len(text) >= 2 and text[0] in "\"'`" and text[-1] == text[0]:
        return text[1:-1]
    return text if text else None


def _parse_imports_ts(ctx: _Ctx, node) -> None:
    source = node.child_by_field_name("source")
    module = _string_content(source) if source is not None else None
    if module is None:
        return
    clause = node.child_by_field_name("import_clause")
    if clause is None:
        # The clause is a child node, not a named field, in this grammar.
        for child in node.children:
            if child.type == "import_clause":
                clause = child
                break
    if clause is None:  # side-effect import: `import "./x"` — module only
        ctx.result.imports.append({"module": module, "name": None, "alias": None})
        return
    for child in clause.children:
        if child.type == "identifier":  # default import
            ctx.result.imports.append({"module": module, "name": _node_text(child), "alias": None})
        elif child.type == "namespace_import":
            # No named fields in this grammar: `*`, `as`, `identifier` —
            # the alias is the last identifier child.
            alias = None
            for inner in child.children:
                if inner.type == "identifier":
                    alias = _node_text(inner)
            ctx.result.imports.append({
                "module": module, "name": "*",
                "alias": alias,
            })
        elif child.type == "named_imports":
            for spec in _flatten_items(child):
                if spec.type != "import_specifier":
                    continue
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                ctx.result.imports.append({
                    "module": module,
                    "name": _node_text(name) if name is not None else None,
                    "alias": _node_text(alias) if alias is not None else None,
                })


def _parse_imports_java(ctx: _Ctx, node) -> None:
    """import_declaration: full dotted path, optional static + wildcard.

    Java imports always name a fully-qualified type (com.example.lib.Greeter)
    or a wildcard package (com.example.lib.*); static imports name a member.
    The graph only keeps the module for import edges, so the last segment is
    dropped (resolved to a file by package structure at assembly time).
    """
    scoped = None
    for child in node.children:
        if child.type == "scoped_identifier":
            scoped = child
            break
    if scoped is None:
        return
    # Strip a trailing wildcard: com.example.lib.* -> com.example.lib
    text = _node_text(scoped)
    module = text[:-2] if text.endswith(".*") else text
    ctx.result.imports.append({"module": module, "name": None, "alias": None})


def _parse_imports_csharp(ctx: _Ctx, node) -> None:
    """using_directive: a namespace (or type) path, optionally aliased.

    The alias, when present, is the ``name`` field and the target rides as a
    plain named child; without an alias the single named child is the target
    (``using static`` keeps its keyword anonymous, so it never shows up here).
    """
    alias_node = node.child_by_field_name("name")
    target = None
    for child in node.named_children:
        if alias_node is not None and child.start_byte == alias_node.start_byte:
            continue
        target = child
        break
    if target is None:
        return
    module = _chain_text(target) or _node_text(target)
    module = module.split("<", 1)[0].strip()  # generic alias target -> raw type path
    if not module:
        return
    alias = _node_text(alias_node) if alias_node is not None else None
    ctx.result.imports.append({"module": module, "name": None, "alias": alias})


def _is_require_call(spec: LanguageSpec, node) -> bool:
    if not spec.is_ts_family():
        return False
    callee = node.child_by_field_name(spec.call_callee_field)
    return callee is not None and callee.type == "identifier" and _node_text(callee) == "require"


def _parse_require_import(ctx: _Ctx, node) -> None:
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return
    for child in arguments.children:
        if child.type == "string":
            module = _string_content(child)
            if module:
                ctx.result.imports.append({"module": module, "name": None, "alias": None})
            return


def _walk(node, ctx: _Ctx) -> None:
    spec = ctx.spec
    typ = node.type

    if typ in spec.symbol_nodes and spec.symbol_nodes[typ] in ("class", "function", "symbol"):
        name_node = node.child_by_field_name(spec.name_field)
        # Name nodes differ per language (identifier / type_identifier /
        # property_identifier); only the text matters. When the name is
        # missing (malformed code) fall through to the generic walk so the
        # subtree's calls are still indexed.
        if name_node is not None:
            name = _node_text(name_node)
            if name:
                qname = _qualified(ctx, name)
                graph_type = spec.symbol_nodes[typ]
                _record_symbol(ctx, qname, graph_type, node)
                if graph_type == "class":
                    _extract_inheritance(ctx, node, qname)
                    ctx.class_stack.append(name)  # methods use the class name
                previous = ctx.current_qname
                ctx.current_qname = qname
                for child in node.children:
                    _walk(child, ctx)
                ctx.current_qname = previous
                if graph_type == "class":
                    ctx.class_stack.pop()
                return

    if spec.is_ts_family() and typ == "variable_declarator":
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name_node is not None and value is not None and name_node.type == "identifier":
            if value.type in ("arrow_function", "function_expression"):
                # Module-level function-consts are indexed; closures inside
                # functions are not (their calls still are, below).
                if ctx.current_qname is None:
                    qname = _qualified(ctx, _node_text(name_node))
                    _record_symbol(ctx, qname, "function", node)
                    previous = ctx.current_qname
                    ctx.current_qname = qname
                    for child in node.children:
                        _walk(child, ctx)
                    ctx.current_qname = previous
                    return
            elif ctx.current_qname is None:  # plain module-level const
                _record_symbol(ctx, _qualified(ctx, _node_text(name_node)), "symbol", node)
        for child in node.children:
            _walk(child, ctx)
        return

    if spec.is_java() and typ in ("method_invocation", "object_creation_expression"):
        # java: callee splits across name + object fields (method_invocation)
        # or sits in the type field (object_creation_expression = `new X(...)`).
        if typ == "method_invocation":
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node) if name_node is not None else ""
            if name:
                obj = node.child_by_field_name("object")
                obj_chain = _chain_text(obj) if obj is not None else None
                _record_call(ctx, f"{obj_chain}.{name}" if obj_chain else name)
        else:
            type_node = node.child_by_field_name("type")
            chain = _chain_text(type_node) if type_node is not None else None
            if chain:
                _record_call(ctx, chain)
        for child in node.children:  # arguments may contain nested calls
            _walk(child, ctx)
        return

    if typ == spec.call_node or (spec.is_ts_family() and typ == "new_expression") \
            or (spec.is_csharp() and typ == "object_creation_expression"):
        # `new X(...)` is a constructor call — same edge shape as a call.
        if typ in ("new_expression", "object_creation_expression"):
            callee = node.child_by_field_name("constructor")
            if callee is None:  # csharp grammar: the type sits in the type field
                callee = node.child_by_field_name("type")
        else:
            callee = node.child_by_field_name(spec.call_callee_field)
        chain = _chain_text(callee)
        if _is_require_call(spec, node):
            _parse_require_import(ctx, node)
        elif chain:
            _record_call(ctx, chain)
        for child in node.children:  # arguments may contain nested calls
            _walk(child, ctx)
        return

    if typ in spec.import_nodes:
        if spec.is_python():
            _parse_imports_python(ctx, node)
        elif spec.is_ts_family():
            _parse_imports_ts(ctx, node)
        elif spec.is_java():
            _parse_imports_java(ctx, node)
        elif spec.is_csharp():
            _parse_imports_csharp(ctx, node)
        return

    for child in node.children:
        _walk(child, ctx)
