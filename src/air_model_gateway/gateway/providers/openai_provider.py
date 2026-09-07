from __future__ import annotations

from typing import Any

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage

# finish_reason meaning the model stopped short — nothing that validates came back.
_LENGTH_FINISH_REASON = "length"


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
        self._rate_limit_error: type[BaseException] = _NeverRaised

    def _ensure(self) -> None:
        if not self._api_key:
            raise ProviderError(self.name, "no OPENAI api key configured")
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(self.name, "openai SDK not installed") from exc
            self._client = openai.AsyncOpenAI(api_key=self._api_key)
            self._rate_limit_error = openai.RateLimitError

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        messages = self._messages(req)
        optional: dict[str, Any] = {}
        if req.json_schema is not None:
            optional["response_format"] = {"type": "json_object"}

        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                **optional,
            )
        except self._rate_limit_error as exc:
            raise ProviderError(self.name, str(exc), rate_limited=True) from exc
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc

        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            return ProviderResult(model=model, refusal=True, finish_reason="refusal")

        usage = resp.usage
        prompt_tokens = _int(usage, "prompt_tokens")
        cache_read = _int(getattr(usage, "prompt_tokens_details", None), "cached_tokens")
        return ProviderResult(
            model=model,
            content=choice.message.content or "",
            usage=Usage(
                # Cached tokens are reported inside prompt_tokens; subtract them
                # out so they are not billed twice at the full input rate.
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
        # Steer JSON mode toward the target shape. JSON mode requires the word
        # "json" somewhere in the prompt, which this instruction also satisfies.
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
            raise ProviderError(self.name, str(exc)) from exc
        return ProviderResult(
            model=model,
            embeddings=[d.embedding for d in resp.data],
            usage=Usage(prompt_tokens=_int(resp.usage, "prompt_tokens")),
        )

    async def health(self) -> bool:
        return bool(self._api_key)

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
