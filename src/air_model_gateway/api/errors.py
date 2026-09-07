from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from air_model_gateway.schemas.common import Problem

_PROBLEM_MEDIA = "application/problem+json"


class AppError(Exception):
    status = 500
    code = "internal_error"
    title = "Internal Server Error"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__(detail or self.title)


class Unauthorized(AppError):
    status, code, title = 401, "unauthorized", "Unauthorized"


class Forbidden(AppError):
    status, code, title = 403, "policy_denied", "Forbidden"


class ResourceNotFound(AppError):
    status, code, title = 404, "resource_not_found", "Not Found"


class RateLimited(AppError):
    status, code, title = 429, "rate_limited", "Too Many Requests"


class ProviderUnavailable(AppError):
    status, code, title = 502, "provider_unavailable", "Bad Gateway"


class DependencyUnavailable(AppError):
    status, code, title = 503, "dependency_unavailable", "Service Unavailable"


def _problem(exc: AppError, request_id: str | None) -> JSONResponse:
    body = Problem(
        title=exc.title,
        status=exc.status,
        code=exc.code,
        detail=exc.detail,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=exc.status,
        content=body.model_dump(),
        media_type=_PROBLEM_MEDIA,
    )


def install_error_handlers(app: FastAPI) -> None:
    log = structlog.get_logger("api")

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        if exc.status >= 500:
            log.error("app_error", code=exc.code, detail=exc.detail, request_id=rid)
        return _problem(exc, rid)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        err = AppError(detail=str(exc.errors()))
        err.status, err.code, err.title = 422, "validation_error", "Unprocessable Entity"
        return _problem(err, rid)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled", request_id=rid)
        return _problem(AppError("unexpected error"), rid)
