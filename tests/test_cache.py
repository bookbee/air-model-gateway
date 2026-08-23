"""ResponseCache.key() and the size bound in set().

Regression coverage for two bugs found in audit: the cache key originally
omitted json_schema/schema_name/cache_prefix (two structurally different
requests with identical messages would collide and one would get back the
other's response shape), and the store had no bound beyond TTL — a write-once
key is only reclaimed lazily on its own next lookup, so a sustained stream of
distinct prompts that are each asked once would grow the dict forever.
"""

from __future__ import annotations

from air_llm.gateway.cache import ResponseCache
from air_llm.schemas.models import ChatRequest, ChatResponse, Message, Usage

FLAT_SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}}}
OTHER_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}}


def _req(**overrides: object) -> ChatRequest:
    base: dict[str, object] = {"messages": [Message(role="user", content="great!")]}
    base.update(overrides)
    return ChatRequest(**base)  # type: ignore[arg-type]


def _resp(id: str) -> ChatResponse:
    return ChatResponse(id=id, model="m", provider="ollama", content="x", usage=Usage())


def test_differing_json_schema_produces_different_keys() -> None:
    plain = ResponseCache.key("m", _req())
    structured = ResponseCache.key("m", _req(json_schema=FLAT_SCHEMA))
    assert plain != structured


def test_differing_json_schema_content_produces_different_keys() -> None:
    a = ResponseCache.key("m", _req(json_schema=FLAT_SCHEMA))
    b = ResponseCache.key("m", _req(json_schema=OTHER_SCHEMA))
    assert a != b


def test_differing_schema_name_produces_different_keys() -> None:
    a = ResponseCache.key("m", _req(json_schema=FLAT_SCHEMA, schema_name="Verdict"))
    b = ResponseCache.key("m", _req(json_schema=FLAT_SCHEMA, schema_name="Label"))
    assert a != b


def test_differing_cache_prefix_produces_different_keys() -> None:
    a = ResponseCache.key("m", _req(cache_prefix=True))
    b = ResponseCache.key("m", _req(cache_prefix=False))
    assert a != b


def test_a_structured_hit_never_returns_an_unstructured_response() -> None:
    """The end-to-end shape of the bug: same messages, different json_schema —
    caching the plain-text answer must not surface it for the structured call."""
    cache = ResponseCache()
    plain_key = cache.key("m", _req())
    structured_key = cache.key("m", _req(json_schema=FLAT_SCHEMA))
    assert plain_key != structured_key

    cache.set(plain_key, _resp("1"))
    assert cache.get(structured_key) is None


def test_set_evicts_when_over_the_max_entries_bound() -> None:
    cache = ResponseCache(max_entries=3)
    for i in range(3):
        cache.set(f"k{i}", _resp(str(i)))
    assert len(cache._store) == 3

    cache.set("k3", _resp("3"))
    assert len(cache._store) == 3  # bounded, not 4
    assert cache.get("k3") is not None  # the newly-written entry survives


def test_updating_an_existing_key_does_not_count_as_growth() -> None:
    cache = ResponseCache(max_entries=2)
    cache.set("k0", _resp("0"))
    cache.set("k1", _resp("1"))
    # Re-setting an existing key must not evict a *different* key to make room
    # for itself.
    cache.set("k0", _resp("0b"))
    assert cache.get("k0") is not None
    assert cache.get("k1") is not None


def test_expired_entries_are_purged_before_evicting_a_live_one() -> None:
    # A key already past its expiry (written with a monotonic time in the
    # past) sits in the store like any other entry until something looks it
    # up or a later set() purges it — this drives that purge path directly.
    cache = ResponseCache(ttl_seconds=300, max_entries=1)
    cache._store["stale"] = (0.0, _resp("0"))
    cache.set("fresh", _resp("1"))
    assert cache.get("fresh") is not None
    assert len(cache._store) == 1
