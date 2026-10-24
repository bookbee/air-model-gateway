"""Config documents for the model gateway: the routing table and the price book.

These are DevOps-owned YAML (``deploy/``), not code — the same shape as
``policy.*.yaml``. Keeping them validated here means a malformed table fails at
boot with a field-level error rather than at the first request that needs it.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from air_model_gateway.constants import LOCAL_PROVIDERS, ModelClass, Provider


class RouteTarget(BaseModel):
    """One rung of a failover chain."""

    provider: Provider
    model: str


# A class with no rungs would resolve to an empty chain, which the router can't
# report a failure against. Rejected at load, not at first call.
Chain = Annotated[list[RouteTarget], Field(min_length=1)]

#: `<local|cloud>-<dimensions>-v<version>`, e.g. `local-768-v1`.
EMBED_NAME_PATTERN = r"^(local|cloud)-([1-9][0-9]*)-v([1-9][0-9]*)$"


class EmbedModel(BaseModel):
    """One published embedding model: exactly one provider model, never a chain."""

    provider: Provider
    model: str
    dimensions: int = Field(gt=0)


class ServiceRoutes(BaseModel):
    """A service's own meaning for some classes (the rest stay global)."""

    generate: dict[ModelClass, Chain] = Field(default_factory=dict)


class RoutingDoc(BaseModel):
    generate: dict[ModelClass, Chain]
    embed: dict[str, EmbedModel] = Field(default_factory=dict)
    overrides: dict[str, ServiceRoutes] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> RoutingDoc:
        missing = set(ModelClass) - set(self.generate)
        if missing:
            raise ValueError(f"generate: no chain for {sorted(m.value for m in missing)}")
        for scope, classes in [("generate", self.generate)] + [
            (f"overrides.{svc}.generate", r.generate) for svc, r in self.overrides.items()
        ]:
            for cls, chain in classes.items():
                _check_location(f"{scope}.{cls.value}", cls, [t.provider for t in chain])
        for name, spec in self.embed.items():
            match = re.match(EMBED_NAME_PATTERN, name)
            if match is None:
                raise ValueError(f"embed: '{name}' is not <local|cloud>-<dimensions>-v<version>")
            location, dims = match.group(1), int(match.group(2))
            if dims != spec.dimensions:
                raise ValueError(
                    f"embed: '{name}' says {dims} dimensions, model has {spec.dimensions}"
                )
            is_local = spec.provider in LOCAL_PROVIDERS
            if is_local != (location == "local"):
                raise ValueError(f"embed: '{name}' is {location} but {spec.provider.value} is not")
        return self


def _check_location(where: str, cls: ModelClass, providers: list[Provider]) -> None:
    """`local` chains use only our own servers and `cloud` chains only cloud
    providers: the class name is a promise about where the prompt goes."""
    if cls is ModelClass.LOCAL and any(p not in LOCAL_PROVIDERS for p in providers):
        local = sorted(p.value for p in LOCAL_PROVIDERS)
        raise ValueError(f"{where}: a local chain may only use {local}")
    if cls is ModelClass.CLOUD and any(p in LOCAL_PROVIDERS for p in providers):
        raise ValueError(f"{where}: a cloud chain may not use local providers")


class ModelPrice(BaseModel):
    """USD per MILLION tokens — the unit providers publish."""

    input: float = 0.0
    output: float = 0.0


class PricingDoc(BaseModel):
    cache_read_ratio: float = 0.1
    cache_write_ratio: float = 2.0
    local_prefixes: list[str] = Field(default_factory=list)
    models: dict[str, ModelPrice] = Field(default_factory=dict)
