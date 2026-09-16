"""HTTP-level tests: status codes, payload shapes, and error contracts.

These exercise the app exactly as a client would -- through routing,
validation, dependency injection and the exception handlers.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

GAME = "space-invaders"


async def _submit(client: AsyncClient, user_id: str, score: int, **extra: object) -> dict:
    response = await client.post(
        f"/v1/games/{GAME}/scores",
        json={"user_id": user_id, "score": score, **extra},
    )
    assert response.status_code == 201, response.text
    return {row["window"]: row for row in response.json()}


# --- happy paths ---------------------------------------------------------


async def test_submit_score_returns_standing_on_every_window(client: AsyncClient) -> None:
    windows = await _submit(client, "alice", 500)
    assert set(windows) == {"all_time", "daily", "weekly"}
    for row in windows.values():
        assert row["score"] == 500
        assert row["rank"] == 1
        assert row["updated"] is True
        assert row["previous_score"] is None


async def test_top_x_is_ordered_and_ranked(client: AsyncClient) -> None:
    for user_id, score in [("alice", 100), ("bob", 300), ("carol", 200)]:
        await _submit(client, user_id, score)

    response = await client.get(f"/v1/games/{GAME}/leaderboard", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total_players"] == 3
    assert body["limit"] == 2
    assert body["window"] == "all_time"
    assert [(e["user_id"], e["rank"]) for e in body["entries"]] == [("bob", 1), ("carol", 2)]


async def test_leaderboard_defaults_to_top_ten(client: AsyncClient) -> None:
    for i in range(15):
        await _submit(client, f"p{i:02d}", i)
    body = (await client.get(f"/v1/games/{GAME}/leaderboard")).json()
    assert len(body["entries"]) == 10
    assert body["entries"][0]["score"] == 14


async def test_user_context_includes_neighbours(client: AsyncClient) -> None:
    for i in range(10):
        await _submit(client, f"p{i:02d}", i * 10)

    response = await client.get(f"/v1/games/{GAME}/users/p05/context", params={"radius": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["user_id"] == "p05"
    assert body["user"]["rank"] == 5
    assert [e["user_id"] for e in body["above"]] == ["p07", "p06"]
    assert [e["user_id"] for e in body["below"]] == ["p04", "p03"]
    assert body["total_players"] == 10


async def test_get_single_user_rank(client: AsyncClient) -> None:
    await _submit(client, "alice", 100, display_name="Alice A.")
    await _submit(client, "bob", 200)
    body = (await client.get(f"/v1/games/{GAME}/users/alice")).json()
    assert body == {"user_id": "alice", "display_name": "Alice A.", "score": 100, "rank": 2}


async def test_a_worse_score_is_accepted_but_changes_nothing(client: AsyncClient) -> None:
    """A bad run never costs a player their high score."""
    await _submit(client, "alice", 500)
    windows = await _submit(client, "alice", 100)
    assert windows["all_time"]["score"] == 500
    assert windows["all_time"]["updated"] is False
    assert windows["all_time"]["previous_score"] == 500


async def test_idempotency_key_suppresses_the_write(client: AsyncClient) -> None:
    """A replayed key is discarded before it reaches the board.

    The replay deliberately carries a *better* score than the original. Under
    best-score semantics a duplicate of the same value would be absorbed
    harmlessly and prove nothing, so the only way the board can still read 100
    is if the idempotency key actually gated the write.
    """
    first = await _submit(client, "alice", 100, idempotency_key="req-1")
    assert first["all_time"]["score"] == 100

    replay = await client.post(
        f"/v1/games/{GAME}/scores",
        json={"user_id": "alice", "score": 999, "idempotency_key": "req-1"},
    )
    assert replay.status_code == 201
    rows = {r["window"]: r for r in replay.json()}
    assert rows["all_time"]["deduplicated"] is True
    assert rows["all_time"]["updated"] is False
    assert (await client.get(f"/v1/games/{GAME}/users/alice")).json()["score"] == 100


async def test_a_fresh_idempotency_key_is_applied(client: AsyncClient) -> None:
    """The guard must reject replays without swallowing genuine submissions."""
    await _submit(client, "alice", 100, idempotency_key="req-1")
    windows = await _submit(client, "alice", 999, idempotency_key="req-2")
    assert windows["all_time"]["score"] == 999
    assert windows["all_time"]["deduplicated"] is False


async def test_idempotency_keys_are_scoped_per_game(client: AsyncClient) -> None:
    await _submit(client, "alice", 100, idempotency_key="shared")
    response = await client.post(
        "/v1/games/pong/scores",
        json={"user_id": "alice", "score": 42, "idempotency_key": "shared"},
    )
    assert response.status_code == 201
    assert {r["window"]: r for r in response.json()}["all_time"]["score"] == 42


async def test_delete_user_removes_them_from_the_board(client: AsyncClient) -> None:
    await _submit(client, "alice", 100)
    assert (await client.delete(f"/v1/games/{GAME}/users/alice")).status_code == 204
    assert (await client.get(f"/v1/games/{GAME}/users/alice")).status_code == 404


async def test_list_games(client: AsyncClient) -> None:
    await _submit(client, "alice", 10)
    await client.post("/v1/games/pong/scores", json={"user_id": "bob", "score": 3})
    body = (await client.get("/v1/games")).json()
    assert {g["game_id"] for g in body} == {GAME, "pong"}


# --- validation and error contracts --------------------------------------


async def test_unknown_user_is_404_with_problem_json(client: AsyncClient) -> None:
    response = await client.get(f"/v1/games/{GAME}/users/ghost")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "not_found"
    assert body["status"] == 404
    assert "ghost" in body["detail"]
    assert body["request_id"]


async def test_empty_leaderboard_is_200_not_404(client: AsyncClient) -> None:
    """An empty board is a valid state, not a client error."""
    response = await client.get("/v1/games/brand-new-game/leaderboard")
    assert response.status_code == 200
    assert response.json()["entries"] == []
    assert response.json()["total_players"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "alice"},  # missing score
        {"score": 10},  # missing user_id
        {"user_id": "", "score": 10},  # empty user id
        {"user_id": "bad id!", "score": 10},  # illegal characters
        {"user_id": "a" * 65, "score": 10},  # too long
        {"user_id": "alice", "score": "abc"},  # wrong type
        {"user_id": "alice", "score": 10, "display_name": ""},  # empty display name
        {"user_id": "alice", "score": 10, "extra": True},  # unexpected field
    ],
)
async def test_malformed_submissions_are_rejected(client: AsyncClient, payload: dict) -> None:
    response = await client.post(f"/v1/games/{GAME}/scores", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["errors"]


async def test_score_outside_configured_bounds_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        f"/v1/games/{GAME}/scores", json={"user_id": "alice", "score": 10**9}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


async def test_negative_score_is_rejected(client: AsyncClient) -> None:
    response = await client.post(f"/v1/games/{GAME}/scores", json={"user_id": "alice", "score": -5})
    assert response.status_code == 422


@pytest.mark.parametrize("limit", [0, -1, 5000])
async def test_bad_page_size_is_rejected(client: AsyncClient, limit: int) -> None:
    response = await client.get(f"/v1/games/{GAME}/leaderboard", params={"limit": limit})
    assert response.status_code == 422


async def test_bad_window_is_rejected(client: AsyncClient) -> None:
    response = await client.get(f"/v1/games/{GAME}/leaderboard", params={"window": "fortnightly"})
    assert response.status_code == 422


async def test_bad_radius_is_rejected(client: AsyncClient) -> None:
    await _submit(client, "alice", 10)
    response = await client.get(f"/v1/games/{GAME}/users/alice/context", params={"radius": 0})
    assert response.status_code == 422


async def test_illegal_game_id_in_path_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/v1/games/not%20a%20game/leaderboard")
    assert response.status_code == 422


async def test_deleting_an_unknown_user_is_404(client: AsyncClient) -> None:
    assert (await client.delete(f"/v1/games/{GAME}/users/ghost")).status_code == 404


# --- windows -------------------------------------------------------------


async def test_windows_are_independent_boards(client: AsyncClient) -> None:
    await _submit(client, "alice", 100)
    for window in ("all_time", "daily", "weekly"):
        body = (await client.get(f"/v1/games/{GAME}/leaderboard", params={"window": window})).json()
        assert body["window"] == window
        assert body["entries"][0]["user_id"] == "alice"


# --- operational endpoints ----------------------------------------------


async def test_healthz(client: AsyncClient) -> None:
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert body["service"] == "leaderboard"


async def test_readyz_reports_the_backend(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["backend"] == "memory"
    assert body["store_reachable"] is True


async def test_metrics_exposes_prometheus_text(client: AsyncClient) -> None:
    await _submit(client, "alice", 10)
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "leaderboard_http_requests_total" in response.text
    assert "leaderboard_score_submissions_total" in response.text


async def test_request_id_is_echoed_back(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["X-Request-ID"]


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert schema["info"]["title"] == "Global Gaming Leaderboard API"
    assert "/v1/games/{game_id}/scores" in schema["paths"]


async def test_submit_contract_is_exactly_these_fields(client: AsyncClient) -> None:
    """Pin the public request shape.

    /docs is generated from this schema, so a field added or removed without a
    deliberate decision shows up here rather than in front of a user.
    """
    schema = (await client.get("/openapi.json")).json()
    request = schema["components"]["schemas"]["SubmitScoreRequest"]
    assert set(request["properties"]) == {
        "user_id",
        "score",
        "display_name",
        "idempotency_key",
    }
    assert set(request["required"]) == {"user_id", "score"}


async def test_api_description_matches_the_implemented_semantics(
    client: AsyncClient,
) -> None:
    """The landing text on /docs is a contract too, and it drifts silently.

    The service previously advertised score modes on /docs for a full release
    after they had been removed from the code, because prose does not fail a
    type check.
    """
    description = (await client.get("/openapi.json")).json()["info"]["description"]
    for removed in ("absolute", "increment"):
        assert removed not in description.lower(), (
            f"/docs still advertises the removed '{removed}' score mode"
        )
