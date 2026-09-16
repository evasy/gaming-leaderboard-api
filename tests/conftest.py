"""Test fixtures.

The important idea here is the `store` fixture: it is parameterized over every
backend, so the conformance suite in `test_store_conformance.py` runs unchanged
against both the in-memory index and a real Redis. If Redis is not reachable
those parameters are skipped rather than failed, which keeps `pytest` a
one-command experience on a clean laptop while CI still exercises both.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.core.config import Settings
from app.main import create_app
from app.service import LeaderboardService
from app.store.base import LeaderboardStore
from app.store.memory import MemoryLeaderboardStore
from app.store.redis_store import RedisLeaderboardStore

REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_reachable() -> bool:
    async def check() -> bool:
        client = Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=0.5)
        try:
            await client.ping()
            return True
        except Exception:
            return False
        finally:
            await client.aclose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


REDIS_AVAILABLE = _redis_reachable()
requires_redis = pytest.mark.skipif(not REDIS_AVAILABLE, reason="no Redis at TEST_REDIS_URL")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        store_backend="memory",
        environment="local",
        log_format="console",
        log_level="WARNING",
        max_score=1_000_000,
        min_score=0,
        max_page_size=100,
        max_context_radius=10,
    )


@pytest_asyncio.fixture(params=["memory", "redis"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[LeaderboardStore]:
    """Every backend, behind one fixture."""
    if request.param == "memory":
        yield MemoryLeaderboardStore()
        return

    if not REDIS_AVAILABLE:
        pytest.skip("no Redis at TEST_REDIS_URL")

    client = Redis.from_url(REDIS_URL, decode_responses=True)
    # Unique prefix per test: parallel runs and leftover keys cannot bleed across.
    prefix = f"test:{uuid.uuid4().hex[:8]}"
    backend = RedisLeaderboardStore(client, prefix=prefix)
    try:
        yield backend
    finally:
        keys = [k async for k in client.scan_iter(match=f"{prefix}*", count=500)]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest_asyncio.fixture
async def service(store: LeaderboardStore, settings: Settings) -> LeaderboardService:
    return LeaderboardService(store, settings)


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the real app, over the in-memory backend."""
    app = create_app(settings=settings, store=MemoryLeaderboardStore())
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as http_client,
        app.router.lifespan_context(app),
    ):
        yield http_client
