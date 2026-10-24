from __future__ import annotations

from pydantic import BaseModel


class Principal(BaseModel):
    """The authenticated caller. `service` is the unit of authorization, rate
    limits, budgets and cache scope; `token_id` is a non-reversible id of the
    token used (security/apikeys.py::token_id)."""

    service: str
    token_id: str | None = None


class Problem(BaseModel):
    """RFC 9457 problem detail."""

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str | None = None
    request_id: str | None = None
