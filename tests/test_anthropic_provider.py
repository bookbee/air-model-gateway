"""AnthropicProvider against a faked ``AsyncAnthropic``-shaped client: request
shaping (system blocks, prompt-cache hint, forced-tool structured output, the
dropped temperature), usage/cache-token mapping, refusal, and error normalization —
none of which need a key or network to pin down."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError
from air_model_gateway.gateway.providers.anthropic_provider import AnthropicProvider
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Message

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
}


class _Throttled(Exception):
    """Stands in for ``anthropic.RateLimitError``."""


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _message(
    *, content: list[Any] | None = None, stop_reason: str = "end_turn", usage: Any = None
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content if content is not None else [SimpleNamespace(type="text", text="pong")],
        stop_reason=stop_reason,
        usage=usage
        or SimpleNamespace(
            input_tokens=12,
            output_tokens=3,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        ),
    )


def _provider(response: Any = None) -> tuple[AnthropicProvider, _FakeMessages]:
    provider = AnthropicProvider("sk-test")
    messages = _FakeMessages(response if response is not None else _message())
    provider._client = SimpleNamespace(messages=messages)
    provider._rate_limit_error = _Throttled
    return provider, messages


def _req(**kwargs: Any) -> ChatRequest:
    return ChatRequest(
        messages=[
            Message(role="system", content="be terse"),
            Message(role="user", content="ping"),
        ],
        **kwargs,
    )


async def test_missing_key_is_a_clean_provider_error() -> None:
    with pytest.raises(ProviderError, match="no ANTHROPIC api key"):
        await AnthropicProvider("").chat(_req(), "claude-haiku-4-5")


async def test_health_reflects_key_presence() -> None:
    assert await AnthropicProvider("sk").health() is True
    assert await AnthropicProvider("").health() is False


async def test_chat_maps_content_and_usage() -> None:
    provider, messages = _provider()
    result = await provider.chat(_req(), "claude-haiku-4-5")

    assert result.content == "pong"
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 3
    assert result.usage.total_tokens == 15
    assert result.finish_reason == "end_turn"

    [call] = messages.calls
    # The system message leaves the turn list and becomes a cacheable block.
    assert call["system"] == [
        {"type": "text", "text": "be terse", "cache_control": {"type": "ephemeral"}}
    ]
    assert call["messages"] == [{"role": "user", "content": "ping"}]
    assert call["max_tokens"] == 1024  # the API requires one; a default is supplied


async def test_cache_prefix_false_drops_the_cache_hint() -> None:
    provider, messages = _provider()
    await provider.chat(_req(cache_prefix=False), "m")
    assert "cache_control" not in messages.calls[0]["system"][0]


async def test_temperature_is_never_forwarded() -> None:
    """The API removed sampling parameters (version 2026-10-21) and the pinned
    SDK rejects the keyword outright, so forwarding it failed the whole rung."""
    provider, messages = _provider()
    await provider.chat(_req(temperature=0.2), "m")
    await provider.chat(_req(), "m")
    assert "temperature" not in messages.calls[0]
    assert "temperature" not in messages.calls[1]


def test_the_pinned_sdk_still_rejects_temperature() -> None:
    """Guards the fake above against drift: if a future SDK accepts the keyword
    again, this fails and the decision to drop it should be revisited."""
    import inspect

    anthropic = pytest.importorskip("anthropic")
    params = inspect.signature(anthropic.AsyncAnthropic(api_key="x").messages.create).parameters
    assert "temperature" not in params


async def test_system_is_omitted_when_there_is_no_system_message() -> None:
    """Sending `system=None` reaches the API as an explicit null, which it
    rejects with a 400."""
    provider, messages = _provider()
    await provider.chat(ChatRequest(messages=[Message(role="user", content="hi")]), "m")
    assert "system" not in messages.calls[0]


async def test_prompt_cache_tokens_are_reported_separately() -> None:
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=4,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=50,
    )
    provider, _ = _provider(_message(usage=usage))
    result = await provider.chat(_req(), "m")
    # Anthropic already reports cache tokens disjoint from input_tokens, so
    # nothing is subtracted (unlike the OpenAI-shaped adapters).
    assert result.usage.prompt_tokens == 10
    assert result.usage.cache_read_tokens == 900
    assert result.usage.cache_write_tokens == 50


async def test_structured_output_uses_a_forced_tool() -> None:
    tool_use = SimpleNamespace(type="tool_use", input={"label": "positive"})
    provider, messages = _provider(_message(content=[tool_use], stop_reason="tool_use"))
    result = await provider.chat(_req(json_schema=SCHEMA, schema_name="sentiment"), "m")

    assert json.loads(result.content) == {"label": "positive"}
    [call] = messages.calls
    assert call["tools"][0]["name"] == "sentiment"
    assert call["tools"][0]["input_schema"] == SCHEMA
    assert call["tool_choice"] == {"type": "tool", "name": "sentiment"}


async def test_refusal_is_flagged_with_no_content() -> None:
    provider, _ = _provider(_message(content=[], stop_reason="refusal"))
    result = await provider.chat(_req(), "m")
    assert result.refusal is True
    assert result.content == ""
    assert result.usage.total_tokens == 15  # refused, but the prompt was processed


async def test_rate_limit_is_flagged() -> None:
    provider, _ = _provider(_Throttled("429"))
    with pytest.raises(ProviderError) as info:
        await provider.chat(_req(), "m")
    assert info.value.rate_limited is True
    assert info.value.provider is Provider.ANTHROPIC


async def test_other_sdk_errors_are_normalized() -> None:
    provider, _ = _provider(RuntimeError("overloaded"))
    with pytest.raises(ProviderError) as info:
        await provider.chat(_req(), "m")
    assert info.value.rate_limited is False


async def test_embeddings_are_unsupported() -> None:
    with pytest.raises(ProviderError, match="no embeddings"):
        await AnthropicProvider("sk").embeddings(EmbeddingsRequest(input=["x"]), "m")


def _conversation(**kwargs: Any) -> ChatRequest:
    return ChatRequest(
        messages=[
            Message(role="system", content="be terse"),
            Message(role="user", content="Hello, my name is Alex."),
            Message(role="assistant", content="Hi Alex!"),
            Message(role="user", content="What is my name?"),
        ],
        **kwargs,
    )


async def test_a_conversation_marks_its_history_for_the_prompt_cache() -> None:
    """The caller resends the whole history each turn; marking its end lets the
    next turn read every earlier turn back at the cache rate."""
    provider, messages = _provider()
    await provider.chat(_conversation(), "m")
    turns = messages.calls[0]["messages"]
    assert turns[-1]["content"] == [
        {"type": "text", "text": "What is my name?", "cache_control": {"type": "ephemeral"}}
    ]
    assert all(isinstance(t["content"], str) for t in turns[:-1])


async def test_a_one_shot_call_pays_no_cache_write_for_its_turns() -> None:
    provider, messages = _provider()
    await provider.chat(_req(), "m")
    assert messages.calls[0]["messages"] == [{"role": "user", "content": "ping"}]


async def test_cache_prefix_false_marks_nothing() -> None:
    provider, messages = _provider()
    await provider.chat(_conversation(cache_prefix=False), "m")
    turns = messages.calls[0]["messages"]
    assert all(isinstance(t["content"], str) for t in turns)
    assert "cache_control" not in messages.calls[0]["system"][0]


class _FakeStream:
    """Stands in for the SDK's AsyncMessageStream (an async context manager)."""

    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        async def events() -> Any:
            for event in self._events:
                yield event

        return events()

    async def get_final_message(self) -> Any:
        return self._final


def _delta_event(kind: str, **fields: Any) -> Any:
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type=kind, **fields))


async def test_chat_stream_yields_text_then_usage_and_stop_reason() -> None:
    provider, _ = _provider()
    calls: list[dict[str, Any]] = []
    events = [
        SimpleNamespace(type="message_start"),
        _delta_event("text_delta", text="Hel"),
        _delta_event("text_delta", text="lo"),
    ]

    def stream(**kwargs: Any) -> _FakeStream:
        calls.append(kwargs)
        return _FakeStream(events, _message())

    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    items = [i async for i in provider.chat_stream(_req(), "m")]
    assert items[:2] == ["Hel", "lo"]
    assert (items[2].finish_reason, items[2].usage.total_tokens) == ("end_turn", 15)
    assert "temperature" not in calls[0]


async def test_streamed_structured_output_is_the_tools_json_text() -> None:
    provider, _ = _provider()
    events = [
        _delta_event("input_json_delta", partial_json='{"label": '),
        _delta_event("input_json_delta", partial_json='"positive"}'),
    ]
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **k: _FakeStream(events, _message()))
    )
    items = [i async for i in provider.chat_stream(_req(json_schema=SCHEMA), "m")]
    assert json.loads("".join(items[:2])) == {"label": "positive"}
