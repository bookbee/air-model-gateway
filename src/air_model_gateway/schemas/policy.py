from __future__ import annotations

from pydantic import BaseModel, Field

from air_model_gateway.constants import Action, ModelClass, Provider


class ServicePolicy(BaseModel):
    # resource name -> allowed actions (e.g. {"inference": ["generate"]}). Typed as
    # Action, so a misspelt or unknown action fails the policy file at load
    # instead of silently granting nothing.
    resources: dict[str, list[Action]] = Field(default_factory=dict)
    rate_per_minute: int | None = Field(default=None, gt=0)
    # Which model choices the service may make. Unset means every one the
    # routing file offers; a list restricts it.
    #   model_classes        /v1/generate classes (local, auto, cloud)
    #   embed_models         /v1/embed names (e.g. local-768-v1)
    #   preferred_providers  providers it may name in `provider` (a preference)
    model_classes: list[ModelClass] | None = None
    embed_models: list[str] | None = None
    preferred_providers: list[Provider] | None = None
    # Whether it may send `pin` (an exact provider:model, bypassing the classes)
    # outside local. For operator tools that test physical models, never for
    # applications, whose model choice is a class the owner governs.
    pin: bool = False
    # USD this service may spend per UTC day; unset means no cap. A soft cap:
    # calls already in flight when it is reached still complete and count.
    daily_budget_usd: float | None = Field(default=None, gt=0)


class PolicyDoc(BaseModel):
    default_allow: bool = False
    default_rate_per_minute: int = Field(default=120, gt=0)
    # Calls per minute this deployment may send to each provider, summed over
    # every service. Protects the provider account's own quota when many
    # services share it: a rung whose provider is over quota is skipped, so the
    # chain fails over instead of the provider returning 429s.
    provider_quotas: dict[Provider, int] = Field(default_factory=dict)
    services: dict[str, ServicePolicy] = Field(default_factory=dict)


class Decision(BaseModel):
    allow: bool
    reason: str
