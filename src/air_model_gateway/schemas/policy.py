from __future__ import annotations

from pydantic import BaseModel, Field


class ServicePolicy(BaseModel):
    # resource name -> allowed action values (e.g. {"inference": ["chat"]})
    resources: dict[str, list[str]] = Field(default_factory=dict)
    rate_per_minute: int | None = None


class PolicyDoc(BaseModel):
    default_allow: bool = False
    default_rate_per_minute: int = 120
    services: dict[str, ServicePolicy] = Field(default_factory=dict)


class Decision(BaseModel):
    allow: bool
    reason: str
