"""The internal chat/embeddings contract that the router and provider adapters
speak — ported unchanged from air-infra's gateway. The public wire contract is
``schemas/inference.py``'s ``InferenceRequest``/``InferenceResponse``, which the
API layer translates to/from these before touching the router."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from air_model_gateway.constants import Provider


class Message(BaseModel):
    role: str
    content: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Cache-attributed input tokens, kept disjoint from prompt_tokens so a
    # consumer can price them at the (much lower) cache rates.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ChatRequest(BaseModel):
    model: str | None = None  # alias; None -> gateway default
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
    model: str | None = None
    input: list[str] = Field(min_length=1)


class EmbeddingsResponse(BaseModel):
    id: str
    model: str
    provider: Provider
    embeddings: list[list[float]]
    usage: Usage
    cost_usd: float = 0.0
    cached: bool = False
