"""Redis-backed leaderboard: the production adapter.

Why Redis sorted sets
---------------------
A leaderboard is exactly the access pattern a ZSET is built for:

    ZADD      write a score                       O(log N)
    ZCOUNT    players above a score  -> rank      O(log N)
    ZRANGE    a page of the board                 O(log N + page)
    ZREVRANK  a player's position                 O(log N)

Ranking stays correct without ever sorting the whole board, and state lives
outside the process, so the API tier is stateless and horizontally scalable.

Key layout
----------
    lb:{game}:board:<bucket>   ZSET   user_id -> score
    lb:{game}:names            HASH   user_id -> display name
    lb:{game}:buckets          SET    known buckets, for cascade deletes
    lb:{game}:idem:<key>       STRING idempotency claim, TTL-bounded
    lb:games                   SET    known game ids

The `{game}` braces are a Redis Cluster hash tag: every key belonging to one
game lands in the same slot, so multi-key scripts stay legal if this is ever
moved onto a clustered deployment.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import StoreUnavailableError
from app.models import ScoreMode
from app.store.base import Entry, LeaderboardStore, Page, SubmitOutcome, rank_page

# Read-modify-write of a score must be atomic: two concurrent "best score"
# submissions for the same player would otherwise race and lose one update.
# Doing it server-side in Lua also collapses 4 round trips into 1.
_SUBMIT_LUA = """
local key    = KEYS[1]
local member = ARGV[1]
local value  = tonumber(ARGV[2])
local mode   = ARGV[3]
local ttl    = tonumber(ARGV[4])

local prev = redis.call('ZSCORE', key, member)
local new_score

if mode == 'increment' then
  new_score = (prev and tonumber(prev) or 0) + value
elseif mode == 'absolute' then
  new_score = value
else
  if prev == false then
    new_score = value
  else
    new_score = math.max(tonumber(prev), value)
  end
end

local updated = 0
if prev == false or tonumber(prev) ~= new_score then
  redis.call('ZADD', key, new_score, member)
  updated = 1
end

if ttl > 0 then
  redis.call('EXPIRE', key, ttl)
end

