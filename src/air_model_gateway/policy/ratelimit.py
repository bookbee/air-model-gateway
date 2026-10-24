"""Token-bucket rate limiting per (service, resource).

A bucket holds up to ``per_minute`` tokens and refills at ``per_minute / 60``
per second, so a service can burst to its full minute's allowance and is then
held to the steady rate. Two kinds of bucket use it: each service's
``inference`` bucket (checked by the endpoint), and one ``provider:<name>``
bucket per provider kept under the service ``*`` and shared by every service
(the policy's provider_quotas, checked by the router before each rung).

``MemoryRateLimiter`` keeps buckets in process (one replica). ``RedisRateLimiter``
keeps them in Redis so every replica draws from the same bucket, and falls back
to the in-memory limiter while Redis is unreachable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from air_model_gateway.redis_store import RedisStore, RedisUnavailable


class RateLimiter(Protocol):
    async def check(self, service: str, resource: str, per_minute: int | None = None) -> bool: ...


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
class MemoryRateLimiter:
    default_per_minute: int = 120
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    async def check(self, service: str, resource: str, per_minute: int | None = None) -> bool:
        return self.take(service, resource, per_minute)

    def take(self, service: str, resource: str, per_minute: int | None = None) -> bool:
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


# The same bucket arithmetic, run atomically inside Redis so concurrent
# replicas cannot both spend the last token. Time comes from Redis itself
# (TIME), not from the caller, so replicas with skewed clocks still agree.
# The key expires once a full bucket would have refilled anyway: an idle
# service's bucket costs no memory.
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
return allowed
"""


class RedisRateLimiter:
    def __init__(self, store: RedisStore, default_per_minute: int = 120) -> None:
        self._store = store
        self._default = default_per_minute
        self._script = store.client.register_script(_TOKEN_BUCKET_LUA)
        # While Redis is down each replica enforces the limit on its own: the
        # effective limit briefly multiplies by the replica count, which beats
        # both alternatives (no limit at all, or refusing every call).
        self._fallback = MemoryRateLimiter(default_per_minute)

    async def check(self, service: str, resource: str, per_minute: int | None = None) -> bool:
        rpm = per_minute or self._default
        key = self._store.key("ratelimit", service, resource)
        try:
            allowed = await self._store.run(
                "ratelimit", lambda r: self._script(keys=[key], args=[rpm, rpm / 60.0])
            )
        except RedisUnavailable:
            return self._fallback.take(service, resource, rpm)
        return bool(allowed)
