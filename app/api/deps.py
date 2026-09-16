"""FastAPI dependencies.

The service is constructed once at startup and handed to routes from app state,
so a request never pays connection-setup cost and tests can swap the whole
backend by overriding a single dependency.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.service import LeaderboardService


def get_service(request: Request) -> LeaderboardService:
    service: LeaderboardService = request.app.state.service
    return service


ServiceDep = Annotated[LeaderboardService, Depends(get_service)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
