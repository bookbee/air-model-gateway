from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response

from air_model_gateway import __version__
from air_model_gateway.api import openapi
from air_model_gateway.api.errors import install_error_handlers
from air_model_gateway.api.middleware import install_middleware
from air_model_gateway.api.v1.model_calls import RESOURCE
from air_model_gateway.api.v1.router import api_router
from air_model_gateway.config import ConfigError, Settings, get_settings, unknown_keys
from air_model_gateway.constants import Action, ModelClass, Provider
from air_model_gateway.gateway.base import ModelProvider
from air_model_gateway.gateway.cache import (
    MemoryResponseCache,
    RedisResponseCache,
    ResponseCache,
)
from air_model_gateway.gateway.cost import PriceBook
from air_model_gateway.gateway.models import ModelCatalog
from air_model_gateway.gateway.providers.anthropic_provider import AnthropicProvider
from air_model_gateway.gateway.providers.gemini_provider import GeminiProvider
from air_model_gateway.gateway.providers.ollama_provider import OllamaProvider
from air_model_gateway.gateway.providers.openai_provider import OpenAIProvider
from air_model_gateway.gateway.providers.self_hosted_provider import SelfHostedProvider
from air_model_gateway.gateway.router import Admit, Chain, ModelRouter
from air_model_gateway.observability.logging import configure_logging, get_logger
from air_model_gateway.observability.metrics import BUILD_INFO, QUOTA_REJECTED, render_latest
from air_model_gateway.policy.budget import MemorySpendLedger, RedisSpendLedger, SpendLedger
from air_model_gateway.policy.engine import PolicyEngine
from air_model_gateway.policy.ratelimit import MemoryRateLimiter, RateLimiter, RedisRateLimiter
from air_model_gateway.redis_store import RedisStore, connect
from air_model_gateway.schemas.common import Principal
from air_model_gateway.security.apikeys import ApiKeyVerifier

# The rate-limit "service" a provider quota's bucket is kept under: it is one
# bucket for all of them. Not a valid service name, so it can't collide.
_ALL_SERVICES = "*"


def _check_shared_environment(settings: Settings) -> None:
    """Outside local, refuse to start on configuration that can only be a mistake.

    Locally a missing file falls back to a built-in table so a bare checkout
    boots. In staging or prod that fallback would hide the error: a mistyped
    policy path denies every call, a missing routing file serves the built-in
    chains instead of the reviewed ones, and no tokens means every call is 401.
    """
    if settings.is_local:
        return
    problems = [
        f"{name} file not found: {path}"
        for name, path in (
            ("policy", settings.policy.config_path),
            ("routing", settings.gateway.routing_config_path),
            ("pricing", settings.gateway.pricing_config_path),
        )
        if not Path(path).is_file()
    ]
    if not problems:
        problems += _unpublished_embed_grants(settings)
        problems += _unpriced_rungs(settings)
        problems += _unbudgeted_services(settings)
    if not settings.security.service_tokens:
        problems.append("no service tokens configured (AIR_MODEL_GATEWAY_SECURITY__SERVICE_TOKENS)")
    if problems:
        raise ConfigError(f"{settings.environment.value}: " + "; ".join(problems))


def _unpublished_embed_grants(settings: Settings) -> list[str]:
    """Policy grants of embedding names the routing file doesn't publish: a typo
    there would 422 a consumer on every call, so it stops startup instead."""
    catalog = ModelCatalog.load(settings.gateway.routing_config_path)
    policy = PolicyEngine.load(settings.policy.config_path, permit_default_allow=False)
    return [
        f"policy grants '{name}' to {service}, but routing publishes no such embedding model"
        for service, sp in policy.doc.services.items()
        for name in sp.embed_models or []
        if name not in catalog.doc.embed
    ]


def _unpriced_rungs(settings: Settings) -> list[str]:
    """Paid models a chain can reach that the price table doesn't know: each
    would serve, bill at the provider, and count $0 against every budget."""
    catalog = ModelCatalog.load(settings.gateway.routing_config_path)
    prices = PriceBook.load(settings.gateway.pricing_config_path)
    return [
        f"routing {where} names {provider.value}:{model}, which pricing doesn't list"
        for where, chain in catalog.all_chains().items()
        for provider, model in chain
        if not prices.is_priced(provider, model)
    ]


def _unbudgeted_services(settings: Settings) -> list[str]:
    """Services that can reach a paid model but have no daily_budget_usd.

    On a gateway many projects share, an uncapped service is unlimited spend
    on the shared provider accounts: one runaway loop bills everyone's
    month. So outside local, a grant that can reach a paid model (a class
    whose chain has a paid rung, a paid embedding model, or `pin`) needs a
    budget. A service limited to local models needs none.
    """
    catalog = ModelCatalog.load(settings.gateway.routing_config_path)
    policy = PolicyEngine.load(settings.policy.config_path, permit_default_allow=False)
    prices = PriceBook.load(settings.gateway.pricing_config_path)
    problems = []
    for service, sp in policy.doc.services.items():
        if sp.daily_budget_usd is not None:
            continue
        who = Principal(service=service)
        actions = set(sp.resources.get(RESOURCE, []))
        chains: list[Chain] = []
        if Action.GENERATE in actions:
            chains += [
                catalog.class_chain(service, c) for c in ModelClass if policy.may_use_class(who, c)
            ]
        if Action.EMBED in actions:
            chains += [
                [(spec.provider, spec.model)]
                for name, spec in catalog.doc.embed.items()
                if policy.may_use_embed_model(who, name)
            ]
        paid = (Action.GENERATE in actions and sp.pin) or any(
            not prices.is_free(p, m) for chain in chains for p, m in chain
        )
        if paid:
            problems.append(f"policy gives {service} paid models but no daily_budget_usd")
    return problems


