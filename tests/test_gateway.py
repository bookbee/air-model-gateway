from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.providers._common import provider_error
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


class _Rejecting(FakeProvider):
    """Answers like a provider refusing the request itself: a 400."""

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        raise ProviderError(self.name, "400 prompt is too long", status=400)


async def test_a_chain_every_provider_rejects_is_rejected_not_an_outage() -> None:
    providers = {
        Provider.ANTHROPIC: _Rejecting(Provider.ANTHROPIC),
        Provider.OPENAI: _Rejecting(Provider.OPENAI),
    }
    router = ModelRouter(providers, retry_attempts=1)
    with pytest.raises(ProviderError) as info:
        await router.chat(_req(), AO)
    assert (info.value.rejected, info.value.status, info.value.rate_limited) == (True, 400, False)
    # A rejection says nothing about the provider's health, and isn't retried.
    assert router.breaker(Provider.ANTHROPIC, "x").state.value == "closed"


async def test_a_chain_with_one_rejection_and_one_outage_is_an_outage() -> None:
    providers = {
        Provider.ANTHROPIC: _Rejecting(Provider.ANTHROPIC),
        Provider.OPENAI: FakeProvider(Provider.OPENAI, fail=True),
    }
    with pytest.raises(ProviderError) as info:
        await ModelRouter(providers).chat(_req(), AO)
    assert (info.value.rejected, info.value.status) == (False, None)


def _rung_failures(provider: str, model: str, reason: str) -> float:
    labels = {"provider": provider, "model": model, "reason": reason}
    return REGISTRY.get_sample_value("air_model_gateway_rung_failures_total", labels) or 0.0


async def test_an_unexpected_adapter_error_is_counted_for_alerting() -> None:
    before = _rung_failures("ollama", "x", "adapter")
    providers = {
        Provider.OLLAMA: _Buggy(Provider.OLLAMA),
        Provider.ANTHROPIC: FakeProvider(Provider.ANTHROPIC),
    }
    await ModelRouter(providers).chat(_req(), [(Provider.OLLAMA, "x"), (Provider.ANTHROPIC, "y")])
    assert _rung_failures("ollama", "x", "adapter") == before + 1


class _BadKey(FakeProvider):
    """Answers like a provider refusing the gateway's key (MG-009)."""

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        raise ProviderError(self.name, "401 invalid x-api-key", status=401)


async def test_a_refused_key_is_an_outage_not_a_rejection_and_is_counted() -> None:
    """MG-009: a bad key failed over silently, and a pin to it blamed the
    caller. It must be a 502 (not rejected, not retried) and visible."""
    before = _rung_failures("gemini", "g", "auth")
    router = ModelRouter({Provider.GEMINI: _BadKey(Provider.GEMINI)}, retry_attempts=1)
    with pytest.raises(ProviderError) as info:
        await router.chat(_req(), [(Provider.GEMINI, "g")])
    assert (info.value.rejected, info.value.rate_limited, info.value.retryable) == (
        False,
        False,
        False,
    )
    assert _rung_failures("gemini", "g", "auth") == before + 1  # one call: no retry


class _SdkError(Exception):
    """The shape every SDK raises: a status attribute and a message."""

    def __init__(self, message: str, *, status_code: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@pytest.mark.parametrize(
    "exc",
    [
        # google-genai: an invalid or expired key is a 400, not a 401.
        _SdkError(
            "400 INVALID_ARGUMENT. {'error': {'details': [{'reason': 'API_KEY_INVALID'}]}}",
            code=400,
        ),
        _SdkError("403 PERMISSION_DENIED", code=403),
        # anthropic: an empty account is a 400 invalid_request_error.
        _SdkError(
            "Error code: 400 - Your credit balance is too low to access the Anthropic API.",
            status_code=400,
        ),
        _SdkError("Error code: 401 - invalid x-api-key", status_code=401),
        # openai: no credit left is a 429, but no wait will fix it.
        _SdkError("Error code: 429 - {'code': 'insufficient_quota'}", status_code=429),
    ],
)
def test_provider_account_failures_are_auth_never_rejected_or_throttled(exc: Exception) -> None:
    err = provider_error(Provider.GEMINI, exc)
    assert (err.reason, err.rejected, err.rate_limited, err.retryable) == (
        "auth",
        False,
        False,
        False,
    )
    assert "the gateway's gemini key" in err.detail


def test_a_request_the_provider_finds_invalid_is_still_rejected() -> None:
    err = provider_error(Provider.GEMINI, _SdkError("400 prompt is too long", code=400))
    assert (err.reason, err.rejected) == ("rejected", True)


def test_a_missing_key_is_not_configured() -> None:
    err = ProviderError(Provider.OPENAI, "no OPENAI api key configured", configured=False)
    assert (err.reason, err.rejected, err.auth) == ("not_configured", False, False)


async def test_repeated_auth_failures_mark_the_provider_not_ready_until_a_success() -> None:
    bad = _BadKey(Provider.GEMINI)
    router = ModelRouter({Provider.GEMINI: bad, Provider.OPENAI: FakeProvider(Provider.OPENAI)})
    chain = [(Provider.GEMINI, "g"), (Provider.OPENAI, "o")]
    for _ in range(3):
        _, provider = await router.chat(_req(), chain)
        assert provider is Provider.OPENAI  # failover still answers
    assert await router.health() == {"gemini": False, "openai": True}
    # A working key again (a restart with the fixed secret, or a stray 403):
    # one success clears it.
    router._providers[Provider.GEMINI] = FakeProvider(Provider.GEMINI)
    await router.chat(_req(), chain)
    assert (await router.health())["gemini"] is True
