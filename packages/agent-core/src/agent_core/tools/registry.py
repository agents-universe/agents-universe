"""Tool registry — builds tool instances on demand.

Tools with optional heavy dependencies (playwright, Pillow) are imported lazily.
"""
from __future__ import annotations

from .base import Tool, ToolContext

# Core tools — always available
from .deliver_file import DeliverFileTool
from .filesystem import FilesystemTool
from .knowledge_rw import KnowledgeRWTool
from .memory_rw import MemoryRWTool
from .planner import PlannerTool
from .shell import ShellTool
from .sql_query import SqlQueryTool
from .web_fetch import WebFetchTool

# plan_task is framework-level behavior (the loop intercepts it into task
# mode), not an ordinary tool an agent definition may omit. Always injected,
# but never inside _CORE_TOOLS — see build_tool_registry.
_PLAN_TOOL = PlannerTool

_CORE_TOOLS: list[type[Tool]] = [
    FilesystemTool,
    KnowledgeRWTool,
    MemoryRWTool,
    WebFetchTool,
    PlannerTool,
    SqlQueryTool,
    ShellTool,
    DeliverFileTool,
]

# Optional tools — imported lazily to avoid breaking on missing packages
_OPTIONAL_TOOL_MODULES = {
    "browser_playwright": ("agent_core.tools.browser_playwright", "BrowserPlaywrightTool"),
    "chart_renderer":     ("agent_core.tools.chart_renderer",     "ChartRendererTool"),
    "code_executor":      ("agent_core.tools.code_executor",      "CodeExecutorTool"),
    "image_annotator":    ("agent_core.tools.image_annotator",    "ImageAnnotatorTool"),
    "focus_template":     ("agent_core.tools.focus_template",     "FocusTemplateTool"),
    "user_confirm":       ("agent_core.tools.user_confirm",       "UserConfirmTool"),
    "jira":               ("agent_core.tools.jira",               "JiraTool"),
    "confluence":         ("agent_core.tools.confluence",          "ConfluenceTool"),
    "github":             ("agent_core.tools.github",             "GitHubTool"),
    "kong":               ("agent_core.tools.kong",               "KongTool"),
    "api_request":        ("agent_core.tools.api_request",        "ApiRequestTool"),
    "secret_vault":       ("agent_core.tools.secret_vault",       "SecretVaultTool"),
    "test_generator":     ("agent_core.tools.test_generator",     "TestGeneratorTool"),
    "script_writer":      ("agent_core.tools.script_writer",      "ScriptWriterTool"),
    "git_repo":           ("agent_core.tools.git_repo",           "GitRepoTool"),
    "repo_graph":         ("agent_core.tools.repo_graph",         "RepoGraphTool"),
}


def _load_optional(name: str) -> type[Tool] | None:
    entry = _OPTIONAL_TOOL_MODULES.get(name)
    if entry is None:
        return None
    module_path, class_name = entry
    try:
        mod = __import__(module_path, fromlist=[class_name])
        return getattr(mod, class_name)
    except (ImportError, AttributeError):
        return None


def build_tool_registry(tool_names: list[str] | None = None) -> dict[str, Tool]:
    """Instantiate the requested tools (or all available built-ins)."""
    import logging
    _log = logging.getLogger("agent_core.tools.registry")

    registry: dict[str, Tool] = {}

    for cls in _CORE_TOOLS:
        instance = cls()
        if tool_names is None or instance.name in tool_names:
            if instance.name in registry:
                raise ValueError(f"Duplicate tool name: {instance.name!r}")
            registry[instance.name] = instance

    # plan_task is framework-level behavior (the loop intercepts it into task
    # mode), not an ordinary tool an agent definition may omit — an explicit
    # tools: list that forgets it must not silently strip the agent's ability
    # to plan. It is injected AFTER the allowlist so it can never leak the
    # other core tools in: only plan_task itself is exempt from the list.
    if tool_names is not None and "plan_task" not in registry:
        registry["plan_task"] = _PLAN_TOOL()

    for name in _OPTIONAL_TOOL_MODULES:
        if tool_names is not None and name not in tool_names:
            continue
        cls = _load_optional(name)
        if cls is not None:
            instance = cls()
            if instance.name in registry:
                raise ValueError(f"Duplicate tool name: {instance.name!r}")
            registry[instance.name] = instance

    if tool_names is not None:
        missing = set(tool_names) - set(registry.keys())
        if missing:
            _log.warning("Agent declares tools not found in registry: %s", sorted(missing))

    return registry
