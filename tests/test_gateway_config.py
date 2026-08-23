"""air-llm's DevOps-owned config: routing table and price book.

These also guard the files actually shipped in deploy/ — a typo there would
otherwise only surface as a mis-routed or unpriced call at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from air_llm.constants import Provider
from air_llm.gateway.cost import DEFAULT_PRICING, PriceBook
from air_llm.gateway.router import DEFAULT_ALIASES, load_aliases
from air_llm.schemas.gateway import RoutingDoc
from air_llm.schemas.models import Usage

ROUTING = "deploy/routing.yaml"
PRICING = "deploy/pricing.yaml"


def test_shipped_routing_config_loads() -> None:
    aliases = load_aliases(ROUTING)
    assert set(aliases) >= {"fast", "smart", "embed"}
    # Every chain names a provider air-llm can actually build.
    for chain in aliases.values():
        assert chain
        for provider, model in chain:
            assert isinstance(provider, Provider)
            assert model


def test_shipped_routing_and_pricing_agree() -> None:
    """Every cloud model a chain can reach must have a price.

    An unpriced model still serves, but bills as 0.0 — the silent-overspend case
    the price table exists to prevent.
    """
    prices = PriceBook.load(PRICING)
    for alias, chain in load_aliases(ROUTING).items():
        for provider, model in chain:
            if prices.is_free(provider, model):
                continue
            usage = Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
            assert prices.price(provider, model, usage) > 0, f"{alias}: {provider.value}:{model}"


def test_missing_config_falls_back_to_builtin(tmp_path: Path) -> None:
    absent = tmp_path / "nope.yaml"
    assert load_aliases(absent) == DEFAULT_ALIASES
    book = PriceBook.load(absent)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
    assert book.price(Provider.ANTHROPIC, "claude-sonnet-5", usage) == pytest.approx(3.0)


def test_config_overrides_builtin_chain(tmp_path: Path) -> None:
    cfg = tmp_path / "routing.yaml"
    cfg.write_text(
        "aliases:\n"
        "  fast:\n"
        "    - provider: openai\n"
        "      model: gpt-4o-mini\n"
    )
    assert load_aliases(cfg) == {"fast": [(Provider.OPENAI, "gpt-4o-mini")]}


def test_empty_alias_chain_is_rejected_at_load() -> None:
    with pytest.raises(ValidationError):
        RoutingDoc.model_validate({"aliases": {"fast": []}})


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
