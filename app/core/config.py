"""Application configuration, sourced from environment variables.

Every knob the service exposes lives here so that behaviour is configurable
without a code change -- a hard requirement for running the same artifact
across dev, staging and production.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LEADERBOARD_",
        extra="ignore",
    )

    # --- service identity -------------------------------------------------
    service_name: str = "leaderboard"
    environment: Literal["local", "dev", "staging", "production"] = "local"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- storage ----------------------------------------------------------
    # "memory" is the zero-dependency default used by tests and local dev.
    # "redis" is the production backend (sorted sets => O(log N) rank ops).
    store_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50
    redis_socket_timeout_seconds: float = 2.0
    redis_key_prefix: str = "lb"

    # --- domain limits ----------------------------------------------------
    max_score: int = Field(default=1_000_000_000, gt=0)
    min_score: int = Field(default=0)
    max_page_size: int = Field(default=1000, gt=0)
    default_page_size: int = Field(default=10, gt=0)
    max_context_radius: int = Field(default=50, gt=0)

    # Retention for time-windowed boards. All-time never expires.
    daily_ttl_seconds: int = 60 * 60 * 24 * 8
    weekly_ttl_seconds: int = 60 * 60 * 24 * 35
    idempotency_ttl_seconds: int = 60 * 60 * 24

    # --- http -------------------------------------------------------------
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])
    docs_enabled: bool = True

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


@lru_cache
def get_settings() -> Settings:
    """Cached so the process parses the environment exactly once."""
    return Settings()
