from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import yaml

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ModelProvider, ProviderError, ProviderResult
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.observability.metrics import PROVIDER_TIMEOUTS
from air_model_gateway.schemas.gateway import RouteTarget, RoutingDoc
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest

log = get_logger("gateway.router")

Chain = list[tuple[Provider, str]]

# Built-in fallback chains, used when no routing config is mounted (a bare
# checkout or a unit test). The authoritative table is deploy/routing.yaml.
DEFAULT_ALIASES: Final[dict[str, Chain]] = {
    "fast": [
        (Provider.OLLAMA, "llama3.2:3b"),
        (Provider.ANTHROPIC, "claude-haiku-4-5"),
    ],
    "smart": [
        (Provider.ANTHROPIC, "claude-sonnet-5"),
        (Provider.OPENAI, "gpt-4o-mini"),
        (Provider.GEMINI, "gemini-2.5-pro"),
    ],
    "embed": [
        (Provider.OLLAMA, "nomic-embed-text"),
        (Provider.OPENAI, "text-embedding-3-small"),
        (Provider.GEMINI, "gemini-embedding-001"),
    ],
}

# A provider that hangs is the failure mode being on-path makes everyone else's
# problem: without a deadline the caller's connection is held until *its* timeout,
# and the failover chain below never gets walked. 60s is generous for a
# completion and still an order of magnitude under any sane client budget.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0


def _to_chain(targets: list[RouteTarget]) -> Chain:
    return [(t.provider, t.model) for t in targets]


def load_aliases(path: str | Path | None) -> dict[str, Chain]:
    """Read the alias table from config, falling back to the built-in defaults."""
    if path is None:
        return dict(DEFAULT_ALIASES)
    p = Path(path)
    if not p.exists():
        log.warning("routing.config_missing", path=str(p), using="built-in aliases")
        return dict(DEFAULT_ALIASES)
    raw = yaml.safe_load(p.read_text()) or {}
    doc = RoutingDoc.model_validate(raw)
    if not doc.aliases:
        log.warning("routing.config_empty", path=str(p), using="built-in aliases")
        return dict(DEFAULT_ALIASES)
    log.info("routing.loaded", path=str(p), aliases=sorted(doc.aliases))
    return {name: _to_chain(targets) for name, targets in doc.aliases.items()}


class ModelRouter:
    def __init__(
        self,
        providers: dict[Provider, ModelProvider],
        default_provider: Provider,
        default_model: str,
        aliases: dict[str, Chain] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._providers = providers
        self._default = (default_provider, default_model)
        self._aliases = DEFAULT_ALIASES if aliases is None else aliases
        self._timeout = timeout_seconds

    def primary(self, model: str | None) -> Provider:
        """The first provider a model would route to, before any failover.

        Used to apply a per-provider rate limit ahead of the call.
        """
        return self.resolve(model)[0][0]

    def resolve(self, model: str | None) -> Chain:
        """Expand a model alias into an ordered (provider, concrete_model) chain."""
        if not model:
            return [self._default]
        if model in self._aliases:
            return list(self._aliases[model])
        if ":" in model:
            prefix, _, rest = model.partition(":")
            try:
                return [(Provider(prefix), rest)]
            except ValueError:
                pass  # not "provider:model"; treat the whole thing as a model name
        return [(self._default[0], model)]

    async def chat(self, req: ChatRequest) -> tuple[ProviderResult, Provider]:
        return await self._run(self.resolve(req.model), lambda p, m: p.chat(req, m))

    async def embeddings(self, req: EmbeddingsRequest) -> tuple[ProviderResult, Provider]:
        return await self._run(self.resolve(req.model), lambda p, m: p.embeddings(req, m))

    async def _run(
        self,
        chain: Chain,
        call: Callable[[ModelProvider, str], Awaitable[ProviderResult]],
    ) -> tuple[ProviderResult, Provider]:
        errors: list[str] = []
        for provider_kind, concrete in chain:
            provider = self._providers.get(provider_kind)
            if provider is None:
                errors.append(f"{provider_kind.value}: not registered")
                continue
            try:
                # A timeout is converted to a ProviderError rather than propagated,
                # so a hung provider degrades to the next rung of the chain exactly
                # as an error response does. Retry and circuit-breaking layer on
                # here later; the deadline is the part that cannot wait, because
                # without it there is no failover from a hang at all.
                result = await asyncio.wait_for(call(provider, concrete), timeout=self._timeout)
                return result, provider_kind
            except TimeoutError:
                PROVIDER_TIMEOUTS.labels(provider_kind.value).inc()
                log.warning(
                    "provider.timeout",
                    provider=provider_kind.value,
                    model=concrete,
                    timeout_seconds=self._timeout,
                )
                errors.append(f"{provider_kind.value}: timed out after {self._timeout}s")
                continue
            except ProviderError as exc:
                log.warning("provider.failed", provider=provider_kind.value, detail=exc.detail)
                errors.append(str(exc))
                continue
        raise ProviderError(chain[0][0], f"all providers failed: {'; '.join(errors)}")

    async def health(self) -> dict[str, bool]:
        return {p.value: await prov.health() for p, prov in self._providers.items()}

    async def aclose(self) -> None:
        for prov in self._providers.values():
            await prov.aclose()
