"""The model catalog: what a caller's choice means (gateway/models.py)."""

from __future__ import annotations

import pytest

from air_model_gateway.constants import ModelClass, Provider
from air_model_gateway.gateway.models import ModelCatalog, ModelSelectionError
from air_model_gateway.schemas.gateway import RoutingDoc

DOC = RoutingDoc.model_validate(
    {
        "generate": {
            "local": [{"provider": "ollama", "model": "l"}],
            "auto": [
                {"provider": "ollama", "model": "l"},
                {"provider": "gemini", "model": "g"},
                {"provider": "anthropic", "model": "a"},
            ],
            "cloud": [
                {"provider": "gemini", "model": "g"},
                {"provider": "anthropic", "model": "a"},
            ],
        },
        "embed": {"local-768-v1": {"provider": "ollama", "model": "e", "dimensions": 768}},
        "overrides": {"svc": {"generate": {"cloud": [{"provider": "anthropic", "model": "s"}]}}},
    }
)
CATALOG = ModelCatalog(DOC)


def test_a_class_resolves_to_its_chain_and_a_service_override_wins() -> None:
    assert CATALOG.generate_chain("other", ModelClass.CLOUD) == [
        (Provider.GEMINI, "g"),
        (Provider.ANTHROPIC, "a"),
    ]
    assert CATALOG.generate_chain("svc", ModelClass.CLOUD) == [(Provider.ANTHROPIC, "s")]
    # An override replaces only the classes it names.
    assert CATALOG.generate_chain("svc", ModelClass.LOCAL) == [(Provider.OLLAMA, "l")]


def test_a_preferred_provider_goes_first_and_the_rest_stay_as_fallback() -> None:
    chain = CATALOG.generate_chain("other", ModelClass.AUTO, Provider.ANTHROPIC)
    assert chain == [(Provider.ANTHROPIC, "a"), (Provider.OLLAMA, "l"), (Provider.GEMINI, "g")]


def test_a_provider_the_class_does_not_offer_is_rejected() -> None:
    with pytest.raises(ModelSelectionError, match="doesn't serve 'local'"):
        CATALOG.generate_chain("other", ModelClass.LOCAL, Provider.ANTHROPIC)


@pytest.mark.parametrize(
    ("pin", "chain"),
    [
        ("anthropic:claude-haiku-4-5", [(Provider.ANTHROPIC, "claude-haiku-4-5")]),
        ("ollama:llama3.2:3b", [(Provider.OLLAMA, "llama3.2:3b")]),  # model names may hold ':'
        ("llama.cpp:llama-3.2-3b-gguf", [(Provider.SELF_HOSTED, "llama-3.2-3b-gguf")]),
    ],
)
def test_a_pin_is_one_exact_rung(pin: str, chain: list[tuple[Provider, str]]) -> None:
    assert ModelCatalog.pin_chain(pin) == chain


@pytest.mark.parametrize("pin", ["gpt-4o", "mistral:7b", "openai:"])
def test_a_pin_must_name_a_known_provider_and_a_model(pin: str) -> None:
    with pytest.raises(ModelSelectionError):
        ModelCatalog.pin_chain(pin)


def test_an_embedding_name_must_be_published() -> None:
    assert CATALOG.embed_model("local-768-v1").dimensions == 768
    with pytest.raises(ModelSelectionError, match="published: \\['local-768-v1'\\]"):
        CATALOG.embed_model("local-1024-v1")


def test_describe_shows_a_service_what_its_choices_mean() -> None:
    described = CATALOG.describe("svc")
    assert described["generate"]["cloud"] == ["anthropic:s"]
    assert described["embed"]["local-768-v1"]["dimensions"] == 768
