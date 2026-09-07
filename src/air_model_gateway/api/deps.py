from __future__ import annotations

from fastapi import Header, Request

from air_model_gateway.api.errors import Forbidden, RateLimited, Unauthorized
from air_model_gateway.constants import Action
from air_model_gateway.observability.metrics import RATELIMITED
from air_model_gateway.schemas.common import Principal


async def require_principal(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    principal: Principal | None = request.app.state.verifier.verify(x_api_key)
    if principal is None:
        raise Unauthorized("missing or invalid X-API-Key")
    return principal


def enforce_policy(request: Request, principal: Principal, resource: str, action: Action) -> None:
    decision = request.app.state.policy.decide(principal, resource, action)
    if not decision.allow:
        raise Forbidden(decision.reason)


def enforce_rate(request: Request, principal: Principal, resource: str) -> None:
    rpm = request.app.state.policy.rate_per_minute(principal)
    if not request.app.state.limiter.check(principal.service, resource, rpm):
        RATELIMITED.labels(principal.service, resource).inc()
        raise RateLimited(f"rate limit exceeded for '{resource}'")
