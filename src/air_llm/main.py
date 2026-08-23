from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from air_llm import __version__
from air_llm.api.errors import install_error_handlers
from air_llm.api.middleware import install_middleware
from air_llm.api.v1.router import api_router
from air_llm.config import Settings, get_settings
from air_llm.constants import Provider
from air_llm.gateway.base import ModelProvider
from air_llm.gateway.cache import ResponseCache
from air_llm.gateway.cost import PriceBook
from air_llm.gateway.providers.anthropic_provider import AnthropicProvider
from air_llm.gateway.providers.gemini_provider import GeminiProvider
from air_llm.gateway.providers.ollama_provider import OllamaProvider
from air_llm.gateway.providers.openai_provider import OpenAIProvider
from air_llm.gateway.providers.self_hosted_provider import SelfHostedProvider
from air_llm.gateway.router import ModelRouter, load_aliases
from air_llm.observability.logging import configure_logging, get_logger
from air_llm.observability.metrics import render_latest
from air_llm.policy.engine import PolicyEngine
from air_llm.policy.ratelimit import RateLimiter
from air_llm.security.apikeys import ApiKeyVerifier


def _build_model_router(settings: Settings) -> ModelRouter:
    providers: dict[Provider, ModelProvider] = {
        Provider.OLLAMA: OllamaProvider(
            settings.ollama.base_url, keep_alive=settings.ollama.keep_alive
        ),
        Provider.ANTHROPIC: AnthropicProvider(settings.providers.anthropic_api_key),
        Provider.OPENAI: OpenAIProvider(settings.providers.openai_api_key),
        Provider.GEMINI: GeminiProvider(settings.providers.gemini_api_key),
        Provider.SELF_HOSTED: SelfHostedProvider(
            settings.self_hosted.base_url, settings.self_hosted.api_key
        ),
    }
    return ModelRouter(
        providers,
        settings.gateway.default_provider,
        settings.gateway.default_model,
        aliases=load_aliases(settings.gateway.routing_config_path),
        timeout_seconds=settings.gateway.request_timeout_seconds,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    log = get_logger("main")

    policy = PolicyEngine.load(
        settings.policy.config_path,
        default_rate_per_minute=settings.policy.default_rate_per_minute,
    )
    verifier = ApiKeyVerifier(settings.security.service_tokens)
    limiter = RateLimiter(default_per_minute=settings.policy.default_rate_per_minute)
    model_router = _build_model_router(settings)
    prices = PriceBook.load(settings.gateway.pricing_config_path)
    cache = ResponseCache(
        ttl_seconds=settings.gateway.cache_ttl_seconds,
        enabled=settings.gateway.cache_enabled,
        max_entries=settings.gateway.cache_max_entries,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("air-llm.start", environment=settings.environment.value, version=__version__)
        yield
        await model_router.aclose()
        log.info("air-llm.stop")

    app = FastAPI(title="air-llm", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.policy = policy
    app.state.verifier = verifier
    app.state.limiter = limiter
    app.state.router = model_router
    app.state.prices = prices
    app.state.cache = cache

    install_middleware(app)
    install_error_handlers(app)
    app.include_router(api_router, prefix="/v1")

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    return app


app = create_app()
