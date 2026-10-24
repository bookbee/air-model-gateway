"""The gateway's one Redis connection, shared by the response cache and the
rate limiter.

Redis is an optimisation and a coordination point here, never a hard
dependency: every caller has a degraded path (a cache miss, a per-replica rate
limit) for when it is unreachable, and the gateway boots and serves without
it. What this module adds is keeping a *dead* Redis cheap: after a few
consecutive failures its breaker opens and calls are refused locally for a few
seconds, instead of every request paying the socket timeout to rediscover it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from redis.asyncio import Redis
from redis.exceptions import RedisError

from air_model_gateway.config import RedisSettings
from air_model_gateway.gateway.resilience import CircuitBreaker
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.observability.metrics import REDIS_ERRORS

log = get_logger("redis")

T = TypeVar("T")

# Three strikes, then leave Redis alone for five seconds. Short on purpose:
# a Redis restart should be noticed within one cooldown, not one minute.
_BREAKER_THRESHOLD = 3
_BREAKER_RESET_SECONDS = 5.0


class RedisUnavailable(Exception):
    """Redis could not serve this operation; the caller takes its degraded path."""


class RedisStore:
    def __init__(self, client: Redis, key_prefix: str) -> None:
        self.client = client
        self._prefix = key_prefix
        self._breaker = CircuitBreaker(_BREAKER_THRESHOLD, _BREAKER_RESET_SECONDS)

    def key(self, *parts: str) -> str:
        return self._prefix + ":".join(parts)

    async def run(self, operation: str, fn: Callable[[Redis], Awaitable[T]]) -> T:
        """Run one Redis operation, or raise RedisUnavailable (never RedisError)."""
        if not self._breaker.allow():
            REDIS_ERRORS.labels(operation).inc()
            raise RedisUnavailable("circuit open")
        try:
            result = await fn(self.client)
        except (RedisError, OSError) as exc:
            self._breaker.record_failure()
            REDIS_ERRORS.labels(operation).inc()
            log.warning("redis.failed", operation=operation, error=type(exc).__name__)
            raise RedisUnavailable(type(exc).__name__) from exc
        self._breaker.record_success()
        return result

    async def ping(self) -> bool:
        try:
            return bool(await self.run("ping", lambda r: r.ping()))
        except RedisUnavailable:
            return False

    async def aclose(self) -> None:
        await self.client.aclose()


def connect(settings: RedisSettings) -> RedisStore | None:
    """A store for the configured URL, or None when Redis is not configured.

    Connections are opened lazily on first use, so an unreachable Redis does
    not stop the gateway from booting.
    """
    if not settings.url:
        return None
    client = Redis.from_url(
        settings.url,
        socket_timeout=settings.socket_timeout_seconds,
        socket_connect_timeout=settings.socket_timeout_seconds,
        health_check_interval=30,
    )
    return RedisStore(client, settings.key_prefix)
