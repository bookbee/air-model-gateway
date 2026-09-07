from __future__ import annotations

from typing import Any

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage

# HTTP status meaning "back off" rather than "this will never work".
_TOO_MANY_REQUESTS = 429

# finish_reason meaning the model stopped short — nothing that validates came back.
_LENGTH_FINISH_REASON = "MAX_TOKENS"

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
      goes to ``response_schema`` and the API constrains decoding to it, rather
      than the schema-in-the-prompt approach the OpenAI adapter uses. Gemini's
      dialect rejects ``$defs``/``$ref``, so a schema carrying them is sent
      unconstrained with the schema appended to the instruction instead — the
      caller validates and repairs client-side either way, so a schema the API
      will not accept degrades to the same contract rather than failing the call.
    """

    name = Provider.GEMINI

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: Any = None
        self._types: Any = None
        self._api_error: type[BaseException] = _NeverRaised

    def _ensure(self) -> None:
        if not self._api_key:
            raise ProviderError(self.name, "no GEMINI api key configured")
        if self._client is None:
            try:
                from google import genai
                from google.genai import errors, types
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(self.name, "google-genai SDK not installed") from exc
            self._client = genai.Client(api_key=self._api_key)
            self._types = types
            self._api_error = errors.APIError

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        types = self._types
        system, contents = self._split(req)

        config: dict[str, Any] = {"max_output_tokens": req.max_tokens}
        if system:
            config["system_instruction"] = system
        if req.temperature is not None:
            config["temperature"] = req.temperature
        if req.json_schema is not None:
            config["response_mime_type"] = "application/json"
            if _schema_is_supported(req.json_schema):
                config["response_schema"] = req.json_schema
            else:
                # Nested models produce $defs, which Gemini rejects. Fall back to
                # JSON mode with the schema in the instruction.
                config["system_instruction"] = _with_schema(system, req.json_schema)

        try:
            resp = await self._client.aio.models.generate_content(
                model=model, contents=contents, config=types.GenerateContentConfig(**config)
            )
        except self._api_error as exc:
            rate_limited = getattr(exc, "code", None) == _TOO_MANY_REQUESTS
            raise ProviderError(self.name, str(exc), rate_limited=rate_limited) from exc
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc

        feedback = getattr(resp, "prompt_feedback", None)
        if getattr(feedback, "block_reason", None) is not None:
            return ProviderResult(model=model, refusal=True, finish_reason="refusal")

        candidates = getattr(resp, "candidates", None) or []
        finish = _name_of(getattr(candidates[0], "finish_reason", None)) if candidates else None
        if finish in _REFUSAL_FINISH_REASONS:
            return ProviderResult(model=model, refusal=True, finish_reason="refusal")

        usage = getattr(resp, "usage_metadata", None)
        prompt_tokens = _int(usage, "prompt_token_count")
        cache_read = _int(usage, "cached_content_token_count")
        # Thinking tokens bill at the output rate but sit outside the candidate
        # count; folding them in is what keeps the reported cost honest.
        completion = _int(usage, "candidates_token_count") + _int(usage, "thoughts_token_count")

        return ProviderResult(
            model=model,
            content=getattr(resp, "text", None) or "",
            usage=Usage(
                # Cached tokens are reported inside prompt_token_count; subtract
                # them out so they are not billed twice at the full input rate.
                prompt_tokens=max(0, prompt_tokens - cache_read),
                completion_tokens=completion,
                total_tokens=_int(usage, "total_token_count"),
                cache_read_tokens=cache_read,
            ),
            finish_reason=_LENGTH_FINISH_REASON if finish == _LENGTH_FINISH_REASON else finish,
        )

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
        try:
            resp = await self._client.aio.models.embed_content(model=model, contents=req.input)
        except self._api_error as exc:
            rate_limited = getattr(exc, "code", None) == _TOO_MANY_REQUESTS
            raise ProviderError(self.name, str(exc), rate_limited=rate_limited) from exc
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        vectors = [list(e.values or []) for e in (getattr(resp, "embeddings", None) or [])]
        return ProviderResult(model=model, embeddings=vectors)

    async def health(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        """No-op: the SDK client owns no session this adapter must close."""
        return None


class _NeverRaised(Exception):
    """Inert stand-in for the SDK error class until the client is built."""


def _schema_is_supported(schema: dict[str, Any]) -> bool:
    """Whether Gemini's schema dialect will accept this document as-is."""
    return "$defs" not in schema and "$ref" not in _dumps(schema)


def _with_schema(system: str, schema: dict[str, Any]) -> str:
    instruction = (
        "Respond with a single JSON object that conforms to this JSON Schema. "
        "Output only the JSON, no prose:\n" + _dumps(schema)
    )
    return f"{system}\n\n{instruction}" if system else instruction


def _name_of(value: object) -> str | None:
    """The enum member's name, tolerating the SDK returning a bare string."""
    name = getattr(value, "name", value)
    return str(name) if name is not None else None


def _int(obj: object, field: str) -> int:
    value = getattr(obj, field, None)
    return int(value) if isinstance(value, int) else 0


def _dumps(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, separators=(",", ":"))
