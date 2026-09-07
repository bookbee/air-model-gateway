from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from air_model_gateway.config import PolicySettings, SecuritySettings, Settings
from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.main import create_app
from air_model_gateway.schemas.models import Usage

SENT = {"X-API-Key": "t-sent"}


class _FakeRouter:
    """Stands in for ModelRouter so the endpoint can be tested without providers."""

    def __init__(
        self,
        result: ProviderResult | None = None,
        error: Exception | None = None,
        provider: Provider = Provider.OLLAMA,
    ) -> None:
        self._result = result
        self._error = error
        self._provider = provider
        self.last_json_schema: dict[str, Any] | None = None

    def primary(self, model: str | None) -> Provider:
        return Provider.OLLAMA

    async def chat(self, req: Any) -> tuple[ProviderResult, Provider]:
        self.last_json_schema = req.json_schema
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, self._provider

    async def embeddings(self, req: Any) -> tuple[ProviderResult, Provider]:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, self._provider

    async def health(self) -> dict[str, bool]:
        return {}

    async def aclose(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        security=SecuritySettings(service_tokens={"t-sent": "air-classifier"}),
        policy=PolicySettings(config_path="deploy/policy/policy.local.yaml"),
    )


async def _client(router: _FakeRouter) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings())
    app.state.router = router
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_chat_passes_schema_and_returns_content() -> None:
    router = _FakeRouter(
        result=ProviderResult(
            model="llama3.2:3b",
            content='{"label":"positive"}',
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    )
    async for client in _client(router):
        r = await client.post(
            "/v1/inference",
            headers=SENT,
            json={
                "task": "chat",
                "model": "ollama:llama3.2:3b",
                "messages": [{"role": "user", "content": "great!"}],
                "json_schema": {"type": "object"},
                "schema_name": "Verdict",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["task"] == "chat"
    assert body["content"] == '{"label":"positive"}'
    assert body["usage"]["total_tokens"] == 15
    assert body["cost_usd"] == 0.0  # local model is free
    assert router.last_json_schema == {"type": "object"}


async def test_chat_prices_a_cloud_model() -> None:
    router = _FakeRouter(
        result=ProviderResult(
            model="claude-haiku-4-5",
            content="{}",
            usage=Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000),
        ),
        provider=Provider.ANTHROPIC,
    )
    async for client in _client(router):
        r = await client.post(
            "/v1/inference",
            headers=SENT,
            json={
                "task": "chat",
                "model": "smart",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
    assert r.status_code == 200
    # (1000 * 1.00 + 1000 * 5.00) / 1e6
    assert r.json()["cost_usd"] == pytest.approx(0.006)


async def test_refusal_is_reported_not_cached() -> None:
    router = _FakeRouter(result=ProviderResult(model="claude-haiku-4-5", refusal=True))
    async for client in _client(router):
        r = await client.post(
            "/v1/inference",
            headers=SENT,
            json={"task": "chat", "messages": [{"role": "user", "content": "x"}]},
        )
    assert r.status_code == 200
    assert r.json()["refusal"] is True


async def test_upstream_rate_limit_maps_to_429() -> None:
    router = _FakeRouter(error=ProviderError(Provider.ANTHROPIC, "throttled", rate_limited=True))
    async for client in _client(router):
        r = await client.post(
            "/v1/inference",
            headers=SENT,
            json={"task": "chat", "messages": [{"role": "user", "content": "x"}]},
        )
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"


async def test_provider_down_maps_to_502() -> None:
    router = _FakeRouter(error=ProviderError(Provider.ANTHROPIC, "connection refused"))
    async for client in _client(router):
        r = await client.post(
            "/v1/inference",
            headers=SENT,
            json={"task": "chat", "messages": [{"role": "user", "content": "x"}]},
        )
    assert r.status_code == 502
    assert r.json()["code"] == "provider_unavailable"


async def test_embeddings_task_routes_to_embeddings() -> None:
    router = _FakeRouter(
        result=ProviderResult(model="text-embedding-3-small", embeddings=[[0.1, 0.2]])
    )
    async for client in _client(router):
        r = await client.post(
            "/v1/inference",
            headers=SENT,
            json={"task": "embeddings", "input": ["hello"]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["task"] == "embeddings"
    assert body["embeddings"] == [[0.1, 0.2]]


async def test_chat_without_messages_is_a_validation_error() -> None:
    router = _FakeRouter()
    async for client in _client(router):
        r = await client.post("/v1/inference", headers=SENT, json={"task": "chat"})
    assert r.status_code == 422


async def test_embeddings_without_input_is_a_validation_error() -> None:
    router = _FakeRouter()
    async for client in _client(router):
        r = await client.post("/v1/inference", headers=SENT, json={"task": "embeddings"})
    assert r.status_code == 422
