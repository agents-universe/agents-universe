"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .database import engine
from .logging_setup import setup_logging
import logging
from .middleware.logging import StructuredLoggingMiddleware

settings = get_settings()
setup_logging()


async def _sync_agents(agents_dir: str) -> None:
    """Upsert global agent definitions from *.agent.md files at startup."""
    import logging
    from .database import AsyncSessionLocal
    from .services.agent_sync import sync_agents_dir

    log = logging.getLogger("agents_universe.startup")
    async with AsyncSessionLocal() as session:
        try:
            synced, removed = await sync_agents_dir(
                session, agents_dir, project_id=None, is_system=True
            )
            if synced or removed:
                log.info("Agent sync: registered/updated %d, removed %d", len(synced), len(removed))
        except Exception:
            log.exception("Agent sync failed at startup")


_LLM_NO_PROXY_HOSTS = "api.openai.com,api.anthropic.com,generativelanguage.googleapis.com"


def _apply_proxy_settings() -> None:
    import os
    if settings.https_proxy:
        os.environ.setdefault("HTTPS_PROXY", settings.https_proxy)
        os.environ.setdefault("HTTP_PROXY", settings.https_proxy)
    no_proxy_parts = [
        settings.no_proxy or "",
        settings.app_no_proxy or "",
        _LLM_NO_PROXY_HOSTS,
    ]
    no_proxy = ",".join(filter(None, no_proxy_parts))
    os.environ["NO_PROXY"] = no_proxy


async def _run_migrations() -> None:
    """Run Alembic migrations to head on startup.

    command.upgrade runs synchronously (pyodbc), so it executes in a worker
    thread to avoid stalling the event loop on slow DDL. Failures never block
    startup — they only warn — and a short retry covers the common case of the
    DB container still starting when the API comes up. This runs per process
    with no cross-replica lock: several API replicas starting at once may run
    the same migration concurrently and one of them may fail; that failure is
    harmless (alembic is transactional per migration), only warns, and the
    next restart retries it, so the cluster self-heals.
    """
    import asyncio
    import logging
    from pathlib import Path

    log = logging.getLogger("agents_universe.startup")
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    if not alembic_ini.exists():
        log.debug("alembic.ini not found at %s, skipping auto-migrate", alembic_ini)
        return
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
    # env.py's fileConfig (alembic.ini logging) would replace the app's root
    # logger with WARN/stderr, swallowing every INFO log for the process
    # lifetime — see alembic/env.py.
    cfg.attributes["configure_logger"] = False
    for attempt in range(3):
        try:
            await asyncio.to_thread(command.upgrade, cfg, "head")
            log.info("Database migrations applied successfully")
            return
        except Exception:
            log.warning(
                "Auto-migration attempt %d/3 failed; retrying in %ds",
                attempt + 1,
                2 * (attempt + 1),
                exc_info=True,
            )
            await asyncio.sleep(2 * (attempt + 1))
    log.warning("Auto-migration failed after retries; database may need manual upgrade")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log = logging.getLogger("agents_universe.startup")
    from .services.redis_client import init_redis, close_redis
    from agent_core.knowledge.cache import KnowledgeCache
    from agent_core.skills.registry import SkillRegistry
    from agent_core.workflows import WorkflowRegistry

    from .paths import AGENTS_DIR, WORKFLOWS_DIR

    _apply_proxy_settings()

    # _run_migrations was dead code — defined with a complete
    # implementation and docstring but never called from lifespan, so a fresh
    # deployment that skipped `alembic upgrade head` drifted silently (missing
    # columns/indexes surfaced as 500s at runtime). Alembic runs migrations
    # idempotently; failures only warn so a broken DB doesn't block startup.
    await _run_migrations()

    # Fail closed on the default SECRET_KEY: the placeholder is a public
    # constant, so a deployment that forgets .env would ship forgeable
    # sessions (JWT + OAuth) and a decryptable token vault .
    if settings.secret_key == "change-me-32-char-secret-key-here!":
        raise RuntimeError(
            "SECRET_KEY is still the default value. Set a strong secret in .env "
            "before starting the API — the public default lets anyone forge "
            "sessions and decrypt the token vault."
        )

    await init_redis()
    await _sync_agents(str(AGENTS_DIR))

    from .database import AsyncSessionLocal
    from .services.project_deletion import startup_sweep
    async with AsyncSessionLocal() as sweep_db:
        try:
            await startup_sweep(sweep_db)
        except Exception:
            # Cleanup is recoverable; it must never prevent the API from starting.
            log.exception("Project deletion sweep failed")
        # In-flight turns from a previous process are dead (all run state is
        # in-memory): flip their rows so a reopened conversation shows an
        # interrupted notice instead of a silently vanished turn. The partial
        # output of those turns is then materialized into the message history
        # so the next turn's agent context includes it (the user continues by
        # typing, not re-running), and mid-flight task rows settle to failed.
        # Single replica — see interrupt_stale_runs docstring.
        from .services.conversation_runs import (
            interrupt_stale_runs,
            interrupt_stale_tasks,
            materialize_interrupted_snapshots,
        )
        try:
            _stale = await interrupt_stale_runs(sweep_db)
            if _stale:
                log.info(
                    "Marked %d stale conversation runs interrupted", _stale
                )
        except Exception:
            # Recoverable; must never prevent the API from starting.
            log.exception("Conversation run sweep failed")
        try:
            _recovered = await materialize_interrupted_snapshots(sweep_db)
            if _recovered:
                log.info(
                    "Materialized %d interrupted-run snapshots into history", _recovered
                )
        except Exception:
            log.exception("Snapshot materialization sweep failed")
        try:
            _stale_tasks = await interrupt_stale_tasks(sweep_db)
            if _stale_tasks:
                log.info(
                    "Settled %d stale agent tasks to failed", _stale_tasks
                )
        except Exception:
            log.exception("Agent task sweep failed")

    app.state.knowledge_cache = KnowledgeCache()

    skill_registry = SkillRegistry()
    skill_registry.load_dir(
        str(AGENTS_DIR / "skills"),
        mixin_dir=str(AGENTS_DIR / "skills" / "_mixins"),
    )
    app.state.skill_registry = skill_registry

    workflow_registry = WorkflowRegistry()
    workflow_registry.load_dir(str(WORKFLOWS_DIR))
    app.state.workflow_registry = workflow_registry

    yield
    await close_redis()
    await engine.dispose()


