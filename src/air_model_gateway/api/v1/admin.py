from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from air_model_gateway.api.deps import enforce_policy, require_principal
from air_model_gateway.constants import Action
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.policy import PolicyDoc

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/policy", response_model=PolicyDoc)
async def dump_policy(
    request: Request, principal: Principal = Depends(require_principal)
) -> PolicyDoc:
    enforce_policy(request, principal, "admin", Action.ADMIN_READ)
    doc: PolicyDoc = request.app.state.policy.doc
    return doc


# TODO: policy writes (create/update service grants).
