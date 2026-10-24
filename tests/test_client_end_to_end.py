"""End to end, in-process: the consumer SDK -> the real app (authN, policy, rate
limit, cache, pricing, problem+json) -> the real ModelRouter walking the
*shipped* deploy/routing.yaml chains. Only the last hop — the provider adapters'
network calls — is faked, so this runs with no server, no Ollama, and no keys.
It is the closest thing to a live call that is still a unit test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from air_model_gateway.config import GatewaySettings, PolicySettings, SecuritySettings, Settings
from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ModelProvider, ProviderError, ProviderResult
from air_model_gateway.gateway.router import ModelRouter
from air_model_gateway.main import create_app
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage
from air_model_gateway_client import AirModelGatewayClient, AirModelGatewayError


class _Scripted:
    """A provider whose behaviour is a script: answer, fail, or throttle."""

    def __init__(self, name: Provider, mode: str = "ok") -> None:
        self.name = name
        self.mode = mode
        self.models: list[str] = []

    def _check(self, model: str) -> None:
        self.models.append(model)
        if self.mode == "down":
            raise ProviderError(self.name, "connection refused")
        if self.mode == "throttled":
            raise ProviderError(self.name, "429", rate_limited=True)

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._check(model)
        return ProviderResult(
            model=model,
            content=f"{self.name.value} says hi",
            usage=Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000),
            finish_reason="stop",
        )

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        self._check(model)
        return ProviderResult(
            model=model,
            embeddings=[[0.5] * 768 for _ in req.input],
            usage=Usage(prompt_tokens=4, total_tokens=4),
        )

    async def health(self) -> bool:
        return self.mode == "ok"

    async def aclose(self) -> None:
        return None


def _app(**modes: str) -> tuple[Any, dict[Provider, _Scripted]]:
    settings = Settings(
        security=SecuritySettings(service_tokens={"tok-platform": "air-orchestrator-service"}),
        policy=PolicySettings(config_path="deploy/policy/policy.local.yaml"),
        gateway=GatewaySettings(routing_config_path="deploy/routing.yaml"),
    )
    app = create_app(settings)
    fakes = {kind: _Scripted(kind, modes.get(kind.name.lower(), "ok")) for kind in Provider}
    providers: dict[Provider, ModelProvider] = dict(fakes)
    app.state.router = ModelRouter(providers)
    return app, fakes


async def _sdk(app: Any, token: str = "tok-platform") -> AsyncIterator[AirModelGatewayClient]:
    async with AirModelGatewayClient(
        base_url="http://gateway.test", token=token, transport=httpx.ASGITransport(app=app)
    ) as gateway:
        yield gateway


async def test_local_fails_over_from_ollama_to_llama_cpp() -> None:
    """The shipped `local` chain: two ollama rungs, then llama.cpp — the case it
    exists for, a dead local ollama with a second local runtime up."""
    app, fakes = _app(ollama="down")
    async for gateway in _sdk(app):
        reply = await gateway.generate([{"role": "user", "content": "hi"}], model="local")

    assert reply["provider"] == "llama.cpp"
    assert reply["content"] == "llama.cpp says hi"
    assert reply["cost_usd"] == 0.0  # self-hosted is free regardless of tokens
    assert fakes[Provider.OLLAMA].models == ["llama3.2:3b", "ministral-3:3b"]


async def test_cloud_call_is_priced_and_then_served_from_cache() -> None:
    app, fakes = _app()
    async for gateway in _sdk(app):
        msgs = [{"role": "user", "content": "classify me"}]
        first = await gateway.generate(msgs, model="cloud")
        second = await gateway.generate(msgs, model="cloud")

    assert first["provider"] == "gemini"
    # gemini-2.5-flash from deploy/pricing.yaml: (1000 * 0.30 + 1000 * 2.50) / 1e6
    assert first["cost_usd"] == pytest.approx(0.0028)
    assert second["cached"] is True
    assert second["cost_usd"] == 0.0
    assert fakes[Provider.GEMINI].models == ["gemini-2.5-flash"]  # one real call


async def test_embeddings_through_a_published_embedding_model() -> None:
    app, _ = _app()
    async for gateway in _sdk(app):
        reply = await gateway.embed(["a", "b"], model="local-768-v1")
    assert (reply["provider"], reply["model"]) == ("ollama", "nomic-embed-text")
    assert (reply["embedding_model"], reply["dimensions"]) == ("local-768-v1", 768)
    assert [len(v) for v in reply["embeddings"]] == [768, 768]


async def test_a_chain_throttled_everywhere_reaches_the_caller_as_429() -> None:
    app, _ = _app(gemini="throttled", anthropic="throttled", openai="throttled")
    async for gateway in _sdk(app):
        with pytest.raises(AirModelGatewayError) as info:
            await gateway.generate([{"role": "user", "content": "x"}], model="cloud")
    assert info.value.status == 429
    assert info.value.code == "rate_limited"
    assert info.value.retryable is True
    assert info.value.request_id


async def test_a_chain_that_is_down_reaches_the_caller_as_502() -> None:
    app, _ = _app(gemini="down", anthropic="throttled", openai="down")
    async for gateway in _sdk(app):
        with pytest.raises(AirModelGatewayError) as info:
            await gateway.generate([{"role": "user", "content": "x"}], model="cloud")
    assert info.value.status == 502
    assert info.value.code == "provider_unavailable"


async def test_a_bad_token_is_a_typed_401() -> None:
    app, _ = _app()
    async for gateway in _sdk(app, token="nope"):
        with pytest.raises(AirModelGatewayError) as info:
            await gateway.generate([{"role": "user", "content": "x"}])
    assert info.value.status == 401
    assert info.value.code == "unauthorized"
    assert info.value.retryable is False


async def test_a_non_problem_error_body_still_raises_typed() -> None:
    """Something in front of the gateway (a proxy) answering with HTML must not
    surface as a JSON decode error."""
    transport = httpx.MockTransport(lambda request: httpx.Response(502, text="<html>bad</html>"))
    async with AirModelGatewayClient("http://gateway.test", "t", transport=transport) as gateway:
        with pytest.raises(AirModelGatewayError) as info:
            await gateway.embed(["x"], model="local-768-v1")
    assert info.value.status == 502
    assert info.value.code == "http_error"
    assert info.value.detail == "<html>bad</html>"


def test_a_spent_budget_is_not_retryable() -> None:
    assert not AirModelGatewayError(429, "budget_exceeded").retryable
    assert AirModelGatewayError(429, "rate_limited").retryable


async def test_the_callers_request_id_reaches_the_gateway_and_comes_back() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Request-ID"))
        return httpx.Response(404, json={"status": 404, "code": "x", "request_id": "trace-7"})

    client = AirModelGatewayClient("http://gw", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(AirModelGatewayError) as info:
        await client.generate([{"role": "user", "content": "hi"}], request_id="trace-7")
    await client.aclose()
    assert seen == ["trace-7"]
    assert info.value.request_id == "trace-7"
