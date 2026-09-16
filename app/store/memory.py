"""In-process leaderboard index.

Used as the default for local development and as the backend for the bulk of
the test suite: it needs no daemon, so `pytest` is a single command on a clean
machine and CI has nothing to orchestrate.

It is deliberately *not* a toy dict-and-sort: the ordering structure mirrors
what Redis does with a sorted set, so the complexity story is the same and the
two backends can be held to one shared conformance suite.

  submit / get_entry / rank lookup : O(log n)
  top(limit, offset)               : O(log n + limit)

Not suitable for production: state is per-process, so it neither survives a
restart nor works behind more than one replica. That is exactly the trade-off
the Redis backend exists to resolve.
"""

from __future__ import annotations

import asyncio
import time
from functools import total_ordering
from typing import Any

from sortedcontainers import SortedList

from app.store.base import Entry, LeaderboardStore, Page, SubmitOutcome, rank_page


@total_ordering
class _DescStr:
    """A string that sorts in reverse, so ties order by descending user_id.

    This is what keeps the in-memory ordering byte-identical to Redis, which
    returns members in reverse-lexicographic order within equal scores.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _DescStr) and other.value == self.value

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, _LowSentinel):
            return False
        return self.value > other.value  # reversed on purpose

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_DescStr({self.value!r})"


@total_ordering
class _LowSentinel:
    """Sorts before every `_DescStr`; used as a bisect boundary."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _LowSentinel)

    def __lt__(self, other: Any) -> bool:
        return not isinstance(other, _LowSentinel)

    def __hash__(self) -> int:
        return hash("__low__")


_LOW = _LowSentinel()


class _Board:
    """One (game, bucket) pair: a score map plus a sorted index over it."""

    __slots__ = ("expires_at", "order", "scores")

    def __init__(self) -> None:
        self.scores: dict[str, int] = {}
        # Sorted by (-score, reversed user_id) => descending score, descending id.
        self.order: SortedList = SortedList()
        self.expires_at: float | None = None

    def set_score(self, user_id: str, score: int) -> None:
        old = self.scores.get(user_id)
        if old is not None:
            self.order.remove((-old, _DescStr(user_id)))
        self.scores[user_id] = score
        self.order.add((-score, _DescStr(user_id)))

    def remove(self, user_id: str) -> bool:
        old = self.scores.pop(user_id, None)
        if old is None:
            return False
        self.order.remove((-old, _DescStr(user_id)))
        return True

    def count_above(self, score: int) -> int:
        """Players with a strictly higher score -- i.e. competition rank minus one."""
        return self.order.bisect_left((-score, _LOW))

    def index_of(self, user_id: str, score: int) -> int:
        return self.order.index((-score, _DescStr(user_id)))

    def slice(self, start: int, stop: int) -> list[tuple[str, int]]:
        return [(item[1].value, -item[0]) for item in self.order[start:stop]]


