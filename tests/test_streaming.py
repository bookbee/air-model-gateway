"""Streaming /v1/generate (`stream: true`).

Pinned: the event sequence and its payloads; errors before the first token are
ordinary HTTP errors; failover only before the first token; a failure after it
is an `error` event that the rung's breaker hears about; the idle deadline;
bookkeeping (cache, spend, audit) identical to a non-streamed call; a cache hit
replayed as a stream; the reference client reading it all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import httpx
import pytest

from air_model_gateway.api.v1 import model_calls
from air_model_gateway.config import GatewaySettings, PolicySettings, SecuritySettings, Settings
from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.router import ModelRouter, OpenedStream
from air_model_gateway.main import create_app
from air_model_gateway.schemas.models import ChatRequest, Message, Usage
from air_model_gateway_client import AirModelGatewayClient, AirModelGatewayError

KEY = {"X-API-Key": "t-svc"}
USAGE = Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
BODY = {"model": "auto", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


def _final(finish: str = "end_turn", refusal: bool = False) -> ProviderResult:
    return ProviderResult(
        model="claude-haiku-4-5", usage=USAGE, finish_reason=finish, refusal=refusal
    )


async def _script(*items: Any, pause: float = 0.0) -> AsyncGenerator[str | ProviderResult, None]:
    for item in items:
        if pause:
            await asyncio.sleep(pause)
        if isinstance(item, Exception):
            raise item
        yield item


class _StreamRouter:
    """Stands in for ModelRouter: opens one scripted stream per call."""

    def __init__(self, *items: Any, pause: float = 0.0, open_error: Exception | None = None):
        self.items = items
        self.pause = pause
        self.open_error = open_error
        self.opened = 0
        self.failures: list[ProviderError] = []

    async def open_chat_stream(
        self, req: ChatRequest, chain: list[tuple[Provider, str]]
    ) -> tuple[OpenedStream, Provider]:
        self.opened += 1
        if self.open_error is not None:
            raise self.open_error
        stream = _script(*self.items, pause=self.pause)
        first = await anext(stream)
        return OpenedStream(model="claude-haiku-4-5", first=first, rest=stream), Provider.ANTHROPIC

    def record_stream_failure(self, provider: Provider, model: str, exc: ProviderError) -> None:
        self.failures.append(exc)

    async def health(self) -> dict[str, bool]:
        return {}

    async def aclose(self) -> None:
        return None


def _app(router: Any, idle: float = 30.0) -> Any:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        security=SecuritySettings(service_tokens={"t-svc": "air-orchestrator-service"}),
        policy=PolicySettings(config_path="deploy/policy/policy.local.yaml"),
        gateway=GatewaySettings(stream_idle_timeout_seconds=idle),
    )
    app = create_app(settings)
    app.state.router = router
    return app


async def _stream(
    app: Any, body: dict[str, Any] = BODY
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/generate", headers=KEY, json=body)
    events: list[tuple[str, dict[str, Any]]] = []
    if r.headers["content-type"].startswith("text/event-stream"):
        for block in r.text.strip().split("\n\n"):
            name, data = block.split("\n")
            events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return r, events


async def test_a_stream_is_start_deltas_then_done() -> None:
    r, events = await _stream(_app(_StreamRouter("Hel", "lo", _final())))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"
    names = [n for n, _ in events]
    assert names == ["start", "delta", "delta", "done"]
    start, done = events[0][1], events[-1][1]
    assert (start["model"], start["provider"], start["cached"]) == (
        "claude-haiku-4-5",
        "anthropic",
        False,
    )
    assert "".join(d["content"] for n, d in events if n == "delta") == "Hello"
    assert done["id"] == start["id"]
    assert (done["finish_reason"], done["cost_usd"]) == ("stop", 1.0)  # priced like any call
    assert "content" not in done


async def test_a_completed_stream_is_cached_and_replayed_as_a_stream() -> None:
    router = _StreamRouter("Hel", "lo", _final())
    app = _app(router)
    await _stream(app)
    _, events = await _stream(app)
    assert router.opened == 1
    assert [n for n, _ in events] == ["start", "delta", "done"]
    assert events[0][1]["cached"] is True and events[1][1]["content"] == "Hello"
    assert events[-1][1]["cost_usd"] == 0.0


@pytest.mark.parametrize("final", [_final("max_tokens"), _final("refusal", refusal=True)])
async def test_cut_off_or_refused_streams_are_not_cached(final: ProviderResult) -> None:
    router = _StreamRouter("x", final)
    app = _app(router)
    await _stream(app)
    await _stream(app)
    assert router.opened == 2


async def test_a_failure_before_the_first_token_is_an_http_error_not_a_stream() -> None:
    router = _StreamRouter(open_error=ProviderError(Provider.ANTHROPIC, "down", retryable=True))
    r, events = await _stream(_app(router))
    assert r.status_code == 502
    assert r.headers["content-type"].startswith("application/problem+json")
    assert events == []


async def test_a_failure_after_the_first_token_is_an_error_event() -> None:
    router = _StreamRouter("Hel", ProviderError(Provider.ANTHROPIC, "reset", retryable=True))
    r, events = await _stream(_app(router))
    assert r.status_code == 200
    assert [n for n, _ in events] == ["start", "delta", "error"]
    error = events[-1][1]
    assert (error["status"], error["code"]) == (502, "provider_unavailable")
    assert error["request_id"]
    assert [f.detail for f in router.failures] == ["reset"]  # the breaker hears about it


async def test_a_stream_that_goes_silent_is_ended_with_an_error_event() -> None:
    router = _StreamRouter("Hel", "lo", _final(), pause=0.2)
    _, events = await _stream(_app(router, idle=0.05))
    assert events[-1][0] == "error"
    assert "silent" in events[-1][1]["detail"]


async def test_a_completed_stream_is_audited_and_counted_toward_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _Log:
        def info(self, event: str, **fields: Any) -> None:
            calls.append({"event": event, **fields})

        def __getattr__(self, name: str) -> Any:
            return lambda *a, **k: None

    monkeypatch.setattr(model_calls, "log", _Log())
    app = _app(_StreamRouter("ok", _final()))
    await _stream(app)
    audit = [c for c in calls if c["event"] == "model.call"]
    assert len(audit) == 1
    assert (audit[0]["operation"], audit[0]["cost_usd"]) == ("generate", 1.0)
    assert await app.state.ledger.spent_today("air-orchestrator-service") == 1.0


async def test_without_stream_the_answer_is_still_json() -> None:
    class _Plain(_StreamRouter):
        async def chat(
            self, req: ChatRequest, chain: list[tuple[Provider, str]]
        ) -> tuple[ProviderResult, Provider]:
            return ProviderResult(
                model="m", content="hi", usage=Usage(), finish_reason="stop"
            ), Provider.OLLAMA

    r, _ = await _stream(_app(_Plain()), {**BODY, "stream": False})
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["content"] == "hi"


async def test_the_reference_client_reads_a_stream_and_raises_on_an_error_event() -> None:
    app = _app(_StreamRouter("Hel", "lo", _final()))
    client = AirModelGatewayClient("http://t", "t-svc", transport=httpx.ASGITransport(app=app))
    events = [
        e async for e in client.generate_stream([{"role": "user", "content": "hi"}], model="auto")
    ]
    assert [n for n, _ in events] == ["start", "delta", "delta", "done"]

    failing = _app(_StreamRouter("Hel", ProviderError(Provider.ANTHROPIC, "reset", retryable=True)))
    client = AirModelGatewayClient("http://t", "t-svc", transport=httpx.ASGITransport(app=failing))
    with pytest.raises(AirModelGatewayError) as info:
        async for _ in client.generate_stream([{"role": "user", "content": "hi"}]):
            pass
    assert (info.value.status, info.value.code) == (502, "provider_unavailable")
    await client.aclose()


# ---- the router -------------------------------------------------------------


class _StreamProvider:
    def __init__(self, name: Provider, *items: Any, hang: bool = False) -> None:
        self.name = name
        self.items = items
        self.hang = hang

    async def chat_stream(self, req: ChatRequest, model: str) -> AsyncIterator[Any]:
        if self.hang:
            await asyncio.sleep(30)
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item


def _router(*providers: _StreamProvider, timeout: float = 5.0) -> ModelRouter:
    return ModelRouter({p.name: p for p in providers}, timeout_seconds=timeout)  # type: ignore[misc]


def _chain(*providers: Provider) -> list[tuple[Provider, str]]:
    return [(p, f"{p.value}-model") for p in providers]


def _req() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="hi")])


async def test_the_router_fails_over_until_a_rung_produces_its_first_item() -> None:
    router = _router(
        _StreamProvider(Provider.OLLAMA, ProviderError(Provider.OLLAMA, "down", retryable=True)),
        _StreamProvider(Provider.ANTHROPIC, "hi", _final()),
    )
    opened, provider = await router.open_chat_stream(
        _req(), _chain(Provider.OLLAMA, Provider.ANTHROPIC)
    )
    assert (provider, opened.model, opened.first) == (Provider.ANTHROPIC, "anthropic-model", "hi")


async def test_a_rung_slow_to_its_first_token_is_failed_over() -> None:
    router = _router(
        _StreamProvider(Provider.OLLAMA, hang=True),
        _StreamProvider(Provider.ANTHROPIC, "hi", _final()),
        timeout=0.05,
    )
    _, provider = await router.open_chat_stream(_req(), _chain(Provider.OLLAMA, Provider.ANTHROPIC))
    assert provider is Provider.ANTHROPIC


async def test_failures_after_the_first_token_trip_the_breaker() -> None:
    router = _router(_StreamProvider(Provider.ANTHROPIC, "hi", _final()))
    for _ in range(5):
        router.record_stream_failure(
            Provider.ANTHROPIC,
            "anthropic-model",
            ProviderError(Provider.ANTHROPIC, "x", retryable=True),
        )
    assert router.breaker(Provider.ANTHROPIC, "anthropic-model").state.value == "open"
