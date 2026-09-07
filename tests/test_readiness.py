"""Readiness gates on provider health. Unlike air-infra, air-model-gateway has no separate
store-broker plane to keep decoupled — it is on the path for every model call,
so "no provider answers" is the one condition that means this replica genuinely
cannot serve (docs/01-hld.md §7)."""

from __future__ import annotations

from typing import Any

import httpx


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
