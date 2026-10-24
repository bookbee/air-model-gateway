from __future__ import annotations

from fastapi import Request, Security
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
    principal: Principal | None = request.app.state.verifier.verify(x_api_key)
    if principal is None:
        raise Unauthorized("missing or invalid X-API-Key")
    return principal


def enforce_policy(request: Request, principal: Principal, resource: str, action: Action) -> None:
    decision = request.app.state.policy.decide(principal, resource, action)
    if not decision.allow:
        raise Forbidden(decision.reason)


async def enforce_rate(request: Request, principal: Principal, resource: str) -> None:
    rpm = request.app.state.policy.rate_per_minute(principal)
    if not await request.app.state.limiter.check(principal.service, resource, rpm):
        RATELIMITED.labels(principal.service, resource).inc()
        raise RateLimited(f"rate limit exceeded for '{resource}'")
