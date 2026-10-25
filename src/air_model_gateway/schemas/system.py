"""Response shapes of the system endpoints, declared so docs/openapi.json
describes them and a generated client gets types rather than bare objects."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    revision: str = Field(
        description=(
            "The build's git revision (`-dirty`: built from uncommitted changes; `unknown`: "
            "not recorded). Record it with any test result."
        )
    )


class Readiness(BaseModel):
    status: Literal["ready", "degraded"]
    providers: dict[str, bool] = Field(
        description=(
            "Provider name -> answered its health check, and its key isn't being refused "
            "(false after repeated auth failures on real calls)."
        )
    )
    redis: bool | None = Field(
        default=None,
        description="Present only when Redis is configured. Reported, never gates readiness.",
    )


class GenerateClass(BaseModel):
    allowed: bool = Field(description="Whether your service may use this class.")
    available: bool = Field(
        description=(
            "Whether this deployment can serve it: some provider in the chain is configured "
            "(`providers`). False means every call would fail, whatever `allowed` says."
        )
    )
    chain: list[str] = Field(
        description="The models behind it for your service, in the order tried (provider:model)."
    )


class EmbedModelInfo(BaseModel):
    allowed: bool = Field(description="Whether your service may use this embedding model.")
    available: bool = Field(
        description=(
            "Whether this deployment can serve it: its one provider is configured "
            "(`providers`). An embedding model never fails over, so false means every "
            "call would fail."
        )
    )
    provider: str
    model: str
    dimensions: int


class Capabilities(BaseModel):
    """What the calling service can ask for: the discovery endpoint for model
    choices. Chains reflect the owner's configuration, including any
    per-service tuning, and may change; the class and embedding names don't."""

    version: str
    revision: str = Field(description="The build's git revision, as in `/v1/health`.")
    environment: Literal["local", "staging", "prod"]
    providers: dict[str, bool] = Field(description="Provider name -> configured here.")
    generate: dict[str, GenerateClass] = Field(
        description="POST /v1/generate classes (`auto` is the default)."
    )
    preferred_providers: list[str] | None = Field(
        description="Providers you may name in `provider`; null means any the class offers."
    )
    may_pin: bool = Field(description="Whether you may send `pin` (an exact provider:model).")
    embed: dict[str, EmbedModelInfo] = Field(description="POST /v1/embed published names.")
