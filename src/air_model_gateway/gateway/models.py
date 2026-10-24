"""What a caller's model choice means: the model catalog.

Callers never name models. POST /v1/generate takes a class (`local`, `auto`,
`cloud`) and optionally a preferred provider; POST /v1/embed takes a published
embedding name (`local-768-v1`). This module turns that choice into the chain of
(provider, model) rungs the router runs, from deploy/routing.yaml: global
chains per class, per-service overrides, and the embedding models. A `pin`
(an exact `provider:model`, for granted operator tools) bypasses the classes.

Whether a service may make a given choice is the policy's call
(policy/engine.py); this module only says what a permitted choice means, and
rejects choices that mean nothing (an unknown name, a provider not in the class).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from air_model_gateway.constants import ModelClass, Provider
from air_model_gateway.gateway.router import Chain
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.schemas.gateway import EmbedModel, RouteTarget, RoutingDoc

log = get_logger("gateway.models")


class ModelSelectionError(ValueError):
    """The caller's model choice means nothing here: the API answers 422."""


# Built-in routing for a bare checkout or a unit test, used only when no
# routing file is present locally (outside local a missing file stops startup).
# The authoritative table is deploy/routing.yaml.
DEFAULT_ROUTING: Final[RoutingDoc] = RoutingDoc.model_validate(
    {
        "generate": {
            "local": [{"provider": "ollama", "model": "llama3.2:3b"}],
            "auto": [
                {"provider": "ollama", "model": "llama3.2:3b"},
                {"provider": "anthropic", "model": "claude-haiku-4-5"},
            ],
            "cloud": [
                {"provider": "anthropic", "model": "claude-haiku-4-5"},
                {"provider": "openai", "model": "gpt-4o-mini"},
            ],
        },
        "embed": {
            "local-768-v1": {"provider": "ollama", "model": "nomic-embed-text", "dimensions": 768}
        },
    }
)


def _chain(targets: list[RouteTarget]) -> Chain:
    return [(t.provider, t.model) for t in targets]


class ModelCatalog:
    def __init__(self, doc: RoutingDoc) -> None:
        self.doc = doc

    @classmethod
    def load(cls, path: str | Path) -> ModelCatalog:
        p = Path(path)
        if not p.is_file():
            log.warning("routing.config_missing", path=str(p), using="built-in routing")
            return cls(DEFAULT_ROUTING)
        doc = RoutingDoc.model_validate(yaml.safe_load(p.read_text()) or {})
        log.info(
            "routing.loaded",
            path=str(p),
            embed=sorted(doc.embed),
            overrides=sorted(doc.overrides),
        )
        return cls(doc)

    def generate_chain(
        self, service: str, model_class: ModelClass, provider: Provider | None = None
    ) -> Chain:
        """The chain for a class, as this service sees it. A preferred provider's
        rungs move to the front; the rest stay as fallback ("prefer", not "only")."""
        chain = self.class_chain(service, model_class)
        if provider is None:
            return chain
        preferred = [r for r in chain if r[0] is provider]
        if not preferred:
            offered = sorted({p.value for p, _ in chain})
            raise ModelSelectionError(
                f"provider '{provider.value}' doesn't serve '{model_class.value}' here; "
                f"it offers {offered}"
            )
        return preferred + [r for r in chain if r[0] is not provider]

    def class_chain(self, service: str, model_class: ModelClass) -> Chain:
        override = self.doc.overrides.get(service)
        if override is not None and model_class in override.generate:
            return _chain(override.generate[model_class])
        return _chain(self.doc.generate[model_class])

    def embed_model(self, name: str) -> EmbedModel:
        spec = self.doc.embed.get(name)
        if spec is None:
            raise ModelSelectionError(
                f"'{name}' is not a published embedding model; published: {sorted(self.doc.embed)}"
            )
        return spec

    @staticmethod
    def pin_chain(pin: str) -> Chain:
        """An exact `provider:model`: one rung, no fallback."""
        prefix, sep, model = pin.partition(":")
        try:
            provider = Provider(prefix)
        except ValueError:
            provider = None
        if not sep or not model or provider is None:
            raise ModelSelectionError(
                f"pin must be provider:model with a provider in {sorted(p.value for p in Provider)}"
            )
        return [(provider, model)]

    def describe(self, service: str) -> dict[str, dict[str, Any]]:
        """What this service's choices mean, for GET /v1/capabilities."""
        return {
            "generate": {
                cls.value: [f"{p.value}:{m}" for p, m in self.class_chain(service, cls)]
                for cls in ModelClass
            },
            "embed": {
                name: {
                    "provider": spec.provider.value,
                    "model": spec.model,
                    "dimensions": spec.dimensions,
                }
                for name, spec in sorted(self.doc.embed.items())
            },
        }
