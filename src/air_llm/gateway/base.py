from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from air_llm.constants import Provider
from air_llm.schemas.models import ChatRequest, EmbeddingsRequest, Usage


class ProviderError(Exception):
    """A provider could not fulfil the call (bad key, upstream down, missing SDK).

    The router treats this as retryable/failover-able; if every provider raises,
    the API layer maps it to 502 — or 429 when ``rate_limited`` is set, so a
    throttled upstream is distinguishable from an unavailable one.
    """

    def __init__(self, provider: Provider, detail: str, *, rate_limited: bool = False) -> None:
        self.provider = provider
        self.detail = detail
        self.rate_limited = rate_limited
        super().__init__(f"{provider.value}: {detail}")


class ProviderResult(BaseModel):
    model: str
    content: str = ""
    embeddings: list[list[float]] | None = None
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    refusal: bool = False


@runtime_checkable
class ModelProvider(Protocol):
    name: Provider

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult: ...

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...
