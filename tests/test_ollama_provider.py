"""OllamaProvider's keep_alive behavior.

Every call must carry keep_alive. Without it Ollama's own default (5m)
applies, every AIR service sharing the instance pays a reload after a quiet
spell, and the first real request undoes a manual `make ollama-warm` by
resetting the timer to 5m.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from air_model_gateway.gateway.base import ProviderError
from air_model_gateway.gateway.providers.ollama_provider import OllamaProvider
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Message


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, path: str, json: dict[str, Any]) -> _FakeResponse:
        self.calls.append({"path": path, "json": json})
        return _FakeResponse(self._response)


def _provider(keep_alive: str = "30m") -> tuple[OllamaProvider, _FakeClient]:
    provider = OllamaProvider("http://127.0.0.1:11434", keep_alive=keep_alive)
    fake = _FakeClient({"model": "llama3.2:3b", "message": {"content": "hi"}})
    provider._client = fake  # type: ignore[assignment]
    return provider, fake


async def test_chat_sends_keep_alive() -> None:
    provider, client = _provider("30m")
    await provider.chat(ChatRequest(messages=[Message(role="user", content="hi")]), "llama3.2:3b")
    assert client.calls[0]["json"]["keep_alive"] == "30m"


async def test_embeddings_sends_keep_alive() -> None:
    provider, client = _provider("30m")
    client._response = {"embeddings": [[0.0]]}
    await provider.embeddings(EmbeddingsRequest(input=["x"]), "nomic-embed-text")
    assert client.calls[0]["json"]["keep_alive"] == "30m"


@pytest.mark.parametrize("keep_alive", ["5m", "1h", "-1"])
async def test_keep_alive_is_configurable_per_instance(keep_alive: str) -> None:
    provider, client = _provider(keep_alive)
    await provider.chat(ChatRequest(messages=[Message(role="user", content="hi")]), "llama3.2:3b")
    assert client.calls[0]["json"]["keep_alive"] == keep_alive


async def test_default_keep_alive_matches_the_makefiles_warm_target() -> None:
    """Makefile's `ollama-warm` warms to 30m; if the two drift, a manual warm
    is silently undone by the first real request after it."""
    provider = OllamaProvider("http://127.0.0.1:11434")
    assert provider._keep_alive == "30m"


@pytest.mark.parametrize("call", ["chat", "embeddings"])
async def test_upstream_429_is_flagged_rate_limited(call: str) -> None:
    """Both endpoints map a throttled Ollama to rate_limited, so the API layer
    answers 429 rather than 502."""
    provider = OllamaProvider("http://ollama.test")
    provider._client = httpx.AsyncClient(
        base_url="http://ollama.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    with pytest.raises(ProviderError) as info:
        if call == "chat":
            await provider.chat(ChatRequest(messages=[Message(role="user", content="x")]), "m")
        else:
            await provider.embeddings(EmbeddingsRequest(input=["x"]), "m")
    assert info.value.rate_limited is True


class _NotJson(_FakeResponse):
    def json(self) -> dict[str, Any]:
        raise ValueError("Expecting value: line 1 column 1")


async def test_a_non_json_body_is_a_provider_error_not_a_crash() -> None:
    """A proxy's HTML error page must fail over like any other failure."""
    provider, fake = _provider()

    async def post(path: str, json: dict[str, Any]) -> _FakeResponse:
        return _NotJson({})

    fake.post = post  # type: ignore[method-assign]
    with pytest.raises(ProviderError, match="not JSON"):
        await provider.chat(ChatRequest(messages=[Message(role="user", content="x")]), "m")


async def test_embeddings_report_total_tokens() -> None:
    provider, fake = _provider()
    fake._response = {"embeddings": [[0.1]], "prompt_eval_count": 7}
    result = await provider.embeddings(EmbeddingsRequest(input=["a"]), "nomic-embed-text")
    assert result.usage.prompt_tokens == 7
    assert result.usage.total_tokens == 7


def _streaming_provider(body: str, status: int = 200) -> OllamaProvider:
    provider = OllamaProvider("http://ollama")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(status, text=body)

    provider._client = httpx.AsyncClient(
        base_url="http://ollama", transport=httpx.MockTransport(handler)
    )
    return provider


async def test_chat_stream_yields_text_then_the_final_counts() -> None:
    lines = [
        {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {
            "message": {"content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 5,
            "eval_count": 2,
            "model": "llama3.2:3b",
        },
    ]
    provider = _streaming_provider("\n".join(json.dumps(x) for x in lines))
    items = [i async for i in provider.chat_stream(_chat_req(), "llama3.2:3b")]
    assert items[:2] == ["Hel", "lo"]
    final = items[2]
    assert (final.finish_reason, final.usage.total_tokens) == ("stop", 7)


async def test_chat_stream_errors_are_provider_errors() -> None:
    provider = _streaming_provider("model not found", status=404)
    with pytest.raises(ProviderError):
        async for _ in provider.chat_stream(_chat_req(), "nope"):
            pass
    truncated = _streaming_provider(json.dumps({"message": {"content": "a"}, "done": False}))
    with pytest.raises(ProviderError, match="ended before done"):
        async for _ in truncated.chat_stream(_chat_req(), "m"):
            pass


def _chat_req() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="hi")])


def _failing(status: int, body: str) -> OllamaProvider:
    provider = OllamaProvider("http://ollama")
    provider._client = httpx.AsyncClient(
        base_url="http://ollama",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body)),
    )
    return provider


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_an_error_keeps_ollamas_own_reason_and_status(stream: bool) -> None:
    """httpx's message names only the status, the URL and an MDN link; the
    reason QA and on-call need is in Ollama's body."""
    provider = _failing(404, json.dumps({"error": "model 'no-such-model' not found"}))
    with pytest.raises(ProviderError) as info:
        if stream:
            async for _ in provider.chat_stream(_chat_req(), "no-such-model"):
                pass
        else:
            await provider.chat(_chat_req(), "no-such-model")
    assert info.value.detail == "404 model 'no-such-model' not found"
    assert (info.value.status, info.value.retryable, info.value.rejected) == (404, False, False)


async def test_an_error_body_that_is_not_json_is_trimmed_into_the_detail() -> None:
    provider = _failing(400, "<html>" + "x" * 2000)
    with pytest.raises(ProviderError) as info:
        await provider.embeddings(EmbeddingsRequest(input=["x"]), "m")
    assert info.value.detail.startswith("400 <html>")
    assert len(info.value.detail) < 600
    assert info.value.rejected is True  # a 400: the request itself is invalid
