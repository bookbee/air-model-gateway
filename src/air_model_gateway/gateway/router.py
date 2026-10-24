from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ModelProvider, ProviderError, ProviderResult
from air_model_gateway.gateway.resilience import BreakerState, CircuitBreaker, backoff_seconds
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.observability.metrics import (
    CHAIN_RETRIES,
    CIRCUIT_OPEN,
    CIRCUIT_SKIPS,
    PROVIDER_TIMEOUTS,
)
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest

log = get_logger("gateway.router")

Chain = list[tuple[Provider, str]]
#: Asked before each rung is called: may this deployment send one more call to
#: this provider right now? (The policy's provider_quotas, see main.py.)
Admit = Callable[[Provider], Awaitable[bool]]

@dataclass
class OpenedStream:
    """A streamed chat call that has produced its first item: from here on the
    stream is committed to `model` on this provider (no more failover)."""

    model: str
    first: str | ProviderResult
    rest: AsyncGenerator[str | ProviderResult, None]


# A provider that hangs is the failure mode being on-path makes everyone else's
# problem: without a deadline the caller's connection is held until *its* timeout,
# and the failover chain below never gets walked. 60s is generous for a
# completion and still an order of magnitude under any sane client budget.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0


class ModelRouter:
    """Runs a call down a chain of (provider, model) rungs: deadlines, failover,
    retries, circuit breakers and provider quotas. Which chain a request gets is
    decided before it gets here (gateway/models.py::ModelCatalog)."""

    def __init__(
        self,
        providers: dict[Provider, ModelProvider],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        retry_attempts: int = 0,
        retry_backoff_seconds: float = 0.5,
        breaker_failure_threshold: int = 5,
        breaker_reset_seconds: float = 30.0,
        admit: Admit | None = None,
    ) -> None:
        self._providers = providers
        self._timeout = timeout_seconds
        self._retry_attempts = retry_attempts
        self._retry_backoff = retry_backoff_seconds
        self._breaker_threshold = breaker_failure_threshold
        self._breaker_reset = breaker_reset_seconds
        self._admit = admit
        # One breaker per (provider, model), not per provider: a model that is
        # missing or broken must not take its provider's healthy models with it.
        self._breakers: dict[tuple[Provider, str], CircuitBreaker] = {}

    def breaker(self, provider: Provider, model: str) -> CircuitBreaker:
        key = (provider, model)
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(self._breaker_threshold, self._breaker_reset)
        return self._breakers[key]

    async def chat(self, req: ChatRequest, chain: Chain) -> tuple[ProviderResult, Provider]:
        return await self._run(chain, lambda p, m: p.chat(req, m))

    async def embeddings(
        self, req: EmbeddingsRequest, chain: Chain
    ) -> tuple[ProviderResult, Provider]:
        return await self._run(chain, lambda p, m: p.embeddings(req, m))

    async def open_chat_stream(
        self, req: ChatRequest, chain: Chain
    ) -> tuple[OpenedStream, Provider]:
        """Open a streamed chat on the first rung that produces output.

        Walks the chain exactly like `chat` (breakers, quotas, retries), but a
        rung counts as answering once its stream yields a first item within the
        per-attempt deadline: that is the time-to-first-token deadline. Failover
        is only possible until then; once text has gone to the caller, a later
        failure can't be hidden by switching models. Report such a failure with
        `record_stream_failure` so the rung's breaker still learns about it.
        """
        return await self._run(chain, lambda p, m: _open_stream(p, m, req))

    def record_stream_failure(self, provider: Provider, model: str, exc: ProviderError) -> None:
        """A stream that failed after its first item: counts toward the rung's
        breaker the way a failed call would (transient, non-throttle only)."""
        if exc.retryable and not exc.rate_limited:
            self._record(self.breaker(provider, model), provider, model, healthy=False)

    async def _run[T](
        self,
        chain: Chain,
        call: Callable[[ModelProvider, str], Awaitable[T]],
    ) -> tuple[T, Provider]:
        attempt = 0
        while True:
            outcome = await self._walk(chain, call)
            if isinstance(outcome, _Served):
                return outcome.result, outcome.provider
            # Retry only if waiting could change the answer: some rung failed
            # transiently. A chain that failed on bad keys, 400s or open
            # breakers would fail identically on a second pass.
            if attempt >= self._retry_attempts or not outcome.retryable:
                raise ProviderError(
                    chain[0][0],
                    f"all providers failed: {'; '.join(outcome.errors)}",
                    rate_limited=outcome.rate_limited,
                )
            attempt += 1
            delay = backoff_seconds(self._retry_backoff, attempt)
            CHAIN_RETRIES.labels(chain[0][0].value).inc()
            log.warning("chain.retry", attempt=attempt, delay_seconds=round(delay, 3))
            await asyncio.sleep(delay)

    async def _walk[T](
        self,
        chain: Chain,
        call: Callable[[ModelProvider, str], Awaitable[T]],
    ) -> _Served[T] | _Failed:
        """One pass over the chain: the first rung that answers wins."""
        failed = _Failed()
        for provider_kind, concrete in chain:
            provider = self._providers.get(provider_kind)
            if provider is None:
                failed.add(f"{provider_kind.value}: not registered")
                continue
            breaker = self.breaker(provider_kind, concrete)
            if not breaker.allow():
                CIRCUIT_SKIPS.labels(provider_kind.value, concrete).inc()
                failed.add(f"{provider_kind.value}:{concrete}: circuit open")
                continue
            if self._admit is not None and not await self._admit(provider_kind):
                # Over this deployment's quota for the provider: skip to the next
                # rung, as the provider itself would have answered 429. Counts as
                # a throttle, so a chain refused everywhere is a 429, not a 502.
                breaker.release()
                failed.add(
                    f"{provider_kind.value}:{concrete}: gateway quota for "
                    f"{provider_kind.value} exhausted",
                    rate_limited=True,
                )
                continue
            try:
                # The deadline turns a hang into an ordinary failure, so a hung
                # provider fails over exactly like one that errored.
                result = await asyncio.wait_for(call(provider, concrete), timeout=self._timeout)
            except TimeoutError:
                PROVIDER_TIMEOUTS.labels(provider_kind.value).inc()
                log.warning(
                    "provider.timeout",
                    provider=provider_kind.value,
                    model=concrete,
                    timeout_seconds=self._timeout,
                )
                self._record(breaker, provider_kind, concrete, healthy=False)
                failed.add(
                    f"{provider_kind.value}: timed out after {self._timeout}s", retryable=True
                )
                continue
            except ProviderError as exc:
                log.warning(
                    "provider.failed",
                    provider=provider_kind.value,
                    model=concrete,
                    detail=exc.detail,
                    rate_limited=exc.rate_limited,
                    retryable=exc.retryable,
                )
                if exc.retryable and not exc.rate_limited:
                    self._record(breaker, provider_kind, concrete, healthy=False)
                else:
                    # A 429 means the provider is up, and a 400 or a missing
                    # key says nothing about its health: neither trips it.
                    breaker.release()
                failed.add(str(exc), rate_limited=exc.rate_limited, retryable=exc.retryable)
                continue
            except Exception as exc:
                # An adapter bug or an SDK raising something unexpected. It is a
                # failure of this rung like any other: fail over rather than 500
                # the request, and free a half-open breaker's probe slot. Not
                # retryable and not counted against health: it's our defect,
                # not the provider's, and the traceback is logged to fix it.
                log.exception(
                    "provider.unexpected_error", provider=provider_kind.value, model=concrete
                )
                breaker.release()
                failed.add(f"{provider_kind.value}:{concrete}: unexpected {type(exc).__name__}")
                continue
            self._record(breaker, provider_kind, concrete, healthy=True)
            return _Served(result, provider_kind)
        return failed

    @staticmethod
    def _record(breaker: CircuitBreaker, provider: Provider, model: str, *, healthy: bool) -> None:
        was = breaker.state
        if healthy:
            breaker.record_success()
        else:
            breaker.record_failure()
        if breaker.state is not was:
            log.warning(
                "circuit.transition",
                provider=provider.value,
                model=model,
                state=breaker.state.value,
            )
        CIRCUIT_OPEN.labels(provider.value, model).set(
            1 if breaker.state is BreakerState.OPEN else 0
        )

    async def health(self) -> dict[str, bool]:
        # Probed concurrently: each probe has its own ~2s deadline, and /v1/ready
        # is polled on a tight budget — run in series, five slow probes would
        # blow past it even though no single one is slow enough to fail.
        kinds = list(self._providers)
        results = await asyncio.gather(*(self._providers[k].health() for k in kinds))
        return {k.value: ok for k, ok in zip(kinds, results, strict=True)}

    async def aclose(self) -> None:
        for prov in self._providers.values():
            await prov.aclose()


