"""Circuit breaker and retry backoff — the router's protection against a rung
that keeps failing.

Without a breaker, a dead provider costs every request a full attempt (up to
the per-attempt timeout) before failover reaches the next rung. The breaker
remembers the failures and skips that rung outright until a cooldown passes.

Breaker state is per replica, deliberately: each replica learns about an
outage from its own traffic within a few requests, and sharing it through
Redis would put a network hop in front of every attempt to save those few.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"  # calls flow
    OPEN = "open"  # calls are skipped until the cooldown passes
    HALF_OPEN = "half_open"  # one trial call is in flight; its outcome decides


class CircuitBreaker:
    """Classic three-state breaker, counting *consecutive* failures.

    A success at any point resets the count, so an intermittently flaky
    provider that still answers most calls never trips it.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow(self) -> bool:
        """Whether to make the call. Moves OPEN to HALF_OPEN once the cooldown
        has passed, letting exactly one trial through; concurrent callers keep
        being refused until that trial reports back."""
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self._reset:
            self._state = BreakerState.HALF_OPEN
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self._state is BreakerState.HALF_OPEN or self._failures >= self._threshold:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()

    def release(self) -> None:
        """The call ended without saying anything about the provider's health
        (a 400, a missing key). A half-open trial that ends this way must not
        leave the breaker stuck refusing everyone, so it goes back to OPEN and
        the next cooldown lets another trial through."""
        if self._state is BreakerState.HALF_OPEN:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()


def backoff_seconds(base: float, attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with ±50% jitter: ``base * 2**(attempt-1)`` scaled
    by a random factor in [0.5, 1.5), so replicas recovering from the same
    outage spread their retries out instead of arriving together."""
    return float(base * (2 ** (attempt - 1)) * (0.5 + rng()))
