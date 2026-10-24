"""Response cache: what goes into the key, the in-memory size bound, and the
Redis-backed cache (shared across replicas, failing open when Redis is down).

Redis is faked in-process with fakeredis, which speaks the real protocol, so
these run with no server.
"""

from __future__ import annotations

import fakeredis
import pytest

from air_model_gateway.gateway.cache import MemoryResponseCache, RedisResponseCache, cache_key
from air_model_gateway.redis_store import RedisStore
from air_model_gateway.schemas.models import ChatRequest, ChatResponse, Message, Usage

FLAT_SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}}}
OTHER_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}}


def _req(**overrides: object) -> ChatRequest:
    base: dict[str, object] = {"messages": [Message(role="user", content="great!")]}
    base.update(overrides)
    return ChatRequest(**base)  # type: ignore[arg-type]


def _resp(id: str) -> ChatResponse:
    return ChatResponse(id=id, model="m", provider="ollama", content="x", usage=Usage())


def _store(server: fakeredis.FakeServer) -> RedisStore:
    return RedisStore(fakeredis.FakeAsyncRedis(server=server), "test:")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ({}, {"json_schema": FLAT_SCHEMA}),
        ({"json_schema": FLAT_SCHEMA}, {"json_schema": OTHER_SCHEMA}),
        (
            {"json_schema": FLAT_SCHEMA, "schema_name": "Verdict"},
            {"json_schema": FLAT_SCHEMA, "schema_name": "Label"},
        ),
        ({"cache_prefix": True}, {"cache_prefix": False}),
        ({"temperature": 0.1}, {"temperature": 0.9}),
    ],
)
def test_anything_that_changes_the_question_changes_the_key(
    a: dict[str, object], b: dict[str, object]
) -> None:
    """Two requests with identical messages but a different schema (or none)
    must never share an entry, or a hit hands back JSON shaped for the wrong
    caller."""
    assert cache_key("svc", "m", _req(**a)) != cache_key("svc", "m", _req(**b))


async def test_a_structured_hit_never_returns_an_unstructured_response() -> None:
    cache = MemoryResponseCache()
    await cache.set(cache_key("svc", "m", _req()), _resp("1"))
    assert await cache.get(cache_key("svc", "m", _req(json_schema=FLAT_SCHEMA))) is None


async def test_memory_cache_is_bounded_by_entry_count() -> None:
    cache = MemoryResponseCache(max_entries=3)
    for i in range(4):
        await cache.set(f"k{i}", _resp(str(i)))
    assert len(cache._store) == 3
    assert await cache.get("k3") is not None  # the newest entry survives


async def test_updating_an_existing_key_does_not_count_as_growth() -> None:
    cache = MemoryResponseCache(max_entries=2)
    await cache.set("k0", _resp("0"))
    await cache.set("k1", _resp("1"))
    await cache.set("k0", _resp("0b"))
    assert await cache.get("k0") is not None
    assert await cache.get("k1") is not None


async def test_expired_entries_are_purged_before_evicting_a_live_one() -> None:
    cache = MemoryResponseCache(ttl_seconds=300, max_entries=1)
    cache._store["stale"] = (0.0, _resp("0"))
    await cache.set("fresh", _resp("1"))
    assert await cache.get("fresh") is not None
    assert len(cache._store) == 1


async def test_disabled_memory_cache_stores_nothing() -> None:
    cache = MemoryResponseCache(enabled=False)
    await cache.set("k", _resp("1"))
    assert await cache.get("k") is None


async def test_redis_cache_is_shared_between_replicas() -> None:
    server = fakeredis.FakeServer()
    replica_a = RedisResponseCache(_store(server), ttl_seconds=60)
    replica_b = RedisResponseCache(_store(server), ttl_seconds=60)

    await replica_a.set("k", _resp("from-a"))
    hit = await replica_b.get("k")

    assert hit is not None
    assert hit.id == "from-a"


async def test_redis_cache_entries_carry_the_ttl_and_the_prefix() -> None:
    server = fakeredis.FakeServer()
    store = _store(server)
    await RedisResponseCache(store, ttl_seconds=60).set("k", _resp("1"))

    ttl = await store.client.ttl("test:cache:k")
    assert 0 < ttl <= 60


async def test_redis_cache_falls_back_to_memory_when_redis_is_down() -> None:
    server = fakeredis.FakeServer()
    cache = RedisResponseCache(_store(server))
    server.connected = False

    await cache.set("k", _resp("1"))  # not raised
    hit = await cache.get("k")  # served from this replica's memory
    assert hit is not None
    assert hit.id == "1"


async def test_an_unreadable_redis_entry_is_a_miss() -> None:
    server = fakeredis.FakeServer()
    store = _store(server)
    await store.client.set("test:cache:k", b'{"not": "a response"}')
    assert await RedisResponseCache(store).get("k") is None
