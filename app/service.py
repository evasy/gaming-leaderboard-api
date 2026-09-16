"""Leaderboard use cases.

Routes stay thin (HTTP concerns only) and stores stay dumb (persistence only);
the rules that make this a *leaderboard* rather than a sorted set live here:
window fan-out, score bounds, idempotent retries, and rank assembly.
"""

from __future__ import annotations

import time

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.core.observability import (
    logger,
    score_submissions_total,
    store_operation_duration_seconds,
)
from app.core.windows import Window, ttl_for, window_suffix
from app.models import (
    GameSummary,
    LeaderboardEntry,
    LeaderboardPage,
    ScoreMode,
    SubmitScoreRequest,
    SubmitScoreResponse,
    UserContextResponse,
)
from app.store.base import Entry, LeaderboardStore


def _to_model(entry: Entry) -> LeaderboardEntry:
    return LeaderboardEntry(
        user_id=entry.user_id,
        display_name=entry.display_name,
        score=entry.score,
        rank=entry.rank,
    )


class LeaderboardService:
    def __init__(self, store: LeaderboardStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    @property
    def store(self) -> LeaderboardStore:
        return self._store

    # -- helpers -----------------------------------------------------------

    def _bucket(self, window: Window) -> str:
        return window_suffix(window)

    def _ttl(self, window: Window) -> int | None:
        return ttl_for(
            window,
            daily=self._settings.daily_ttl_seconds,
            weekly=self._settings.weekly_ttl_seconds,
        )

    def _validate_score(self, score: int, mode: ScoreMode) -> None:
        s = self._settings
        if mode is ScoreMode.INCREMENT:
            # A delta may legitimately be negative (a penalty), but it must not
            # be large enough to push a total outside the configured domain.
            if abs(score) > s.max_score:
                raise ValidationError(
                    f"increment magnitude must be <= {s.max_score}, got {score}",
                    field="score",
                )
            return
        if not (s.min_score <= score <= s.max_score):
            raise ValidationError(
                f"score must be between {s.min_score} and {s.max_score}, got {score}",
                field="score",
            )

    def _validate_page(self, limit: int, offset: int) -> None:
        if limit < 1 or limit > self._settings.max_page_size:
            raise ValidationError(
                f"limit must be between 1 and {self._settings.max_page_size}", field="limit"
            )
        if offset < 0:
            raise ValidationError("offset must be >= 0", field="offset")

    async def _timed(self, operation: str, coro):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await coro
        finally:
            store_operation_duration_seconds.labels(self._store.backend, operation).observe(
                time.perf_counter() - started
            )

    # -- use cases ---------------------------------------------------------

    async def submit_score(
        self, *, game_id: str, payload: SubmitScoreRequest
    ) -> list[SubmitScoreResponse]:
        """Apply one submission to every window and report the new standing."""
        self._validate_score(payload.score, payload.mode)

        if payload.idempotency_key:
            fresh = await self._store.claim_idempotency_key(
                game_id=game_id,
                key=payload.idempotency_key,
                ttl_seconds=self._settings.idempotency_ttl_seconds,
            )
            if not fresh:
                # A retry of a submission we already applied. Report current
                # standings rather than double-counting an INCREMENT.
                logger.info(
                    "submission_deduplicated",
                    game_id=game_id,
                    user_id=payload.user_id,
                    idempotency_key=payload.idempotency_key,
                )
                score_submissions_total.labels(game_id, payload.mode.value, "deduplicated").inc()
                return await self._current_standings(game_id, payload.user_id)

        if payload.display_name:
            await self._store.set_display_name(
                game_id=game_id, user_id=payload.user_id, display_name=payload.display_name
            )

        responses: list[SubmitScoreResponse] = []
        for window in Window:
            outcome = await self._timed(
                "submit",
                self._store.submit(
                    game_id=game_id,
                    bucket=self._bucket(window),
                    user_id=payload.user_id,
                    score=payload.score,
                    mode=payload.mode,
                    ttl_seconds=self._ttl(window),
                ),
            )
            responses.append(
                SubmitScoreResponse(
                    game_id=game_id,
                    user_id=payload.user_id,
                    window=window,
                    score=outcome.score,
                    previous_score=outcome.previous_score,
                    rank=outcome.rank,
                    updated=outcome.updated,
                )
            )

        all_time = responses[0]
        score_submissions_total.labels(
            game_id, payload.mode.value, "applied" if all_time.updated else "unchanged"
        ).inc()
        logger.info(
            "score_submitted",
            game_id=game_id,
            user_id=payload.user_id,
            mode=payload.mode.value,
            score=all_time.score,
            rank=all_time.rank,
            updated=all_time.updated,
        )
        return responses

    async def _current_standings(self, game_id: str, user_id: str) -> list[SubmitScoreResponse]:
        responses: list[SubmitScoreResponse] = []
        for window in Window:
            entry = await self._store.get_entry(
                game_id=game_id, bucket=self._bucket(window), user_id=user_id
            )
            responses.append(
                SubmitScoreResponse(
                    game_id=game_id,
                    user_id=user_id,
                    window=window,
                    score=entry.score if entry else 0,
                    previous_score=entry.score if entry else None,
                    rank=entry.rank if entry else 0,
                    updated=False,
                    deduplicated=True,
                )
            )
        return responses

    async def top(
        self, *, game_id: str, window: Window, limit: int, offset: int
    ) -> LeaderboardPage:
        self._validate_page(limit, offset)
        page = await self._timed(
            "top",
            self._store.top(
                game_id=game_id, bucket=self._bucket(window), limit=limit, offset=offset
            ),
        )
        return LeaderboardPage(
            game_id=game_id,
            window=window,
            total_players=page.total,
            limit=limit,
            offset=offset,
            entries=[_to_model(e) for e in page.entries],
        )

    async def get_user(self, *, game_id: str, window: Window, user_id: str) -> LeaderboardEntry:
        entry = await self._timed(
            "get_entry",
            self._store.get_entry(game_id=game_id, bucket=self._bucket(window), user_id=user_id),
        )
        if entry is None:
            raise NotFoundError(
                f"user '{user_id}' has no score on the '{window.value}' board for game '{game_id}'"
            )
        return _to_model(entry)

    async def user_context(
        self, *, game_id: str, window: Window, user_id: str, radius: int
    ) -> UserContextResponse:
        if radius < 1 or radius > self._settings.max_context_radius:
            raise ValidationError(
                f"radius must be between 1 and {self._settings.max_context_radius}", field="radius"
            )
        result = await self._timed(
            "context",
            self._store.context(
                game_id=game_id, bucket=self._bucket(window), user_id=user_id, radius=radius
            ),
        )
        if result is None:
            raise NotFoundError(
                f"user '{user_id}' has no score on the '{window.value}' board for game '{game_id}'"
            )
        above, user, below, total = result
        return UserContextResponse(
            game_id=game_id,
            window=window,
            total_players=total,
            user=_to_model(user),
            above=[_to_model(e) for e in above],
            below=[_to_model(e) for e in below],
        )

    async def delete_user(self, *, game_id: str, user_id: str) -> int:
        removed = await self._timed(
            "remove_player", self._store.remove_player(game_id=game_id, user_id=user_id)
        )
        if removed == 0:
            raise NotFoundError(f"user '{user_id}' has no scores for game '{game_id}'")
        logger.info("player_removed", game_id=game_id, user_id=user_id, buckets=removed)
        return removed

    async def list_games(self) -> list[GameSummary]:
        game_ids = await self._store.list_games()
        summaries = []
        for game_id in game_ids:
            total = await self._store.total_players(
                game_id=game_id, bucket=self._bucket(Window.ALL_TIME)
            )
            summaries.append(GameSummary(game_id=game_id, total_players=total))
        return summaries