local above = redis.call('ZCOUNT', key, '(' .. new_score, '+inf')
return { prev and tostring(prev) or '', tostring(new_score), updated, above }
"""


def _as_int(value: float | str) -> int:
    """ZSET scores are IEEE doubles; the domain is integral."""
    return round(float(value))


class RedisLeaderboardStore(LeaderboardStore):
    backend = "redis"

    def __init__(self, client: Redis, *, prefix: str = "lb") -> None:
        self._redis = client
        self._prefix = prefix
        self._submit = client.register_script(_SUBMIT_LUA)

    # -- key helpers -------------------------------------------------------

    def _ns(self, game_id: str) -> str:
        return f"{self._prefix}:{{{game_id}}}"

    def _board_key(self, game_id: str, bucket: str) -> str:
        return f"{self._ns(game_id)}:board:{bucket}"

    def _names_key(self, game_id: str) -> str:
        return f"{self._ns(game_id)}:names"

    def _buckets_key(self, game_id: str) -> str:
        return f"{self._ns(game_id)}:buckets"

    def _idem_key(self, game_id: str, key: str) -> str:
        return f"{self._ns(game_id)}:idem:{key}"

    @property
    def _games_key(self) -> str:
        return f"{self._prefix}:games"

    # -- writes ------------------------------------------------------------

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
        key = self._board_key(game_id, bucket)
        try:
            raw: list[Any] = await self._submit(
                keys=[key],
                args=[user_id, score, mode.value, ttl_seconds or 0],
            )
            # Registering the game/bucket is bookkeeping for listings and
            # cascade deletes; it is intentionally outside the atomic section.
            pipe = self._redis.pipeline(transaction=False)
            pipe.sadd(self._games_key, game_id)
            pipe.sadd(self._buckets_key(game_id), bucket)
            await pipe.execute()
        except RedisError as exc:
            raise StoreUnavailableError(f"redis submit failed: {exc}") from exc

        prev_raw, new_raw, updated, above = raw
        prev_str = prev_raw.decode() if isinstance(prev_raw, bytes) else str(prev_raw)
        new_str = new_raw.decode() if isinstance(new_raw, bytes) else str(new_raw)
        return SubmitOutcome(
            score=_as_int(new_str),
            previous_score=_as_int(prev_str) if prev_str else None,
            rank=int(above) + 1,
            updated=bool(updated),
        )

    async def set_display_name(self, *, game_id: str, user_id: str, display_name: str) -> None:
        try:
            await self._redis.hset(self._names_key(game_id), user_id, display_name)
        except RedisError as exc:
            raise StoreUnavailableError(f"redis hset failed: {exc}") from exc

    async def remove_player(self, *, game_id: str, user_id: str) -> int:
        try:
            buckets = await self._redis.smembers(self._buckets_key(game_id))
            if not buckets:
                return 0
            pipe = self._redis.pipeline(transaction=False)
            for bucket in buckets:
                pipe.zrem(self._board_key(game_id, _text(bucket)), user_id)
            pipe.hdel(self._names_key(game_id), user_id)
            results = await pipe.execute()
        except RedisError as exc:
            raise StoreUnavailableError(f"redis remove failed: {exc}") from exc
        return sum(1 for removed in results[: len(buckets)] if removed)

    async def claim_idempotency_key(self, *, game_id: str, key: str, ttl_seconds: int) -> bool:
        try:
            claimed = await self._redis.set(
                self._idem_key(game_id, key), "1", nx=True, ex=ttl_seconds
            )
        except RedisError as exc:
            raise StoreUnavailableError(f"redis set failed: {exc}") from exc
        return bool(claimed)

    # -- reads -------------------------------------------------------------

    async def _names_for(self, game_id: str, user_ids: list[str]) -> dict[str, str | None]:
        if not user_ids:
            return {}
        values = await self._redis.hmget(self._names_key(game_id), user_ids)
        return {
            uid: (_text(value) if value is not None else None)
            for uid, value in zip(user_ids, values, strict=True)
        }

    def _decorate(self, entries: list[Entry], names: dict[str, str | None]) -> list[Entry]:
        return [
            Entry(
                user_id=e.user_id,
                score=e.score,
                rank=e.rank,
                display_name=names.get(e.user_id),
            )
            for e in entries
        ]

    async def top(self, *, game_id: str, bucket: str, limit: int, offset: int) -> Page:
        key = self._board_key(game_id, bucket)
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.zrevrange(key, offset, offset + limit - 1, withscores=True)
            pipe.zcard(key)
            rows_raw, total = await pipe.execute()
            if not rows_raw:
                return Page(entries=[], total=int(total))
            rows = [(_text(m), _as_int(s)) for m, s in rows_raw]
            above = await self._redis.zcount(key, f"({rows[0][1]}", "+inf")
            names = await self._names_for(game_id, [uid for uid, _ in rows])
        except RedisError as exc:
            raise StoreUnavailableError(f"redis read failed: {exc}") from exc

        entries = rank_page(rows, start_index=offset, rank_of_first=int(above) + 1)
        return Page(entries=self._decorate(entries, names), total=int(total))

    async def get_entry(self, *, game_id: str, bucket: str, user_id: str) -> Entry | None:
        key = self._board_key(game_id, bucket)
        try:
            score = await self._redis.zscore(key, user_id)
            if score is None:
                return None
            value = _as_int(score)
            pipe = self._redis.pipeline(transaction=False)
            pipe.zcount(key, f"({value}", "+inf")
            pipe.hget(self._names_key(game_id), user_id)
            above, name = await pipe.execute()
        except RedisError as exc:
            raise StoreUnavailableError(f"redis read failed: {exc}") from exc
        return Entry(
            user_id=user_id,
            score=value,
            rank=int(above) + 1,
            display_name=_text(name) if name is not None else None,
        )

    async def context(
        self, *, game_id: str, bucket: str, user_id: str, radius: int
    ) -> tuple[list[Entry], Entry, list[Entry], int] | None:
        key = self._board_key(game_id, bucket)
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.zrevrank(key, user_id)
            pipe.zcard(key)
            index, total = await pipe.execute()
            if index is None:
                return None

            start = max(0, int(index) - radius)
            stop = int(index) + radius
            rows_raw = await self._redis.zrevrange(key, start, stop, withscores=True)
            if not rows_raw:
                return None
            rows = [(_text(m), _as_int(s)) for m, s in rows_raw]
            above = await self._redis.zcount(key, f"({rows[0][1]}", "+inf")
            names = await self._names_for(game_id, [uid for uid, _ in rows])
        except RedisError as exc:
            raise StoreUnavailableError(f"redis read failed: {exc}") from exc

        entries = self._decorate(
            rank_page(rows, start_index=start, rank_of_first=int(above) + 1), names
        )
        pivot = int(index) - start
        return entries[:pivot], entries[pivot], entries[pivot + 1 :], int(total)

    async def total_players(self, *, game_id: str, bucket: str) -> int:
        try:
            return int(await self._redis.zcard(self._board_key(game_id, bucket)))
        except RedisError as exc:
            raise StoreUnavailableError(f"redis zcard failed: {exc}") from exc

    async def list_games(self) -> list[str]:
        try:
            return sorted(_text(g) for g in await self._redis.smembers(self._games_key))
        except RedisError as exc:
            raise StoreUnavailableError(f"redis smembers failed: {exc}") from exc

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        await self._redis.aclose()  # type: ignore[attr-defined]


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
