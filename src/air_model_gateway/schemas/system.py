"""Response shapes of the system endpoints, declared so docs/openapi.json
describes them and a generated client gets types rather than bare objects."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Health(BaseModel):
    status: Literal["ok"]
    version: str


class Readiness(BaseModel):
    status: Literal["ready", "degraded"]
    providers: dict[str, bool] = Field(description="Provider name -> answered its health check.")
    redis: bool | None = Field(
        default=None,
        description="Present only when Redis is configured. Reported, never gates readiness.",
    )


class GenerateClass(BaseModel):
    allowed: bool = Field(description="Whether your service may use this class.")
    chain: list[str] = Field(
        description="The models behind it for your service, in the order tried (provider:model)."
    )


class EmbedModelInfo(BaseModel):
    allowed: bool = Field(description="Whether your service may use this embedding model.")
    provider: str
    model: str
    dimensions: int


class Capabilities(BaseModel):
    """What the calling service can ask for: the discovery endpoint for model
    choices. Chains reflect the owner's configuration, including any
    per-service tuning, and may change; the class and embedding names don't."""

    version: str
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
