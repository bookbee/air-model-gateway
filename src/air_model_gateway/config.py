"""Settings. One image, environment chosen at boot via AIR_MODEL_GATEWAY_ENVIRONMENT.

Nested groups use a double-underscore delimiter, e.g. AIR_MODEL_GATEWAY_OLLAMA__BASE_URL.

Secrets can also come from files: point AIR_MODEL_GATEWAY_SECRETS_DIR at a
directory where each file is named exactly like the env var it replaces
(``AIR_MODEL_GATEWAY_PROVIDERS__ANTHROPIC_API_KEY``) and holds the value. That
is the shape Vault Agent templates, Kubernetes secret volumes and the cloud
secret-store CSI drivers all produce, so one mechanism covers each of them
without an SDK in the image. Real environment variables still win over files.

There is deliberately no bind host/port here: the listen address belongs to the
process manager (`uvicorn --host/--port` in the Dockerfile and Makefile, or the
orchestrator's container spec), not to app config. A Settings field for it
could only ever disagree with the CLI that actually binds the socket.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from air_model_gateway.constants import Environment


class ConfigError(RuntimeError):
    """The deployment is misconfigured in a way that would make it serve wrongly.
    Raised at startup, so the pod crash-loops with this message instead of
    answering every call with a puzzling 401 or 403."""


class LogSettings(BaseModel):
    level: str = "INFO"
    render: Literal["console", "json"] = "console"


class SecuritySettings(BaseModel):
    # token -> service name. In shared environments these come from the secrets
    # store (env vars or SECRETS_DIR files), never from a file in this repo.
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
    #: Sent on every /api/chat and /api/embed call. Ollama unloads a model
    #: after this long without calls (its own default is 5m), and every AIR
    #: service sharing the instance then pays the reload. Setting it here, once,
    #: spares each consumer its own warm-up workaround. Keep it in step with the
    #: Makefile's `ollama-warm`, or the first real request undoes a manual warm.
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
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300
    #: Caps the in-process cache regardless of TTL — a write-once entry (the
    #: common case for a distinct prompt) is only reclaimed lazily, on its own
    #: next lookup, so TTL alone does not bound memory. See gateway/cache.py.
    cache_max_entries: int = 10_000
    #: Which models serve each class and embedding name (gateway/models.py), and
    #: the per-model price table. Owner-managed YAML: a model change or a reprice
    #: is a config edit plus restart, not a release. Locally a missing file falls
    #: back to built-in tables; elsewhere the gateway refuses to start without it.
    routing_config_path: str = "deploy/routing.yaml"
    pricing_config_path: str = "deploy/pricing.yaml"
    #: Deadline on a single provider attempt. Exceeding it fails that rung over to
    #: the next in the chain, so a hung upstream cannot hold a caller's connection.
    request_timeout_seconds: float = 60.0
    #: Extra passes over a chain after every rung failed, when at least one
    #: failure was transient (timeout, 429, 5xx, dropped connection). The cloud
    #: SDKs' own retries are switched off so this is the only retry layer.
    #: Streams only (`stream: true`): the longest a stream may go silent after
    #: its first token before the gateway gives up on it. Time to first token
    #: is request_timeout_seconds, the same per-attempt deadline as any call.
    stream_idle_timeout_seconds: float = Field(default=30.0, gt=0)
    retry_attempts: int = Field(default=1, ge=0, le=5)
    #: Backoff before retry n is base * 2**(n-1), with ±50% jitter so replicas
    #: retrying the same outage do not arrive in lockstep.
    retry_backoff_seconds: float = Field(default=0.5, ge=0)
    #: Consecutive transient failures after which a (provider, model) rung is
    #: skipped without being called, until breaker_reset_seconds pass and one
    #: trial call is let through.
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_reset_seconds: float = Field(default=30.0, gt=0)


class RedisSettings(BaseModel):
    """Shared state for the response cache and the rate limiter.

    Unset (the default) keeps both in process memory, which is correct for a
    single replica and for tests. Set it before running more than one replica:
    otherwise each replica caches separately and enforces its own copy of every
    rate limit, multiplying the effective limit by the replica count.
    """

    #: e.g. redis://:password@host:6379/0 (rediss:// for TLS).
    url: str = ""
    #: Namespaces every key, so the gateway can share a Redis with other services.
    key_prefix: str = "air-model-gateway:"
    #: Redis sits on the request path, so a slow Redis must not become a slow
    #: gateway: past this, the call degrades (cache miss, local rate limit).
    socket_timeout_seconds: float = Field(default=0.25, gt=0)


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
    #: The build: a git revision, `-dirty` when built from an uncommitted tree.
    #: Not configuration: the image bakes it in (Dockerfile `REVISION` build
    #: arg) and `make run` sets it from the checkout, so /v1/health, the
    #: startup log and `build_info` can say exactly which code answered.
    revision: str = "unknown"
    log: LogSettings = Field(default_factory=LogSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    self_hosted: SelfHostedSettings = Field(default_factory=SelfHostedSettings)
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)

    @property
    def is_local(self) -> bool:
        """Local is the one environment where convenience may trump strictness
        (policy ``default_allow``) — staging and prod both fail closed."""
        return self.environment == Environment.LOCAL

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        secrets_dir = os.environ.get("AIR_MODEL_GATEWAY_SECRETS_DIR", "")
        return (
            init_settings,
            env_settings,
            _SecretsDirSource(settings_cls, Path(secrets_dir) if secrets_dir else None),
            dotenv_settings,
        )


class _SecretsDirSource(EnvSettingsSource):
    """Settings read from a directory of files, one per env var name.

    Built on the env source so a file is parsed exactly like the variable it
    stands in for, `__` nesting and the service-token CSV included. Hidden
    entries are skipped: Kubernetes secret volumes keep their bookkeeping in
    `..data`-style dot entries next to the real files.
    """

    def __init__(self, settings_cls: type[BaseSettings], directory: Path | None) -> None:
        self._directory = directory
        super().__init__(settings_cls)

    def _load_env_vars(self) -> dict[str, Any]:
        if self._directory is None or not self._directory.is_dir():
            return {}
        return {
            entry.name.lower(): entry.read_text().strip()
            for entry in self._directory.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        }


#: `AIR_MODEL_GATEWAY_*` variables that aren't settings: the secrets-dir
#: switch, compose interpolation vars the Makefile exports, and the reference
#: client's own.
NOT_SETTINGS: frozenset[str] = frozenset(
    {
        "AIR_MODEL_GATEWAY_SECRETS_DIR",
        "AIR_MODEL_GATEWAY_OLLAMA_URL",
        "AIR_MODEL_GATEWAY_REDIS_URL",
        "AIR_MODEL_GATEWAY_URL",
        "AIR_MODEL_GATEWAY_TOKEN",
    }
)


def setting_names() -> set[str]:
    """Every environment variable a setting is read from, e.g.
    `AIR_MODEL_GATEWAY_OLLAMA__BASE_URL`."""
    names: set[str] = set()

    def walk(model: type[BaseModel], prefix: str) -> None:
        for field, info in model.model_fields.items():
            kind = info.annotation
            if isinstance(kind, type) and issubclass(kind, BaseModel):
                walk(kind, f"{prefix}{field.upper()}__")
            else:
                names.add(f"{prefix}{field.upper()}")

    walk(Settings, "AIR_MODEL_GATEWAY_")
    return names


def unknown_keys(environ: Mapping[str, str], env_file: str | Path = ".env") -> list[str]:
    """`AIR_MODEL_GATEWAY_*` names, in the environment or the `.env` file, that
    no setting reads. Settings ignore extra keys, so a misspelt or retired one
    (a `.env` copied from an old `.env.example`) would otherwise do nothing,
    silently. Names only: the values may be secrets."""
    keys = set(environ)
    if Path(env_file).is_file():
        keys |= set(dotenv_values(env_file))
    known = setting_names() | NOT_SETTINGS
    return sorted(
        k for k in keys if k.upper().startswith("AIR_MODEL_GATEWAY_") and k.upper() not in known
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
