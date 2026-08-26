"""Builder/store/report/queries tests for the repo knowledge graph.

A real local git repo (bare remote pattern, no network) seeds py+ts files
with cross-file calls, inheritance, a broken file, an excluded vendor dir,
and a non-code notes.md. Content assertions require the tree-sitter
grammars; incremental/cache tests monkeypatch parse_file and never touch
grammars.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.knowledge.graph.builder import build_repo_graph
from agent_core.knowledge.graph import queries
from agent_core.knowledge.graph.languages import get_grammar
from agent_core.knowledge.graph.report import compact_map, render_report
from agent_core.knowledge.graph.store import load_cached

FILES = {
    "main.py": (
        "import os\n"
        "from util.helper import parse_json\n"
        "import util\n"
        "\n"
        "def formatIt(x):\n"
        "    return x\n"
        "\n"
        "class MainApp:\n"
        "    def start(self):\n"
        "        data = parse_json('{}')\n"
        "        return self._go()\n"
        "    def _go(self):\n"
        "        return util.version()\n"
        "\n"
        "def main():\n"
        "    app = MainApp()\n"
        "    app.start()\n"
        "    print(os.getcwd())\n"
    ),
    "util/__init__.py": (
        "from .helper import parse_json\n"
        "\n"
        "def version():\n"
        "    return '1.0'\n"
    ),
    "util/helper.py": (
        "import json\n"
        "def parse_json(text):\n"
        "    return json.loads(text)\n"
    ),
    "app.ts": (
        "import { formatIt } from './lib/util';\n"
        "import * as U from './lib/util';\n"
        "import './side-effect';\n"
        "\n"
        "export function run(): void {\n"
        "  const g = new Greeter('hi');\n"
        "  g.greet();\n"
        "  formatIt('x');\n"
        "  U.helper();\n"
        "}\n"
    ),
    "lib/util.ts": (
        "export interface Iface { name: string }\n"
        "export class Greeter {\n"
        "  constructor(public name: string) {}\n"
        "  greet(): string { return this.name; }\n"
        "}\n"
        "export function formatIt(s: string): string { return s; }\n"
        "export function helper(): void {}\n"
    ),
    "lib/side-effect.ts": "export const loaded = true;\n",
    "broken.py": "{{{ this is not python at all\n",
    "vendor/x.js": "var noisy = 1;\n",
    "notes.md": "# notes\nnot code\n",
    "src/main/java/com/example/app/MainApp.java": (
        "package com.example.app;\n"
        "import com.example.lib.Greeter;\n"
        "import static com.example.lib.Util.helper;\n"
        "public class MainApp extends BaseApp implements Service {\n"
        "    public static void main(String[] args) {\n"
        "        Greeter g = new Greeter(\"hi\");\n"
        "        g.greet();\n"
        "        helper();\n"
        "    }\n"
        "    void start() { this._go(); }\n"
        "    void _go() {}\n"
        "}\n"
        "class BaseApp {}\n"
        "interface Service {}\n"
    ),
    "src/main/java/com/example/lib/Greeter.java": (
        "package com.example.lib;\n"
        "public class Greeter {\n"
        "    public String greet() { return \"hi\"; }\n"
        "}\n"
    ),
    "src/main/java/com/example/lib/Util.java": (
        "package com.example.lib;\n"
        "public class Util {\n"
        "    public static void helper() {}\n"
        "}\n"
    ),
}


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A local git repo with the fixture files committed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run("init", "-b", "main", cwd=repo)
    _run("config", "user.email", "t@t.t", cwd=repo)
    _run("config", "user.name", "t", cwd=repo)
    _run("add", ".", cwd=repo)
    _run("commit", "-m", "seed", cwd=repo)
    return repo


@pytest.fixture
def grammars():
    if get_grammar("python") is None or get_grammar("typescript") is None:
        pytest.skip("tree-sitter grammars unavailable (offline?)")
    return True


async def _build(repo: Path, tmp_path: Path, **kw):
    kg_dir = tmp_path / "kg"
    return await build_repo_graph(repo, kg_dir, **kw)


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_content(repo: Path, tmp_path: Path, grammars):
    summary = await _build(repo, tmp_path)
    assert summary["status"] == "built"
    stats = summary["stats"]
    assert stats["files"] == 10
    assert stats["failed"] == 1          # broken.py
    assert stats["skipped"] == 0
    assert stats["unresolved_calls"] == 2  # os.getcwd, json.loads

    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    nodes = {n.id: n for n in graph.nodes}
    # file nodes: vendor/ and notes.md never indexed
    assert "f:vendor/x.js" not in nodes
    assert "f:notes.md" not in nodes
    assert "f:main.py" in nodes and nodes["f:main.py"].lang == "python"
    assert "f:app.ts" in nodes and nodes["f:app.ts"].lang == "typescript"
    # java file nodes + symbols
    java_app = "f:src/main/java/com/example/app/MainApp.java"
    assert java_app in nodes and nodes[java_app].lang == "java"
    assert nodes["s:src/main/java/com/example/app/MainApp.java:MainApp"].type == "class"
    assert nodes["s:src/main/java/com/example/app/MainApp.java:MainApp.main"].type == "function"
    # symbols with line numbers
    assert nodes["s:util/helper.py:parse_json"].line == 2
    assert nodes["s:lib/util.ts:Greeter"].type == "class"
    assert nodes["s:lib/util.ts:Greeter.greet"].type == "function"
    # broken.py parsed but yielded nothing -> failed, no symbol node
    assert all("broken.py" not in n.id for n in graph.nodes if n.type != "file")

    edges = {(e.src, e.dst, e.type) for e in graph.edges}
    # cross-file call through from-import
    assert ("s:main.py:MainApp.start", "s:util/helper.py:parse_json", "calls") in edges
    # call through a local variable resolves via suffix
    assert ("s:main.py:main", "s:main.py:MainApp.start", "calls") in edges
    assert ("s:app.ts:run", "s:lib/util.ts:Greeter.greet", "calls") in edges
    # namespace import alias
    assert ("s:app.ts:run", "s:lib/util.ts:helper", "calls") in edges
    # constructor call
    assert ("s:app.ts:run", "s:lib/util.ts:Greeter", "calls") in edges
    # import edges file -> file
    assert ("f:main.py", "f:util/helper.py", "imports") in edges
    assert ("f:app.ts", "f:lib/util.ts", "imports") in edges
    # java: cross-file call via instance + static import + constructor
    mj = "s:src/main/java/com/example/app/MainApp.java"
    gj = "s:src/main/java/com/example/lib/Greeter.java"
    uj = "s:src/main/java/com/example/lib/Util.java"
    assert (f"{mj}:MainApp.main", f"{gj}:Greeter.greet", "calls") in edges
    assert (f"{mj}:MainApp.main", f"{gj}:Greeter", "calls") in edges
    assert (f"{mj}:MainApp.main", f"{uj}:Util.helper", "calls") in edges
    assert (f"{mj}:MainApp.start", f"{mj}:MainApp._go", "calls") in edges
    # java: inheritance + imports
    assert (f"{mj}:MainApp", f"{mj}:BaseApp", "inherits") in edges
    assert (f"{mj}:MainApp", f"{mj}:Service", "inherits") in edges
    assert (java_app, "f:src/main/java/com/example/lib/Greeter.java", "imports") in edges
    assert (java_app, "f:src/main/java/com/example/lib/Util.java", "imports") in edges

    assert len(compact_map(graph)) <= 1200
    assert "hint:" in compact_map(graph)


@pytest.mark.asyncio
async def test_up_to_date_fast_path(repo: Path, tmp_path: Path):
    first = await _build(repo, tmp_path)
    assert first["status"] == "built"
    with patch("agent_core.knowledge.graph.builder.parse_file") as parse:
        second = await _build(repo, tmp_path)
    assert second["status"] == "up_to_date"
    parse.assert_not_called()  # fast path: zero hashing, zero parsing


@pytest.mark.asyncio
async def test_incremental_reparse_only_changed(repo: Path, tmp_path: Path):
    await _build(repo, tmp_path)
    (repo / "util" / "helper.py").write_text(
        (repo / "util" / "helper.py").read_text(encoding="utf-8")
        + "\ndef extra():\n    return 1\n",
        encoding="utf-8",
    )
    from agent_core.knowledge.graph import parser as _parser_mod
    with patch("agent_core.knowledge.graph.builder.parse_file",
               wraps=_parser_mod.parse_file) as counted:
        summary = await _build(repo, tmp_path, force=True)
        assert summary["status"] == "built"
        assert summary["stats"]["parsed"] == 1
        assert summary["stats"]["reused"] == 9
        assert counted.call_count == 1  # only the edited file is re-parsed

    graph = load_cached(tmp_path / "kg")
    assert graph.node("s:util/helper.py:extra") is not None


@pytest.mark.asyncio
async def test_deleted_file_drops_from_graph(repo: Path, tmp_path: Path):
    await _build(repo, tmp_path)
    _run("rm", "util/helper.py", cwd=repo)
    _run("commit", "-am", "drop helper", cwd=repo)
    summary = await _build(repo, tmp_path, force=True)
    assert summary["stats"]["files"] == 9
    graph = load_cached(tmp_path / "kg")
    assert graph.node("s:util/helper.py:parse_json") is None
    assert graph.node("f:util/helper.py") is None


@pytest.mark.asyncio
async def test_report_renders(repo: Path, tmp_path: Path, grammars):
    await _build(repo, tmp_path)
    graph = load_cached(tmp_path / "kg")
    report = render_report(graph)
    assert "# Repository Graph Report" in report
    assert "God nodes" in report
    assert "repo_graph" in report  # usage section


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queries(repo: Path, tmp_path: Path, grammars):
    await _build(repo, tmp_path)
    graph = load_cached(tmp_path / "kg")
    assert graph is not None

    resolved = queries.resolve_node(graph, "parse_json")
    assert resolved["node_id"] == "s:util/helper.py:parse_json"

    exact = queries.resolve_node(graph, "run")
    assert exact["node_id"] == "s:app.ts:run"

    ambiguous = queries.resolve_node(graph, "formatIt")
    assert "ambiguous" in ambiguous
    assert len(ambiguous["ambiguous"]) == 2

    missing = queries.resolve_node(graph, "no_such_symbol")
    assert "error" in missing

    impact = queries.impact_set(graph, "s:util/helper.py:parse_json")
    assert "main.py" in impact["affected_files"]

    path = queries.shortest_path(graph, "main", "parse_json")
    assert path["hops"] == 2
    assert path["path"][0] == "s:main.py:main"
    assert path["path"][-1] == "s:util/helper.py:parse_json"

    no_path = queries.shortest_path(graph, "main", "no_such_symbol")
    assert "error" in no_path

    nb = queries.neighbors(graph, "s:lib/util.ts:Greeter", depth=1)
    assert any(n["node_id"] == "s:app.ts:run" for n in nb["neighbors"])

    found = queries.search(graph, "greet")
    assert any(m["name"] == "Greeter.greet" for m in found["matches"])
