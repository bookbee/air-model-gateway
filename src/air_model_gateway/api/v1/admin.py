from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from air_model_gateway.api.deps import enforce_policy, require_principal
from air_model_gateway.api.openapi import problems
from air_model_gateway.constants import Action
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.policy import PolicyDoc

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/policy",
    response_model=PolicyDoc,
    responses=problems(401, 403),
    summary="The effective policy (needs the admin_read grant)",
)
async def dump_policy(
    request: Request, principal: Principal = Depends(require_principal)
) -> PolicyDoc:
    enforce_policy(request, principal, "admin", Action.ADMIN_READ)
    doc: PolicyDoc = request.app.state.policy.doc
    return doc


# Read-only by design: policy changes are a reviewed file edit and a deploy.
# Write endpoints are future work (docs/00-plan.md §6), only if policy must
# change without a restart.
