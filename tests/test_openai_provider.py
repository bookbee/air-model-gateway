"""OpenAIProvider against a faked ``AsyncOpenAI``-shaped client: JSON mode plus
the schema-in-system-prompt instruction, cached-token accounting, refusal,
embeddings, and error normalization."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from air_model_gateway.gateway.base import ProviderError
from air_model_gateway.gateway.providers.openai_provider import OpenAIProvider
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Message

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"label": {"type": "string"}}}


class _Throttled(Exception):
    """Stands in for ``openai.RateLimitError``."""


class _FakeCreate:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _completion(*, content: str = "pong", refusal: str | None = None, usage: Any = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, refusal=refusal), finish_reason="stop"
            )
        ],
        usage=usage
        or SimpleNamespace(
            prompt_tokens=20, completion_tokens=5, total_tokens=25, prompt_tokens_details=None
        ),
    )


def _provider(
    chat: Any = None, embeddings: Any = None
) -> tuple[OpenAIProvider, _FakeCreate, _FakeCreate]:
    provider = OpenAIProvider("sk-test")
    completions = _FakeCreate(chat if chat is not None else _completion())
    embed = _FakeCreate(embeddings)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions), embeddings=embed
    )
    provider._rate_limit_error = _Throttled
    return provider, completions, embed


def _req(messages: list[Message] | None = None, **kwargs: Any) -> ChatRequest:
    return ChatRequest(messages=messages or [Message(role="user", content="ping")], **kwargs)


async def test_missing_key_is_a_clean_provider_error() -> None:
    with pytest.raises(ProviderError, match="no OPENAI api key"):
        await OpenAIProvider("").chat(_req(), "gpt-4o-mini")


async def test_plain_chat_sends_no_response_format() -> None:
    provider, completions, _ = _provider()
    result = await provider.chat(_req(temperature=0.3, max_tokens=40), "gpt-4o-mini")

    assert result.content == "pong"
    assert result.usage.total_tokens == 25
    [call] = completions.calls
    assert "response_format" not in call
    assert call["temperature"] == 0.3
    assert call["max_tokens"] == 40
    assert call["messages"] == [{"role": "user", "content": "ping"}]


async def test_structured_output_is_json_mode_plus_instruction() -> None:
    provider, completions, _ = _provider()
    await provider.chat(_req(json_schema=SCHEMA), "gpt-4o-mini")

    [call] = completions.calls
    assert call["response_format"] == {"type": "json_object"}
    system = call["messages"][0]
    assert system["role"] == "system"
    # JSON mode 400s unless "json" appears in the prompt; the instruction
    # carries both that word and the schema itself.
    assert "JSON" in system["content"]
    assert '"label"' in system["content"]


async def test_instruction_is_appended_to_an_existing_system_message() -> None:
    provider, completions, _ = _provider()
    msgs = [Message(role="system", content="be terse"), Message(role="user", content="x")]
    await provider.chat(_req(msgs, json_schema=SCHEMA), "m")

    sent = completions.calls[0]["messages"]
    assert len(sent) == 2
    assert sent[0]["content"].startswith("be terse\n\n")


async def test_cached_tokens_are_not_billed_twice() -> None:
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=10,
        total_tokens=1010,
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    )
    provider, _, _ = _provider(_completion(usage=usage))
    result = await provider.chat(_req(), "m")
    assert result.usage.prompt_tokens == 200
    assert result.usage.cache_read_tokens == 800


async def test_refusal_is_flagged_and_still_billed() -> None:
    provider, _, _ = _provider(_completion(content="", refusal="I can't help with that"))
    result = await provider.chat(_req(), "m")
    assert result.refusal is True
    assert result.finish_reason == "refusal"
    assert result.usage.total_tokens == 25  # the prompt was still processed


async def test_a_content_filter_stop_is_a_refusal() -> None:
    completion = _completion(content="partial")
    completion.choices[0].finish_reason = "content_filter"
    provider, _, _ = _provider(completion)
    result = await provider.chat(_req(), "m")
    assert result.refusal is True
    assert result.content == ""


async def test_rate_limit_is_flagged() -> None:
    provider, _, _ = _provider(_Throttled("429"))
    with pytest.raises(ProviderError) as info:
        await provider.chat(_req(), "m")
    assert info.value.rate_limited is True


async def test_embeddings_map_vectors_and_usage() -> None:
    response = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2]), SimpleNamespace(embedding=[0.3, 0.4])],
        usage=SimpleNamespace(prompt_tokens=6),
    )
    provider, _, embed = _provider(embeddings=response)
    result = await provider.embeddings(
        EmbeddingsRequest(input=["a", "b"]), "text-embedding-3-small"
    )

    assert result.embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert result.usage.prompt_tokens == 6
    assert result.usage.total_tokens == 6
    assert embed.calls == [{"model": "text-embedding-3-small", "input": ["a", "b"]}]


def _delta(
    content: str | None = None, finish: str | None = None, refusal: str | None = None
) -> Any:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, refusal=refusal), finish_reason=finish
            )
        ],
    )


async def _chunks(*chunks: Any) -> Any:
    for chunk in chunks:
        if isinstance(chunk, Exception):
            raise chunk
        yield chunk


_USAGE_CHUNK = SimpleNamespace(
    choices=[],
    usage=SimpleNamespace(
        prompt_tokens=20,
        completion_tokens=2,
        total_tokens=22,
        prompt_tokens_details=SimpleNamespace(cached_tokens=8),
    ),
)


async def test_chat_stream_asks_for_usage_and_yields_text_then_the_final() -> None:
    stream = _chunks(_delta("Hel"), _delta("lo", finish="stop"), _USAGE_CHUNK)
    provider, completions, _ = _provider(stream)
    items = [i async for i in provider.chat_stream(_req(), "gpt-4o-mini")]
    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["stream_options"] == {"include_usage": True}
    assert items[:2] == ["Hel", "lo"]
    final = items[2]
    assert final.finish_reason == "stop"
    assert (final.usage.prompt_tokens, final.usage.cache_read_tokens) == (12, 8)


async def test_a_streamed_content_filter_stop_is_a_refusal() -> None:
    provider, _, _ = _provider(
        _chunks(_delta("par"), _delta(finish="content_filter"), _USAGE_CHUNK)
    )
    final = [i async for i in provider.chat_stream(_req(), "m")][-1]
    assert (final.refusal, final.finish_reason) == (True, "refusal")


async def test_a_mid_stream_sdk_error_is_a_provider_error() -> None:
    provider, _, _ = _provider(_chunks(_delta("a"), _Throttled("429")))
    with pytest.raises(ProviderError) as info:
        async for _ in provider.chat_stream(_req(), "m"):
            pass
    assert info.value.rate_limited is True
