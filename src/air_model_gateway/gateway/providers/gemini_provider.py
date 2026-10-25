from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.providers._common import (
    int_attr,
    provider_error,
)
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage

log = get_logger("gateway.gemini")

# finish_reason values that are the safety layer declining, not a fault. Strings
# rather than enum members so a new SDK member cannot break the import.
_REFUSAL_FINISH_REASONS = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
)


class GeminiProvider:
    """Cloud provider. SDK imported lazily (see AnthropicProvider for rationale).

    Two shape differences from the other cloud adapters here:

    * **Roles.** Gemini has no ``system`` role. The system message becomes
      ``system_instruction`` and only user/assistant turns go in ``contents``,
      where the assistant role is spelled ``model``.
    * **Structured output is enforced, not suggested.** The caller's JSON Schema
      goes to ``response_json_schema`` and the API constrains decoding to it,
      rather than the schema-in-the-prompt approach the OpenAI adapter uses.
      Not ``response_schema``: that field takes Gemini's own OpenAPI-subset
      ``Schema``, where ``type`` is a single enum value, so the SDK crashes
      building the request for a union type such as ``["string", "null"]`` (the
      usual way to write an optional field). ``response_json_schema`` takes
      standard JSON Schema, union types, ``anyOf`` and ``$defs``/``$ref`` included
      (checked live against gemini-2.5-flash), so the schema is sent as given.

    **Thinking.** 2.5 models think before they answer, and Gemini counts those
    tokens against ``max_output_tokens``. Left to its dynamic budget,
    gemini-2.5-flash spent all of a 300-token cap thinking and returned a
    cut-off half sentence (MG-011). So each model's ``thinking_budget`` comes
    from ``model_settings`` in ``deploy/routing.yaml`` and is sent as
    ``thinking_config``, and the budget is added to the caller's
    ``max_tokens``: the cap then bounds the visible answer, as on every other
    provider. A model with no configured budget keeps Gemini's default.
    """

    name = Provider.GEMINI

    def __init__(self, api_key: str, thinking_budgets: Mapping[str, int] | None = None) -> None:
        self._api_key = api_key
        self._thinking_budgets = dict(thinking_budgets or {})
        self._client: Any = None
        self._types: Any = None

    def _ensure(self) -> None:
        if not self._api_key:
            raise ProviderError(self.name, "no GEMINI api key configured", configured=False)
        if self._client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(
                    self.name, "google-genai SDK not installed", configured=False
                ) from exc
            self._client = genai.Client(api_key=self._api_key)
            self._types = types

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        contents, config = self._request(req, model)
        try:
            resp = await self._client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception as exc:
            raise self._error(exc) from exc
        result = _result(resp, model, blocked=_blocked(resp))
        if not result.refusal:
            result.content = _text(resp)
        return result

    async def chat_stream(
        self, req: ChatRequest, model: str
    ) -> AsyncGenerator[str | ProviderResult, None]:
        """Each streamed chunk carries a slice of text; the last one carries the
        usage (thinking tokens included) and the finish reason. A blocked prompt
        is reported on the chunk that carries `prompt_feedback`."""
        self._ensure()
        contents, config = self._request(req, model)
        last: Any = None
        blocked = False
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=model, contents=contents, config=config
            )
            async for chunk in stream:
                last = chunk
                blocked = blocked or _blocked(chunk)
                text = _text(chunk)
                if text:
                    yield text
        except Exception as exc:
            raise self._error(exc) from exc
        if last is None:
            raise ProviderError(self.name, "stream ended without a response", retryable=True)
        yield _result(last, model, blocked=blocked)

    def _request(self, req: ChatRequest, model: str) -> tuple[list[dict[str, Any]], Any]:
        """Contents and config, shared by the plain and the streamed call."""
        system, contents = self._split(req)
        budget = self._thinking_budgets.get(model)
        cap = req.max_tokens
        config: dict[str, Any] = {}
        if budget is not None:
            config["thinking_config"] = self._types.ThinkingConfig(thinking_budget=budget)
            if cap is not None:
                cap += budget
        config["max_output_tokens"] = cap
        if system:
            config["system_instruction"] = system
        if req.temperature is not None:
            config["temperature"] = req.temperature
        if req.json_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = req.json_schema
        return contents, self._types.GenerateContentConfig(**config)

    def _error(self, exc: Exception) -> ProviderError:
        return provider_error(self.name, exc)

    def _split(self, req: ChatRequest) -> tuple[str, list[dict[str, Any]]]:
        """System text and the turn list, in Gemini's own vocabulary."""
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for message in req.messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})
        return "\n\n".join(system_parts), contents

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        self._ensure()
        # The Gemini API's embed response carries no token usage at all, so
        # without a count the call would price at $0 despite having a row in
        # the price book. countTokens is a separate (free) endpoint; it runs
        # concurrently so it adds no latency, and a router timeout cancels both.
        resp, tokens = await asyncio.gather(self._embed(req, model), self._count_tokens(req, model))
        vectors = [list(e.values or []) for e in (getattr(resp, "embeddings", None) or [])]
        return ProviderResult(
            model=model,
            embeddings=vectors,
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    async def _embed(self, req: EmbeddingsRequest, model: str) -> Any:
        try:
            return await self._client.aio.models.embed_content(model=model, contents=req.input)
        except Exception as exc:
            raise self._error(exc) from exc

    async def _count_tokens(self, req: EmbeddingsRequest, model: str) -> int:
        """Billing only — a failure here must not fail vectors that came back
        fine, so it degrades to 0 tokens (unbilled) with a warning instead."""
        try:
            resp = await self._client.aio.models.count_tokens(model=model, contents=req.input)
        except Exception as exc:
            log.warning("gemini.count_tokens_failed", model=model, error=str(exc))
            return 0
        return int_attr(resp, "total_tokens")

    async def health(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        """No-op: the SDK client owns no session this adapter must close."""
        return None


def _name_of(value: object) -> str | None:
    """The enum member's name, tolerating the SDK returning a bare string."""
    name = getattr(value, "name", value)
    return str(name) if name is not None else None


def _text(resp: Any) -> str:
    """The answer text. The SDK's `text` accessor joins the parts of the first
    candidate; with no candidate (or a part shape it doesn't expect) it can
    return None or raise, which must not escape the failover chain."""
    try:
        return getattr(resp, "text", None) or ""
    except ValueError:
        return ""


def _blocked(resp: Any) -> bool:
    feedback = getattr(resp, "prompt_feedback", None)
    return getattr(feedback, "block_reason", None) is not None


def _result(resp: Any, model: str, *, blocked: bool) -> ProviderResult:
    """Usage and finish reason from a response (or a stream's last chunk), with
    empty content: the caller fills it in, or has streamed it already.

    A blocked prompt or a safety stop is a refusal. It still consumed (prompt)
    tokens, so it keeps its usage and is priced like any call.
    """
    candidates = getattr(resp, "candidates", None) or []
    finish = _name_of(getattr(candidates[0], "finish_reason", None)) if candidates else None
    refused = blocked or finish in _REFUSAL_FINISH_REASONS
    usage = getattr(resp, "usage_metadata", None)
    prompt_tokens = int_attr(usage, "prompt_token_count")
    cache_read = int_attr(usage, "cached_content_token_count")
    # Thinking tokens bill at the output rate but sit outside the candidate
    # count; folding them in is what keeps the reported cost honest.
    completion = int_attr(usage, "candidates_token_count") + int_attr(usage, "thoughts_token_count")
    return ProviderResult(
        model=model,
        refusal=refused,
        usage=Usage(
            # Cached tokens are reported inside prompt_token_count; subtract
            # them out so they are not billed twice at the full input rate.
            prompt_tokens=max(0, prompt_tokens - cache_read),
            completion_tokens=completion,
            total_tokens=int_attr(usage, "total_token_count"),
            cache_read_tokens=cache_read,
        ),
        finish_reason="refusal" if refused else finish,
    )
