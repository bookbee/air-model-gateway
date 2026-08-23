from __future__ import annotations

import httpx

SENT = {"X-API-Key": "t-sent"}


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_capabilities_requires_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/capabilities")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "unauthorized"


async def test_capabilities_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/capabilities", headers=SENT)
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == "local"
    assert body["providers"]["ollama"] is True


async def test_metrics_endpoint(client: httpx.AsyncClient) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "air_llm_requests_total" in r.text
