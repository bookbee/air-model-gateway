"""The Gemini adapter's own translation layer.

Worth testing directly rather than through the router: Gemini is the only
provider here whose request shape differs structurally from the others. It has
no `system` role, spells the assistant `model`, and enforces a JSON Schema at
decode time — in a dialect that rejects the `$defs` pydantic emits for nested
models. Each of those is a silent wrong-answer if it regresses, not a crash.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError
from air_model_gateway.gateway.providers.gemini_provider import GeminiProvider
from air_model_gateway.schemas.models import ChatRequest, Message

FLAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
}

#: What `model_json_schema()` produces for a model with a nested field.
NESTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"aspect": {"$ref": "#/$defs/Aspect"}},
    "$defs": {"Aspect": {"type": "object", "properties": {"name": {"type": "string"}}}},
}


class _FakeModels:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _response(
    *,
    text: str = '{"label":"positive"}',
    finish_reason: str = "STOP",
    block_reason: str | None = None,
    prompt_tokens: int = 100,
    candidate_tokens: int = 10,
    thought_tokens: int = 0,
    cached_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish_reason))],
        prompt_feedback=(
            None if block_reason is None else SimpleNamespace(block_reason=block_reason)
        ),
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidate_tokens,
            thoughts_token_count=thought_tokens,
            cached_content_token_count=cached_tokens,
            total_token_count=prompt_tokens + candidate_tokens,
        ),
    )


def _provider(response: Any) -> tuple[GeminiProvider, _FakeModels]:
    from google.genai import errors, types

    provider = GeminiProvider("AIza-test")
    models = _FakeModels(response)
    provider._client = SimpleNamespace(aio=SimpleNamespace(models=models))
    provider._types = types
    provider._api_error = errors.APIError
    return provider, models


def _req(messages: list[Message], **kwargs: Any) -> ChatRequest:
    return ChatRequest(messages=messages, max_tokens=256, **kwargs)


async def test_no_key_is_a_provider_error() -> None:
    with pytest.raises(ProviderError, match="no GEMINI api key"):
        await GeminiProvider("").chat(_req([Message(role="user", content="x")]), "gemini-2.5-flash")


async def test_system_becomes_an_instruction_and_never_a_turn() -> None:
    provider, models = _provider(_response())
    await provider.chat(
        _req(
            [
                Message(role="system", content="RUBRIC"),
                Message(role="user", content="Hated it."),
                Message(role="assistant", content="Noted."),
            ]
        ),
        "gemini-2.5-flash",
    )

    call = models.calls[0]
    assert call["config"].system_instruction == "RUBRIC"
    # Only user/assistant turns survive, and the assistant is spelled `model`.
    assert [c["role"] for c in call["contents"]] == ["user", "model"]


async def test_a_flat_schema_is_enforced_at_decode_time() -> None:
    provider, models = _provider(_response())
    await provider.chat(_req([Message(role="user", content="x")], json_schema=FLAT_SCHEMA), "m")

    config = models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is not None


async def test_a_nested_schema_degrades_to_json_mode_rather_than_400ing() -> None:
    """Gemini rejects $defs/$ref, so the schema moves into the instruction."""
    provider, models = _provider(_response())
    await provider.chat(_req([Message(role="user", content="x")], json_schema=NESTED_SCHEMA), "m")

    config = models.calls[0]["config"]
    assert config.response_schema is None
    assert config.response_mime_type == "application/json"
    assert "JSON Schema" in (config.system_instruction or "")


async def test_cached_tokens_are_not_billed_twice() -> None:
    provider, _ = _provider(_response(prompt_tokens=1000, cached_tokens=900))
    result = await provider.chat(_req([Message(role="user", content="x")]), "m")
    assert result.usage.prompt_tokens == 100
    assert result.usage.cache_read_tokens == 900


async def test_thinking_tokens_are_counted_as_completion() -> None:
    provider, _ = _provider(_response(candidate_tokens=10, thought_tokens=90))
    result = await provider.chat(_req([Message(role="user", content="x")]), "m")
    assert result.usage.completion_tokens == 100


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"block_reason": "SAFETY"}, "blocked before generation"),
        ({"finish_reason": "SAFETY"}, "blocked during"),
    ],
)
async def test_both_refusal_channels_become_a_refusal(kwargs: dict[str, Any], why: str) -> None:
    provider, _ = _provider(_response(**kwargs))
    result = await provider.chat(_req([Message(role="user", content="x")]), "m")
    assert result.refusal is True, why


async def test_rate_limit_is_flagged_so_the_api_can_answer_429() -> None:
    from google.genai import errors

    provider, _ = _provider(errors.ClientError(429, {"error": {"message": "quota"}}))
    with pytest.raises(ProviderError) as caught:
        await provider.chat(_req([Message(role="user", content="x")]), "m")
    assert caught.value.rate_limited is True


async def test_other_errors_are_not_flagged_as_rate_limits() -> None:
    from google.genai import errors

    provider, _ = _provider(errors.ClientError(400, {"error": {"message": "bad"}}))
    with pytest.raises(ProviderError) as caught:
        await provider.chat(_req([Message(role="user", content="x")]), "m")
    assert caught.value.rate_limited is False


async def test_name_is_the_gemini_provider() -> None:
    assert GeminiProvider("k").name is Provider.GEMINI