def _build_model_router(
    settings: Settings, catalog: ModelCatalog, admit: Admit | None = None
) -> ModelRouter:
    providers: dict[Provider, ModelProvider] = {
        Provider.OLLAMA: OllamaProvider(
            settings.ollama.base_url, keep_alive=settings.ollama.keep_alive
        ),
        Provider.ANTHROPIC: AnthropicProvider(settings.providers.anthropic_api_key),
        Provider.OPENAI: OpenAIProvider(settings.providers.openai_api_key),
        Provider.GEMINI: GeminiProvider(
            settings.providers.gemini_api_key,
            thinking_budgets=catalog.doc.thinking_budgets(Provider.GEMINI),
        ),
        Provider.SELF_HOSTED: SelfHostedProvider(
            settings.self_hosted.base_url, settings.self_hosted.api_key
        ),
    }
    return ModelRouter(
        providers,
        timeout_seconds=settings.gateway.request_timeout_seconds,
        retry_attempts=settings.gateway.retry_attempts,
        retry_backoff_seconds=settings.gateway.retry_backoff_seconds,
        breaker_failure_threshold=settings.gateway.breaker_failure_threshold,
        breaker_reset_seconds=settings.gateway.breaker_reset_seconds,
        admit=admit,
    )


def _build_shared_state(
    settings: Settings, redis: RedisStore | None
) -> tuple[ResponseCache, RateLimiter, SpendLedger]:
    """Redis-backed cache, limiter and spend ledger when Redis is configured,
    else in-process."""
    gw = settings.gateway
    rpm = settings.policy.default_rate_per_minute
    if redis is None:
        cache: ResponseCache = MemoryResponseCache(
            ttl_seconds=gw.cache_ttl_seconds,
            enabled=gw.cache_enabled,
            max_entries=gw.cache_max_entries,
        )
        return cache, MemoryRateLimiter(default_per_minute=rpm), MemorySpendLedger()
    return (
        RedisResponseCache(
            redis,
            ttl_seconds=gw.cache_ttl_seconds,
            enabled=gw.cache_enabled,
            fallback_max_entries=gw.cache_max_entries,
        ),
        RedisRateLimiter(redis, default_per_minute=rpm),
        RedisSpendLedger(redis),
    )


def _provider_quota(policy: PolicyEngine, limiter: RateLimiter) -> Admit:
    """The router's admission check for the policy's provider_quotas: one
    bucket per provider, shared by every service (and, with Redis, every
    replica)."""

    async def admit(provider: Provider) -> bool:
        quota = policy.provider_quota(provider)
        if quota is None:
            return True
        if await limiter.check(_ALL_SERVICES, f"provider:{provider.value}", quota):
            return True
        QUOTA_REJECTED.labels(provider.value).inc()
        return False

    return admit


def create_app(settings: Settings | None = None) -> FastAPI:
    from_environment = settings is None
    settings = settings or get_settings()
    configure_logging(settings)
    log = get_logger("main")
    _check_shared_environment(settings)
    if from_environment and (unknown := unknown_keys(os.environ)):
        log.warning("config.unknown_keys", keys=unknown, consequence="ignored; see .env.example")

    policy = PolicyEngine.load(
        settings.policy.config_path,
        default_rate_per_minute=settings.policy.default_rate_per_minute,
        permit_default_allow=settings.is_local,
    )
    verifier = ApiKeyVerifier(settings.security.service_tokens)
    redis = connect(settings.redis)
    if redis is None and not settings.is_local:
        log.warning(
            "redis.not_configured",
            consequence="cache and rate limits are per replica; run one replica only",
        )
    cache, limiter, ledger = _build_shared_state(settings, redis)
    catalog = ModelCatalog.load(settings.gateway.routing_config_path)
    model_router = _build_model_router(settings, catalog, _provider_quota(policy, limiter))
    prices = PriceBook.load(settings.gateway.pricing_config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "air-model-gateway.start",
            environment=settings.environment.value,
            version=__version__,
            revision=settings.revision,
            shared_state="redis" if redis is not None else "in-process",
        )
        yield
        await model_router.aclose()
        if redis is not None:
            await redis.aclose()
        log.info("air-model-gateway.stop")

    BUILD_INFO.labels(__version__, settings.revision).set(1)
    app = FastAPI(title="air-model-gateway", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.policy = policy
    app.state.verifier = verifier
    app.state.limiter = limiter
    app.state.router = model_router
    app.state.catalog = catalog
    app.state.prices = prices
    app.state.cache = cache
    app.state.ledger = ledger
    app.state.redis = redis

    install_middleware(app)
    install_error_handlers(app)
    openapi.install(app)
    app.include_router(api_router, prefix="/v1")

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    return app


app = create_app()
