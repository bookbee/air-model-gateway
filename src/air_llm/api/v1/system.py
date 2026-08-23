from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from air_llm import __version__
from air_llm.api.deps import require_principal
from air_llm.constants import Provider
from air_llm.schemas.common import Principal

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up. Never consults a provider."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: should this replica receive traffic?

    Gated on whether at least one provider is reachable. air-llm is on the path
    for every model call — unlike air-infra, it has no separate store-broker
    plane to keep decoupled from this — so "no provider answers" is the one
    condition that means this replica genuinely cannot serve.
    """
    providers = await request.app.state.router.health()
    ok = any(providers.values())
    body = {"status": "ready" if ok else "degraded", "providers": providers}
    return JSONResponse(status_code=200 if ok else 503, content=body)


@router.get("/capabilities")
async def capabilities(
    request: Request, principal: Principal = Depends(require_principal)
) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "version": __version__,
        "environment": settings.environment.value,
        "providers": {
            Provider.OLLAMA.value: True,
            Provider.ANTHROPIC.value: bool(settings.providers.anthropic_api_key),
            Provider.OPENAI.value: bool(settings.providers.openai_api_key),
            Provider.GEMINI.value: bool(settings.providers.gemini_api_key),
            # No key concept, same as Ollama — "configured" means "has a
            # base_url", which it always does (a sensible default is set).
            Provider.SELF_HOSTED.value: True,
        },
        "default_model": settings.gateway.default_model,
    }
