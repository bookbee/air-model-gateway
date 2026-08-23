from __future__ import annotations

import json
from typing import Any

from air_llm.constants import Provider
from air_llm.gateway.base import ProviderError, ProviderResult
from air_llm.schemas.models import ChatRequest, EmbeddingsRequest, Usage

# stop_reason the safety layer sets when it declines; content carries nothing.
_REFUSAL_STOP_REASON = "refusal"


class _NeverRaised(Exception):
    """Placeholder for the SDK's rate-limit class before the client is built.

    ``except _NeverRaised`` catches nothing, so the rate-limit clause is inert
    until :meth:`_ensure` swaps in ``anthropic.RateLimitError``.
    """


class AnthropicProvider:
    """Cloud provider. The SDK is imported lazily so a missing key or an
    uninstalled ``anthropic`` package is a clean ProviderError, not an import crash.

    Structured output uses a forced tool: the caller's JSON Schema becomes a
    tool's ``input_schema`` and ``tool_choice`` pins it, so the model must return
    an object of that shape rather than prose we would have to scrape.
    """

    name = Provider.ANTHROPIC

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: Any = None  # built on first use
        self._rate_limit_error: type[BaseException] = _NeverRaised

    def _ensure(self) -> None:
        if not self._api_key:
            raise ProviderError(self.name, "no ANTHROPIC api key configured")
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(self.name, "anthropic SDK not installed") from exc
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
            self._rate_limit_error = anthropic.RateLimitError

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        system = self._system_blocks(req)
        turns = [
            {"role": m.role, "content": m.content}
            for m in req.messages
            if m.role in ("user", "assistant")
        ]
        optional: dict[str, Any] = {}
        if req.json_schema is not None:
            name = req.schema_name or "structured_output"
            optional["tools"] = [
                {
                    "name": name,
                    "description": "Return the result in this structured form.",
                    "input_schema": req.json_schema,
                }
            ]
            optional["tool_choice"] = {"type": "tool", "name": name}

        try:
            msg = await self._client.messages.create(
                model=model,
                system=system,
                messages=turns,
                max_tokens=req.max_tokens or 1024,
                **optional,
            )
        except self._rate_limit_error as exc:
            raise ProviderError(self.name, str(exc), rate_limited=True) from exc
        except Exception as exc:  # SDK raises a family of errors; normalize them
            raise ProviderError(self.name, str(exc)) from exc

        if msg.stop_reason == _REFUSAL_STOP_REASON:
            return ProviderResult(model=model, refusal=True, finish_reason=msg.stop_reason)

        content = self._extract_content(msg, structured=req.json_schema is not None)
        usage = msg.usage
        return ProviderResult(
            model=model,
            content=content,
            usage=Usage(
                prompt_tokens=_int(usage, "input_tokens"),
                completion_tokens=_int(usage, "output_tokens"),
                total_tokens=_int(usage, "input_tokens") + _int(usage, "output_tokens"),
                cache_read_tokens=_int(usage, "cache_read_input_tokens"),
                cache_write_tokens=_int(usage, "cache_creation_input_tokens"),
            ),
            finish_reason=msg.stop_reason,
        )

    def _system_blocks(self, req: ChatRequest) -> list[dict[str, Any]] | None:
        text = "\n".join(m.content for m in req.messages if m.role == "system")
        if not text:
            return None
        block: dict[str, Any] = {"type": "text", "text": text}
        if req.cache_prefix:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    @staticmethod
    def _extract_content(msg: Any, *, structured: bool) -> str:
        if structured:
            for block in msg.content:
                if getattr(block, "type", None) == "tool_use":
                    return json.dumps(block.input)
        return "".join(getattr(b, "text", "") for b in msg.content)

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        raise ProviderError(self.name, "anthropic has no embeddings endpoint")

    async def health(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()


def _int(usage: object, field: str) -> int:
    value = getattr(usage, field, None)
    return int(value) if isinstance(value, int) else 0
