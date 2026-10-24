"""Token-bucket rate limiting, in memory and in Redis.

The Redis tests run the real Lua script against fakeredis: two limiter
instances on one server stand in for two replicas sharing a bucket.
"""

from __future__ import annotations

import fakeredis

from air_model_gateway.policy.ratelimit import MemoryRateLimiter, RedisRateLimiter
from air_model_gateway.redis_store import RedisStore


def _store(server: fakeredis.FakeServer) -> RedisStore:
    return RedisStore(fakeredis.FakeAsyncRedis(server=server), "test:")


async def test_memory_limiter_allows_a_full_burst_then_refuses() -> None:
    limiter = MemoryRateLimiter()
    results = [await limiter.check("svc", "inference", per_minute=3) for _ in range(4)]
    assert results == [True, True, True, False]


async def test_buckets_are_per_service_and_per_resource() -> None:
    limiter = MemoryRateLimiter()
    assert await limiter.check("a", "inference", per_minute=1)
    assert not await limiter.check("a", "inference", per_minute=1)
    assert await limiter.check("b", "inference", per_minute=1)
    assert await limiter.check("a", "provider:ollama", per_minute=1)


async def test_redis_limiter_enforces_one_limit_across_replicas() -> None:
    """The point of Redis here: two replicas draw from the same bucket, so
    the limit holds for the deployment, not per replica."""
    server = fakeredis.FakeServer()
    replica_a = RedisRateLimiter(_store(server))
    replica_b = RedisRateLimiter(_store(server))

    results = [
        await replica_a.check("svc", "inference", per_minute=3),
        await replica_b.check("svc", "inference", per_minute=3),
        await replica_a.check("svc", "inference", per_minute=3),
        await replica_b.check("svc", "inference", per_minute=3),
    ]
    assert results == [True, True, True, False]


async def test_redis_bucket_expires_once_it_would_be_full_again() -> None:
    server = fakeredis.FakeServer()
    store = _store(server)
    await RedisRateLimiter(store).check("svc", "inference", per_minute=60)

    ttl_ms = await store.client.pttl("test:ratelimit:svc:inference")
    # 60/min refills fully in 60s; the key lives that long plus a 1s margin.
    assert 0 < ttl_ms <= 61_000


async def test_redis_limiter_falls_back_to_per_replica_limits_when_redis_is_down() -> None:
    server = fakeredis.FakeServer()
    limiter = RedisRateLimiter(_store(server))
    server.connected = False

    results = [await limiter.check("svc", "inference", per_minute=2) for _ in range(3)]
    # Still limited (locally), neither wide open nor refusing everything.
    assert results == [True, True, False]
