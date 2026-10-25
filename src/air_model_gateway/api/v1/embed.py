from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from air_model_gateway.api.deps import AuthenticatedRoute, require_principal
from air_model_gateway.api.openapi import problems
from air_model_gateway.api.v1 import model_calls
from air_model_gateway.constants import Action
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.embed import EmbedInput, EmbedOutput

router = APIRouter(tags=["embed"], route_class=AuthenticatedRoute)


@router.post(
    "/embed",
    response_model=EmbedOutput,
    responses=problems(401, 403, 422, 429, 502),
    summary="Embed texts with a published embedding model",
)
async def embed(
    request: Request,
    body: EmbedInput,
    principal: Principal = Depends(require_principal),
) -> EmbedOutput:
    """Texts in, one vector per text out."""
    await model_calls.admit(request, principal, Action.EMBED)
    return await model_calls.embed(request.app.state, principal, body)
