"""Deterministic reports: graph_report.md + the compact in-context repo map.

Neither ever contains file contents — nodes/edges with names and line
numbers only. The compact map is the "look up the graph first" payload
(<= ~300 tokens) embedded in git_repo results; the full report stays on
disk and is only pointed to.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .model import RepoGraph, parse_node_id

REPORT_FILE = "graph_report.md"

# The compact map is bounded by chars (roughly 4 chars/token for code).
COMPACT_MAP_MAX_CHARS = 1200


def god_nodes(graph: RepoGraph, top: int = 8) -> list[tuple[str, int]]:
    """Most-connected symbols by total degree (forward + reverse)."""
    fwd, rev = graph.adjacency()
    degrees: dict[str, int] = {}
    for node_id in fwd:
        degrees[node_id] = degrees.get(node_id, 0) + len(fwd[node_id])
    for node_id in rev:
        degrees[node_id] = degrees.get(node_id, 0) + len(rev[node_id])
    ranked = sorted(
        (degrees.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    return [(node_id, degree) for node_id, degree in ranked[:top]]


def compact_map(graph: RepoGraph, max_chars: int = COMPACT_MAP_MAX_CHARS) -> str:
    """One-block map of the repo — modules, hubs, and a usage hint."""
    stats = graph.stats
    langs = "+".join(sorted(graph.repo.langs)) or "?"
    hubs = god_nodes(graph, top=5)
    hub_text = ", ".join(
        f"{parse_node_id(nid)[2] or nid}({deg})" for nid, deg in hubs
    )

    # One line per file with the most symbols; cap at 12 files.
    symbol_counts: dict[str, list[str]] = {}
    for node in graph.nodes:
        if node.type == "file":
            continue
        kind, rel, qname = parse_node_id(node.id)
        symbol_counts.setdefault(rel, []).append(qname or node.name)
    modules = sorted(symbol_counts.items(), key=lambda item: len(item[1]), reverse=True)
    mod_lines = []
    for rel, names in modules[:12]:
        shown = ", ".join(names[:4])
        if len(names) > 4:
            shown += f",+{len(names) - 4}"
        mod_lines.append(f"{rel}({shown})")

    failed = stats.get("failed", 0)
    lines = [
        f"repo={graph.repo.name} | langs={langs} | "
        f"files={stats.get('files', 0)} sym={stats.get('nodes', 0)} "
        f"edges={stats.get('edges', 0)} parsed={stats.get('parsed', 0)}"
        + (f" failed={failed}" if failed else "") + " ok",
        f"mods: {', '.join(mod_lines) if mod_lines else '(no symbols)'}",
        f"hubs: {hub_text or '(none)'}",
        "hint: consult the repo_graph tool (query/neighbors/impact/path) before "
        "reading files; full report: .tmp/repo_graph/<repo>/graph_report.md",
    ]
    cov_line = _coverage_line(stats)
    # Inserted right after the header so the 1200-char cap truncates `mods`
    # first and the hint line always survives.
    if cov_line:
        lines.insert(1, cov_line)
    text = "\n".join(lines)
    return text[:max_chars]


def render_report(graph: RepoGraph) -> str:
    """graphify-style graph_report.md — stats, modules, god nodes, usage."""
    stats = graph.stats
    built = datetime.fromtimestamp(graph.repo.built_at, tz=timezone.utc).isoformat()
    lines = [
        "# Repository Graph Report",
        "",
        f"- repo: `{graph.repo.name}`",
        f"- head: `{graph.repo.head_sha or '(not a git repo)'}`",
        f"- langs: {', '.join(sorted(graph.repo.langs)) or '(none)'}",
        f"- files: {stats.get('files', 0)} | symbols: {stats.get('nodes', 0)} "
        f"| edges: {stats.get('edges', 0)}",
        f"- parsed: {stats.get('parsed', 0)} (reused from cache: {stats.get('reused', 0)})"
        f" | failed: {stats.get('failed', 0)} | skipped: {stats.get('skipped', 0)}",
        *_coverage_lines(stats),
        f"- unresolved calls: {stats.get('unresolved_calls', 0)} | build: {stats.get('build_ms', 0)} ms",
        f"- built at: {built}",
        "",
    ]
    failed_reasons = stats.get("failed_reasons") or {}
    if failed_reasons:
        lines += ["## Failed files", ""]
        for reason, count in sorted(failed_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {reason}: {count}")
    lines += ["", "## God nodes (most connected)", ""]
    for node_id, degree in god_nodes(graph):
        kind, rel, qname = parse_node_id(node_id)
        label = qname if qname else rel
        node_type = (graph.node(node_id).type if graph.node(node_id) else kind)
        lines.append(f"- `{label}` — degree {degree} ({node_type})")
    lines += ["", "## Modules"]
    lines += _module_section(graph)
    lines += ["", "## Usage"]
    lines += [
        "",
        "- `repo_graph query` — search symbols/files by name",
        "- `repo_graph neighbors` — symbols connected to one",
        "- `repo_graph impact` — what depends on a symbol (refactor blast radius)",
        "- `repo_graph path` — shortest call path between two symbols",
        "- `repo_graph build` — rebuild after edits",
        "",
        "The graph is deterministic (tree-sitter AST, no LLM); edges may miss "
        "indirect or dynamic references — it is a navigation aid, not a spec.",
    ]
    return "\n".join(lines)


def _module_section(graph: RepoGraph) -> list[str]:
    """Per-file symbol listing, files sorted by symbol count."""
    symbol_counts: dict[str, list[str]] = {}
    for node in graph.nodes:
        if node.type == "file":
            continue
        kind, rel, qname = parse_node_id(node.id)
        symbol_counts.setdefault(rel, []).append(qname or node.name)
    lines = []
    for rel in sorted(symbol_counts, key=lambda r: -len(symbol_counts[r])):
        names = ", ".join(symbol_counts[rel])
        lines.append(f"- `{rel}` — {len(symbol_counts[rel])} symbols: {names}")
    if not lines:
        lines.append("- (no symbols)")
    return lines


def _lang_cov_sorted(stats: dict[str, Any]) -> list[tuple[str, dict[str, int]]]:
    """Per-language coverage, worst ratio first (problem languages surface)."""
    lang_cov = stats.get("lang_coverage") or {}
    return sorted(
        lang_cov.items(),
        key=lambda kv: kv[1].get("with_symbols", 0) / max(kv[1].get("files", 1), 1),
    )


def _coverage_line(stats: dict[str, Any]) -> str | None:
    """One-line per-language coverage for the compact map (worst 6 langs)."""
    parts = []
    for lang, cov in _lang_cov_sorted(stats)[:6]:
        n = cov.get("files", 0)
        if not n:
            continue
        s = cov.get("with_symbols", 0)
        pct = round(100 * s / n)
        parts.append(f"{lang} {s}/{n}" + (f" ({pct}%)" if s < n else ""))
    return f"cov: {' '.join(parts)}" if parts else None


def _coverage_lines(stats: dict[str, Any]) -> list[str]:
    """Parse-rate lines for graph_report.md: overall + per language + untracked."""
    files = stats.get("files", 0)
    with_symbols = stats.get("with_symbols", 0)
    lines = []
    if files:
        pct = round(100 * with_symbols / files)
        lines.append(f"- parse rate: {with_symbols}/{files} files with symbols ({pct}%)")
    parts = []
    for lang, cov in _lang_cov_sorted(stats):
        n = cov.get("files", 0)
        if not n:
            continue
        s = cov.get("with_symbols", 0)
        pct = round(100 * s / n)
        parts.append(f"{lang} {s}/{n}" + (f" ({pct}%)" if s < n else ""))
    if parts:
        lines.append(f"- by language: {', '.join(parts)}")
    untracked = stats.get("untracked_sources", 0)
    if untracked:
        lines.append(f"- untracked source files (not indexed): {untracked}")
    return lines
