"""Structured logging, request correlation, and Prometheus metrics.

Three things a service needs before anyone will run it in production: you can
tell what it is doing (structured logs), you can tie a user's complaint to a
specific request (request ids), and you can alert on it (metrics).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

http_requests_total = Counter(
    "leaderboard_http_requests_total",
    "HTTP requests handled, by route and outcome.",
    ["method", "route", "status"],
)
http_request_duration_seconds = Histogram(
    "leaderboard_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "route"],
    # Tuned for an API whose p99 target is single-digit milliseconds.
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
score_submissions_total = Counter(
    "leaderboard_score_submissions_total",
    "Score submissions, by game and outcome.",
    ["game_id", "result"],
)
store_operation_duration_seconds = Histogram(
    "leaderboard_store_operation_duration_seconds",
    "Backing-store operation latency in seconds.",
    ["backend", "operation"],
)


def configure_logging(*, level: str = "INFO", fmt: str = "json") -> None:
    """Route stdlib logging through structlog so every line is one JSON object."""
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", level=level.upper())
    # uvicorn's own access log would duplicate our structured one.
    logging.getLogger("uvicorn.access").disabled = True


logger = structlog.get_logger("leaderboard")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, times the request, logs it, and records metrics."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        except Exception:
            logger.exception("request_failed", method=request.method, path=request.url.path)
            raise
        finally:
            elapsed = time.perf_counter() - started
            # Use the matched route template, not the raw path: per-user-id
            # labels would blow up metric cardinality.
            route = request.scope.get("route")
            route_label = getattr(route, "path", request.url.path)
            http_requests_total.labels(request.method, route_label, str(status_code)).inc()
            http_request_duration_seconds.labels(request.method, route_label).observe(elapsed)
            if route_label not in ("/metrics", "/healthz"):
                logger.info(
                    "request_completed",
                    method=request.method,
                    path=request.url.path,
                    route=route_label,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 3),
                )
            structlog.contextvars.clear_contextvars()


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
