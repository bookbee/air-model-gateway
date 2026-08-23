from __future__ import annotations

from pydantic import BaseModel


class Principal(BaseModel):
    """The authenticated caller. `service` is the v1 unit of authorization;
    `tenant` is carried now so quotas/audit can split per-tenant later."""

    service: str
    tenant: str | None = None
    token_id: str | None = None


class RequestContext(BaseModel):
    request_id: str
    principal: Principal | None = None


class Problem(BaseModel):
    """RFC 9457 problem detail."""

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str | None = None
    request_id: str | None = None
