"""Single-file tree-sitter parsing -> per-file parse results.

One generic recursive walker driven by ``LanguageSpec``: no .scm query files,
so nothing depends on the tree-sitter query API. tree-sitter is
error-tolerant by design (syntax errors produce ERROR nodes, the parse still
succeeds), so a broken file yields whatever symbols are recoverable and is
never fatal.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .languages import LanguageSpec, detect_language, get_grammar, language_spec

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
        if path.stat().st_size > 2 * 1024 * 1024:  # noqa: PLR2004 (2MB guard)
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return parse_bytes(data, rel_path, lang)


def parse_bytes(data: bytes, rel_path: str, lang: str) -> FileParseResult:
    """Pure parse entry point (also used by tests)."""
    started = time.perf_counter()
    result = FileParseResult(
        sha256=hashlib.sha256(data).hexdigest(),
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
    rb"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?"
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
_SQL_WRITE_RES = (
    re.compile(rb"\bINSERT\s+(?:INTO\s+)?(" + _SQL_IDENT + rb")", re.IGNORECASE),
    re.compile(rb"\bUPDATE\s+(?:TOP\s*\([^)]*\)\s+)?(" + _SQL_IDENT + rb")", re.IGNORECASE),
    re.compile(rb"\bDELETE\s+(?:FROM\s+)?(" + _SQL_IDENT + rb")", re.IGNORECASE),
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


def _sql_sanitize(data: bytes) -> bytes:
    """Blank comments and string literals in place, preserving length.

    Newlines survive so line numbers computed on the result match the
    original file, and a CREATE TABLE inside a comment or string provably
    never becomes a node. Double quotes are left alone — in T-SQL they are
    usually identifiers, rarely literals in DDL.
    """
    out = bytearray(data)
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x2D and i + 1 < n and data[i + 1] == 0x2D:  # -- line comment
            j = data.find(b"\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = 0x20
            i = j
        elif c == 0x2F and i + 1 < n and data[i + 1] == 0x2A:  # /* block */
            close = data.find(b"*/", i + 2)
            end = n if close == -1 else close + 2
            for k in range(i, end):
                if out[k] != 0x0A:
                    out[k] = 0x20
            i = end
        elif c == 0x27:  # 'literal' with '' escape
            j = i + 1
            while j < n:
                if data[j] == 0x27:
                    if j + 1 < n and data[j + 1] == 0x27:
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            for k in range(i, end):
                if out[k] != 0x0A:
                    out[k] = 0x20
            i = end
        else:
            i += 1
    return bytes(out)


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


def _sql_valid_table(name: str) -> bool:
    if not name or name.startswith("#"):  # temp tables are session-local
        return False
    lowered = name.lower()
    if lowered in _SQL_PSEUDO_TABLES:
        return False
    if lowered in _SQL_NOT_TABLE or lowered.rsplit(".", 1)[-1] in _SQL_NOT_TABLE:
        return False
    return True


def _sql_split_columns(text: bytes, start: int, end: int) -> list[tuple[str, int, bytes]]:
    """Column name, offset and text for each top-level part of a table body.

    The paren search stops at the first statement keyword or `;` after the
    table name so `CREATE TABLE x AS SELECT ...` (no column body) can never
    mistake a following statement's argument list for its own.
    """
    stop = _SQL_BODY_STOP_RE.search(text, start, end)
    search_end = stop.start() if stop is not None else end
    open_at = text.find(b"(", start, search_end)
    if open_at == -1:
        return []
    depth = 0
    body_end = end
    for i in range(open_at, end):
        if text[i] == 0x28:
            depth += 1
        elif text[i] == 0x29:
            depth -= 1
            if depth == 0:
                body_end = i
                break
    # Split strictly *inside* the parens: starting the scan at the opening
    # paren itself would leave depth at 1 for every comma, collapsing the
    # whole body into one part (only the first column would ever be seen).
    columns: list[tuple[str, int, bytes]] = []
    depth = 0
    part_start = open_at + 1
    for i in range(open_at + 1, body_end):
        c = text[i]
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
        elif c == 0x2C and depth == 0:
            columns.append((part_start, i))
            part_start = i + 1
    columns.append((part_start, body_end))
    out = []
    for col_start, col_end in columns:
        part = text[col_start:col_end]
        m = _re_ident_start(part)
        if m is None:
            continue
        first = _sql_norm_name(m.group(1)).lower()
        if first in _SQL_CONSTRAINT_LEAD:
            continue  # table-level constraint, handled by the global PK/FK pass
        out.append((_sql_norm_name(m.group(1)), col_start, part))
    return out


def _re_ident_start(part: bytes):
    return re.match(rb"^\s*(" + _SQL_IDENT_ONLY + rb")", part)


def _parse_sql(data: bytes, rel_path: str, result: FileParseResult) -> None:
    text = _sql_sanitize(data)

    def line_of(offset: int) -> int:
        return text.count(b"\n", 0, offset) + 1

    sym_seen: set[str] = set()
    edge_seen: set[tuple[str, str | None, str]] = set()

    def emit_symbol(name: str, graph_type: str, line: int) -> None:
        if not name or name in sym_seen:
            return
        sym_seen.add(name)
        result.symbols.append({"name": name, "type": graph_type, "line": line})

    def emit_edge(etype: str, src: str | None, target: str) -> None:
        if not _sql_valid_table(target):
            return
        key = (etype, src, target)
        if key in edge_seen:
            return
        edge_seen.add(key)
        result.edges.append({
            "type": etype,
            "target": target,
            "target_kind": "sql",
            "from": src,
        })

    # Declarations own [start, next start) — one ownership rule for all DML.
    decls: list[dict[str, Any]] = []
    for m in _SQL_DECL_RE.finditer(text):
        kind = _SQL_DECL_KIND[m.group(1).upper().decode()]
        qname = _sql_norm_name(m.group(2))
        if not qname or qname.startswith("#"):
            continue
        decls.append({"offset": m.start(), "end": m.end(), "kind": kind,
                      "qname": qname, "line": line_of(m.start())})

    # (region_start, region_end, owning declaration | None). Text before the
    # first declaration and DML in table regions has no owning symbol — src
    # stays None so the builder attaches it to the file node.
    regions: list[tuple[int, int, dict[str, Any] | None]] = []
    prev_end = 0
    for i, decl in enumerate(decls):
        region_end = decls[i + 1]["offset"] if i + 1 < len(decls) else len(text)
        if decl["offset"] > prev_end:
            regions.append((prev_end, decl["offset"], None))
        regions.append((decl["offset"], region_end, decl))
        prev_end = region_end
    if prev_end < len(text):
        regions.append((prev_end, len(text), None))

    for start, region_end, decl in regions:
        if region_end <= start:
            continue
        src: str | None = None
        if decl is not None:
            src = decl["qname"] if decl["kind"] in (
                "procedure", "function", "view", "trigger"
            ) else None
            emit_symbol(decl["qname"], decl["kind"], decl["line"])
            if decl["kind"] == "table":
                for col, col_off, part in _sql_split_columns(text, decl["end"], region_end):
                    emit_symbol(f'{decl["qname"]}.{col}', "column", line_of(col_off))
                    if _SQL_PK_INLINE_RE.search(part):
                        emit_edge("primary_key", f'{decl["qname"]}.{col}', decl["qname"])
            if decl["kind"] == "trigger":
                # the header ends at the first AS: `CREATE TRIGGER t ON x AFTER
                # INSERT AS ...` — later ON clauses are joins, not the target.
                header = text[decl["end"]:region_end]
                as_at = _SQL_AS_RE.search(header)
                on_target = _SQL_TRIGGER_ON_RE.search(
                    header[:as_at.start()] if as_at else header[:400]
                )
                if on_target:
                    emit_edge("reads", decl["qname"], _sql_norm_name(on_target.group(1)))

        region = text[start:region_end]
        # writes first, so `DELETE FROM x`'s FROM never double-counts as a read
        spans: list[tuple[int, int]] = []
        for rx in _SQL_WRITE_RES:
            for m in rx.finditer(region):
                emit_edge("writes", src, _sql_norm_name(m.group(1)))
                spans.append((m.start(), m.end()))
        for m in _SQL_READ_RE.finditer(region):
            if any(s <= m.start() and m.end() <= e for s, e in spans):
                continue
            emit_edge("reads", src, _sql_norm_name(m.group(1)))
        for m in _SQL_EXEC_RE.finditer(region):
            target = _sql_norm_name(m.group(1))
            if not target.startswith("@"):
                emit_edge("calls", src, target)
        for m in _SQL_QUAL_CALL_RE.finditer(region):
            if any(s <= m.start() and m.end() <= e for s, e in spans):
                continue
            before = region[max(0, m.start() - 24):m.start()]
            words = re.findall(rb"[A-Za-z_]+", before)
            if words and words[-1].decode("ascii", "ignore").lower() in _SQL_QUAL_CALL_NO_PRECEDE:
                continue
            emit_edge("calls", src, _sql_norm_name(m.group(1)))

    # Table-level PK/FK (also covers inline REFERENCES): attributed to the
    # closest preceding CREATE TABLE, unless an ALTER TABLE sits after it and
    # within reach — then the ALTER names the table being constrained.
    def attr_table(offset: int) -> str | None:
        create = None
        for decl in decls:
            if decl["kind"] == "table" and decl["offset"] < offset:
                create = decl
            else:
                break
        window_start = max(0, offset - 200)
        alter = None
        for m in _SQL_ALTER_RE.finditer(text[window_start:offset]):
            alter = m  # last one wins
        if alter is not None:
            alter_abs = window_start + alter.start()
            if create is None or alter_abs > create["offset"]:
                return _sql_norm_name(alter.group(1))
        return create["qname"] if create else None

    for m in _SQL_PK_RE.finditer(text):
        table = attr_table(m.start())
        if table is None:
            continue
        for raw_col in m.group(1).split(b","):
            col = _sql_norm_name(raw_col)
            if col:
                emit_edge("primary_key", f"{table}.{col}", table)
    for rx in (_SQL_FK_RE, _SQL_REF_RE):
        for m in rx.finditer(text):
            table = attr_table(m.start())
            if table is None:
                continue
            emit_edge("foreign_key", table, _sql_norm_name(m.group(1)))


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
