"""Readiness gates on provider health: "no provider answers" is the one
condition under which a replica genuinely cannot serve. Redis is reported
but never gates (docs/01-hld.md §7)."""

from __future__ import annotations

from typing import Any

import fakeredis
import httpx

from air_model_gateway.redis_store import RedisStore


class _Router:
    def __init__(self, providers: dict[str, bool]) -> None:
        self._providers = providers

    async def health(self) -> dict[str, bool]:
        return self._providers


async def test_ready_when_a_provider_answers(app: Any, client: httpx.AsyncClient) -> None:
    app.state.router = _Router({"ollama": True, "anthropic": False})
    r = await client.get("/v1/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["providers"]["ollama"] is True


async def test_not_ready_when_no_provider_answers(app: Any, client: httpx.AsyncClient) -> None:
    app.state.router = _Router({"ollama": False})
    r = await client.get("/v1/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


async def test_liveness_never_consults_a_provider(app: Any, client: httpx.AsyncClient) -> None:
    app.state.router = _Router({"ollama": False})
    r = await client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_redis_is_omitted_when_not_configured(app: Any, client: httpx.AsyncClient) -> None:
    app.state.router = _Router({"ollama": True})
    assert "redis" not in (await client.get("/v1/ready")).json()


async def test_redis_down_is_reported_but_does_not_gate(
    app: Any, client: httpx.AsyncClient
) -> None:
    """Without Redis the replica still serves (cache miss, local limits), so
    failing readiness would turn a degraded cache into a full outage."""
    server = fakeredis.FakeServer()
    app.state.router = _Router({"ollama": True})
    app.state.redis = RedisStore(fakeredis.FakeAsyncRedis(server=server), "t:")
    assert (await client.get("/v1/ready")).json()["redis"] is True

    server.connected = False
    r = await client.get("/v1/ready")
    assert r.status_code == 200
    assert r.json()["redis"] is False
