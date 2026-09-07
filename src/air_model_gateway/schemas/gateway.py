"""Config documents for the model gateway: the routing table and the price book.

These are DevOps-owned YAML (``deploy/``), not code — the same shape as
``policy.*.yaml``. Keeping them validated here means a malformed table fails at
boot with a field-level error rather than at the first request that needs it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from air_model_gateway.constants import Provider


class RouteTarget(BaseModel):
    """One rung of an alias's failover chain."""

    provider: Provider
    model: str


class RoutingDoc(BaseModel):
    # An alias with no targets would resolve to an empty failover chain, which the
    # router cannot report a failure against. Rejected at load, not at first call.
    aliases: dict[str, Annotated[list[RouteTarget], Field(min_length=1)]] = Field(
        default_factory=dict
    )


class ModelPrice(BaseModel):
    """USD per MILLION tokens — the unit providers publish."""

    input: float = 0.0
    output: float = 0.0


class PricingDoc(BaseModel):
    cache_read_ratio: float = 0.1
    cache_write_ratio: float = 2.0
    local_prefixes: list[str] = Field(default_factory=list)
    models: dict[str, ModelPrice] = Field(default_factory=dict)
