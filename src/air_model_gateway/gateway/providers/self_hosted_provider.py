from __future__ import annotations

from typing import Any

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
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
        self._rate_limit_error: type[BaseException] = _NeverRaised

    def _ensure(self) -> None:
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(self.name, "openai SDK not installed") from exc
            self._client = openai.AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
            self._rate_limit_error = openai.RateLimitError

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        messages = self._messages(req)
        # No response_format here, deliberately — unlike real OpenAI cloud,
        # self-hosted servers disagree on what they accept. Verified live
        # against LM Studio: it rejects response_format={"type":"json_object"}
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
        except self._rate_limit_error as exc:
            raise ProviderError(self.name, str(exc), rate_limited=True) from exc
        except Exception as exc:
            raise ProviderError(self.name, f"{self._base_url}: {exc}") from exc

        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            return ProviderResult(model=model, refusal=True, finish_reason="refusal")

        usage = resp.usage
        # Unlike Gemini's separately-reported thinking tokens, a reasoning-model
        # server following OpenAI's convention (confirmed against LM Studio +
        # gemma) already folds reasoning tokens into completion_tokens; the
        # completion_tokens_details.reasoning_tokens breakdown is informational
        # only, so no extra folding is needed here.
        prompt_tokens = _int(usage, "prompt_tokens")
        # Confirmed against llama.cpp's server too (prompt caching across calls
        # sharing a prefix, same shape as OpenAI's): cached tokens are reported
        # inside prompt_tokens, so — same as OpenAIProvider — they're subtracted
        # back out here. Free either way (SELF_HOSTED prices at 0), but this
        # keeps usage/metrics honest, not just cost.
        cache_read = _int(getattr(usage, "prompt_tokens_details", None), "cached_tokens")
        return ProviderResult(
            model=model,
            content=choice.message.content or "",
            usage=Usage(
                prompt_tokens=max(0, prompt_tokens - cache_read),
                completion_tokens=_int(usage, "completion_tokens"),
                total_tokens=_int(usage, "total_tokens"),
                cache_read_tokens=cache_read,
            ),
            finish_reason=choice.finish_reason,
        )

    def _messages(self, req: ChatRequest) -> list[dict[str, str]]:
        messages = [m.model_dump() for m in req.messages]
        if req.json_schema is None:
            return messages
        instruction = (
            "Respond with a single JSON object that conforms to this JSON Schema. "
            "Output only the JSON, no prose:\n" + _dumps(req.json_schema)
        )
        for msg in messages:
            if msg["role"] == "system":
                msg["content"] = msg["content"] + "\n\n" + instruction
                return messages
        return [{"role": "system", "content": instruction}, *messages]

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        self._ensure()
        try:
            resp = await self._client.embeddings.create(model=model, input=req.input)
        except self._rate_limit_error as exc:
            raise ProviderError(self.name, str(exc), rate_limited=True) from exc
        except Exception as exc:
            raise ProviderError(self.name, f"{self._base_url}: {exc}") from exc
        return ProviderResult(
            model=model,
            embeddings=[d.embedding for d in resp.data],
            usage=Usage(prompt_tokens=_int(resp.usage, "prompt_tokens")),
        )

    async def health(self) -> bool:
        # No API-key gate — unlike the cloud providers, this backend has no
        # "not configured" state; health means "the server answers", same as
        # Ollama's probe.
        try:
            self._ensure()
            await self._client.models.list(timeout=_HEALTH_TIMEOUT_S)
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()


class _NeverRaised(Exception):
    """Inert stand-in for the SDK rate-limit class until the client is built."""


def _int(obj: object, field: str) -> int:
    value = getattr(obj, field, None)
    return int(value) if isinstance(value, int) else 0


def _dumps(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, separators=(",", ":"))