def create_app() -> FastAPI:
    import logging as _logging
    from fastapi import Request
    from fastapi.responses import JSONResponse

    app = FastAPI(
        title="Agents Universe API",
        version="0.1.0",
        root_path=settings.app_root_path,
        lifespan=lifespan,
    )

    _app_log = _logging.getLogger("agents_universe.http")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        _app_log.error(
            "Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    app.add_middleware(StructuredLoggingMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth (public — login/callback/logout don't require a session)
    from .routers.auth import router as auth_router
    app.include_router(auth_router)

    # API routers
    from .routers import agents, api_keys, conversations, integrations, knowledge, mcp_servers, media, memories, model_configs, preferences, project_members, project_secrets, projects, publish, scripts, tier_models, tokens

    app.include_router(agents.router, tags=["agents"])
    app.include_router(projects.router, tags=["projects"])
    app.include_router(conversations.router, tags=["conversations"])
    app.include_router(knowledge.router, tags=["knowledge"])
    app.include_router(tokens.router, prefix="/api/tokens", tags=["tokens"])
    app.include_router(api_keys.router, prefix="/api/api-keys", tags=["api-keys"])
    app.include_router(tier_models.router, prefix="/api/tier-models", tags=["tier-models"])
    app.include_router(model_configs.router, prefix="/api/model-configs", tags=["model-configs"])
    app.include_router(preferences.router, prefix="/api/preferences", tags=["preferences"])
    app.include_router(media.router, tags=["media"])
    app.include_router(scripts.router, tags=["scripts"])
    app.include_router(memories.router, tags=["memories"])
    app.include_router(integrations.router, tags=["integrations"])
    app.include_router(mcp_servers.router, tags=["mcp-servers"])
    app.include_router(project_secrets.router, prefix="/api/projects/{project_id}/secrets", tags=["project-secrets"])
    app.include_router(project_members.router, tags=["project-members"])
    app.include_router(publish.router, tags=["publish"])

    # WebSocket
    from .websocket import handlers
    app.include_router(handlers.router, tags=["websocket"])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        return {"status": "ready"}

    # Serve frontend static files if built
    static_dir = Path(__file__).parent.parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")

    return app


app = create_app()
