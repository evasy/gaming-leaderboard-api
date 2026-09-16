"""Backend selection.

The only place in the codebase that knows which store implementations exist.
"""

from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from app.core.config import Settings
from app.store.base import LeaderboardStore
from app.store.memory import MemoryLeaderboardStore
from app.store.redis_store import RedisLeaderboardStore


def build_store(settings: Settings) -> LeaderboardStore:
    if settings.store_backend == "redis":
        pool: ConnectionPool = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            decode_responses=True,
            health_check_interval=30,
        )
        return RedisLeaderboardStore(Redis(connection_pool=pool), prefix=settings.redis_key_prefix)
    return MemoryLeaderboardStore()
