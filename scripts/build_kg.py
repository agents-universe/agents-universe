"""Build the repo knowledge graph for agent-world (or any repo under it).

Reuses the built-in incremental graph engine (the same one the ``repo_graph``
tool and auto-build on git ops use) so later runs only re-parse changed files.
Output lives under ``.tmp/repo_graph/<name>/`` (git-ignored):
graph.json + graph_report.md + cache/index.json.

Usage:
    python scripts/build_kg.py            # incremental (fast when nothing changed)
    python scripts/build_kg.py --force    # re-hash and re-parse everything
    python scripts/build_kg.py --repo PATH  # build a different local repo
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Package source isn't installed; make agent_core importable from the checkout.
sys.path.insert(0, str(REPO_ROOT / "packages" / "agent-core" / "src"))

from agent_core.knowledge.graph.builder import build_repo_graph  # noqa: E402
from agent_core.knowledge.graph.model import repo_graph_dir  # noqa: E402
from agent_core.knowledge.graph.report import REPORT_FILE  # noqa: E402
from agent_core.knowledge.graph.store import load_repo_graph  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="repo to graph (default: this checkout)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-hash and re-parse every candidate file (default: incremental)",
    )
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="also index untracked source files (default: committed files only; "
        "untracked files are reported in the summary warning instead)",
    )
    parser.add_argument(
        "--name",
        help="graph directory name (default: repo directory name)",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    name = args.name or repo.name
    kg_dir = repo_graph_dir(str(REPO_ROOT), name)

    async def _run() -> dict:
        return await build_repo_graph(
            repo, kg_dir, force=args.force, include_untracked=args.include_untracked
        )

    summary = asyncio.run(_run())
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if summary.get("warning"):
        print(f"\nWarning: {summary['warning']}")

    graph = load_repo_graph(kg_dir)
    print(f"\nReport: {kg_dir / REPORT_FILE}")
    print(f"Graph:  {kg_dir / 'graph.json'}")
    if graph is not None:
        print(
            f"Files: {graph.stats.get('files', 0)} | symbols: "
            f"{graph.stats.get('nodes', 0)} | edges: {graph.stats.get('edges', 0)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
