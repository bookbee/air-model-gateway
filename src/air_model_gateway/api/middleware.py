from __future__ import annotations

import time
import uuid

import structlog
from fastapi import FastAPI, Request, Response

from air_model_gateway.observability.metrics import REQUESTS

_REQUEST_ID_HEADER = "X-Request-ID"


def install_middleware(app: FastAPI) -> None:
    log = structlog.get_logger("access")

    @app.middleware("http")
    async def _observe(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        route = request.scope.get("route")
        route_label = getattr(route, "path", request.url.path)
        REQUESTS.labels(route_label, request.method, str(response.status_code)).inc()
        response.headers[_REQUEST_ID_HEADER] = request_id
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=elapsed_ms,
        )
        return response
