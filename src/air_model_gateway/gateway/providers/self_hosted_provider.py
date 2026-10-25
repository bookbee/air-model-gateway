from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.providers._common import (
    NeverRaised,
    int_attr,
    openai_stream,
    openai_usage,
    provider_error,
    with_schema_instruction,
)
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage

# Timeout for the reachability probe backing health() — short on purpose, same
# rationale as OllamaProvider's: this backs /v1/ready, polled on a far tighter
# budget than the completion timeout the main client is built with.
_HEALTH_TIMEOUT_S = 2.0


class SelfHostedProvider:
    """Any OpenAI-wire-compatible self-hosted backend: llama.cpp/vLLM/TGI
    locally, either of the latter two in prod. Verified directly against two
    running instances — LM Studio and llama.cpp's `llama-server` — rather than
    assumed from the spec: chat completions, usage (incl. prompt-cache
    reporting), and embeddings all match OpenAI's shapes on both.

    Reuses the ``openai`` SDK pointed at a configurable ``base_url`` — the same
    technique the SDK itself documents for self-hosted servers — but is a
    distinct class from :class:`OpenAIProvider`, not that class with a
    different URL: it carries its own :class:`Provider` identity so
    ``PriceBook.is_free`` can price it free unconditionally (cost.py), the same
    way it already does for Ollama. No API key is required — most self-hosted
    servers (LM Studio included) don't check one; a vLLM/TGI deployment that
    does enforce a token still works by setting ``self_hosted.api_key``.
    """

    name = Provider.SELF_HOSTED

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url
        self._api_key = api_key or "not-needed"
        self._client: Any = None
        self._rate_limit_error: type[BaseException] = NeverRaised

    def _ensure(self) -> None:
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(
                    self.name, "openai SDK not installed", configured=False
                ) from exc
            self._client = openai.AsyncOpenAI(
                api_key=self._api_key, base_url=self._base_url, max_retries=0
            )
            self._rate_limit_error = openai.RateLimitError

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        messages = self._messages(req)
        # No response_format here, deliberately — unlike real OpenAI cloud,
        # self-hosted servers disagree on what they accept. LM Studio rejects
        # response_format={"type":"json_object"}
        # outright (400, "must be 'json_schema' or 'text'"), and the stricter
        # "json_schema" dialect is not something every self-hosted server
        # (vLLM, TGI, llama.cpp-server, ...) supports either. The prompt
        # instruction _messages() already injects is what actually carries the
        # schema request; response_format would only add a way to 400 that
        # varies per backend, for no adapter that can't already 400 on it.
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
            )
        except Exception as exc:
            raise self._error(exc) from exc

        choice = resp.choices[0]
        # A refusal still consumed tokens: it keeps its usage like any call.
        refused = bool(getattr(choice.message, "refusal", None))
        # Usage follows OpenAI's convention on these servers (confirmed against
        # LM Studio and llama.cpp): reasoning tokens are already inside
        # completion_tokens, and prompt-cache hits are reported inside
        # prompt_tokens, which openai_usage subtracts back out. Free either way
        # (SELF_HOSTED prices at 0), but this keeps usage and metrics honest.
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
                # Accepted by llama.cpp, vLLM and LM Studio; without it the
                # stream carries no usage at all.
                stream_options={"include_usage": True},
            )
        except Exception as exc:
            raise self._error(exc) from exc
        async for item in openai_stream(stream, model, self._error):
            yield item

    def _error(self, exc: Exception) -> ProviderError:
        return provider_error(
            self.name,
            exc,
            detail=f"{self._base_url}: {exc}",
            throttled=isinstance(exc, self._rate_limit_error),
        )

    def _messages(self, req: ChatRequest) -> list[dict[str, str]]:
        return with_schema_instruction([m.model_dump() for m in req.messages], req.json_schema)

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        self._ensure()
        try:
            resp = await self._client.embeddings.create(model=model, input=req.input)
        except Exception as exc:
            raise self._error(exc) from exc
        tokens = int_attr(resp.usage, "prompt_tokens")
        return ProviderResult(
            model=model,
            embeddings=[d.embedding for d in resp.data],
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    async def health(self) -> bool:
        # No API-key gate — unlike the cloud providers, this backend has no
        # "not configured" state; health means "the server answers", same as
        # Ollama's probe.
        # max_retries=0: the SDK otherwise retries a refused connection twice
        # with backoff, each attempt getting the full timeout again: ~1.3s for
        # a llama-server that isn't running, up to ~3x the timeout for one that
        # hangs. That would push /v1/ready past the 2s budget consumers' probes
        # give it, making the whole gateway look down over one optional backend.
        try:
            self._ensure()
            await self._client.with_options(max_retries=0).models.list(timeout=_HEALTH_TIMEOUT_S)
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
