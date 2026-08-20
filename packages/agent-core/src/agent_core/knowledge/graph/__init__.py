"""Deterministic code knowledge graph for cloned git repositories.

Pure local pipeline: tree-sitter AST parsing (no LLM, no embeddings) -> a
compact graph.json + graph_report.md per repo, stored under
``{project_fs_path}/.tmp/repo_graph/{repo_name}/``. Queried by the
``repo_graph`` tool before the agent reads repository files, so lookups
navigate the graph instead of scanning the whole tree (fewer tokens).
"""
