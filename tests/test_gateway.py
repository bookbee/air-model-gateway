from __future__ import annotations

import asyncio

import pytest

from air_llm.constants import Provider
from air_llm.gateway.base import ProviderError, ProviderResult
from air_llm.gateway.router import ModelRouter
from air_llm.schemas.models import ChatRequest, EmbeddingsRequest, Message, Usage


class FakeProvider:
    def __init__(self, name: Provider, *, fail: bool = False, hang: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.hang = hang

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        if self.hang:
            await asyncio.sleep(30)  # cancelled by the router's deadline
        if self.fail:
            raise ProviderError(self.name, "boom")
        return ProviderResult(
            model=model,
            content="hi",
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        return ProviderResult(model=model, embeddings=[[0.0]])

    async def health(self) -> bool:
        return not self.fail

    async def aclose(self) -> None:
        return None


def _req(model: str | None = None) -> ChatRequest:
    return ChatRequest(model=model, messages=[Message(role="user", content="x")])


async def test_default_route_uses_default_provider() -> None:
    router = ModelRouter({Provider.OLLAMA: FakeProvider(Provider.OLLAMA)}, Provider.OLLAMA, "m")
    result, provider = await router.chat(_req())
    assert provider == Provider.OLLAMA
    assert result.content == "hi"


async def test_alias_failover_walks_the_chain() -> None:
    providers = {
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC, fail=True),
        Provider.OPENAI: FakeProvider(Provider.OPENAI),
    }
    router = ModelRouter(providers, Provider.OPENAI, "m")
    # alias "smart" -> anthropic (fails) then openai (succeeds)
    result, provider = await router.chat(_req("smart"))
    assert provider == Provider.OPENAI
    assert result.content == "hi"


async def test_all_providers_failing_raises() -> None:
    router = ModelRouter(
        {Provider.OLLAMA: FakeProvider(Provider.OLLAMA, fail=True)}, Provider.OLLAMA, "m"
    )
    with pytest.raises(ProviderError):
        await router.chat(_req())


def test_resolve_explicit_provider_prefix() -> None:
    router = ModelRouter({Provider.OLLAMA: FakeProvider(Provider.OLLAMA)}, Provider.OLLAMA, "m")
    assert router.resolve("openai:gpt-4o-mini") == [(Provider.OPENAI, "gpt-4o-mini")]
    assert router.resolve(None) == [(Provider.OLLAMA, "m")]
    assert router.resolve("mistral-7b") == [(Provider.OLLAMA, "mistral-7b")]


async def test_a_hung_provider_fails_over_rather_than_hanging() -> None:
    """The reason the deadline exists: without it a hang is not an error, so the
    failover chain is never walked and the caller waits on its own timeout."""
    providers = {
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC, hang=True),
        Provider.OPENAI: FakeProvider(Provider.OPENAI),
    }
    router = ModelRouter(providers, Provider.OPENAI, "m", timeout_seconds=0.05)
    result, provider = await router.chat(_req("smart"))
    assert provider == Provider.OPENAI
    assert result.content == "hi"


async def test_every_provider_hanging_raises_with_the_timeout_named() -> None:
    router = ModelRouter(
        {Provider.OLLAMA: FakeProvider(Provider.OLLAMA, hang=True)},
        Provider.OLLAMA,
        "m",
        timeout_seconds=0.05,
    )
    with pytest.raises(ProviderError, match="timed out"):
        await router.chat(_req())


async def test_configured_aliases_replace_the_builtin_table() -> None:
    providers = {
        Provider.OLLAMA: FakeProvider(Provider.OLLAMA),
        Provider.OPENAI: FakeProvider(Provider.OPENAI),
    }
    router = ModelRouter(
        providers,
        Provider.OLLAMA,
        "m",
        aliases={"smart": [(Provider.OPENAI, "gpt-4o-mini")]},
    )
    assert router.primary("smart") == Provider.OPENAI
    # An alias only present in the built-in table is no longer known: the config
    # is the table, not an overlay on top of it.
    assert router.resolve("fast") == [(Provider.OLLAMA, "fast")]
