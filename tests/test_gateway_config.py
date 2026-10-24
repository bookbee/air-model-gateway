"""air-model-gateway's owner-managed config: routing, policy and prices.

These also guard the files actually shipped in deploy/ — a typo there would
otherwise only surface as a mis-routed or unpriced call at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from air_model_gateway.constants import LOCAL_PROVIDERS, Action, ModelClass, Provider
from air_model_gateway.gateway.cost import DEFAULT_PRICING, PriceBook
from air_model_gateway.gateway.models import DEFAULT_ROUTING, ModelCatalog
from air_model_gateway.policy.engine import PolicyEngine
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.gateway import RoutingDoc
from air_model_gateway.schemas.models import Usage

ROUTING = "deploy/routing.yaml"
#: Every shipped policy file: a consumer must be granted in each environment,
#: or it works locally and 403s the day it is deployed.
POLICIES = [
    "deploy/policy/policy.local.yaml",
    "deploy/policy/policy.staging.yaml",
    "deploy/policy/policy.prod.yaml",
]
PRICING = "deploy/pricing.yaml"


def _catalog() -> ModelCatalog:
    return ModelCatalog.load(ROUTING)


def _all_chains(catalog: ModelCatalog) -> dict[str, list[tuple[Provider, str]]]:
    """Every chain a call can run: each class, globally and per service, and each
    embedding model."""
    doc = catalog.doc
    chains = {f"generate.{c.value}": catalog.class_chain("", c) for c in ModelClass}
    for service, routes in doc.overrides.items():
        for c in routes.generate:
            chains[f"{service}.{c.value}"] = catalog.class_chain(service, c)
    for name, spec in doc.embed.items():
        chains[f"embed.{name}"] = [(spec.provider, spec.model)]
    return chains


def test_shipped_routing_config_loads() -> None:
    catalog = _catalog()
    assert set(catalog.doc.generate) == set(ModelClass)
    assert "local-768-v1" in catalog.doc.embed


def test_shipped_routing_and_pricing_agree() -> None:
    """Every cloud model a chain can reach must have a price.

    An unpriced model still serves, but bills as 0.0 — the silent-overspend case
    the price table exists to prevent.
    """
    prices = PriceBook.load(PRICING)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
    for where, chain in _all_chains(_catalog()).items():
        for provider, model in chain:
            if not prices.is_free(provider, model):
                assert prices.price(provider, model, usage) > 0, (
                    f"{where}: {provider.value}:{model}"
                )


def test_missing_config_falls_back_to_builtin(tmp_path: Path) -> None:
    absent = tmp_path / "nope.yaml"
    assert ModelCatalog.load(absent).doc == DEFAULT_ROUTING
    book = PriceBook.load(absent)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
    assert book.price(Provider.ANTHROPIC, "claude-sonnet-5", usage) == pytest.approx(3.0)


def test_an_empty_routing_file_is_rejected_not_silently_replaced(tmp_path: Path) -> None:
    cfg = tmp_path / "routing.yaml"
    cfg.write_text("")
    with pytest.raises(ValidationError):
        ModelCatalog.load(cfg)


_LOCAL = [{"provider": "ollama", "model": "m"}]
_CLOUD = [{"provider": "openai", "model": "m"}]


@pytest.mark.parametrize(
    ("doc", "why"),
    [
        ({"generate": {"local": _LOCAL, "auto": _LOCAL}}, "every class needs a chain"),
        ({"generate": {"local": _CLOUD, "auto": _LOCAL, "cloud": _CLOUD}}, "local means local"),
        ({"generate": {"local": _LOCAL, "auto": _LOCAL, "cloud": _LOCAL}}, "cloud means cloud"),
        ({"generate": {"local": [], "auto": _LOCAL, "cloud": _CLOUD}}, "no empty chains"),
    ],
)
def test_routing_classes_keep_their_promise(doc: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError):
        RoutingDoc.model_validate(doc)


_CLASSES = {"local": _LOCAL, "auto": _LOCAL, "cloud": _CLOUD}


@pytest.mark.parametrize(
    "embed",
    [
        {"rag": {"provider": "ollama", "model": "m", "dimensions": 768}},  # not a published name
        {"local-768-v1": {"provider": "ollama", "model": "m", "dimensions": 1024}},  # wrong dims
        {"local-768-v1": {"provider": "openai", "model": "m", "dimensions": 768}},  # not local
        {"cloud-768-v1": {"provider": "ollama", "model": "m", "dimensions": 768}},  # not cloud
    ],
)
def test_an_embedding_name_must_say_what_it_is(embed: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RoutingDoc.model_validate({"generate": _CLASSES, "embed": embed})


def test_unknown_model_prices_at_zero() -> None:
    book = PriceBook.load(None)
    usage = Usage(prompt_tokens=1_000, completion_tokens=1_000, total_tokens=2_000)
    assert book.price(Provider.OPENAI, "gpt-does-not-exist", usage) == 0.0


def test_local_models_are_free_whoever_serves_them() -> None:
    book = PriceBook.load(None)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    assert book.price(Provider.OLLAMA, "claude-sonnet-5", usage) == 0.0  # provider wins
    assert book.price(Provider.OPENAI, "llama3.2:3b", usage) == 0.0  # prefix wins


def test_self_hosted_is_free_regardless_of_model_name() -> None:
    """SELF_HOSTED is free by provider identity, not by name — a model like
    "google/gemma-4-e2b" doesn't match any local_prefixes entry (it starts
    with "google/"), so this only holds if the provider check catches it."""
    book = PriceBook.load(None)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    assert book.price(Provider.SELF_HOSTED, "google/gemma-4-e2b", usage) == 0.0
    assert not "google/gemma-4-e2b".startswith(tuple(DEFAULT_PRICING.local_prefixes))


def test_cache_tokens_bill_at_the_published_ratios() -> None:
    book = PriceBook(DEFAULT_PRICING)
    usage = Usage(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    # input rate 3.00/Mtok -> read at 10% (0.30) + write at 2x (6.00)
    assert book.price(Provider.ANTHROPIC, "claude-sonnet-5", usage) == pytest.approx(6.30)


#: What each consumer asks of the gateway, by operation: the cross-repo
#: contract. A consumer names a class or an embedding model in its own code, so a
#: grant or a published name it relies on disappearing here 403s or 422s it.
CONSUMERS: dict[str, dict[str, set[str]]] = {
    "air-classifier-service": {"generate": {"local", "auto", "cloud"}},
    "air-orchestrator-service": {"generate": {"cloud"}},
    "air-tools": {"generate": {"cloud"}},
    "air-rag": {"embed": {"local-768-v1"}},
    "air-web-console": {"generate": {"auto"}},  # plus `pin` for exact models
}


@pytest.mark.parametrize("policy_file", POLICIES)
@pytest.mark.parametrize("service", sorted(CONSUMERS))
def test_every_consumer_choice_exists_and_is_granted(service: str, policy_file: str) -> None:
    catalog = _catalog()
    # permit_default_allow=False: the grant must be explicit, as in staging/prod.
    policy = PolicyEngine.load(policy_file, permit_default_allow=False)
    who = Principal(service=service)
    for action, choices in CONSUMERS[service].items():
        assert policy.decide(who, "inference", Action(action)).allow
        for choice in choices:
            if action == "generate":
                assert policy.may_use_class(who, ModelClass(choice)), f"{service}: {choice}"
            else:
                assert choice in catalog.doc.embed, f"{service}: {choice} unpublished"
                assert policy.may_use_embed_model(who, choice), f"{service}: {choice}"


@pytest.mark.parametrize("policy_file", POLICIES[1:])  # staging and prod
def test_only_the_operator_console_may_pin(policy_file: str) -> None:
    """Applications choose a class the owner governs; only the operator console
    tests exact models. A new pin grant should be a deliberate edit here."""
    policy = PolicyEngine.load(policy_file, permit_default_allow=False)
    pinning = {s for s in CONSUMERS if policy.may_pin(Principal(service=s))}
    assert pinning == {"air-web-console"}


def test_the_classifier_keeps_its_tier_semantics() -> None:
    """Its ladder depends on these: Tier 2 local and never failing over, Tier 3
    cloud-only (it already tried local), arbitration on the strongest models."""
    catalog = _catalog()
    service = "air-classifier-service"
    assert catalog.class_chain(service, ModelClass.LOCAL) == [(Provider.OLLAMA, "llama3.2:3b")]
    assert all(p not in LOCAL_PROVIDERS for p, _ in catalog.class_chain(service, ModelClass.AUTO))
    assert catalog.class_chain(service, ModelClass.CLOUD)[0][1] == "gemini-3.1-pro-preview"