class MemoryLeaderboardStore(LeaderboardStore):
    backend = "memory"

    def __init__(self) -> None:
        self._boards: dict[tuple[str, str], _Board] = {}
        self._names: dict[tuple[str, str], str] = {}
        self._idempotency: dict[tuple[str, str], float] = {}
        # All mutations funnel through one lock. The critical sections are pure
        # CPU with no awaits, so this never blocks the event loop meaningfully.
        self._lock = asyncio.Lock()

    # -- internals ---------------------------------------------------------

    def _board(self, game_id: str, bucket: str, *, create: bool) -> _Board | None:
        key = (game_id, bucket)
        board = self._boards.get(key)
        if board is not None and board.expires_at is not None and board.expires_at <= time.time():
            del self._boards[key]
            board = None
        if board is None and create:
            board = _Board()
            self._boards[key] = board
        return board

    # -- writes ------------------------------------------------------------

    async def submit(
        self,
        *,
        game_id: str,
        bucket: str,
        user_id: str,
        score: int,
        ttl_seconds: int | None,
    ) -> SubmitOutcome:
        async with self._lock:
            board = self._board(game_id, bucket, create=True)
            assert board is not None
            if ttl_seconds is not None:
                board.expires_at = time.time() + ttl_seconds

            previous = board.scores.get(user_id)
            new_score = score if previous is None else max(previous, score)

            updated = previous != new_score
            if updated or previous is None:
                board.set_score(user_id, new_score)

            return SubmitOutcome(
                score=new_score,
                previous_score=previous,
                rank=board.count_above(new_score) + 1,
                updated=updated,
            )

    async def set_display_name(self, *, game_id: str, user_id: str, display_name: str) -> None:
        async with self._lock:
            self._names[(game_id, user_id)] = display_name

    async def remove_player(self, *, game_id: str, user_id: str) -> int:
        async with self._lock:
            removed = 0
            for (g, _bucket), board in list(self._boards.items()):
                if g == game_id and board.remove(user_id):
                    removed += 1
            self._names.pop((game_id, user_id), None)
            return removed

    async def claim_idempotency_key(self, *, game_id: str, key: str, ttl_seconds: int) -> bool:
        async with self._lock:
            now = time.time()
            composite = (game_id, key)
            expiry = self._idempotency.get(composite)
            if expiry is not None and expiry > now:
                return False
            self._idempotency[composite] = now + ttl_seconds
            # Opportunistic sweep keeps the dict from growing without bound.
            if len(self._idempotency) > 10_000:
                self._idempotency = {k: v for k, v in self._idempotency.items() if v > now}
            return True

    # -- reads -------------------------------------------------------------

    def _decorate(self, game_id: str, entries: list[Entry]) -> list[Entry]:
        return [
            Entry(
                user_id=e.user_id,
                score=e.score,
                rank=e.rank,
                display_name=self._names.get((game_id, e.user_id)),
            )
            for e in entries
        ]

    async def top(self, *, game_id: str, bucket: str, limit: int, offset: int) -> Page:
        async with self._lock:
            board = self._board(game_id, bucket, create=False)
            if board is None or offset >= len(board.order):
                return Page(entries=[], total=0 if board is None else len(board.order))
            rows = board.slice(offset, offset + limit)
            rank_of_first = board.count_above(rows[0][1]) + 1
            entries = rank_page(rows, start_index=offset, rank_of_first=rank_of_first)
            return Page(entries=self._decorate(game_id, entries), total=len(board.order))

    async def get_entry(self, *, game_id: str, bucket: str, user_id: str) -> Entry | None:
        async with self._lock:
            board = self._board(game_id, bucket, create=False)
            if board is None:
                return None
            score = board.scores.get(user_id)
            if score is None:
                return None
            return Entry(
                user_id=user_id,
                score=score,
                rank=board.count_above(score) + 1,
                display_name=self._names.get((game_id, user_id)),
            )

    async def context(
        self, *, game_id: str, bucket: str, user_id: str, radius: int
    ) -> tuple[list[Entry], Entry, list[Entry], int] | None:
        async with self._lock:
            board = self._board(game_id, bucket, create=False)
            if board is None:
                return None
            score = board.scores.get(user_id)
            if score is None:
                return None

            index = board.index_of(user_id, score)
            start = max(0, index - radius)
            stop = min(len(board.order), index + radius + 1)
            rows = board.slice(start, stop)
            rank_of_first = board.count_above(rows[0][1]) + 1
            entries = self._decorate(
                game_id, rank_page(rows, start_index=start, rank_of_first=rank_of_first)
            )

            pivot = index - start
            return entries[:pivot], entries[pivot], entries[pivot + 1 :], len(board.order)

    async def total_players(self, *, game_id: str, bucket: str) -> int:
        async with self._lock:
            board = self._board(game_id, bucket, create=False)
            return 0 if board is None else len(board.order)

    async def list_games(self) -> list[str]:
        async with self._lock:
            return sorted({game_id for game_id, _ in self._boards})

    async def ping(self) -> bool:
        return True
