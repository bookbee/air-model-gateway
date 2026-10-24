from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.providers._common import (
    CONTENT_FILTER_FINISH_REASON,
    NeverRaised,
    int_attr,
    is_transient,
    openai_stream,
    openai_usage,
    with_schema_instruction,
)
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage


class OpenAIProvider:
    """Cloud provider. SDK imported lazily (see AnthropicProvider for rationale).

    Structured output uses JSON mode plus the schema appended to the system
    message. This never 400s on a schema the strict parser would reject, and the
    caller validates (and repairs once) client-side — so correctness does not
    hinge on the gateway massaging every schema into OpenAI's strict dialect.
    """

    name = Provider.OPENAI

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: Any = None
        self._rate_limit_error: type[BaseException] = NeverRaised

    def _ensure(self) -> None:
        if not self._api_key:
            raise ProviderError(self.name, "no OPENAI api key configured")
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(self.name, "openai SDK not installed") from exc
            self._client = openai.AsyncOpenAI(api_key=self._api_key, max_retries=0)
            self._rate_limit_error = openai.RateLimitError

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        messages = self._messages(req)
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                **self._optional(req),
            )
        except Exception as exc:
            raise self._error(exc) from exc

        choice = resp.choices[0]
        # A refusal (explicit, or the content filter cutting the answer) still
        # consumed tokens, so it keeps its usage and is priced like any call.
        refused = bool(getattr(choice.message, "refusal", None)) or (
            choice.finish_reason == CONTENT_FILTER_FINISH_REASON
        )
        return ProviderResult(
            model=model,
            content="" if refused else choice.message.content or "",
            usage=openai_usage(resp.usage),
            finish_reason="refusal" if refused else choice.finish_reason,
            refusal=refused,
        )

    async def chat_stream(
        self, req: ChatRequest, model: str
    ) -> AsyncGenerator[str | ProviderResult, None]:
        self._ensure()
        try:
            stream = await self._client.chat.completions.create(
                model=model,
                messages=self._messages(req),
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                stream=True,
                stream_options={"include_usage": True},
                **self._optional(req),
            )
        except Exception as exc:
            raise self._error(exc) from exc
        async for item in openai_stream(stream, model, self._error):
            yield item

    @staticmethod
    def _optional(req: ChatRequest) -> dict[str, Any]:
        if req.json_schema is not None:
            return {"response_format": {"type": "json_object"}}
        return {}

    def _error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, self._rate_limit_error):
            return ProviderError(self.name, str(exc), rate_limited=True)
        return ProviderError(self.name, str(exc), retryable=is_transient(exc))

    def _messages(self, req: ChatRequest) -> list[dict[str, str]]:
        # JSON mode only constrains output to *some* JSON object; the schema
        # instruction is what steers it toward the target shape.
        return with_schema_instruction([m.model_dump() for m in req.messages], req.json_schema)

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        self._ensure()
        try:
            resp = await self._client.embeddings.create(model=model, input=req.input)
        except self._rate_limit_error as exc:
            raise ProviderError(self.name, str(exc), rate_limited=True) from exc
        except Exception as exc:
            raise ProviderError(self.name, str(exc), retryable=is_transient(exc)) from exc
        tokens = int_attr(resp.usage, "prompt_tokens")
        return ProviderResult(
            model=model,
            embeddings=[d.embedding for d in resp.data],
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    async def health(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
