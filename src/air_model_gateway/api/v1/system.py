from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from air_model_gateway import __version__
from air_model_gateway.api.deps import AuthenticatedRoute, require_principal
from air_model_gateway.api.openapi import problems
from air_model_gateway.constants import ModelClass, Provider
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.system import Capabilities, Health, Readiness

router = APIRouter(tags=["system"], route_class=AuthenticatedRoute)


@router.get("/health", response_model=Health, summary="Liveness")
async def health(request: Request) -> dict[str, str]:
    """Liveness: the process is up, and which build it runs. Never consults a
    provider."""
    return {
        "status": "ok",
        "version": __version__,
        "revision": request.app.state.settings.revision,
    }


@router.get(
    "/ready",
    response_model=Readiness,
    response_model_exclude_none=True,
    responses={503: {"model": Readiness, "description": "No provider answers."}},
    summary="Readiness",
)
async def ready(request: Request) -> JSONResponse:
    """Readiness: should this replica receive traffic?

    Gated on whether at least one provider is reachable: "no provider answers"
    is the one condition under which this replica genuinely cannot serve.

    Redis is reported but never gates. Without it the cache misses and rate
    limits fall back to per replica, so the replica still serves correctly,
    and pulling every replica out of rotation over a Redis blip would turn a
    degraded cache into a full outage.
    """
    state = request.app.state
    providers = await state.router.health()
    ok = any(providers.values())
    body: dict[str, object] = {"status": "ready" if ok else "degraded", "providers": providers}
    if state.redis is not None:
        body["redis"] = await state.redis.ping()
    return JSONResponse(status_code=200 if ok else 503, content=body)


@router.get(
    "/capabilities",
    response_model=Capabilities,
    responses=problems(401),
    summary="Version, environment, providers, and the model choices open to you",
)
async def capabilities(
    request: Request, principal: Principal = Depends(require_principal)
) -> dict[str, object]:
    settings = request.app.state.settings
    configured = {
        Provider.OLLAMA.value: True,
        Provider.ANTHROPIC.value: bool(settings.providers.anthropic_api_key),
        Provider.OPENAI.value: bool(settings.providers.openai_api_key),
        Provider.GEMINI.value: bool(settings.providers.gemini_api_key),
        # No key concept, same as Ollama — "configured" means "has a
        # base_url", which it always does (a sensible default is set).
        Provider.SELF_HOSTED.value: True,
    }
    return {
        "version": __version__,
        "revision": settings.revision,
        "environment": settings.environment.value,
        "providers": configured,
        **_model_choices(request, principal, configured),
    }


def _model_choices(
    request: Request, principal: Principal, configured: dict[str, bool]
) -> dict[str, object]:
    """`allowed` is the policy's answer alone; `available` is whether this
    deployment can serve the choice at all (some provider behind it is
    configured), so a consumer can tell "not permitted" from "not set up here"."""
    state = request.app.state
    policy, catalog = state.policy, state.catalog
    described = catalog.describe(principal.service)
    sp = policy.doc.services.get(principal.service)
    return {
        "generate": {
            name: {
                "allowed": policy.may_use_class(principal, ModelClass(name)),
                "available": any(configured[rung.partition(":")[0]] for rung in chain),
                "chain": chain,
            }
            for name, chain in described["generate"].items()
        },
        "preferred_providers": (
            [p.value for p in sp.preferred_providers]
            if sp is not None and sp.preferred_providers is not None
            else None
        ),
        "may_pin": state.settings.is_local or policy.may_pin(principal),
        "embed": {
            name: {
                "allowed": policy.may_use_embed_model(principal, name),
                "available": configured[info["provider"]],
                **info,
            }
            for name, info in described["embed"].items()
        },
    }
