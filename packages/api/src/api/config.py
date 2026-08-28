"""Application settings loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py is installed at /app/src/api in Docker and lives four levels below
# the repository root during local development. Prefer a directory containing
# the bundled agent definitions, then fall back to the package parent.
def _find_project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "agents").is_dir():
            return candidate
    return here.parents[2]


_PROJECT_ROOT = _find_project_root()
_ENV_FILES = (str(_PROJECT_ROOT / ".env"), ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    # Database — individual fields (preferred over MSSQL_CONNECTION_STRING)
    db_host: str = "127.0.0.1"
    db_port: int = 1433
    db_name: str = "agentsuniverse"
    db_user: str = "sa"
    db_password: str = "YourPassword"
    db_driver: str = "ODBC Driver 17 for SQL Server"
    db_trust_cert: bool = True

    # DB connection pool (tune for expected concurrent users)
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30

    # Fallback: full connection string (used only when db_host is left at default
    # AND this env var is explicitly set, e.g. for Docker or SQLite test overrides)
    mssql_connection_string: str = ""

    # Full connection URL — highest-priority override for multi-database support.
    # Takes precedence over MSSQL_CONNECTION_STRING and the DB_* fields; accepts
    # any SQLAlchemy URL (mssql+aioodbc / postgresql+asyncpg / mysql+aiomysql /
    # sqlite+aiosqlite). Unset in production means "SQL Server via DB_* fields".
    database_url: str = ""

    # Security
    secret_key: str = "change-me-32-char-secret-key-here!"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""

    # App URLs
    app_base_url: str = "http://localhost:8000"
    app_root_path: str = ""
    web_base_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173"

    # Test override
    test_database_url: str = ""

    # Project workspaces — required outside tests. Each project lives under this root.
    projects_root: str = ""

    # Uploads
    max_upload_size_mb: int = 10


    # Atlassian (base URL is system-level; tokens are per-user in DB)
    # Cloud:  https://your-org.atlassian.net  → basic auth, no sub-paths
    # Server: https://jira.your-company.com   → bearer auth, /jira + /confluence sub-paths
    atlassian_base_url: str = ""

    # Git (base URL is system-level; token is per-user in DB)
    # All other git settings are derived from git_base_url — see properties below.
    git_base_url: str = "https://github.com"
    git_ssl_verify: bool = False
    atlassian_ssl_verify: bool = False

    # TLS / SSL for outbound LLM provider requests.
    llm_ssl_verify: bool = False

    # TLS / SSL for Playwright browser requests (set False to ignore self-signed certs).
    browser_ssl_verify: bool = True

    # Network policy for Python executed by agents (code_executor):
    # "" (default) -> allow all network (Playwright downloads, screen recording, ...);
    # "localhost"  -> loopback only; "none" -> all sockets blocked.
    sandbox_network: str = ""

    # System default model (OpenAI-compatible fallback when user has no configs)
    system_default_model_id: str = ""
    system_default_base_url: str = ""
    system_default_api_key: str = ""

    # Outbound HTTP proxy (applied to all LLM provider calls at startup)
    https_proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1"
    # Extra NO_PROXY entries from .env (won't be overridden by system env NO_PROXY)
    app_no_proxy: str = ""

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "human"

    # Auth bypass (local dev only — never enable in production)
    auth_bypass_enabled: bool = False
    auth_bypass_user_id: str = "SYSTEM"

    # OAuth SSO — only the domain/issuer is needed; endpoints are discovered via OIDC
    oauth_sso_domain: str = ""  # must expose /.well-known/openid-configuration
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    oauth_acr_values: str = ""
    oauth_scope: str = "openid profile email"
    # OIDC discovery/token-exchange/revocation calls hardcoded
    # verify=False. Self-hosted SSO (Keycloak, Casdoor) often uses
    # self-signed certs, so the default stays False — production can
    # enforce real TLS verification via this flag.
    oauth_ssl_verify: bool = False
    # None → derived from web_base_url (https ⇒ Secure cookie). Explicit
    # true/false overrides. Defaulting to False leaks the session cookie over
    # plaintext on HTTPS deployments .
    oauth_secure_cookie: bool | None = None
    # Lifetime of the OAuth state nonce, the duplicate-callback recovery cache,
    # and the signed anchor cookie. All three must outlive a full login
    # round-trip: a user can sit on the SSO page (or the api container can
    # restart mid-login) long past 10 minutes, and an expired state bounces
    # them back to the login page instead of signing them in.
    oauth_state_ttl: int = 1800

    @property
    def cookie_secure(self) -> bool:
        if self.oauth_secure_cookie is not None:
            return self.oauth_secure_cookie
        return self.web_base_url.startswith("https://")

    # Session (Redis-backed)
    session_ttl: int = 86400       # 24 hours
    auth_cookie_name: str = "x-auth-token"
    active_users_window: int = 300  # 5-minute active-user window

    @property
    def effective_redis_url(self) -> str:
        if not self.redis_password:
            return self.redis_url
        from urllib.parse import urlparse, urlunparse
        p = urlparse(self.redis_url)
        host_part = f":{quote_plus(self.redis_password)}@{p.hostname}"
        if p.port:
            host_part += f":{p.port}"
        return urlunparse(p._replace(netloc=host_part))

    @property
    def _atlassian_is_cloud(self) -> bool:
        return self.atlassian_base_url.rstrip("/").endswith(".atlassian.net")

    @property
    def atlassian_auth_type(self) -> str:
        return "basic" if self._atlassian_is_cloud else "bearer"

    @property
    def atlassian_jira_base_path(self) -> str:
        return "" if self._atlassian_is_cloud else "/jira"

    @property
    def atlassian_confluence_base_path(self) -> str:
        return "" if self._atlassian_is_cloud else "/confluence"

    @property
    def git_provider(self) -> str:
        return "github"

    @property
    def git_api_base_path(self) -> str:
        # Public GitHub uses https://api.github.com directly (no sub-path needed).
        # GitHub Enterprise Server exposes the REST API under /api/v3.
        if self.git_base_url.rstrip("/") == "https://github.com":
            return ""
        return "/api/v3"

    @property
    def git_web_base_path(self) -> str:
        return ""

    @property
    def git_commit_search_mode(self) -> str:
        return "by-jira-key"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_db_url(self) -> str:
        if self.test_database_url:
            return self.test_database_url
        if self.database_url:
            return self.database_url
        # DB_* individual fields are the primary path.
        # mssql_connection_string is a last-resort legacy override (e.g. non-standard drivers).
        if not self.mssql_connection_string:
            driver = quote_plus(self.db_driver)
            password = quote_plus(self.db_password)
            trust = "&TrustServerCertificate=yes" if self.db_trust_cert else ""
            return (
                f"mssql+aioodbc://{self.db_user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
                f"?driver={driver}{trust}"
            )
        return self.mssql_connection_string


@lru_cache
def get_settings() -> Settings:
    return Settings()
