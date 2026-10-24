"""Helpers shared by the provider adapters. Kept private to this package: these
are adapter plumbing, not part of the ModelProvider contract."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx

from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.schemas.models import Usage

# Statuses where the same request may succeed if asked again later: request
# timeout, conflict, too early, throttled, and any server-side failure.
_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429})


class NeverRaised(Exception):
    """Inert stand-in for an SDK's error class until that SDK is imported.

    Each cloud adapter imports its SDK lazily (``_ensure``) so a missing key or
    package is a clean ProviderError rather than an import crash — which means
    the SDK's own rate-limit/API error class doesn't exist yet when the adapter
    is constructed. ``except NeverRaised`` catches nothing, so that clause is
    inert until ``_ensure`` swaps the real class in.
    """


def is_transient(exc: BaseException) -> bool:
    """Whether a provider failure could clear on its own (see ProviderError.retryable).

    Reads the HTTP status off whichever shape the SDK raised: ``status_code``
    (anthropic, openai), ``code`` (google-genai), or an httpx response. With no
    status at all, only a transport-level failure (connection refused, reset,
    timed out) counts: an unknown exception is more likely a bug than a blip.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if not isinstance(status, int) and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if isinstance(status, int):
        return status in _TRANSIENT_STATUSES or status >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    name = type(exc).__name__
    return "Connection" in name or "Timeout" in name


def int_attr(obj: object, field: str) -> int:
    """An integer usage field off an SDK response object, 0 when absent or not
    an int — SDKs leave optional counters as None rather than omitting them."""
    value = getattr(obj, field, None)
    return int(value) if isinstance(value, int) else 0


def compact_json(schema: dict[str, Any]) -> str:
    return json.dumps(schema, separators=(",", ":"))


def schema_instruction(schema: dict[str, Any]) -> str:
    """The prompt-side half of structured output, for adapters whose API can't
    (or, for self-hosted backends, can't reliably) enforce the schema itself.
    The caller validates client-side either way — the gateway stays
    schema-agnostic."""
    return (
        "Respond with a single JSON object that conforms to this JSON Schema. "
        "Output only the JSON, no prose:\n" + compact_json(schema)
    )


def with_schema_instruction(
    messages: list[dict[str, str]], schema: dict[str, Any] | None
) -> list[dict[str, str]]:
    """OpenAI-shaped messages with the schema instruction appended to the first
    system message, or prepended as one if there is none. Also satisfies
    OpenAI JSON mode's requirement that the word "json" appear in the prompt."""
    if schema is None:
        return messages
    instruction = schema_instruction(schema)
    for msg in messages:
        if msg["role"] == "system":
            msg["content"] = msg["content"] + "\n\n" + instruction
            return messages
    return [{"role": "system", "content": instruction}, *messages]


# finish_reason an OpenAI-wire server sets when its content filter removed the answer.
CONTENT_FILTER_FINISH_REASON = "content_filter"


def openai_usage(usage: object) -> Usage:
    """Usage from an OpenAI-wire `usage` object. Cached tokens are reported
    inside prompt_tokens; they are subtracted out so they aren't billed twice
    at the full input rate."""
    prompt_tokens = int_attr(usage, "prompt_tokens")
    cache_read = int_attr(getattr(usage, "prompt_tokens_details", None), "cached_tokens")
    return Usage(
        prompt_tokens=max(0, prompt_tokens - cache_read),
        completion_tokens=int_attr(usage, "completion_tokens"),
        total_tokens=int_attr(usage, "total_tokens"),
        cache_read_tokens=cache_read,
    )


async def openai_stream(
    stream: Any, model: str, wrap: Callable[[Exception], ProviderError]
) -> AsyncGenerator[str | ProviderResult, None]:
    """Text deltas from an OpenAI-wire chat-completions stream, then the final
    result. Usage arrives on a last chunk with no choices, which the server sends
    only when the request asked for `stream_options={"include_usage": True}`.
    SDK errors mid-stream go through `wrap`, so they surface as ProviderError."""
    finish: str | None = None
    refused = False
    usage: object = None
    try:
        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice.delta, "refusal", None):
                refused = True
            text = getattr(choice.delta, "content", None)
            if text:
                yield text
            if choice.finish_reason:
                finish = choice.finish_reason
    except Exception as exc:
        raise wrap(exc) from exc
    # A refusal still consumed tokens: it keeps its usage like any call.
    refused = refused or finish == CONTENT_FILTER_FINISH_REASON
    yield ProviderResult(
        model=model,
        usage=openai_usage(usage),
        finish_reason="refusal" if refused else finish,
        refusal=refused,
    )
