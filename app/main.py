"""Application factory and process entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health, routes
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.observability import (
    RequestContextMiddleware,
    configure_logging,
    logger,
)
from app.store.base import LeaderboardStore
from app.store.factory import build_store

DESCRIPTION = """
A REST API for real-time global gaming leaderboards.

* **Submit scores** with `best` / `absolute` / `increment` semantics and optional idempotency.
* **Read the top X** players for a game, paginated.
* **Read a player's surroundings** -- their rank plus neighbours above and below.
* **All-time, daily and weekly** boards are maintained from a single submission.

Ranking is 1-based *competition* ranking: tied players share a rank and the next
distinct score skips ahead (1, 2, 2, 4).
"""


def create_app(settings: Settings | None = None, store: LeaderboardStore | None = None) -> FastAPI:
    """Build the app.

    `store` is injectable so tests can run the identical app object against any
    backend without monkeypatching.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from app.service import LeaderboardService

        backing = store or build_store(settings)
        app.state.store = backing
        app.state.service = LeaderboardService(backing, settings)
        logger.info(
            "service_started",
            backend=backing.backend,
            environment=settings.environment,
        )
        try:
            yield
        finally:
            await backing.close()
            logger.info("service_stopped")

    app = FastAPI(
        title="Global Gaming Leaderboard API",
        description=DESCRIPTION,
        version=health.VERSION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(routes.router)
    return app


app = create_app()
