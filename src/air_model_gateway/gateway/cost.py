from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml

from air_model_gateway.constants import Provider
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.observability.metrics import MODEL_UNPRICED
from air_model_gateway.schemas.gateway import ModelPrice, PricingDoc
from air_model_gateway.schemas.models import Usage

log = get_logger("gateway.cost")

_TOKENS_PER_MTOK: Final[float] = 1_000_000.0

# Providers whose compute you own — electricity, not tokens. SELF_HOSTED is
# unconditionally free the same way OLLAMA is: the identity itself means "a
# backend you run", not "a paid API that happens to speak the OpenAI wire
# format" — pointing that settings slot at a paid compatible API instead is a
# misconfiguration this pricing model doesn't defend against, same as it
# wouldn't for a billed proxy placed in front of Ollama.
_FREE_PROVIDERS: Final[frozenset[Provider]] = frozenset({Provider.OLLAMA, Provider.SELF_HOSTED})

# Built-in fallback so the gateway still prices sanely with no config mounted
# (a bare `uvicorn air_model_gateway.main:app` from a checkout, or a unit test). The
# authoritative table is deploy/pricing.yaml.
DEFAULT_PRICING: Final[PricingDoc] = PricingDoc(
    cache_read_ratio=0.1,
    cache_write_ratio=1.25,
    local_prefixes=["llama", "qwen", "mistral", "mixtral", "gemma", "phi", "deepseek"],
    models={
        "claude-haiku-4-5": ModelPrice(input=1.00, output=5.00),
        "claude-sonnet-5": ModelPrice(input=3.00, output=15.00),
        "gpt-4o-mini": ModelPrice(input=0.15, output=0.60),
        "gpt-4.1-mini": ModelPrice(input=0.40, output=1.60),
        "gpt-4.1": ModelPrice(input=2.00, output=8.00),
        # Thinking tokens bill at the output rate and the adapter folds them
        # into completion_tokens, so they need no row of their own.
        "gemini-2.5-flash": ModelPrice(input=0.30, output=2.50),
        "gemini-3.1-pro-preview": ModelPrice(input=2.00, output=12.00),
        # Embedding models bill input only.
        "text-embedding-3-small": ModelPrice(input=0.02),
        "text-embedding-3-large": ModelPrice(input=0.13),
        "gemini-embedding-001": ModelPrice(input=0.15),
    },
)


class PriceBook:
    """Prices a call's token usage from a DevOps-owned table.

    A model the table does not know prices at 0.0 rather than failing the call —
    an inference that already ran must still return — but the omission is counted
    on ``air_model_gateway_model_unpriced_total`` so it surfaces as a metric instead of as
    silently free spend.
    """

    def __init__(self, doc: PricingDoc) -> None:
        self._doc = doc
        self._prefixes = tuple(p.lower() for p in doc.local_prefixes)

    @classmethod
    def load(cls, path: str | Path | None) -> PriceBook:
        if path is None:
            return cls(DEFAULT_PRICING)
        p = Path(path)
        if not p.exists():
            log.warning("pricing.config_missing", path=str(p), using="built-in table")
            return cls(DEFAULT_PRICING)
        raw = yaml.safe_load(p.read_text()) or {}
        doc = PricingDoc.model_validate(raw)
        log.info("pricing.loaded", path=str(p), models=len(doc.models))
        return cls(doc)

    def is_free(self, provider: Provider, model: str) -> bool:
        """Whether the call costs nothing — locally served, so electricity not tokens."""
        return provider in _FREE_PROVIDERS or model.strip().lower().startswith(self._prefixes)

    def is_priced(self, provider: Provider, model: str) -> bool:
        """Whether a call to this model can be costed: free, or in the table. A
        model that isn't would bill at the provider and count $0 against every
        budget, so the caller may not pin it and a chain may not name it."""
        return self.is_free(provider, model) or model.strip().lower() in self._doc.models

    def price(self, provider: Provider, model: str, usage: Usage) -> float:
        if self.is_free(provider, model):
            return 0.0
        entry = self._doc.models.get(model.strip().lower())
        if entry is None:
            MODEL_UNPRICED.labels(provider.value, model).inc()
            return 0.0
        return round(
            (
                usage.prompt_tokens * entry.input
                + usage.completion_tokens * entry.output
                + usage.cache_read_tokens * entry.input * self._doc.cache_read_ratio
                + usage.cache_write_tokens * entry.input * self._doc.cache_write_ratio
            )
            / _TOKENS_PER_MTOK,
            6,
        )
