from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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

    def __init__(self, detail: str | None = None, *, headers: dict[str, str] | None = None) -> None:
        self.detail = detail
        self.headers = headers or {}
        super().__init__(detail or self.title)


class Unauthorized(AppError):
    status, code, title = 401, "unauthorized", "Unauthorized"


class Forbidden(AppError):
    status, code, title = 403, "policy_denied", "Forbidden"


class RateLimited(AppError):
    status, code, title = 429, "rate_limited", "Too Many Requests"


class BudgetExceeded(AppError):
    """The service's daily_budget_usd is spent. 429 like other "not now"
    answers, with its own code and a Retry-After of the next UTC day, so a
    client can tell it from a momentary throttle and stop retrying."""

    status, code, title = 429, "budget_exceeded", "Too Many Requests"


class InvalidModelChoice(AppError):
    """A model choice that means nothing here: an unknown embedding name, a
    provider the class doesn't offer, a malformed pin. The caller's to fix."""

    status, code, title = 422, "invalid_model_choice", "Unprocessable Entity"


class ProviderUnavailable(AppError):
    status, code, title = 502, "provider_unavailable", "Bad Gateway"


def problem_response(exc: AppError, request_id: str | None) -> JSONResponse:
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
        headers=exc.headers,
    )


def validation_detail(errors: Sequence[Any]) -> str:
    """One `field: message` clause per error, e.g. `messages.0.role: Input should
    be 'system', 'user' or 'assistant'`.

    Not ``str(exc.errors())``: that is a Python repr, unreadable to a non-Python
    caller, and each entry's ``input`` echoes the offending value back, which for
    a body-level error is the whole request, prompts included. Neither belongs
    in an error body (or in whatever logs and forwards error bodies)."""
    clauses = []
    for e in errors:
        loc = [str(p) for p in e.get("loc", ()) if p != "body"]
        msg = str(e.get("msg", "invalid")).removeprefix("Value error, ")
        clauses.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return "; ".join(clauses)


def install_error_handlers(app: FastAPI) -> None:
    log = structlog.get_logger("api")

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        if exc.status >= 500:
            log.error("app_error", code=exc.code, detail=exc.detail, request_id=rid)
        return problem_response(exc, rid)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        err = AppError(detail=validation_detail(exc.errors()))
        err.status, err.code, err.title = 422, "validation_error", "Unprocessable Entity"
        return problem_response(err, rid)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled", request_id=rid)
        return problem_response(AppError("unexpected error"), rid)
