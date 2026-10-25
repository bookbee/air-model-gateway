from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response, Security
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader

from air_model_gateway.api.errors import Forbidden, RateLimited, Unauthorized
from air_model_gateway.constants import Action
from air_model_gateway.observability.metrics import RATELIMITED
from air_model_gateway.schemas.common import Principal

# Declared as a security scheme so docs/openapi.json (and clients generated from
# it) know the token is a credential. auto_error=False: a missing key must reach
# the verifier and come back as our problem+json 401, not FastAPI's own 403.
_API_KEY = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ServiceToken",
    description="The calling service's token, issued per service and environment.",
    auto_error=False,
)


async def require_principal(
    request: Request,
    x_api_key: str | None = Security(_API_KEY),
) -> Principal:
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal if principal is not None else _authenticate(request)


def _authenticate(request: Request) -> Principal:
    principal: Principal | None = request.app.state.verifier.verify(
        request.headers.get(_API_KEY.model.name)
    )
    if principal is None:
        raise Unauthorized("missing or invalid X-API-Key")
    request.state.principal = principal
    return principal


class AuthenticatedRoute(APIRoute):
    """A route that checks the caller's key before anything else.

    FastAPI reads and validates the body before it runs any dependency, so with
    `require_principal` alone an anonymous request with a broken body gets a 422
    describing the API's validation instead of a 401. For routes that depend on
    `require_principal`, this checks the key first; the dependency then reuses
    the principal it found. Routes without it (health, readiness) are untouched.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if not _needs_principal(self.dependant):
            return handler

        async def authenticate_first(request: Request) -> Response:
            _authenticate(request)
            return await handler(request)

        return authenticate_first


def _needs_principal(dependant: Dependant) -> bool:
    return any(d.call is require_principal or _needs_principal(d) for d in dependant.dependencies)


def enforce_policy(request: Request, principal: Principal, resource: str, action: Action) -> None:
    decision = request.app.state.policy.decide(principal, resource, action)
    if not decision.allow:
        raise Forbidden(decision.reason)


async def enforce_rate(request: Request, principal: Principal, resource: str) -> None:
    rpm = request.app.state.policy.rate_per_minute(principal)
    if not await request.app.state.limiter.check(principal.service, resource, rpm):
        RATELIMITED.labels(principal.service, resource).inc()
        raise RateLimited(f"rate limit exceeded for '{resource}'")
