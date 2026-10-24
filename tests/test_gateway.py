from __future__ import annotations

import asyncio

import pytest

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.router import ModelRouter
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Message, Usage


class FakeProvider:
    def __init__(
        self,
        name: Provider,
        *,
        fail: bool = False,
        hang: bool = False,
        throttled: bool = False,
        health_delay: float = 0.0,
    ) -> None:
        self.name = name
        self.fail = fail
        self.hang = hang
        self.throttled = throttled
        self.health_delay = health_delay

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        if self.hang:
            await asyncio.sleep(30)  # cancelled by the router's deadline
        if self.throttled:
            raise ProviderError(self.name, "429 slow down", rate_limited=True)
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
        await asyncio.sleep(self.health_delay)
        return not self.fail

    async def aclose(self) -> None:
        return None


def _req() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="x")])


AO = [(Provider.ANTHROPIC, "x"), (Provider.OPENAI, "y")]


async def test_a_single_rung_chain_answers() -> None:
    router = ModelRouter({Provider.OLLAMA: FakeProvider(Provider.OLLAMA)})
    result, provider = await router.chat(_req(), [(Provider.OLLAMA, "m")])
    assert provider == Provider.OLLAMA
    assert result.content == "hi"


async def test_failover_walks_the_chain() -> None:
    providers = {
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC, fail=True),
        Provider.OPENAI: FakeProvider(Provider.OPENAI),
    }
    result, provider = await ModelRouter(providers).chat(_req(), AO)
    assert provider == Provider.OPENAI
    assert result.content == "hi"


async def test_all_providers_failing_raises() -> None:
    router = ModelRouter({Provider.OLLAMA: FakeProvider(Provider.OLLAMA, fail=True)})
    with pytest.raises(ProviderError):
        await router.chat(_req(), [(Provider.OLLAMA, "m")])


async def test_a_hung_provider_fails_over_rather_than_hanging() -> None:
    """The reason the deadline exists: without it a hang is not an error, so the
    failover chain is never walked and the caller waits on its own timeout."""
    providers = {
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC, hang=True),
        Provider.OPENAI: FakeProvider(Provider.OPENAI),
    }
    result, provider = await ModelRouter(providers, timeout_seconds=0.05).chat(_req(), AO)
    assert provider == Provider.OPENAI
    assert result.content == "hi"


async def test_every_provider_hanging_raises_with_the_timeout_named() -> None:
    router = ModelRouter(
        {Provider.OLLAMA: FakeProvider(Provider.OLLAMA, hang=True)}, timeout_seconds=0.05
    )
    with pytest.raises(ProviderError, match="timed out"):
        await router.chat(_req(), [(Provider.OLLAMA, "m")])


async def test_a_chain_that_is_throttled_everywhere_is_rate_limited() -> None:
    """LLD §5: an upstream 429 surfaces as 429, not 502 — and the router is in
    between, so its own all-failed error has to carry the flag through."""
    providers = {
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC, throttled=True),
        Provider.OPENAI: FakeProvider(Provider.OPENAI, throttled=True),
    }
    with pytest.raises(ProviderError) as info:
        await ModelRouter(providers).chat(_req(), AO)
    assert info.value.rate_limited is True


async def test_a_chain_with_any_real_outage_is_not_rate_limited() -> None:
    """One rung down (not throttled) means waiting won't fix the chain on its
    own — the caller must see an outage, not be told to back off."""
    providers = {
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC, throttled=True),
        Provider.OPENAI: FakeProvider(Provider.OPENAI, fail=True),
    }
    with pytest.raises(ProviderError) as info:
        await ModelRouter(providers).chat(_req(), AO)
    assert info.value.rate_limited is False


async def test_an_unregistered_rung_is_not_rate_limited() -> None:
    with pytest.raises(ProviderError) as info:
        await ModelRouter({}).chat(_req(), [(Provider.OLLAMA, "m")])
    assert info.value.rate_limited is False


async def test_health_probes_run_concurrently() -> None:
    """/v1/ready sits behind a probe budget: five providers each taking their
    full ~2s deadline must cost ~2s in total, not ~10s."""
    providers = {
        kind: FakeProvider(kind, health_delay=0.2)
        for kind in (Provider.OLLAMA, Provider.OPENAI, Provider.GEMINI)
    }
    router = ModelRouter(providers)
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await router.health() == {"ollama": True, "openai": True, "gemini": True}
    assert loop.time() - start < 0.5


class _Buggy(FakeProvider):
    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        raise KeyError("choices")  # an adapter bug, not a ProviderError


async def test_an_unexpected_adapter_error_fails_over_instead_of_crashing() -> None:
    providers = {
        Provider.OLLAMA: _Buggy(Provider.OLLAMA),
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC),
    }
    router = ModelRouter(providers)
    _, provider = await router.chat(_req(), [(Provider.OLLAMA, "x"), (Provider.ANTHROPIC, "y")])
    assert provider is Provider.ANTHROPIC
    # Our defect, not the provider's health: the breaker stays closed.
    assert router.breaker(Provider.OLLAMA, "x").state.value == "closed"


async def test_a_provider_over_its_gateway_quota_is_skipped_like_a_429() -> None:
    providers = {
        Provider.OLLAMA: FakeProvider(Provider.OLLAMA),
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC),
    }
    chain = [(Provider.OLLAMA, "x"), (Provider.ANTHROPIC, "y")]

    async def admit(provider: Provider) -> bool:
        return provider is not Provider.OLLAMA

    _, provider = await ModelRouter(providers, admit=admit).chat(_req(), chain)
    assert provider is Provider.ANTHROPIC

    async def nobody(provider: Provider) -> bool:
        return False

    with pytest.raises(ProviderError) as info:
        await ModelRouter(providers, admit=nobody).chat(_req(), chain)
    assert info.value.rate_limited  # every rung throttled -> the API answers 429
