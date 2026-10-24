"""The internal contract the router and provider adapters speak.

Named after the provider APIs it maps onto: ``ChatRequest`` is a call to a
provider's chat API (POST /v1/generate), ``EmbeddingsRequest`` to its embeddings
API (POST /v1/embed). The public wire contracts are schemas/generate.py and
schemas/embed.py; api/v1/model_calls.py translates between the two layers, so
either can change without the other."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from air_model_gateway.constants import Provider


class Message(BaseModel):
    # extra="forbid": fields from other APIs' message shapes (`name`,
    # `tool_call_id`, `tool_calls`) are the caller's 422, not silently dropped.
    model_config = ConfigDict(extra="forbid")

    # A Literal, not a free string: an unknown role was silently dropped by the
    # Anthropic adapter (a 400 "at least one message is required" surfacing as
    # a 502) and passed through to Ollama, which answered as if it were absent.
    role: Literal["system", "user", "assistant"] = Field(
        description="`system`, `user` or `assistant`."
    )
    content: str


class Usage(BaseModel):
    """Token counts as the provider reports them. For embeddings, total_tokens
    equals prompt_tokens (there is no completion)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Cache-attributed input tokens, kept disjoint from prompt_tokens so a
    # consumer can price them at the (much lower) cache rates.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ChatRequest(BaseModel):
    """One chat call. Which model runs it is the router's chain, not part of
    the request: the same request may be tried on several models."""

    messages: list[Message]
    max_tokens: int | None = None
    temperature: float | None = None
    # Structured output: when set, providers are asked to emit JSON that
    # conforms to this JSON Schema and `content` is that JSON string. The caller
    # validates it against its own model — the gateway stays schema-agnostic.
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    # Request prompt caching of the stable prefix where the provider supports it.
    cache_prefix: bool = True


class ChatResponse(BaseModel):
    id: str
    model: str
    provider: Provider
    content: str
    usage: Usage
    cost_usd: float = 0.0
    cached: bool = False  # served from the gateway's own response cache
    refusal: bool = False  # the provider's safety layer declined
    finish_reason: str | None = None


class EmbeddingsRequest(BaseModel):
    input: list[str] = Field(min_length=1)
