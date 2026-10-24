"""Response cache for chat calls: identical requests within the TTL are served
without calling a provider, at zero cost.

Two implementations behind one async interface. ``MemoryResponseCache`` is per
process (one replica, tests, a laptop). ``RedisResponseCache`` is shared by every
replica and is chosen whenever ``AIR_MODEL_GATEWAY_REDIS__URL`` is set. Embeddings
are not cached, and neither are refusals (the endpoint decides that).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Protocol

from pydantic import ValidationError

from air_model_gateway.observability.logging import get_logger
from air_model_gateway.redis_store import RedisStore, RedisUnavailable
from air_model_gateway.schemas.models import ChatRequest, ChatResponse

log = get_logger("gateway.cache")


def cache_key(service: str, model: str, req: ChatRequest) -> str:
    """Every field that can change what the provider is asked for, and so what
    a legitimate answer looks like, is in the key. json_schema/schema_name are
    the sharp edge: two calls with identical messages but different schemas (or
    none) must never collide, or a hit hands back JSON shaped for the wrong
    caller.

    The calling service is in the key too: an answer is only ever reused by the
    service that paid for it, so projects sharing the gateway never see each
    other's cached answers (or learn from a hit that someone asked the same)."""
    payload = {
        "service": service,
        "model": model,
        "messages": [m.model_dump() for m in req.messages],
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "json_schema": req.json_schema,
        "schema_name": req.schema_name,
        "cache_prefix": req.cache_prefix,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class ResponseCache(Protocol):
    async def get(self, key: str) -> ChatResponse | None: ...

    async def set(self, key: str, value: ChatResponse) -> None: ...


class MemoryResponseCache:
    """In-process TTL cache, bounded by entry count as well as TTL."""

    def __init__(
        self, ttl_seconds: int = 300, enabled: bool = True, max_entries: int = 10_000
    ) -> None:
        self._ttl = ttl_seconds
        self._enabled = enabled
        self._max_entries = max_entries
        self._store: dict[str, tuple[float, ChatResponse]] = {}

    async def get(self, key: str) -> ChatResponse | None:
        if not self._enabled:
            return None
        hit = self._store.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: ChatResponse) -> None:
        if not self._enabled:
            return
        # TTL alone only reclaims an entry when its own key is looked up again,
        # and most prompts are written once and never repeated, so the size cap
        # is what actually bounds memory.
        if len(self._store) >= self._max_entries and key not in self._store:
            self._evict_to_make_room()
        self._store[key] = (time.monotonic() + self._ttl, value)

    def _evict_to_make_room(self) -> None:
        now = time.monotonic()
        expired = [k for k, (expires_at, _) in self._store.items() if expires_at <= now]
        for k in expired:
            self._store.pop(k, None)
        if len(self._store) < self._max_entries:
            return
        # Still full: drop the entry closest to expiry, so eviction under
        # sustained load approximates TTL order rather than picking at random.
        soonest = min(self._store, key=lambda k: self._store[k][0])
        self._store.pop(soonest, None)


class RedisResponseCache:
    """Cache shared across replicas. Redis expires entries by TTL and bounds
    memory with its own eviction policy, so no size cap is kept here.

    While Redis is unreachable, reads and writes go to a per-replica memory
    cache instead: hit rates drop, answers stay correct, and nothing fails.
    """

    def __init__(
        self,
        store: RedisStore,
        ttl_seconds: int = 300,
        enabled: bool = True,
        fallback_max_entries: int = 10_000,
    ) -> None:
        self._store = store
        self._ttl = ttl_seconds
        self._enabled = enabled
        self._fallback = MemoryResponseCache(ttl_seconds, enabled, fallback_max_entries)

    async def get(self, key: str) -> ChatResponse | None:
        if not self._enabled:
            return None
        try:
            raw = await self._store.run("cache_get", lambda r: r.get(self._store.key("cache", key)))
        except RedisUnavailable:
            return await self._fallback.get(key)
        if raw is None:
            return None
        try:
            return ChatResponse.model_validate_json(raw)
        except ValidationError:
            # Written by a version with a different response shape: a miss,
            # and the fresh answer overwrites it.
            log.warning("cache.unreadable_entry")
            return None

    async def set(self, key: str, value: ChatResponse) -> None:
        if not self._enabled:
            return
        blob = value.model_dump_json()
        try:
            await self._store.run(
                "cache_set", lambda r: r.set(self._store.key("cache", key), blob, ex=self._ttl)
            )
        except RedisUnavailable:
            await self._fallback.set(key, value)
