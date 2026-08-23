"""SelfHostedProvider — the OpenAI-wire-compatible adapter for LM Studio /
vLLM / TGI. Verified live against a running LM Studio instance (chat
completions, usage, and embeddings) before this was written; these tests pin
that behavior with a fake client so a regression doesn't need LM Studio
running to be caught.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from air_llm.constants import Provider
from air_llm.gateway.providers.self_hosted_provider import SelfHostedProvider
from air_llm.schemas.models import ChatRequest, EmbeddingsRequest, Message

FLAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
}


class _FakeCompletions:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeEmbeddings:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


class _FakeModels:
    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    async def list(self, **kwargs: Any) -> Any:
        if not self._healthy:
            raise RuntimeError("connection refused")
        return SimpleNamespace(data=[])


def _chat_response(
    *, content: str = "pong", finish_reason: str = "stop", refusal: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, refusal=refusal),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=23, completion_tokens=49, total_tokens=72),
    )


def _provider(
    chat_response: Any = None, embeddings_response: Any = None, *, healthy: bool = True
) -> tuple[SelfHostedProvider, _FakeCompletions, _FakeEmbeddings]:
    provider = SelfHostedProvider("http://127.0.0.1:1234/v1")
    completions = _FakeCompletions(chat_response if chat_response is not None else _chat_response())
    embeddings = _FakeEmbeddings(embeddings_response)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        embeddings=embeddings,
        models=_FakeModels(healthy=healthy),
    )
    return provider, completions, embeddings


def _req(messages: list[Message], **kwargs: Any) -> ChatRequest:
    return ChatRequest(messages=messages, max_tokens=50, **kwargs)


def test_name_is_self_hosted() -> None:
    assert SelfHostedProvider("http://x").name is Provider.SELF_HOSTED


def test_no_api_key_is_required_to_construct() -> None:
    # Unlike Anthropic/OpenAI/Gemini, a missing key is not a ProviderError here
    # — most self-hosted servers (LM Studio included) don't check one.
    provider = SelfHostedProvider("http://127.0.0.1:1234/v1")
    assert provider._api_key == "not-needed"


async def test_plain_chat_maps_content_and_usage() -> None:
    provider, completions, _ = _provider()
    result = await provider.chat(_req([Message(role="user", content="ping")]), "google/gemma-4-e2b")
    assert result.content == "pong"
    assert result.usage.prompt_tokens == 23
    assert result.usage.completion_tokens == 49
    assert result.usage.total_tokens == 72
    assert result.finish_reason == "stop"
    assert completions.calls[0]["model"] == "google/gemma-4-e2b"


async def test_json_schema_becomes_a_system_instruction_only() -> None:
    """No response_format kwarg — verified live against LM Studio, which 400s
    on response_format={"type": "json_object"} (it wants "json_schema" or
    "text" specifically) and self-hosted servers disagree on what dialect they
    accept at all. The prompt instruction is the only mechanism relied on."""
    provider, completions, _ = _provider()
    await provider.chat(
        _req([Message(role="user", content="classify")], json_schema=FLAT_SCHEMA), "m"
    )
    call = completions.calls[0]
    assert "response_format" not in call
    assert call["messages"][0]["role"] == "system"
    assert "JSON Schema" in call["messages"][0]["content"]


async def test_refusal_field_is_normalized() -> None:
    provider, _, _ = _provider(_chat_response(refusal="I can't help with that"))
    result = await provider.chat(_req([Message(role="user", content="x")]), "m")
    assert result.refusal is True


async def test_cached_prompt_tokens_are_not_billed_twice() -> None:
    """Confirmed live against llama.cpp: prompt-cache hits are reported inside
    prompt_tokens (same convention as OpenAI), not disjoint from it — so they
    must be subtracted back out, same treatment OpenAIProvider already gets."""
    response = _chat_response()
    response.usage = SimpleNamespace(
        prompt_tokens=82, completion_tokens=6, total_tokens=88,
        prompt_tokens_details=SimpleNamespace(cached_tokens=25),
    )
    provider, _, _ = _provider(response)
    result = await provider.chat(_req([Message(role="user", content="x")]), "m")
    assert result.usage.prompt_tokens == 57  # 82 - 25
    assert result.usage.cache_read_tokens == 25


async def test_embeddings_maps_vectors() -> None:
    resp = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2]), SimpleNamespace(embedding=[0.3, 0.4])],
        usage=SimpleNamespace(prompt_tokens=5),
    )
    provider, _, embeddings = _provider(embeddings_response=resp)
    result = await provider.embeddings(
        EmbeddingsRequest(input=["a", "b"]), "text-embedding-nomic-embed-text-v1.5"
    )
    assert result.embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert result.usage.prompt_tokens == 5
    assert embeddings.calls[0]["model"] == "text-embedding-nomic-embed-text-v1.5"


async def test_health_true_when_server_answers() -> None:
    provider, _, _ = _provider(healthy=True)
    assert await provider.health() is True


async def test_health_false_when_unreachable_not_raised() -> None:
    provider, _, _ = _provider(healthy=False)
    assert await provider.health() is False
