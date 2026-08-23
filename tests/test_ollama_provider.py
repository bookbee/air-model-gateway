"""OllamaProvider's keep_alive behavior.

Regression coverage for a gap flagged from outside this repo (an air-classifier
session, integrating against air-llm): the adapter never sent keep_alive, so
Ollama's own server default (5m) applied and every AIR service sharing that
Ollama instance paid a reload cold-start on the next call past that window —
undoing even a manual `make ollama-warm` the moment the first real request
landed and reset the timer back down to 5m.
"""

from __future__ import annotations

from typing import Any

import pytest

from air_llm.gateway.providers.ollama_provider import OllamaProvider
from air_llm.schemas.models import ChatRequest, EmbeddingsRequest, Message


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
