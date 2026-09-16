"""Domain errors and RFC 7807 (application/problem+json) error rendering.

A single, machine-readable error shape across every endpoint means clients can
write one error handler instead of one per route.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"

# Starlette renamed its 422 constant; the literal is stable across versions.
HTTP_422 = 422


class AppError(Exception):
    """Base class for errors that map cleanly onto an HTTP response."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"
    error_code: str = "internal_error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    title = "Not Found"
    error_code = "not_found"


class ValidationError(AppError):
    status_code = HTTP_422
    title = "Validation Failed"
    error_code = "validation_failed"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    title = "Conflict"
    error_code = "conflict"


class StoreUnavailableError(AppError):
    """Raised when the backing store cannot serve the request."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    title = "Service Unavailable"
    error_code = "store_unavailable"


def problem(
    *,
    status_code: int,
    title: str,
    detail: str,
    error_code: str,
    request: Request,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://docs.leaderboard.dev/errors/{error_code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "code": error_code,
        "instance": str(request.url.path),
        "request_id": getattr(request.state, "request_id", None),
    }
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return problem(
            status_code=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            error_code=exc.error_code,
            request=request,
            **exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Surface *which* field failed; generic "422" responses are hostile to clients.
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return problem(
            status_code=HTTP_422,
            title="Validation Failed",
            detail="One or more fields failed validation.",
            error_code="validation_failed",
            request=request,
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(
            status_code=exc.status_code,
            title=str(exc.detail),
            detail=str(exc.detail),
            error_code=f"http_{exc.status_code}",
            request=request,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to the caller; the traceback goes to the logs.
        return problem(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            title="Internal Server Error",
            detail="An unexpected error occurred.",
            error_code="internal_error",
            request=request,
        )
