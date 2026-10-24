"""Circuit breaker, retry backoff, transient-error classification, and how
the router uses them."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.providers._common import is_transient
from air_model_gateway.gateway.resilience import BreakerState, CircuitBreaker, backoff_seconds
from air_model_gateway.gateway.router import ModelRouter
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Message


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_breaker_opens_after_consecutive_failures_only() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=10, clock=_Clock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()  # resets the run
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allow()


def test_breaker_lets_one_trial_through_after_the_cooldown() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    assert breaker.allow()  # the trial
    assert breaker.state is BreakerState.HALF_OPEN
    assert not breaker.allow()  # everyone else waits for it


@pytest.mark.parametrize(("healthy", "state"), [(True, "closed"), (False, "open")])
def test_the_trial_outcome_decides(healthy: bool, state: str) -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    breaker.allow()
    if healthy:
        breaker.record_success()
    else:
        breaker.record_failure()
    assert breaker.state.value == state


def test_a_trial_that_says_nothing_reopens_rather_than_wedging() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    breaker.allow()
    breaker.release()
    assert breaker.state is BreakerState.OPEN
    clock.now = 20
    assert breaker.allow()  # a later trial is still possible


def test_backoff_doubles_with_jitter() -> None:
    assert backoff_seconds(0.5, 1, rng=lambda: 0.5) == pytest.approx(0.5)
    assert backoff_seconds(0.5, 2, rng=lambda: 0.5) == pytest.approx(1.0)
    assert backoff_seconds(0.5, 2, rng=lambda: 0.0) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (SimpleNamespace(status_code=503), True),
        (SimpleNamespace(status_code=429), True),
        (SimpleNamespace(status_code=400), False),
        (SimpleNamespace(code=500), True),  # google-genai's spelling
        (SimpleNamespace(code=404), False),
        (httpx.ConnectError("refused"), True),
        (ValueError("bad json"), False),
    ],
)
def test_is_transient(exc: object, transient: bool) -> None:
    assert is_transient(exc) is transient  # type: ignore[arg-type]


class _Flaky:
    """Fails with the given errors in order, then answers."""

    def __init__(self, name: Provider, *errors: ProviderError) -> None:
        self.name = name
        self.errors = list(errors)
        self.calls = 0

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return ProviderResult(model=model, content="ok")

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        raise NotImplementedError

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _router(provider: _Flaky, **kwargs: object) -> ModelRouter:
    return ModelRouter(
        {provider.name: provider},
        retry_backoff_seconds=0,
        **kwargs,  # type: ignore[arg-type]
    )


def _ask() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="hi")])


async def test_a_transient_failure_is_retried() -> None:
    flaky = _Flaky(Provider.OLLAMA, ProviderError(Provider.OLLAMA, "503", retryable=True))
    result, _ = await _router(flaky, retry_attempts=1).chat(_ask(), [(Provider.OLLAMA, "m")])
    assert result.content == "ok"
    assert flaky.calls == 2


async def test_a_permanent_failure_is_not_retried() -> None:
    flaky = _Flaky(Provider.OLLAMA, ProviderError(Provider.OLLAMA, "400 bad request"))
    with pytest.raises(ProviderError):
        await _router(flaky, retry_attempts=3).chat(_ask(), [(Provider.OLLAMA, "m")])
    assert flaky.calls == 1


async def test_an_open_breaker_skips_the_rung_without_calling_it() -> None:
    down = [ProviderError(Provider.OLLAMA, "503", retryable=True) for _ in range(5)]
    flaky = _Flaky(Provider.OLLAMA, *down)
    router = _router(flaky, breaker_failure_threshold=2)
    for _ in range(2):
        with pytest.raises(ProviderError):
            await router.chat(_ask(), [(Provider.OLLAMA, "m")])
    assert flaky.calls == 2

    with pytest.raises(ProviderError, match="circuit open"):
        await router.chat(_ask(), [(Provider.OLLAMA, "m")])
    assert flaky.calls == 2  # not called again


async def test_throttling_does_not_trip_the_breaker() -> None:
    """A 429 means the provider is up; skipping it would turn a throttle into
    a 502 for the next caller."""
    throttles = [ProviderError(Provider.OLLAMA, "429", rate_limited=True) for _ in range(3)]
    flaky = _Flaky(Provider.OLLAMA, *throttles)
    router = _router(flaky, breaker_failure_threshold=1)
    for _ in range(3):
        with pytest.raises(ProviderError) as info:
            await router.chat(_ask(), [(Provider.OLLAMA, "m")])
        assert info.value.rate_limited
    assert router.breaker(Provider.OLLAMA, "m").state is BreakerState.CLOSED
