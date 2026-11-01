"""Deterministic code knowledge graph for cloned git repositories.

Pure local pipeline: graphify's tree-sitter AST extraction (no LLM, no
embeddings; see graphify_adapter.py — the sole import point) converted to
our schema-v2 graph.json + graph_report.md per repo, stored under
``{project_fs_path}/.tmp/repo_graph/{repo_name}/``. Queried by the
``repo_graph`` tool before the agent reads repository files, so lookups
navigate the graph instead of scanning the whole tree (fewer tokens).
"""
