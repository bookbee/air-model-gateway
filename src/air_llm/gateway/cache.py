from __future__ import annotations

import hashlib
import json
import time

from air_llm.schemas.models import ChatRequest, ChatResponse


class ResponseCache:
    """In-process TTL cache of chat responses, keyed on the normalized request.

    A Redis-backed implementation (shared across replicas) drops in behind this
    same get/set interface — see docs/02-lld.md §5 / §10.
    """

    def __init__(
        self, ttl_seconds: int = 300, enabled: bool = True, max_entries: int = 10_000
    ) -> None:
        self._ttl = ttl_seconds
        self._enabled = enabled
        self._max_entries = max_entries
        self._store: dict[str, tuple[float, ChatResponse]] = {}

    @staticmethod
    def key(model: str, req: ChatRequest) -> str:
        # Every field that can change what the provider is asked for — and
        # therefore what a legitimate response looks like — must be in the key.
        # json_schema/schema_name are the sharp edge: two calls with identical
        # messages but different schemas (or no schema at all) must never
        # collide, or a cache hit hands back JSON shaped for the wrong caller.
        payload = {
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

    def get(self, key: str) -> ChatResponse | None:
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

    def set(self, key: str, value: ChatResponse) -> None:
        if not self._enabled:
            return
        # TTL alone only expires an entry the next time its *own* key is
        # looked up (get() pops it lazily) — a key that is written once and
        # never re-requested (the common case for distinct prompts) would
        # otherwise sit in `_store` forever, well past its TTL. Bounding size
        # here, rather than relying on a background sweeper, is enough for an
        # in-process cache that Phase 5 replaces with Redis (which enforces
        # its own memory/eviction policy) — see docs/00-plan.md Phase 5.
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
        # Still full after purging expired entries: drop the one closest to
        # expiry rather than an arbitrary one, so eviction under sustained
        # load approximates TTL order instead of picking randomly.
        soonest = min(self._store, key=lambda k: self._store[k][0])
        self._store.pop(soonest, None)
