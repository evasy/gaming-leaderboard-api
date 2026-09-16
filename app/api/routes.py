"""Leaderboard HTTP routes.

Routes only translate HTTP <-> domain calls. Business rules live in the
service, storage details live in the store.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import ServiceDep
from app.core.windows import Window
from app.models import (
    GameSummary,
    LeaderboardEntry,
    LeaderboardPage,
    SubmitScoreRequest,
    SubmitScoreResponse,
    UserContextResponse,
)

router = APIRouter(prefix="/v1", tags=["leaderboard"])

GameId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$",
        description="Game identifier, e.g. 'space-invaders'.",
    ),
]
UserId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$",
        description="Player identifier.",
    ),
]
WindowQuery = Annotated[
    Window, Query(description="Which board to read: all-time, today, or this ISO week.")
]


@router.post(
    "/games/{game_id}/scores",
    response_model=list[SubmitScoreResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Submit a score",
    description=(
        "Records a score on the all-time, daily and weekly boards in one call and returns the "
        "player's standing on each. A player's best score is what stands, so submitting a "
        "worse score is accepted but leaves the board unchanged. Supplying an "
        "`idempotency_key` discards a submission whose key has already been seen."
    ),
)
async def submit_score(
    game_id: GameId,
    payload: SubmitScoreRequest,
    service: ServiceDep,
) -> list[SubmitScoreResponse]:
    return await service.submit_score(game_id=game_id, payload=payload)


@router.get(
    "/games/{game_id}/leaderboard",
    response_model=LeaderboardPage,
    summary="Top X players",
    description="Returns a ranked page of the board, highest score first.",
)
async def get_leaderboard(
    game_id: GameId,
    service: ServiceDep,
    window: WindowQuery = Window.ALL_TIME,
    limit: Annotated[int, Query(ge=1, le=1000, description="Page size.")] = 10,
    offset: Annotated[int, Query(ge=0, description="Rows to skip, for pagination.")] = 0,
) -> LeaderboardPage:
    return await service.top(game_id=game_id, window=window, limit=limit, offset=offset)


@router.get(
    "/games/{game_id}/users/{user_id}",
    response_model=LeaderboardEntry,
    summary="A player's rank",
)
async def get_user(
    game_id: GameId,
    user_id: UserId,
    service: ServiceDep,
    window: WindowQuery = Window.ALL_TIME,
) -> LeaderboardEntry:
    return await service.get_user(game_id=game_id, window=window, user_id=user_id)


@router.get(
    "/games/{game_id}/users/{user_id}/context",
    response_model=UserContextResponse,
    summary="A player's surroundings",
    description=(
        "Returns the player plus the `radius` players ranked immediately above and below them "
        "-- the 'you are here' strip a game client renders next to the top-10 board."
    ),
)
async def get_user_context(
    game_id: GameId,
    user_id: UserId,
    service: ServiceDep,
    window: WindowQuery = Window.ALL_TIME,
    radius: Annotated[
        int, Query(ge=1, le=50, description="How many players to include on each side.")
    ] = 2,
) -> UserContextResponse:
    return await service.user_context(
        game_id=game_id, window=window, user_id=user_id, radius=radius
    )


@router.delete(
    "/games/{game_id}/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Erase a player",
    description=(
        "Removes the player from every board of the game. Exists so that an account deletion "
        "or a cheat-detection takedown has a supported path rather than manual surgery."
    ),
)
async def delete_user(game_id: GameId, user_id: UserId, service: ServiceDep) -> Response:
    await service.delete_user(game_id=game_id, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/games",
    response_model=list[GameSummary],
    summary="List games",
)
async def list_games(service: ServiceDep) -> list[GameSummary]:
    return await service.list_games()
