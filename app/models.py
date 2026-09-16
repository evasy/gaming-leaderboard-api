"""Request and response schemas.

Validation lives in the schema layer so that malformed input is rejected at the
edge, before it can reach the store.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.windows import Window

# Identifiers are URL path segments and Redis key components: keep them to a
# conservative, unambiguous charset rather than escaping at every call site.
Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$",
        strip_whitespace=True,
    ),
]

DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]


class ScoreMode(StrEnum):
    """How an incoming score combines with the score already on record."""

    BEST = "best"  # keep the higher of the two (default; arcade semantics)
    ABSOLUTE = "absolute"  # overwrite unconditionally (authoritative resync)
    INCREMENT = "increment"  # add to the running total (accumulating seasons)


class SubmitScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Identifier
    score: int = Field(description="Score value. Bounds are enforced against service config.")
    mode: ScoreMode = ScoreMode.BEST
    display_name: DisplayName | None = Field(
        default=None, description="Optional human-readable name shown on the board."
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Optional client-supplied key. Repeating a submission with the same key is a no-op, "
            "which makes retries from flaky mobile networks safe."
        ),
    )


class LeaderboardEntry(BaseModel):
    user_id: str
    display_name: str | None = None
    score: int
    rank: int = Field(description="Competition rank; tied players share a rank (1,2,2,4).")


class SubmitScoreResponse(BaseModel):
    game_id: str
    user_id: str
    window: Window
    score: int = Field(description="The player's score after applying the submission.")
    previous_score: int | None = None
    rank: int
    updated: bool = Field(description="False when the submission did not change the stored score.")
    deduplicated: bool = Field(
        default=False, description="True when this was a replay of a known idempotency key."
    )


class LeaderboardPage(BaseModel):
    game_id: str
    window: Window
    total_players: int
    limit: int
    offset: int
    entries: list[LeaderboardEntry]


class UserContextResponse(BaseModel):
    game_id: str
    window: Window
    total_players: int
    user: LeaderboardEntry
    above: list[LeaderboardEntry] = Field(
        description="Players ranked immediately above, nearest last."
    )
    below: list[LeaderboardEntry] = Field(
        description="Players ranked immediately below, nearest first."
    )


class GameSummary(BaseModel):
    game_id: str
    total_players: int


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    version: str


class ReadinessResponse(BaseModel):
    status: str
    backend: str
    store_reachable: bool
    latency_ms: float | None = None
