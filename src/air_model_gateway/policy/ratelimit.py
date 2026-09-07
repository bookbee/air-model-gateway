from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float
    capacity: float
    refill_per_sec: float

    def take(self, now: float) -> bool:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


@dataclass
class RateLimiter:
    """Per-(service, resource) token bucket.

    In-process for now. The Redis-backed implementation (limits shared across
    replicas) is a drop-in behind this same ``check`` method — see docs/02-lld.md
    §10.
    """

    default_per_minute: int = 120
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    def check(self, service: str, resource: str, per_minute: int | None = None) -> bool:
        rpm = per_minute or self.default_per_minute
        now = time.monotonic()
        key = (service, resource)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                tokens=float(rpm),
                updated=now,
                capacity=float(rpm),
                refill_per_sec=rpm / 60.0,
            )
            self._buckets[key] = bucket
        return bucket.take(now)