class _Served[R]:
    def __init__(self, result: R, provider: Provider) -> None:
        self.result = result
        self.provider = provider


class _Failed:
    """What one pass over a chain learned from its failures."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.retryable = False
        # Whether every rung answered "back off". Only then is the chain's
        # failure a throttle (429) rather than an outage (502): one rung that
        # timed out, errored or was skipped means waiting won't help on its own,
        # and a caller told "rate limited" would wait rather than escalate.
        self._all_rate_limited = True

    def add(self, error: str, *, rate_limited: bool = False, retryable: bool = False) -> None:
        self.errors.append(error)
        self.retryable = self.retryable or retryable
        self._all_rate_limited = self._all_rate_limited and rate_limited

    @property
    def rate_limited(self) -> bool:
        return self._all_rate_limited and bool(self.errors)


async def _open_stream(provider: ModelProvider, model: str, req: ChatRequest) -> OpenedStream:
    """Start a provider stream and wait for its first item. Runs under the
    router's per-attempt deadline; on any failure (or cancellation by that
    deadline) the provider stream is closed before the error propagates."""
    stream = provider.chat_stream(req, model)
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        raise ProviderError(
            provider.name, "stream ended before any output", retryable=True
        ) from None
    except BaseException:
        await stream.aclose()
        raise
    return OpenedStream(model=model, first=first, rest=stream)
