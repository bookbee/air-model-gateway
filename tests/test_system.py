from __future__ import annotations

import httpx

SENT = {"X-API-Key": "t-sent"}


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_every_build_says_which_revision_it_is() -> None:
    """MG-010: a test run, a deploy smoke test and an incident all need to know
    which code answered."""
    from air_model_gateway.config import Settings
    from air_model_gateway.main import create_app

    app = create_app(Settings(_env_file=None, revision="abc123-dirty"))  # type: ignore[call-arg]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        health = (await c.get("/v1/health")).json()
        metrics = (await c.get("/metrics")).text
    assert health["revision"] == "abc123-dirty"
    assert any(
        line.startswith("air_model_gateway_build_info{") and 'revision="abc123-dirty"' in line
        for line in metrics.splitlines()
    )


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
    assert "air_model_gateway_requests_total" in r.text
