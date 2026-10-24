from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from air_model_gateway.constants import Provider
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage


class ProviderError(Exception):
    """A provider could not fulfil the call (bad key, upstream down, missing SDK).

    Always fails over to the next rung of the chain. If every rung fails, the
    API layer maps it to 502, or to 429 when every rung was ``rate_limited``,
    so a throttled upstream is distinguishable from an unavailable one.

    ``retryable`` says whether asking again *later* could succeed: a timeout,
    a 429, a 5xx or a dropped connection, but not a missing key or a 400. The
    router retries a failed chain only when some rung failed retryably, and
    only retryable failures count toward a rung's circuit breaker.
    """

    def __init__(
        self,
        provider: Provider,
        detail: str,
        *,
        rate_limited: bool = False,
        retryable: bool = False,
    ) -> None:
        self.provider = provider
        self.detail = detail
        self.rate_limited = rate_limited
        self.retryable = retryable or rate_limited
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

    def chat_stream(
        self, req: ChatRequest, model: str
    ) -> AsyncGenerator[str | ProviderResult, None]:
        """The same call, streamed: yields each text delta as it arrives, then
        exactly one final ProviderResult (empty `content`; model, usage,
        finish_reason, refusal). Failures raise ProviderError like `chat`, at
        open or mid-stream; the router fails over only before the first item."""
        ...

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...
