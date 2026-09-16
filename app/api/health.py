"""Liveness, readiness and metrics endpoints.

Liveness and readiness are deliberately different: a pod that has lost Redis is
still alive (restarting it will not help) but is not ready to take traffic.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response, status

from app.api.deps import ServiceDep, SettingsDep
from app.core.observability import metrics_response
from app.models import HealthResponse, ReadinessResponse

router = APIRouter(tags=["ops"])

VERSION = "0.1.0"


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
async def healthz(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        environment=settings.environment,
        version=VERSION,
    )


@router.get("/readyz", response_model=ReadinessResponse, summary="Readiness probe")
async def readyz(service: ServiceDep, response: Response) -> ReadinessResponse:
    started = time.perf_counter()
    reachable = await service.store.ping()
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    if not reachable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if reachable else "degraded",
        backend=service.store.backend,
        store_reachable=reachable,
        latency_ms=latency_ms,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return metrics_response()
