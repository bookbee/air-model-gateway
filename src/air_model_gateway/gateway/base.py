from __future__ import annotations

from collections.abc import AsyncGenerator
from http import HTTPStatus
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from air_model_gateway.constants import Provider
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage

#: Provider answers meaning "this request is invalid": too long, a schema or
#: parameter the model can't take. Asking again unchanged, here or on another
#: rung, gets the same answer. Not 404 (an unknown model: the caller's mistake
#: only when it pinned it), and never an auth failure, even one a provider
#: spells as a 400 (Google's API_KEY_INVALID, Anthropic's "credit balance is
#: too low"): see AUTH_STATUSES.
REJECTED_STATUSES = frozenset({400, 413, 422})

#: Provider answers meaning the gateway's own account is unusable: the key is
#: invalid or revoked (401), out of credit (402), or not permitted (403). The
#: gateway's fault, never the caller's: a 502, and loud (MG-009). Providers
#: that report these with other statuses are recognised by their message
#: (`_common.provider_error`).
AUTH_STATUSES = frozenset({401, 402, 403})

#: Why a rung failed, the `reason` label of `air_model_gateway_rung_failures_total`.
FailureReason = Literal[
    "auth",
    "not_configured",
    "rate_limited",
    "rejected",
    "not_found",
    "timeout",
    "unavailable",
    "adapter",
    "other",
]


class ProviderError(Exception):
    """A provider could not fulfil the call (bad key, upstream down, missing SDK).

    Always fails over to the next rung of the chain. If every rung fails, the
    API layer maps it to 429 when every rung was ``rate_limited``, to 422 when
    every rung ``rejected`` the request as invalid, and to 502 otherwise, so a
    caller can tell "back off", "fix the request" and "the providers are down"
    apart.

    ``retryable`` says whether asking again *later* could succeed: a timeout,
    a 429, a 5xx or a dropped connection, but not a missing key or a 400. The
    router retries a failed chain only when some rung failed retryably, and
    only retryable failures count toward a rung's circuit breaker.

    ``status`` is the provider's HTTP status, when it answered with one.
    ``auth`` means the provider refused the gateway's own credentials or
    account; ``configured`` is false when the gateway has no key or SDK for
    the provider at all. Neither is ever ``rejected``.
    """

    def __init__(
        self,
        provider: Provider,
        detail: str,
        *,
        rate_limited: bool = False,
        retryable: bool = False,
        status: int | None = None,
        rejected: bool | None = None,
        auth: bool | None = None,
        configured: bool = True,
    ) -> None:
        self.provider = provider
        self.detail = detail
        self.auth = status in AUTH_STATUSES if auth is None else auth
        self.configured = configured
        self.rate_limited = rate_limited and not self.auth
        self.retryable = (retryable or self.rate_limited) and not self.auth
        self.status = status
        default_rejected = status in REJECTED_STATUSES and not self.auth and configured
        self.rejected = default_rejected if rejected is None else rejected
        super().__init__(f"{provider.value}: {detail}")

    @property
    def reason(self) -> FailureReason:
        if not self.configured:
            return "not_configured"
        if self.auth:
            return "auth"
        if self.rate_limited:
            return "rate_limited"
        if self.rejected:
            return "rejected"
        if self.status == HTTPStatus.NOT_FOUND:
            return "not_found"
        if self.retryable:
            return "unavailable"
        return "other"


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
