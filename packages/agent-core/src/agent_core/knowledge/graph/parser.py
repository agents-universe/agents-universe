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
    __slots__ = ("spec", "lang", "result", "class_stack", "current_qname")

    def __init__(self, spec: LanguageSpec, lang: str, result: FileParseResult) -> None:
        self.spec = spec
        self.lang = lang
        self.result = result
        self.class_stack: list[str] = []
        self.current_qname: str | None = None


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
    ctx.result.edges.append({
        "type": "inherits",
        "target": cleaned,
        "target_kind": "inherits",
        "from": class_qname,
    })


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
        # java: `superclass` field wraps a type_identifier; the implements
        # clause lives in the `interfaces` field as a type_list.
        superclass = class_node.child_by_field_name("superclass")
        if superclass is not None:
            # the `superclass` field wraps a type_identifier (the base class)
            type_node = next(
                (c for c in superclass.children if c.type == "type_identifier"), None
            )
            chain = _chain_text(type_node) if type_node is not None else None
            if chain:
                _record_inherits(ctx, chain, class_qname)
        interfaces = class_node.child_by_field_name("interfaces")
        if interfaces is not None:
            for child in interfaces.children:
                if child.type == "type_list":
                    for type_node in child.children:
                        chain = _chain_text(type_node)
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


def _parse_imports_python(ctx: _Ctx, node) -> None:
    imports = ctx.result.imports
    if node.type == "import_statement":
        name_field = node.child_by_field_name("name")
        for item in _flatten_items(name_field):
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
    name_field = node.child_by_field_name("name")
    for item in _flatten_items(name_field):
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

    if typ in spec.symbol_nodes and spec.symbol_nodes[typ] in ("class", "function"):
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

    if typ == spec.call_node or (spec.is_ts_family() and typ == "new_expression"):
        # `new X(...)` is a constructor call — same edge shape as a call.
        if typ == "new_expression":
            callee = node.child_by_field_name("constructor")
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
        return

    for child in node.children:
        _walk(child, ctx)
