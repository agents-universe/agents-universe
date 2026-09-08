"""compact_map char-cap behavior: the hint/hubs lines must survive trimming.

The map exists to tell the agent "consult the graph first"; a plain
text[:max_chars] cut dropped both tail lines on a repo with many modules.
"""
from __future__ import annotations

from agent_core.knowledge.graph.model import GraphNode, RepoGraph, RepoMeta
from agent_core.knowledge.graph.report import compact_map


def _big_graph(modules: int = 12) -> RepoGraph:
    nodes = []
    for i in range(modules):
        rel = f"packages/service-{i}/src/deeply/nested/module_{i}/implementation_file_{i}.ts"
        for j in range(6):
            nodes.append(GraphNode(id=f"s:{rel}:sym_{j}", type="function", name=f"sym_{j}"))
    return RepoGraph(
        repo=RepoMeta(name="big", head_sha="abc", langs=["typescript"], built_at=0.0),
        nodes=nodes,
    )


def test_hint_survives_long_module_list():
    out = compact_map(_big_graph())
    assert len(out) <= 1200
    assert "hint:" in out
    assert "hubs:" in out
    assert out.startswith("repo=big |")


def test_mods_line_is_what_gets_trimmed():
    out = compact_map(_big_graph())
    mods_line = out.split("\n")[1]
    assert mods_line.startswith("mods: ")
    # Whole entries only — never a half-written module path.
    assert not mods_line.rstrip().endswith(",")


def test_small_graph_keeps_all_sections():
    graph = RepoGraph(
        repo=RepoMeta(name="tiny", head_sha="abc", langs=["python"], built_at=0.0),
        nodes=[GraphNode(id="s:a.py:run", type="function", name="run")],
    )
    out = compact_map(graph)
    assert "mods: a.py(run)" in out
    assert "hubs: (none)" in out  # no edges -> no degrees
    assert out.endswith("graph_report.md")


def test_no_symbols_still_labels_mods():
    graph = RepoGraph(
        repo=RepoMeta(name="empty", head_sha="", langs=[], built_at=0.0),
    )
    out = compact_map(graph)
    assert "mods: (no symbols)" in out
    assert "hint:" in out
