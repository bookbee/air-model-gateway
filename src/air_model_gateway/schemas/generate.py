"""The public wire contract of POST /v1/generate: text generation (chat-style
messages in, one answer out), with optional structured output.

The API layer translates it into the internal ``ChatRequest`` the router and
adapters speak (schemas/models.py, named after the providers' chat APIs), so
this contract can evolve without touching adapters.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from air_model_gateway.constants import ModelClass, Provider
from air_model_gateway.schemas.models import Message, Usage

#: Why the model stopped, the same vocabulary whichever provider answered.
FinishReason = Literal["stop", "length", "refusal", "other"]

#: A `pin`: `provider:model`, e.g. `anthropic:claude-haiku-4-5`. Built from
#: `Provider`, so adding a provider can't leave pins to it rejected.
PIN_PATTERN = rf"^({'|'.join(re.escape(p.value) for p in Provider)}):.+$"

#: Size limits, checked up front so an oversized request is an immediate 422
#: rather than a provider tied up for a long time before it fails. The
#: character total is about 200k tokens, the smallest cloud context window in
#: the chains (Claude); a local model reads far less and truncates.
MAX_MESSAGES = 500
MAX_TOTAL_CHARS = 800_000

# Shown in docs/openapi.json and the /docs page.
_EXAMPLES: list[JsonValue] = [
    {"messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 50},
    {"model": "local", "messages": [{"role": "user", "content": "Summarize: ..."}]},
    {
        "model": "cloud",
        "provider": "anthropic",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": "Classify sentiment: I love this product!"}],
        "json_schema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
        "schema_name": "sentiment",
    },
]


class GenerateInput(BaseModel):
    # extra="forbid": an option the gateway doesn't implement (`stream`, `tools`,
    # `top_p`, ...) is the caller's 422, not silently ignored. Ignoring it would
    # answer a request for a stream with one JSON body, or a request for tools
    # with a model that can't call any, and the caller would only find out by
    # breaking.
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": _EXAMPLES})

    model: ModelClass = Field(
        default=ModelClass.AUTO,
        description=(
            "Where the model runs, not which model: `local` (our own servers; the prompt "
            "never leaves the network), `cloud` (cloud providers), or `auto` (the gateway's "
            "choice, which may be either). The gateway owner decides which models serve "
            "each class; `/v1/capabilities` shows them for your service."
        ),
    )
    provider: Provider | None = Field(
        default=None,
        description=(
            "Optional preference within the class: that provider's models are tried first, "
            "the rest stay as fallback. Must be one the class offers (422 otherwise), and "
            "one your service may prefer (403 otherwise)."
        ),
    )
    pin: str | None = Field(
        default=None,
        pattern=PIN_PATTERN,
        description=(
            "Operator tools only: an exact `provider:model` (e.g. "
            "`anthropic:claude-haiku-4-5`), bypassing the classes, with no fallback. Needs "
            "a `pin` grant outside local. When set, `model` and `provider` must be unset."
        ),
    )
    messages: list[Message] = Field(
        min_length=1,
        max_length=MAX_MESSAGES,
        description=(
            "The whole conversation, oldest first. The gateway keeps no history: the "
            "caller sends every turn it wants the model to see, on every call. At most "
            f"{MAX_MESSAGES} messages and {MAX_TOTAL_CHARS:,} characters of content in all."
        ),
    )
    max_tokens: int | None = Field(
        default=None,
        # ge=1: Ollama reads 0 and negatives as "no cap" (num_predict -1 is
        # unlimited) and the Anthropic adapter's `or 1024` would read 0 as its
        # default, so a bad value would silently remove the cap instead of failing.
        ge=1,
        description=(
            "Output token cap, at least 1. Thinking models (Gemini) spend part of it before "
            "the visible answer; leave room (300+ for structured output)."
        ),
    )
    # Bounded so an out-of-range value is the caller's 422, not a provider 400
    # dressed up as a retryable 502 after walking the whole chain.
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description=(
            "0-2. Provider default when omitted. Ignored by Anthropic, whose API version "
            "`2026-10-21` rejects sampling parameters."
        ),
    )
    json_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "A JSON Schema the answer must follow; `content` is then a JSON string. Validate "
            "it on your side: the gateway forwards the schema, it doesn't check the answer."
        ),
    )
    schema_name: str | None = Field(
        default=None, description="A name for `json_schema` (some providers require one)."
    )
    cache_prefix: bool = Field(
        default=True,
        description=(
            "Ask the provider to cache the stable prompt prefix, if supported: the system "
            "prompt and, in a conversation (once there is an assistant turn), the history, "
            "so each new turn re-reads earlier ones at the cache rate."
        ),
    )
    stream: bool = Field(
        default=False,
        description=(
            "Stream the answer as Server-Sent Events (`text/event-stream`): `start`, then "
            "`delta` events with text as it is generated, then `done` (GenerateSummary) or "
            "`error` (Problem). Errors before the first token are ordinary HTTP errors."
        ),
    )
    cache: bool = Field(
        default=True,
        description=(
            "Use the gateway's response cache: an identical request from this service "
            "within the cache TTL gets the stored answer at no cost. `false` always asks "
            "the provider (e.g. for a fresh sample of a creative answer)."
        ),
    )

    @model_validator(mode="after")
    def _pin_stands_alone(self) -> GenerateInput:
        if self.pin is not None and ("model" in self.model_fields_set or self.provider):
            raise ValueError("pin names the exact model: send it without model or provider")
        return self

    @model_validator(mode="after")
    def _within_size_limit(self) -> GenerateInput:
        total = sum(len(m.content) for m in self.messages)
        if total > MAX_TOTAL_CHARS:
            raise ValueError(
                f"messages hold {total:,} characters; the limit is {MAX_TOTAL_CHARS:,}"
            )
        return self


class GenerateSummary(BaseModel):
    """Everything about an answer except its text: the body of /v1/generate's
    response (GenerateOutput adds `content`), and of a stream's `done` event."""

    id: str = Field(description="Gateway-assigned id; a cache hit returns the original call's id.")
    model_class: ModelClass | None = Field(
        description="The class that served the call; null when it was pinned."
    )
    model: str = Field(
        description="The concrete model that answered, after resolving the class and failover."
    )
    provider: Provider = Field(description="The provider that answered.")
    usage: Usage
    cost_usd: float = Field(
        default=0.0,
        description="Priced from the gateway's table; 0 for local models and cache hits.",
    )
    cached: bool = Field(default=False, description="Served from the gateway's response cache.")
    refusal: bool = Field(
        default=False, description="The provider's safety layer declined; never cached."
    )
    finish_reason: FinishReason | None = Field(
        default=None,
        description=(
            "Normalized across providers: `stop` (finished), `length` (cut off at "
            "`max_tokens`: treat structured output as invalid; never cached), `refusal`, "
            "or `other`."
        ),
    )
    provider_finish_reason: str | None = Field(
        default=None,
        description="The provider's own finish reason, unmapped (for diagnostics only).",
    )


class GenerateOutput(GenerateSummary):
    content: str = Field(
        default="", description="The answer (a JSON string when `json_schema` was sent)."
    )


class StreamStart(BaseModel):
    """The first event of a stream: which model answers (failover is over)."""

    id: str
    model: str = Field(description="The concrete model streaming the answer.")
    provider: Provider
    cached: bool = Field(description="A cache hit, replayed as one `delta`.")


class StreamDelta(BaseModel):
    """A slice of the answer; concatenate every `delta`'s `content` in order."""

    content: str
