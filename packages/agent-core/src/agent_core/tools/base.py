"""Abstract Tool interface."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable

_log = logging.getLogger("agent_core.tools")


class Tool(ABC):
    """Abstract base for all agent tools."""

    # Behavior-oriented usage hint injected into the system prompt (1-2 lines).
    # Focuses on when to prefer/avoid the tool, not on its parameter schema.
    prompt_hint: str = ""

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name as used in LLM tool calls."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the LLM."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """JSON Schema for the tool's input parameters."""
        ...

    @abstractmethod
    async def execute(self, params: dict[str, Any], context: "ToolContext") -> dict[str, Any]:
        """Execute the tool and return a result dict."""
        ...

    def to_definition(self):
        """Convert to ToolDefinition for LLM providers."""
        from agent_core.providers.base import ToolDefinition
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


class ToolContext:
    """Runtime context passed to tools during execution."""

    def __init__(
        self,
        project_id: str,
        project_fs_path: str,
        conversation_id: str,
        user_id: str,
        db_session=None,
        http_client=None,
        browser=None,
        project_context=None,
        current_turn: int = 0,
        current_task_id: str | None = None,
        knowledge_cache=None,
        session=None,
        framework_root: str | None = None,
        secret_key: str = "",
        session_memories: list | None = None,
        upload_file_lookup: Callable[[str], bytes | None] | None = None,
        upload_file_names: Callable[[], list[str]] | None = None,
        db_session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.project_id = project_id
        self.project_fs_path = project_fs_path
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.db_session = db_session
        self.db_session_factory = db_session_factory
        self.http_client = http_client
        self.browser = browser
        self.project_context = project_context
        self.current_turn = current_turn
        self.current_task_id = current_task_id
        self.knowledge_cache = knowledge_cache
        self.session = session
        self.framework_root = framework_root
        self.secret_key = secret_key
        self.session_memories: list[dict] = session_memories if session_memories is not None else []
        self.upload_file_lookup = upload_file_lookup
        self.upload_file_names = upload_file_names
        self.ssl_verify: bool = False
        self.browser_ssl_verify: bool = True
        self.integration_settings: dict[str, str] = {}
        self._tool_registry: dict[str, Tool] = {}
        self.http_client_no_proxy = None
        self._browser_lock = asyncio.Lock()
        self.mcp_manager = None  # McpConnectionManager, lazily set by attach_mcp_tools

    def copy_for_task(self, task_id: str, turn: int) -> "ToolContext":
        """Shallow copy for parallel task isolation.

        Shares browser, http_client, db_session, project_context, mcp_manager,
        _browser_lock, _tool_registry (all asyncio-safe or read-only).
        Owns independent current_task_id / current_turn so tools emit events
        with the correct task ownership during concurrent execution.
        """
        import copy

        clone = copy.copy(self)  # shallow - shared refs for all resources
        # Lazy resource slots (browser, http_client, _browser_page) are
        # shared through the owner reference: ensure_* writes back to the
        # session's context so parallel task clones reuse ONE browser/client
        # instead of each starting their own — a per-clone resource would
        # never be closed (cleanup() only sees the shared context's slots).
        clone._shared = self
        clone.current_task_id = task_id
        clone.current_turn = turn
        # Concurrent tasks must not share one SQLAlchemy async session:
        # interleaved executes on the same session corrupt its state machine.
        # When a factory is provided, give each task its own session — the
        # caller (_execute_single_task) closes it when the task loop ends.
        if self.db_session_factory is not None and self.db_session is not None:
            try:
                clone.db_session = self.db_session_factory()
            except Exception:
                _log.warning("db_session_factory failed, sharing parent session", exc_info=True)
        return clone

    async def ensure_browser(self):
        """Lazily start and return the shared Playwright Chromium browser."""
        if self.browser is not None:
            return self.browser
        async with self._browser_lock:
            if self.browser is not None:
                return self.browser
            # Lazy resources live on the SHARED context (see copy_for_task):
            # a task clone must reuse the session's browser instead of
            # launching one Chromium process per task, and cleanup() closes
            # only the shared context's slots.
            owner = getattr(self, "_shared", None) or self
            if owner.browser is not None:
                self.browser = owner.browser
                return owner.browser
            import os
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            try:
                proxy_url = (
                    self.cfg("HTTPS_PROXY")
                    or self.cfg("HTTP_PROXY")
                    or os.environ.get("https_proxy")
                    or os.environ.get("http_proxy")
                )
                launch_kwargs: dict = {"headless": True}
                if proxy_url:
                    launch_kwargs["proxy"] = {"server": proxy_url}
                browser = await playwright.chromium.launch(**launch_kwargs)
            except Exception:
                await playwright.stop()
                raise
            owner._playwright = playwright
            owner.browser = browser
            self.browser = browser
            return browser

    _CREDENTIAL_KEYS = frozenset({
        "TOKEN", "SECRET", "PASSWORD", "COOKIE", "KEY", "CREDENTIAL", "AUTH",
    })

    def cfg(self, key: str, default: str = "") -> str:
        """Read a config value — prefers injected settings over os.environ.

        Credential keys are never returned from injected settings; they must be
        stored in user_tokens and retrieved via get_token(). The settings check
        matches only the final underscore segment (so ATLASSIAN_AUTH_TYPE is
        allowed but GIT_TOKEN is not); the os.environ fallback keeps the
        broader substring blocking for defense-in-depth.
        """
        upper = key.upper()
        if upper.rsplit("_", 1)[-1] in self._CREDENTIAL_KEYS:
            return default
        from_settings = self.integration_settings.get(key)
        if from_settings:
            return from_settings
        if any(segment in upper for segment in self._CREDENTIAL_KEYS):
            return default
        import os
        return os.environ.get(key, default)

    # Env-var keys to strip when building subprocess environments for tools
    # that pass env to LLM-generated code (code_executor, shell).  Matches by
    # final underscore segment OR known sensitive prefixes.
    _ENV_DENY_SUFFIXES = _CREDENTIAL_KEYS | {"DRIVER", "URL", "Dsn".upper()}
    _ENV_DENY_PREFIXES = (
        "DB_", "DATABASE", "REDIS", "SECRET", "PASSWORD",
        "TOKEN", "CREDENTIAL", "AUTH", "API_KEY", "PRIVATE",
        # Cloud credential prefixes — keys like AWS_REGION are harmless but
        # stripping them wholesale is the conservative default for
        # LLM-generated code (missing region config just fails, never leaks).
        "AWS_", "AZURE_", "GOOGLE_", "GCP_", "ALICLOUD_",
    )
    # Exact-match keys: PROJECTS_ROOT would reveal sibling-project locations
    # to LLM-generated code, enabling cross-project path construction. The
    # credential keys below have suffixes that dodge the suffix/prefix rules
    # (ID, URI, CREDENTIALS, PWD).
    _ENV_DENY_EXACT = frozenset({
        "PROJECTS_ROOT",
        "AWS_ACCESS_KEY_ID",
        "SQLALCHEMY_DATABASE_URI",
        "DATABASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "MYSQL_PWD",
        "PGPASSWORD",
    })

    def safe_env(self, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return os.environ with credential-like keys stripped.

        Used by code_executor and shell so LLM-generated code cannot
        exfiltrate SECRET_KEY, DB connection strings, etc.
        """
        import os
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if upper in self._ENV_DENY_EXACT:
                continue
            if upper.rsplit("_", 1)[-1] in self._ENV_DENY_SUFFIXES:
                continue
            if any(upper.startswith(p) for p in self._ENV_DENY_PREFIXES):
                continue
            env[key] = value
        if extra:
            env.update(extra)
        return env

    @property
    def conversation_media_dir(self) -> str:
        from pathlib import Path
        return str(Path(self.project_fs_path) / ".tmp" / "media" / self.conversation_id)

    def register_tool(self, tool: Tool) -> None:
        self._tool_registry[tool.name] = tool

    def get_tool(self, name: str) -> Tool | None:
        return self._tool_registry.get(name)

    def knowledge_dir(self) -> str:
        from pathlib import Path
        return str(Path(self.project_fs_path) / "knowledge")

    def tests_dir(self) -> str:
        from pathlib import Path
        return str(Path(self.project_fs_path) / "tests")

    async def cleanup(self) -> None:
        """Release resources (browser, HTTP client, playwright, MCP) at end of session."""
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.close_all()
            except Exception:
                _log.debug("mcp_manager close_all failed", exc_info=True)
            self.mcp_manager = None
        if self.http_client is not None:
            try:
                await self.http_client.aclose()
            except Exception:
                _log.debug("http_client close failed", exc_info=True)
            self.http_client = None
        if self.http_client_no_proxy is not None:
            try:
                await self.http_client_no_proxy.aclose()
            except Exception:
                _log.debug("http_client_no_proxy close failed", exc_info=True)
            self.http_client_no_proxy = None
        page = getattr(self, "_browser_page", None)
        if page is not None:
            try:
                await page.close()
            except Exception:
                _log.debug("browser page close failed", exc_info=True)
        self._browser_page = None
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                _log.debug("browser close failed", exc_info=True)
            self.browser = None
        pw = getattr(self, "_playwright", None)
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                _log.debug("playwright stop failed", exc_info=True)
            self._playwright = None
