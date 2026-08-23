"""The public wire contract for POST /v1/inference — the single entry point
every AIR service calls, over HTTP today and (per docs/00-plan.md) a future gRPC
transport carrying the same fields. `task` picks chat or embeddings; the API
layer validates the task-appropriate fields are present and translates this into
the internal ``ChatRequest``/``EmbeddingsRequest`` shapes the router speaks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from air_llm.constants import Action, Provider
from air_llm.schemas.models import Message, Usage


class InferenceRequest(BaseModel):
    task: Action = Action.CHAT
    model: str | None = None  # alias, "provider:model", or a bare model name

    # task=chat
    messages: list[Message] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    cache_prefix: bool = True

    # task=embeddings
    input: list[str] | None = None

    @model_validator(mode="after")
    def _require_task_fields(self) -> InferenceRequest:
        if self.task == Action.CHAT and not self.messages:
            raise ValueError("'messages' is required when task is 'chat'")
        if self.task == Action.EMBEDDINGS and not self.input:
            raise ValueError("'input' is required when task is 'embeddings'")
        return self


class InferenceResponse(BaseModel):
    id: str
    task: Action
    model: str
    provider: Provider
    content: str = ""
    embeddings: list[list[float]] | None = None
    usage: Usage
    cost_usd: float = 0.0
    cached: bool = False  # served from air-llm's own response cache (chat only)
    refusal: bool = False  # the provider's safety layer declined
    finish_reason: str | None = None
