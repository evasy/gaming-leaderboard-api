"""The storage contract every leaderboard backend must satisfy.

Keeping this abstract is what lets the same service run against an in-process
index for tests and CI, and against Redis in production, with no route changes.

Ranking contract (identical across backends):
  * Rank is 1-based *competition* ranking: tied players share the better rank
    and the next distinct score skips ahead (1, 2, 2, 4).
  * Within a tie, entries are ordered by descending user_id. This is arbitrary
    but *deterministic*, which is what pagination actually requires -- an
    unstable intra-tie order would let a player appear on two pages or neither.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models import ScoreMode


@dataclass(frozen=True, slots=True)
class Entry:
    user_id: str
    score: int
    rank: int
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    score: int
    previous_score: int | None
    rank: int
    updated: bool


@dataclass(frozen=True, slots=True)
class Page:
    entries: list[Entry]
    total: int


class LeaderboardStore(ABC):
    """Storage port. One concrete adapter per backend."""

    backend: str

    @abstractmethod
    async def submit(
        self,
        *,
        game_id: str,
        bucket: str,
        user_id: str,
        score: int,
        mode: ScoreMode,
        ttl_seconds: int | None,
    ) -> SubmitOutcome:
        """Apply a score to one bucket and return the resulting standing."""

    @abstractmethod
    async def set_display_name(self, *, game_id: str, user_id: str, display_name: str) -> None:
        """Record the player's display name (bucket-independent)."""

    @abstractmethod
    async def top(self, *, game_id: str, bucket: str, limit: int, offset: int) -> Page:
        """Return a ranked page of the board, best score first."""

    @abstractmethod
    async def get_entry(self, *, game_id: str, bucket: str, user_id: str) -> Entry | None:
        """Return a single player's standing, or None if they are not on the board."""

    @abstractmethod
    async def context(
        self, *, game_id: str, bucket: str, user_id: str, radius: int
    ) -> tuple[list[Entry], Entry, list[Entry], int] | None:
        """Return (above, user, below, total) for a player's neighbourhood."""

    @abstractmethod
    async def total_players(self, *, game_id: str, bucket: str) -> int:
        """Number of players on a board."""

    @abstractmethod
    async def remove_player(self, *, game_id: str, user_id: str) -> int:
        """Erase a player from every bucket of a game. Returns buckets touched."""

    @abstractmethod
    async def list_games(self) -> list[str]:
        """Known game ids."""

    @abstractmethod
    async def claim_idempotency_key(self, *, game_id: str, key: str, ttl_seconds: int) -> bool:
        """Return True if this key is new (caller should proceed), False on replay."""

    @abstractmethod
    async def ping(self) -> bool:
        """Liveness probe for the backing store."""

    async def close(self) -> None:  # pragma: no cover - default is a no-op
        """Release backend resources."""
        return None


def rank_page(
    rows: list[tuple[str, int]],
    *,
    start_index: int,
    rank_of_first: int,
) -> list[Entry]:
    """Assign competition ranks to an already-sorted page.

    `rank_of_first` is computed by the backend (count of strictly-higher scores
    plus one). Subsequent rows either inherit the previous rank when tied, or
    take their absolute 1-based position.
    """
    entries: list[Entry] = []
    prev_score: int | None = None
    prev_rank = rank_of_first
    for offset, (user_id, score) in enumerate(rows):
        if offset == 0:
            rank = rank_of_first
        elif score == prev_score:
            rank = prev_rank
        else:
            rank = start_index + offset + 1
        entries.append(Entry(user_id=user_id, score=score, rank=rank))
        prev_score, prev_rank = score, rank
    return entries
