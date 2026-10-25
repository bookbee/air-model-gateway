from __future__ import annotations

import re
import time
import uuid

import structlog
from fastapi import FastAPI, Request, Response

from air_model_gateway.api.errors import AppError, problem_response
from air_model_gateway.observability.metrics import REQUEST_SECONDS, REQUESTS

_REQUEST_ID_HEADER = "X-Request-ID"
# The caller's id is echoed in a header, every log line and every error body, so
# it must be bounded: UUIDs, W3C trace ids and the usual `svc-123` shapes fit;
# anything else (multi-KB values, quotes, markup) is replaced, not trusted.
_REQUEST_ID_SHAPE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def request_id_from(header: str | None) -> str:
    if header and _REQUEST_ID_SHAPE.fullmatch(header):
        return header
    return uuid.uuid4().hex


def install_middleware(app: FastAPI) -> None:
    log = structlog.get_logger("access")

    @app.middleware("http")
    async def _observe(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request_id_from(request.headers.get(_REQUEST_ID_HEADER))
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            # Unhandled errors would otherwise bypass this middleware: the 500
            # would carry no X-Request-ID and never reach requests_total, so a
            # 5xx alert couldn't see gateway bugs at all.
            log.exception("unhandled", path=request.url.path)
            response = problem_response(AppError("unexpected error"), request_id)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        elapsed = time.perf_counter() - start
        elapsed_ms = round(elapsed * 1000, 2)
        # The label must stay bounded. A matched route is labelled by its path
        # (this API has no path parameters, so paths are a fixed set; if one is
        # ever added, label by the route template instead). Anything unmatched
        # is one label, or a scanner probing random URLs would mint a new
        # series per URL. Not route.path: inside an included router that
        # lacks the /v1 prefix.
        route_label = request.url.path if request.scope.get("route") else "<unmatched>"
        REQUESTS.labels(route_label, request.method, str(response.status_code)).inc()
        REQUEST_SECONDS.labels(route_label, request.method).observe(elapsed)
        response.headers[_REQUEST_ID_HEADER] = request_id
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=elapsed_ms,
        )
        return response
