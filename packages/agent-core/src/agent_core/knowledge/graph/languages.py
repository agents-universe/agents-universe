"""Language detection and per-language tree-sitter node-type maps.

Grammar objects load lazily from ``tree_sitter_language_pack`` (bundles the
python/typescript/javascript/tsx/jsx/vue grammars) and are cached — the pack
is only imported on first build/query, keeping plain ``import agent_core``
light. No .scm query files: the parser walks the AST generically from these
tables, so the tree-sitter query API (which churns across versions) never
comes into play.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger(__name__)

SUPPORTED_EXT: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".vue": "vue",
}

# Committed vendor/build dirs are still skipped even when git ls-files lists
# them — they are noise, not architecture.
EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "vendor", "dist", "build", "out", "coverage",
    "__pycache__", ".venv", "venv", ".next", ".nuxt", ".tox",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
})

# Skip giant / minified / generated files outright.
MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class LanguageSpec:
    key: str
    symbol_nodes: dict[str, str]        # tree-sitter node type -> graph node type
    call_node: str
    call_callee_field: str
    import_nodes: tuple[str, ...]
    inherited_field: str | None         # python: class field with superclasses
    name_field: str = "name"
    builtins: frozenset[str] = field(default_factory=frozenset)

    def is_python(self) -> bool:
        return self.key == "python"

    def is_ts_family(self) -> bool:
        return self.key in ("typescript", "tsx", "javascript", "jsx")


_PY_BUILTINS = frozenset({
    "print", "len", "range", "str", "int", "float", "bool", "list", "dict",
    "set", "tuple", "type", "isinstance", "issubclass", "enumerate", "zip",
    "map", "filter", "sorted", "sum", "min", "max", "abs", "round", "open",
    "input", "repr", "id", "getattr", "setattr", "hasattr", "super", "dir",
    "vars", "next", "iter", "any", "all", "format", "bytes", "bytearray",
    "object", "classmethod", "staticmethod", "property", "Exception",
    "ValueError", "TypeError", "KeyError", "NotImplementedError", "hash",
    "callable", "reversed", "ord", "chr", "divmod", "pow", "oct", "hex",
})

_JS_BUILTINS = frozenset({
    "console", "Math", "JSON", "Object", "Array", "Number", "String",
    "Boolean", "Promise", "parseInt", "parseFloat", "isNaN", "isFinite",
    "Date", "RegExp", "Error", "TypeError", "Set", "Map", "Symbol",
    "globalThis", "window", "document", "setTimeout", "setInterval",
    "clearTimeout", "clearInterval", "fetch", "alert", "prompt", "confirm",
    "require", "export", "decodeURIComponent", "encodeURIComponent",
})

_SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        key="python",
        symbol_nodes={
            "class_definition": "class",
            "function_definition": "function",
            "async_function_definition": "function",
        },
        call_node="call",
        call_callee_field="function",
        import_nodes=("import_statement", "import_from_statement"),
        inherited_field="superclasses",
        builtins=_PY_BUILTINS,
    ),
    "typescript": LanguageSpec(
        key="typescript",
        symbol_nodes={
            "class_declaration": "class",
            "interface_declaration": "class",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "method_definition": "function",
        },
        call_node="call_expression",
        call_callee_field="function",
        import_nodes=("import_statement",),
        inherited_field=None,
        builtins=_JS_BUILTINS,
    ),
    "tsx": LanguageSpec(
        key="tsx",
        symbol_nodes={
            "class_declaration": "class",
            "interface_declaration": "class",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "method_definition": "function",
        },
        call_node="call_expression",
        call_callee_field="function",
        import_nodes=("import_statement",),
        inherited_field=None,
        builtins=_JS_BUILTINS,
    ),
    "javascript": LanguageSpec(
        key="javascript",
        symbol_nodes={
            "class_declaration": "class",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "method_definition": "function",
        },
        call_node="call_expression",
        call_callee_field="function",
        import_nodes=("import_statement",),
        inherited_field=None,
        builtins=_JS_BUILTINS,
    ),
    "jsx": LanguageSpec(
        key="jsx",
        symbol_nodes={
            "class_declaration": "class",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "method_definition": "function",
        },
        call_node="call_expression",
        call_callee_field="function",
        import_nodes=("import_statement",),
        inherited_field=None,
        builtins=_JS_BUILTINS,
    ),
    "vue": LanguageSpec(
        key="vue",
        symbol_nodes={},
        call_node="",
        call_callee_field="",
        import_nodes=(),
        inherited_field=None,
        builtins=frozenset(),
    ),
}


def detect_language(rel_path: str) -> str | None:
    """Map a repo-relative path to a grammar key; None for unsupported files."""
    rel = rel_path.replace("\\", "/")
    lowered = rel.lower()
    if lowered.endswith(".d.ts") or lowered.endswith(".min.js"):
        return None
    return SUPPORTED_EXT.get(Path(lowered).suffix)


def language_spec(key: str) -> LanguageSpec:
    return _SPECS[key]


def _load_grammar(key: str):
    """Lazily load a tree-sitter grammar from the language pack; None on failure."""
    try:
        from tree_sitter_language_pack import get_language
        return get_language(key)
    except Exception as exc:  # grammar not in pack / import broken
        _log.warning("tree-sitter grammar %r unavailable: %s", key, exc)
        return None


_GRAMMAR_FALLBACK = {
    "jsx": "javascript",  # tree-sitter-javascript parses JSX natively; the
    "tsx": "typescript",  # pack ships no separate jsx grammar.
}


@lru_cache(maxsize=None)
def get_grammar(key: str):
    """Cached grammar lookup — the pack import happens once per key."""
    grammar = _load_grammar(key)
    if grammar is None:
        fallback = _GRAMMAR_FALLBACK.get(key)
        if fallback:
            grammar = _load_grammar(fallback)
    return grammar
