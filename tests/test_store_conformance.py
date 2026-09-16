"""One suite, every backend.

These tests are the real specification of the storage contract. Because the
`store` fixture is parameterized, each test below runs against the in-memory
index *and* against Redis -- so the two implementations cannot drift apart in
ranking, tie-breaking or score semantics without a test going red.
"""

from __future__ import annotations

import asyncio

import pytest

from app.store.base import LeaderboardStore

GAME = "space-invaders"
BUCKET = "all"


async def _seed(store: LeaderboardStore, scores: dict[str, int]) -> None:
    for user_id, score in scores.items():
        await store.submit(
            game_id=GAME,
            bucket=BUCKET,
            user_id=user_id,
            score=score,
            ttl_seconds=None,
        )


async def test_empty_board_reads_are_empty_not_errors(store: LeaderboardStore) -> None:
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=10, offset=0)
    assert page.entries == []
    assert page.total == 0
    assert await store.get_entry(game_id=GAME, bucket=BUCKET, user_id="nobody") is None
    assert await store.context(game_id=GAME, bucket=BUCKET, user_id="nobody", radius=2) is None


async def test_first_submission_ranks_first(store: LeaderboardStore) -> None:
    outcome = await store.submit(
        game_id=GAME,
        bucket=BUCKET,
        user_id="alice",
        score=100,
        ttl_seconds=None,
    )
    assert outcome.score == 100
    assert outcome.previous_score is None
    assert outcome.rank == 1
    assert outcome.updated is True


async def test_ordering_is_by_descending_score(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 100, "bob": 200, "dave": 50})
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=10, offset=0)
    assert [(e.user_id, e.score, e.rank) for e in page.entries] == [
        ("bob", 200, 1),
        ("alice", 100, 2),
        ("dave", 50, 3),
    ]
    assert page.total == 3


async def test_ties_share_a_rank_and_the_next_score_skips_ahead(
    store: LeaderboardStore,
) -> None:
    """Competition ranking: 1, 2, 2, 4 -- never 1, 2, 2, 3."""
    await _seed(store, {"alice": 100, "bob": 200, "carol": 200, "dave": 50})
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=10, offset=0)
    assert [(e.user_id, e.rank) for e in page.entries] == [
        ("carol", 1),
        ("bob", 1),
        ("alice", 3),
        ("dave", 4),
    ]


async def test_tie_order_is_deterministic_across_backends(store: LeaderboardStore) -> None:
    """Within a tie, order is descending user_id.

    Arbitrary, but stable -- an unstable order would let a player show up on
    two pages, or on none, as a client paginates.
    """
    await _seed(store, {"aaa": 50, "zzz": 50, "mmm": 50})
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=10, offset=0)
    assert [e.user_id for e in page.entries] == ["zzz", "mmm", "aaa"]
    assert {e.rank for e in page.entries} == {1}


async def test_pagination_covers_the_board_exactly_once(store: LeaderboardStore) -> None:
    await _seed(store, {f"p{i:03d}": i for i in range(25)})
    seen: list[str] = []
    for offset in range(0, 25, 7):
        page = await store.top(game_id=GAME, bucket=BUCKET, limit=7, offset=offset)
        assert page.total == 25
        seen.extend(e.user_id for e in page.entries)
    assert len(seen) == 25
    assert len(set(seen)) == 25
    # Ranks stay globally correct, not per-page.
    last = await store.top(game_id=GAME, bucket=BUCKET, limit=7, offset=21)
    assert [e.rank for e in last.entries] == [22, 23, 24, 25]


async def test_offset_past_the_end_returns_empty_page_with_real_total(
    store: LeaderboardStore,
) -> None:
    await _seed(store, {"alice": 10})
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=10, offset=500)
    assert page.entries == []
    assert page.total == 1


@pytest.mark.parametrize(
    ("existing", "incoming", "expected"),
    [
        (100, 150, 150),  # an improvement is kept
        (100, 50, 100),  # a worse run never costs you your high score
        (100, 100, 100),  # resubmitting the same score changes nothing
        (0, 0, 0),  # zero is a real score, not a missing one
    ],
)
async def test_best_score_stands(
    store: LeaderboardStore,
    existing: int,
    incoming: int,
    expected: int,
) -> None:
    await _seed(store, {"alice": existing})
    outcome = await store.submit(
        game_id=GAME,
        bucket=BUCKET,
        user_id="alice",
        score=incoming,
        ttl_seconds=None,
    )
    assert outcome.score == expected
    assert outcome.previous_score == existing
    assert outcome.updated is (expected != existing)


async def test_concurrent_submissions_do_not_lose_the_winning_score(
    store: LeaderboardStore,
) -> None:
    """A read-modify-write race must not lose the winning score.

    This is why the Redis backend does the comparison in Lua rather than
    ZSCORE-then-ZADD from the client.
    """
    await asyncio.gather(
        *[
            store.submit(
                game_id=GAME,
                bucket=BUCKET,
                user_id="alice",
                score=score,
                ttl_seconds=None,
            )
            for score in (10, 900, 250, 40, 700)
        ]
    )
    entry = await store.get_entry(game_id=GAME, bucket=BUCKET, user_id="alice")
    assert entry is not None
    assert entry.score == 900


