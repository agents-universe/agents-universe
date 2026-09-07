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
from agent_core.knowledge.graph.parser import parse_file
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

# Java 21 modern-syntax fixture: records, sealed hierarchy, interface extends,
# arrow switch with null case. Exercises the parser improvements that turn a
# file-only graph into symbol-level nodes + inheritance edges for these forms.
JAVA21_FILES = {
    "src/main/java/com/example/modern/OrderService.java": (
        "package com.example.modern;\n"
        "\n"
        "import com.example.modern.model.OrderRecord;\n"
        "import lombok.extern.slf4j.Slf4j;\n"
        "import org.springframework.stereotype.Service;\n"
        "\n"
        "@Slf4j\n"
        "@Service\n"
        "public class OrderService extends BaseService implements OrderPort {\n"
        "    private final OrderRepository repo;\n"
        "\n"
        "    public OrderService(OrderRepository repo) {\n"
        "        this.repo = repo;\n"
        "    }\n"
        "\n"
        "    public OrderRecord placeOrder(OrderRequest req) {\n"
        "        return switch (req.kind()) {\n"
        "            case STANDARD -> repo.save(OrderRecord.of(req.name()));\n"
        "            case EXPRESS -> repo.save(OrderRecord.express(req.name()));\n"
        "            case null -> throw new IllegalStateException(\"kind null\");\n"
        "        };\n"
        "    }\n"
        "}\n"
        "\n"
        "class BaseService {}\n"
        "interface OrderPort {}\n"
    ),
    "src/main/java/com/example/modern/OrderRequest.java": (
        "package com.example.modern;\n"
        "\n"
        "public record OrderRequest(String name, OrderKind kind) {\n"
        "    public String upper() { return name.toUpperCase(); }\n"
        "}\n"
    ),
    "src/main/java/com/example/modern/OrderKind.java": (
        "package com.example.modern;\n"
        "\n"
        "public enum OrderKind { STANDARD, EXPRESS }\n"
    ),
    "src/main/java/com/example/modern/Shape.java": (
        "package com.example.modern;\n"
        "\n"
        "public sealed interface Shape permits Circle, Square {}\n"
        "interface Drawable extends Shape {}\n"
    ),
    "src/main/java/com/example/modern/Circle.java": (
        "package com.example.modern;\n"
        "\n"
        "public record Circle(double radius) implements Shape {}\n"
    ),
    "src/main/java/com/example/modern/Square.java": (
        "package com.example.modern;\n"
        "\n"
        "public final class Square implements Shape {}\n"
    ),
}

