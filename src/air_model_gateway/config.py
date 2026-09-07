"""Settings. One image, environment chosen at boot via AIR_MODEL_GATEWAY_ENVIRONMENT.

Nested groups use a double-underscore delimiter, e.g. AIR_MODEL_GATEWAY_OLLAMA__BASE_URL.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from air_model_gateway.constants import Environment, Provider


class ApiSettings(BaseModel):
    """Not currently read anywhere — the actual bind host/port always comes
    from the `uvicorn` CLI invocation (Dockerfile's CMD, Makefile's `run`
    target), which don't consult Settings. Kept as documented, reserved config
    rather than wired up or removed: the fix depends on a deployment-pattern
    decision (settings-driven bind vs. CLI-driven) this repo hasn't made yet,
    not on any code in this class."""

    host: str = "0.0.0.0"
    port: int = 8080


class LogSettings(BaseModel):
    level: str = "INFO"
    render: Literal["console", "json"] = "console"


class SecuritySettings(BaseModel):
    # token -> service name. Local only; in prod these are injected by DevOps
    # (Vault/cloud secrets manager) directly into the environment — air-model-gateway has
    # no other AIR service to source them from at runtime (docs/00-plan.md §1).
    #
    # NoDecode suppresses pydantic-settings' JSON pre-decode for this field. Without
    # it the env source calls json.loads() on the raw value *before* any validator
    # runs, so the CSV form documented in .env.example raises SettingsError and
    # _parse_pairs below is unreachable.
    service_tokens: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    @field_validator("service_tokens", mode="before")
    @classmethod
    def _parse_pairs(cls, v: object) -> object:
        """Accept either a mapping or a CSV of ``token:service`` pairs."""
        if not isinstance(v, str):
            return v
        out: dict[str, str] = {}
        for pair in v.split(","):
            pair = pair.strip()
            if not pair:
                continue
            token, _, service = pair.partition(":")
            if token and service:
                out[token.strip()] = service.strip()
        return out


class OllamaSettings(BaseModel):
    base_url: str = "http://localhost:11434"
    #: Sent on every /api/chat and /api/embed call. Ollama unloads a model from
    #: memory after its keep_alive window of inactivity elapses — 5m if a call
    #: never specifies one, which every call here previously did not. Every AIR
    #: service sharing this Ollama instance pays that reload cold-start the
    #: same way, so the fix belongs here (one place), not in each consumer's
    #: own warmup/padded-timeout workaround. Matches the value the Makefile's
    #: `ollama-warm` target already warms to — keeping this in step with that
    #: is what makes a manual warm actually stick across real traffic instead
    #: of being undone by the first request after it.
    keep_alive: str = "30m"


class SelfHostedSettings(BaseModel):
    """A single OpenAI-wire-compatible backend slot — llama.cpp's `llama-server`
    locally by default, a self-hosted vLLM/TGI deployment in prod (LM Studio
    also works, unchanged, if pointed at this slot instead). One slot, not a
    named map: this covers "one alternate local/self-hosted runtime active per
    environment", which is the shape asked for. A second concurrent
    self-hosted backend would need this to become a map — not built ahead of
    that need."""

    base_url: str = "http://localhost:8181/v1"
    #: Most self-hosted servers (llama.cpp, LM Studio included) don't check
    #: this at all; kept so a vLLM/TGI deployment that does enforce a token
    #: still works.
    api_key: str = ""


class ProvidersSettings(BaseModel):
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""


class GatewaySettings(BaseModel):
    default_provider: Provider = Provider.OLLAMA
    default_model: str = "llama3.2:3b"
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300
    #: Caps the in-process cache regardless of TTL — a write-once entry (the
    #: common case for a distinct prompt) is only reclaimed lazily, on its own
    #: next lookup, so TTL alone does not bound memory. See gateway/cache.py.
    cache_max_entries: int = 10_000
    #: Alias -> failover chain, and the per-model price table. DevOps-owned YAML:
    #: adding a model or repricing one is a config edit plus restart, not a release.
    #: A missing file falls back to the built-in tables so a bare checkout still runs.
    routing_config_path: str = "deploy/routing.yaml"
    pricing_config_path: str = "deploy/pricing.yaml"
    #: Deadline on a single provider attempt. Exceeding it fails that rung over to
    #: the next in the chain, so a hung upstream cannot hold a caller's connection.
    request_timeout_seconds: float = 60.0


class PolicySettings(BaseModel):
    config_path: str = "deploy/policy/policy.local.yaml"
    default_rate_per_minute: int = 120


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIR_MODEL_GATEWAY_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL
    api: ApiSettings = Field(default_factory=ApiSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    self_hosted: SelfHostedSettings = Field(default_factory=SelfHostedSettings)
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)

    @property
    def is_prod(self) -> bool:
        return self.environment == Environment.PROD


@lru_cache
def get_settings() -> Settings:
    return Settings()