async def test_get_entry_rank_matches_the_board(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 100, "bob": 200, "carol": 200, "dave": 50})
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=10, offset=0)
    for listed in page.entries:
        single = await store.get_entry(game_id=GAME, bucket=BUCKET, user_id=listed.user_id)
        assert single is not None
        assert single.rank == listed.rank
        assert single.score == listed.score


async def test_context_returns_neighbours_on_both_sides(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 100, "bob": 200, "carol": 200, "dave": 50})
    result = await store.context(game_id=GAME, bucket=BUCKET, user_id="alice", radius=1)
    assert result is not None
    above, user, below, total = result
    assert user.user_id == "alice"
    assert user.rank == 3
    assert [e.user_id for e in above] == ["bob"]
    assert [e.user_id for e in below] == ["dave"]
    assert total == 4


async def test_context_clamps_at_the_top_of_the_board(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 100, "bob": 200, "dave": 50})
    result = await store.context(game_id=GAME, bucket=BUCKET, user_id="bob", radius=5)
    assert result is not None
    above, user, below, _ = result
    assert above == []
    assert user.rank == 1
    assert [e.user_id for e in below] == ["alice", "dave"]


async def test_context_clamps_at_the_bottom_of_the_board(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 100, "bob": 200, "dave": 50})
    result = await store.context(game_id=GAME, bucket=BUCKET, user_id="dave", radius=5)
    assert result is not None
    above, user, below = result[0], result[1], result[2]
    assert [e.user_id for e in above] == ["bob", "alice"]
    assert user.rank == 3
    assert below == []


async def test_context_on_a_single_player_board(store: LeaderboardStore) -> None:
    await _seed(store, {"solo": 1})
    result = await store.context(game_id=GAME, bucket=BUCKET, user_id="solo", radius=3)
    assert result is not None
    above, user, below, total = result
    assert (above, below, total) == ([], [], 1)
    assert user.rank == 1


async def test_display_name_is_attached_to_reads(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 10})
    await store.set_display_name(game_id=GAME, user_id="alice", display_name="Alice A.")
    entry = await store.get_entry(game_id=GAME, bucket=BUCKET, user_id="alice")
    assert entry is not None and entry.display_name == "Alice A."
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=5, offset=0)
    assert page.entries[0].display_name == "Alice A."


async def test_remove_player_erases_them_from_every_bucket(store: LeaderboardStore) -> None:
    for bucket in ("all", "d:2026-09-16"):
        await store.submit(
            game_id=GAME,
            bucket=bucket,
            user_id="alice",
            score=100,
            ttl_seconds=None,
        )
    removed = await store.remove_player(game_id=GAME, user_id="alice")
    assert removed == 2
    for bucket in ("all", "d:2026-09-16"):
        assert await store.get_entry(game_id=GAME, bucket=bucket, user_id="alice") is None
    assert await store.remove_player(game_id=GAME, user_id="alice") == 0


async def test_removing_a_player_reranks_the_rest(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 100, "bob": 200, "dave": 50})
    await store.remove_player(game_id=GAME, user_id="bob")
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=10, offset=0)
    assert [(e.user_id, e.rank) for e in page.entries] == [("alice", 1), ("dave", 2)]


async def test_idempotency_key_is_claimable_exactly_once(store: LeaderboardStore) -> None:
    assert await store.claim_idempotency_key(game_id=GAME, key="k-1", ttl_seconds=60) is True
    assert await store.claim_idempotency_key(game_id=GAME, key="k-1", ttl_seconds=60) is False
    # Scoped per game: the same key is free elsewhere.
    assert await store.claim_idempotency_key(game_id="other", key="k-1", ttl_seconds=60) is True


async def test_games_are_discoverable(store: LeaderboardStore) -> None:
    await _seed(store, {"alice": 1})
    await store.submit(
        game_id="pong",
        bucket=BUCKET,
        user_id="bob",
        score=5,
        ttl_seconds=None,
    )
    assert sorted(await store.list_games()) == ["pong", GAME]


async def test_buckets_are_isolated_from_each_other(store: LeaderboardStore) -> None:
    await store.submit(
        game_id=GAME,
        bucket="all",
        user_id="alice",
        score=100,
        ttl_seconds=None,
    )
    assert await store.get_entry(game_id=GAME, bucket="d:2026-01-01", user_id="alice") is None
    assert await store.total_players(game_id=GAME, bucket="all") == 1
    assert await store.total_players(game_id=GAME, bucket="d:2026-01-01") == 0


async def test_ping_reports_healthy(store: LeaderboardStore) -> None:
    assert await store.ping() is True


async def test_large_board_keeps_ranks_consistent(store: LeaderboardStore) -> None:
    """Sanity check at a size where an accidental O(n log n) sort would show."""
    await _seed(store, {f"u{i:04d}": i for i in range(1000)})
    page = await store.top(game_id=GAME, bucket=BUCKET, limit=5, offset=0)
    assert [e.score for e in page.entries] == [999, 998, 997, 996, 995]
    assert [e.rank for e in page.entries] == [1, 2, 3, 4, 5]
    middle = await store.get_entry(game_id=GAME, bucket=BUCKET, user_id="u0500")
    assert middle is not None and middle.rank == 500