# Java constructs that used to yield zero symbols or wrong edges: @interface
# annotation types, module-info, scoped implements targets, chained calls.
JAVA_EDGE_FILES = {
    "src/main/java/com/example/edge/Marker.java": (
        "package com.example.edge;\n"
        "\n"
        "public @interface Marker { String value() default \"\"; }\n"
    ),
    "module-info.java": (
        "module com.example.edge { requires java.sql; }\n"
    ),
    "src/main/java/com/example/edge/Impl.java": (
        "package com.example.edge;\n"
        "\n"
        "public class Impl implements Outer.Inner {}\n"
        "class Outer { static class Inner {} }\n"
    ),
    "src/main/java/com/example/edge/Chain.java": (
        "package com.example.edge;\n"
        "\n"
        "public class Chain {\n"
        "    Service getService() { return new Service(); }\n"
        "    Foo a() { return new Foo(); }\n"
        "    void helper(String s) {}\n"
        "    void f() {\n"
        "        getService().fetch().run();\n"
        "        new Foo().bar();\n"
        "        helper(a().b());\n"
        "    }\n"
        "}\n"
        "class Service { Service fetch() { return this; } void run() {} }\n"
        "class Foo { void bar() {} }\n"
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
def java21_repo(tmp_path: Path) -> Path:
    """A local git repo seeded only with the Java 21 fixture files."""
    repo = tmp_path / "java21"
    repo.mkdir()
    for rel, text in JAVA21_FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run("init", "-b", "main", cwd=repo)
    _run("config", "user.email", "t@t.t", cwd=repo)
    _run("config", "user.name", "t", cwd=repo)
    _run("add", ".", cwd=repo)
    _run("commit", "-m", "seed java21", cwd=repo)
    return repo


@pytest.fixture
def java_edge_repo(tmp_path: Path) -> Path:
    """A local git repo seeded only with the Java edge-case fixture files."""
    repo = tmp_path / "java_edge"
    repo.mkdir()
    for rel, text in JAVA_EDGE_FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run("init", "-b", "main", cwd=repo)
    _run("config", "user.email", "t@t.t", cwd=repo)
    _run("config", "user.name", "t", cwd=repo)
    _run("add", ".", cwd=repo)
    _run("commit", "-m", "seed java edge", cwd=repo)
    return repo


@pytest.fixture
def grammars():
    # get_grammar already attempts a prefetch download before giving up.
    if get_grammar("python") is None or get_grammar("typescript") is None:
        pytest.skip("tree-sitter grammars unavailable even after prefetch attempt (offline?)")
    return True


@pytest.fixture
def java_grammar():
    if get_grammar("java") is None:
        pytest.skip("tree-sitter java grammar unavailable even after prefetch attempt (offline?)")
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


# ---------------------------------------------------------------------------
# Java 21 modern syntax: records, sealed, interface extends, arrow switch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_java21_parse_records_and_sealed(java21_repo: Path, java_grammar):
    """Parse layer: records become class symbols, sealed/extends edges exist,
    and permits + implements do not double-emit the same inherits edge."""
    result = parse_file(
        java21_repo / "src/main/java/com/example/modern/Shape.java",
        "src/main/java/com/example/modern/Shape.java",
    )
    assert result is not None
    assert result.stats.get("error") is None
    names = {s["name"] for s in result.symbols}
    assert "Shape" in names and "Drawable" in names
    inherits = {(e["from"], e["target"]) for e in result.edges if e["type"] == "inherits"}
    # permits: Circle/Square live in other files, so no edge is emitted from
    # this file (their own implements clauses create it); same-file Drawable
    # extends Shape does
    assert ("Drawable", "Shape") in inherits
    assert len(inherits) == 1

    rec = parse_file(
        java21_repo / "src/main/java/com/example/modern/OrderRequest.java",
        "src/main/java/com/example/modern/OrderRequest.java",
    )
    assert rec is not None
    assert rec.stats.get("error") is None
    rec_names = {s["name"] for s in rec.symbols}
    assert "OrderRequest" in rec_names
    assert "OrderRequest.upper" in rec_names
    # arrow switch inside OrderService produces calls from the method
    svc = parse_file(
        java21_repo / "src/main/java/com/example/modern/OrderService.java",
        "src/main/java/com/example/modern/OrderService.java",
    )
    assert svc is not None
    svc_names = {s["name"] for s in svc.symbols}
    assert "OrderService" in svc_names
    assert "OrderService.placeOrder" in svc_names
    calls = {e["target"] for e in svc.edges if e["type"] == "calls"}
    assert "repo.save" in calls
    assert "OrderRecord.of" in calls
    assert "OrderRecord.express" in calls
    svc_inherits = {(e["from"], e["target"]) for e in svc.edges if e["type"] == "inherits"}
    assert ("OrderService", "BaseService") in svc_inherits
    assert ("OrderService", "OrderPort") in svc_inherits


@pytest.mark.asyncio
async def test_java21_build_symbols_and_edges(java21_repo: Path, tmp_path: Path, java_grammar):
    """End-to-end build: records/sealed produce class+method symbol nodes and
    inherits edges resolve between files (sealed parent in Shape.java)."""
    summary = await _build(java21_repo, tmp_path)
    assert summary["status"] == "built"
    assert summary["stats"]["files"] == 6
    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    nodes = {n.id: n for n in graph.nodes}

    order_req = "s:src/main/java/com/example/modern/OrderRequest.java"
    assert nodes[f"{order_req}:OrderRequest"].type == "class"
    assert nodes[f"{order_req}:OrderRequest.upper"].type == "function"

    svc = "s:src/main/java/com/example/modern/OrderService.java"
    assert nodes[f"{svc}:OrderService"].type == "class"
    assert nodes[f"{svc}:OrderService.placeOrder"].type == "function"

    shape = "s:src/main/java/com/example/modern/Shape.java"
    assert nodes[f"{shape}:Shape"].type == "class"
    circle = "s:src/main/java/com/example/modern/Circle.java"
    assert nodes[f"{circle}:Circle"].type == "class"
    square = "s:src/main/java/com/example/modern/Square.java"
    assert nodes[f"{square}:Square"].type == "class"

    edges = {(e.src, e.dst, e.type) for e in graph.edges}
    # cross-file sealed inheritance: permitted subtype -> sealed parent file
    assert (f"{circle}:Circle", f"{shape}:Shape", "inherits") in edges
    assert (f"{square}:Square", f"{shape}:Shape", "inherits") in edges
    # same-file inheritance inside OrderService.java
    assert (f"{svc}:OrderService", f"{svc}:BaseService", "inherits") in edges
    assert (f"{svc}:OrderService", f"{svc}:OrderPort", "inherits") in edges
    # the arrow switch's method calls (repo.save / OrderRecord.of / ...) were
    # extracted by the parser (see test_java21_parse_records_and_sealed) and
    # land in the unresolved bucket at build time because their targets are
    # not defined inside this fixture's symbol set — expected, no fabricated
    # call edges may appear
    assert summary["stats"]["unresolved_calls"] >= 9
    assert not any(
        e.type == "calls" and e.src == f"{svc}:OrderService.placeOrder"
        for e in graph.edges
    )

    # class node count sanity: 6 classes/records + their methods
    class_nodes = [n for n in graph.nodes if n.type == "class"]
    assert len(class_nodes) >= 6


# ---------------------------------------------------------------------------
# Java edge constructs: @interface, module-info, scoped implements, chains
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_java_edge_parse_constructs(java_edge_repo: Path, java_grammar):
    """Parse layer: constructs that used to yield zero symbols or wrong edges."""
    marker = parse_file(
        java_edge_repo / "src/main/java/com/example/edge/Marker.java",
        "src/main/java/com/example/edge/Marker.java",
    )
    assert marker is not None and marker.stats.get("error") is None
    assert "Marker" in {s["name"] for s in marker.symbols}

    module = parse_file(
        java_edge_repo / "module-info.java", "module-info.java"
    )
    assert module is not None and module.stats.get("error") is None
    assert any(
        s["type"] == "symbol" and s["name"] == "com.example.edge"
        for s in module.symbols
    )

    impl = parse_file(
        java_edge_repo / "src/main/java/com/example/edge/Impl.java",
        "src/main/java/com/example/edge/Impl.java",
    )
    assert impl is not None and impl.stats.get("error") is None
    inherits = {(e["from"], e["target"]) for e in impl.edges if e["type"] == "inherits"}
    assert ("Impl", "Outer.Inner") in inherits
    # the scoped target stays one edge — never split into Outer/Inner
    assert not any(t in ("Outer", "Inner") for _, t in inherits)

    chain = parse_file(
        java_edge_repo / "src/main/java/com/example/edge/Chain.java",
        "src/main/java/com/example/edge/Chain.java",
    )
    assert chain is not None and chain.stats.get("error") is None
    calls = {e["target"] for e in chain.edges if e["type"] == "calls"}
    assert "getService.fetch.run" in calls  # full chain, no lost object prefix
    assert "Foo.bar" in calls              # new Foo().bar()
    assert "helper" in calls               # argument calls don't leak into the chain
    assert "a.b" in calls


@pytest.mark.asyncio
async def test_java_edge_build_coverage(java_edge_repo: Path, tmp_path: Path, java_grammar):
    """End-to-end build: every java edge fixture file yields symbols and the
    parse-rate numbers land in stats + report."""
    summary = await _build(java_edge_repo, tmp_path)
    assert summary["status"] == "built"
    stats = summary["stats"]
    assert stats["files"] == 4
    assert stats["failed"] == 0
    assert stats["with_symbols"] == 4
    assert stats["lang_coverage"]["java"] == {"files": 4, "with_symbols": 4}

    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    nodes = {n.id: n for n in graph.nodes}
    assert nodes["s:module-info.java:com.example.edge"].type == "symbol"
    marker = "s:src/main/java/com/example/edge/Marker.java"
    assert nodes[f"{marker}:Marker"].type == "class"

    report = render_report(graph)
    assert "parse rate: 4/4 files with symbols (100%)" in report
    assert "by language: java 4/4" in report


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
        # helper.py changed + broken.py re-parses (error entries are never
        # reused, they heal on every build) — everything else is a cache hit.
        assert summary["stats"]["parsed"] == 2
        assert summary["stats"]["reused"] == 8
        assert summary["stats"]["failed"] == 1
        assert counted.call_count == 2

    graph = load_cached(tmp_path / "kg")
    assert graph.node("s:util/helper.py:extra") is not None


@pytest.mark.asyncio
async def test_error_entries_reparsed_on_force(repo: Path, tmp_path: Path):
    """Error entries are not served from the SHA cache: a forced rebuild with
    no file changes re-parses exactly the failed file so parser fixes heal
    the graph without touching the cache directory."""
    await _build(repo, tmp_path)
    from agent_core.knowledge.graph import parser as _parser_mod
    with patch("agent_core.knowledge.graph.builder.parse_file",
               wraps=_parser_mod.parse_file) as counted:
        summary = await _build(repo, tmp_path, force=True)
    assert summary["status"] == "built"
    assert summary["stats"]["parsed"] == 1
    assert summary["stats"]["reused"] == 9
    assert summary["stats"]["failed"] == 1
    assert summary["stats"]["failed_reasons"] == {"syntax": 1}
    assert counted.call_count == 1  # only broken.py is re-parsed
    assert counted.call_args[0][1] == "broken.py"


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


# ---------------------------------------------------------------------------
# Untracked files: warning by default, indexable on request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_untracked_files_warn_but_not_indexed(repo: Path, tmp_path: Path, grammars):
    (repo / "wip.py").write_text("def wip():\n    return 1\n", encoding="utf-8")
    summary = await _build(repo, tmp_path, force=True)
    # committed files only — wip.py is not in the graph
    assert summary["stats"]["files"] == 10
    assert summary["stats"]["untracked_sources"] == 1
    assert "wip.py" in summary["warning"]
    graph = load_cached(tmp_path / "kg")
    assert graph.node("s:wip.py:wip") is None


@pytest.mark.asyncio
async def test_untracked_indexed_when_include_untracked(repo: Path, tmp_path: Path, grammars):
    (repo / "wip.py").write_text("def wip():\n    return 1\n", encoding="utf-8")
    summary = await _build(repo, tmp_path, force=True, include_untracked=True)
    assert summary["stats"]["files"] == 11
    assert "warning" not in summary
    graph = load_cached(tmp_path / "kg")
    assert graph.node("s:wip.py:wip") is not None


@pytest.mark.asyncio
async def test_untracked_warning_clears_after_commit(repo: Path, tmp_path: Path, grammars):
    (repo / "wip.py").write_text("def wip():\n    return 1\n", encoding="utf-8")
    assert "warning" in (await _build(repo, tmp_path, force=True))
    _run("add", "wip.py", cwd=repo)
    _run("commit", "-m", "add wip", cwd=repo)
    summary = await _build(repo, tmp_path, force=True)
    assert "warning" not in summary
    assert summary["stats"]["files"] == 11


@pytest.mark.asyncio
async def test_untracked_warning_on_up_to_date_fast_path(repo: Path, tmp_path: Path, grammars):
    await _build(repo, tmp_path)
    (repo / "wip.py").write_text("def wip():\n    return 1\n", encoding="utf-8")
    summary = await _build(repo, tmp_path)  # not forced -> fast path
    assert summary["status"] == "up_to_date"
    assert "wip.py" in summary["warning"]


@pytest.mark.asyncio
async def test_mode_switch_rebuilds_not_fast_path(repo: Path, tmp_path: Path):
    """A manifest built with untracked files is not reused by a tracked-only
    build, and vice versa — the fast path must never resurrect a stale mode."""
    (repo / "wip.py").write_text("def wip():\n    return 1\n", encoding="utf-8")
    await _build(repo, tmp_path, force=True, include_untracked=True)
    # tracked-only rebuild must drop wip.py from the graph
    summary = await _build(repo, tmp_path, force=True)
    assert summary["status"] == "built"
    assert summary["stats"]["files"] == 10
    graph = load_cached(tmp_path / "kg")
    assert graph.node("s:wip.py:wip") is None


@pytest.mark.asyncio
async def test_report_renders(repo: Path, tmp_path: Path, grammars):
    await _build(repo, tmp_path)
    graph = load_cached(tmp_path / "kg")
    report = render_report(graph)
    assert "# Repository Graph Report" in report
    assert "God nodes" in report
    assert "repo_graph" in report  # usage section


@pytest.mark.asyncio
async def test_stats_visibility(repo: Path, tmp_path: Path, grammars):
    """Parse-rate stats reach both the report and the compact map."""
    await _build(repo, tmp_path)
    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    stats = graph.stats
    assert stats["with_symbols"] == 9  # 10 files, broken.py has none
    assert stats["failed_reasons"] == {"syntax": 1}
    assert "cov:" in compact_map(graph)
    report = render_report(graph)
    assert "parse rate: 9/10 files with symbols (90%)" in report
    assert "by language:" in report
    assert "python 3/4 (75%)" in report  # worst coverage surfaces first
    assert "syntax: 1" in report


@pytest.mark.asyncio
async def test_grammar_unavailable_warning(repo: Path, tmp_path: Path):
    """A missing grammar is reported loudly with remediation, not silently:
    the summary warning names the language and failed_reasons keeps counts."""
    from agent_core.knowledge.graph.parser import FileParseResult

    def fake_parse_file(path: Path, rel: str):
        if rel.endswith(".java"):
            return FileParseResult(sha256="0" * 64, lang="java",
                                   stats={"error": "grammar unavailable: java"})
        return parse_file(path, rel)

    with patch("agent_core.knowledge.graph.builder.parse_file",
               side_effect=fake_parse_file):
        summary = await _build(repo, tmp_path, force=True)
    assert summary["status"] == "built"
    assert "unavailable" in summary["warning"]
    assert "java" in summary["warning"]
    assert "prefetch" in summary["warning"]
    reasons = summary["stats"]["failed_reasons"]
    assert reasons["grammar unavailable: java"] == 3
    assert reasons["syntax"] == 1  # broken.py still parses for real


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
