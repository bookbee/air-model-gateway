from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.providers._common import NeverRaised, int_attr, provider_error
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage

# stop_reason the safety layer sets when it declines; content carries nothing.
_REFUSAL_STOP_REASON = "refusal"


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
        self._rate_limit_error: type[BaseException] = NeverRaised

    def _ensure(self) -> None:
        if not self._api_key:
            raise ProviderError(self.name, "no ANTHROPIC api key configured", configured=False)
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(
                    self.name, "anthropic SDK not installed", configured=False
                ) from exc
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key, max_retries=0)
            self._rate_limit_error = anthropic.RateLimitError

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        self._ensure()
        try:
            msg = await self._client.messages.create(**self._request(req, model))
        except Exception as exc:  # SDK raises a family of errors; normalize them
            raise self._error(exc) from exc
        # A refusal still consumed tokens: it keeps its usage like any call.
        refused = msg.stop_reason == _REFUSAL_STOP_REASON
        content = (
            "" if refused else self._extract_content(msg, structured=req.json_schema is not None)
        )
        return _result(msg, model, content=content)

    async def chat_stream(
        self, req: ChatRequest, model: str
    ) -> AsyncGenerator[str | ProviderResult, None]:
        """Streams text deltas, or for structured output the forced tool's input
        as it is generated (`input_json_delta`: the JSON text itself), then the
        final message's usage and stop reason."""
        self._ensure()
        try:
            async with self._client.messages.stream(**self._request(req, model)) as stream:
                async for event in stream:
                    if getattr(event, "type", None) != "content_block_delta":
                        continue
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield delta.text
                    elif delta.type == "input_json_delta":
                        yield delta.partial_json
                msg = await stream.get_final_message()
        except Exception as exc:
            raise self._error(exc) from exc
        yield _result(msg, model, content="")

    def _request(self, req: ChatRequest, model: str) -> dict[str, Any]:
        """The Messages API arguments, shared by the plain and the streamed call."""
        turns: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content}
            for m in req.messages
            if m.role in ("user", "assistant")
        ]
        if req.cache_prefix and _is_conversation(turns):
            _mark_cache_breakpoint(turns)
        request: dict[str, Any] = {
            "model": model,
            "messages": turns,
            "max_tokens": req.max_tokens or 1024,
        }
        # Omitted rather than sent as None when absent: the SDK serializes None
        # as an explicit null, which the API rejects (for `system`, with a 400
        # "system: Input should be a valid array"). Absent means the API's
        # own default.
        system = self._system_blocks(req)
        if system is not None:
            request["system"] = system
        # req.temperature is deliberately not forwarded. Anthropic removed the
        # sampling parameters (temperature/top_p/top_k) in API version
        # 2026-10-21: the API 400s on them for every model, and the SDK this
        # adapter pins no longer accepts the keyword at all, so sending it
        # failed this rung on every call that set it. Anthropic samples at its
        # own default; other providers still honour the caller's value.
        if req.json_schema is not None:
            name = req.schema_name or "structured_output"
            request["tools"] = [
                {
                    "name": name,
                    "description": "Return the result in this structured form.",
                    "input_schema": req.json_schema,
                }
            ]
            request["tool_choice"] = {"type": "tool", "name": name}
        return request

    def _error(self, exc: Exception) -> ProviderError:
        return provider_error(self.name, exc, throttled=isinstance(exc, self._rate_limit_error))

    def _system_blocks(self, req: ChatRequest) -> list[dict[str, Any]] | None:
        text = "\n".join(m.content for m in req.messages if m.role == "system")
        if not text:
            return None
        block: dict[str, Any] = {"type": "text", "text": text}
        if req.cache_prefix:
            # No `ttl`: the 5-minute cache, whose writes bill at 1.25x input, the
            # cache_write_ratio in pricing.yaml. A `1h` ttl bills 2x.
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


def _is_conversation(turns: list[dict[str, Any]]) -> bool:
    """A conversation, as opposed to a one-shot call, is a request that already
    carries an assistant reply: its caller will send this history again next turn."""
    return any(t["role"] == "assistant" for t in turns)


def _mark_cache_breakpoint(turns: list[dict[str, Any]]) -> None:
    """Mark the end of the history as a prompt-cache breakpoint.

    The gateway is stateless: the caller resends the whole conversation every
    turn. With the last turn marked, this call writes the prefix to Anthropic's
    cache and the next turn, whose prefix is exactly this request, reads it back
    at the cache rate instead of paying full input for every earlier turn again.
    Only for conversations: a cache write bills above the plain input rate, so a
    one-shot call (which nobody resends) would pay the premium and never read it.
    With the system block, that is two of the four breakpoints Anthropic allows.
    """
    last = turns[-1]
    turns[-1] = {
        "role": last["role"],
        "content": [
            {"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}
        ],
    }


def _result(msg: Any, model: str, *, content: str) -> ProviderResult:
    """A Messages API response (or a stream's final message) as a ProviderResult."""
    usage = msg.usage
    return ProviderResult(
        model=model,
        content=content,
        refusal=msg.stop_reason == _REFUSAL_STOP_REASON,
        usage=Usage(
            prompt_tokens=int_attr(usage, "input_tokens"),
            completion_tokens=int_attr(usage, "output_tokens"),
            total_tokens=int_attr(usage, "input_tokens") + int_attr(usage, "output_tokens"),
            cache_read_tokens=int_attr(usage, "cache_read_input_tokens"),
            cache_write_tokens=int_attr(usage, "cache_creation_input_tokens"),
        ),
        finish_reason=msg.stop_reason,
    )
