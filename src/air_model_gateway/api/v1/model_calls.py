"""The path every model call takes, whichever route it came in on.

POST /v1/generate and POST /v1/embed are thin: they authenticate, then call
`admit`, then `generate` or `embed` here. Keeping the control plane, cache,
budget, routing, pricing, spend and audit in one place means the routes can't
drift apart in behaviour.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import structlog
from fastapi import Request
from pydantic import BaseModel
from starlette.datastructures import State

from air_model_gateway.api.deps import enforce_policy, enforce_rate
from air_model_gateway.api.errors import (
    AppError,
    BudgetExceeded,
    Forbidden,
    InvalidModelChoice,
    ProviderUnavailable,
    RateLimited,
)
from air_model_gateway.constants import Action, ModelClass, Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.cache import cache_key
from air_model_gateway.gateway.models import ModelSelectionError
from air_model_gateway.gateway.router import Chain, OpenedStream
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.observability.metrics import (
    BUDGET_REJECTED,
    MODEL_COST,
    MODEL_TOKENS,
    SERVICE_COST,
    STREAMS,
)
from air_model_gateway.policy.budget import seconds_until_next_utc_day
from air_model_gateway.schemas.common import Principal, Problem
from air_model_gateway.schemas.embed import EmbedInput, EmbedOutput
from air_model_gateway.schemas.gateway import EmbedModel
from air_model_gateway.schemas.generate import (
    FinishReason,
    GenerateInput,
    GenerateOutput,
    GenerateSummary,
    StreamDelta,
    StreamStart,
)
from air_model_gateway.schemas.models import ChatRequest, ChatResponse, EmbeddingsRequest

log = get_logger("gateway")

# The one policy resource for model calls; its actions are the operations
# (Action.GENERATE, Action.EMBED), named after the routes.
RESOURCE = "inference"

# Each provider's own finish_reason spelling, mapped onto the wire's four
# values (compared lower-cased). Consumers branch on finish_reason, so it must
# mean the same thing whichever provider answered: a cut-off answer is
# "length" from OpenAI and Ollama, "max_tokens" from Anthropic, "MAX_TOKENS"
# from Gemini. Anything unrecognised is "other"; the raw value is still sent
# as provider_finish_reason.
_LENGTH = frozenset({"length", "max_tokens"})
_STOP = frozenset({"stop", "end_turn", "stop_sequence", "tool_use", "tool_calls"})


async def admit(request: Request, principal: Principal, action: Action) -> None:
    """The control plane for one call: grant, then rate limit."""
    enforce_policy(request, principal, RESOURCE, action)
    await enforce_rate(request, principal, RESOURCE)


@dataclass(frozen=True)
class GenerateChoice:
    """What a /v1/generate call will run: its chain, and the class it came from
    (None when pinned)."""

    chain: Chain
    model_class: ModelClass | None


def choose_generate(state: State, principal: Principal, body: GenerateInput) -> GenerateChoice:
    """The caller's model choice, checked against the policy (403) and resolved
    by the catalog (422 if it means nothing here)."""
    policy, catalog = state.policy, state.catalog
    if body.pin is not None:
        if not (state.settings.is_local or policy.may_pin(principal)):
            raise Forbidden(f"'{principal.service}' may not pin a model")
        return GenerateChoice(_chain_or_422(lambda: catalog.pin_chain(body.pin)), None)
    if not policy.may_use_class(principal, body.model):
        raise Forbidden(f"'{principal.service}' may not use the '{body.model.value}' class")
    if body.provider is not None and not policy.may_prefer(principal, body.provider):
        raise Forbidden(f"'{principal.service}' may not prefer '{body.provider.value}'")
    chain = _chain_or_422(
        lambda: catalog.generate_chain(principal.service, body.model, body.provider)
    )
    return GenerateChoice(chain, body.model)


def choose_embed(state: State, principal: Principal, body: EmbedInput) -> tuple[Chain, EmbedModel]:
    if not state.policy.may_use_embed_model(principal, body.model):
        raise Forbidden(f"'{principal.service}' may not use '{body.model}'")
    try:
        spec = state.catalog.embed_model(body.model)
    except ModelSelectionError as exc:
        raise InvalidModelChoice(str(exc)) from exc
    return [(spec.provider, spec.model)], spec


def _chain_or_422(resolve: Callable[[], Chain]) -> Chain:
    try:
        return resolve()
    except ModelSelectionError as exc:
        raise InvalidModelChoice(str(exc)) from exc


def _chat_request(body: GenerateInput) -> ChatRequest:
    return ChatRequest(
        messages=body.messages,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        json_schema=body.json_schema,
        schema_name=body.schema_name,
        cache_prefix=body.cache_prefix,
    )


def _key(principal: Principal, choice: GenerateChoice, req: ChatRequest) -> str:
    """Keyed on the chain itself, not the class name: when the owner changes what
    a class means, the old answers simply stop matching."""
    return cache_key(principal.service, _chain_id(choice.chain), req)


def _chain_id(chain: Chain) -> str:
    return ",".join(f"{p.value}:{m}" for p, m in chain)


async def generate(
    state: State, principal: Principal, body: GenerateInput, choice: GenerateChoice
) -> GenerateOutput:
    chat_req = _chat_request(body)
    key = _key(principal, choice, chat_req)

    # `cache: false` skips both the lookup and the store: the caller wants the
    # provider's answer to this call, not a stored one.
    cached: ChatResponse | None = await state.cache.get(key) if body.cache else None
    if cached is not None:
        # cost_usd is what *this* call cost, and a hit costs nothing — echoing
        # the original call's cost would make a consumer tracking spend against
        # a budget count the same tokens twice. Hits are served even over
        # budget, for the same reason.
        out = _generate_output(
            cached.model_copy(update={"cached": True, "cost_usd": 0.0}), choice.model_class
        )
        _audit(
            principal,
            Action.GENERATE,
            _choice_label(choice),
            out.provider.value,
            out.model,
            out.usage.total_tokens,
            0.0,
            cached=True,
            finish_reason=out.finish_reason,
        )
        return out

    await _check_budget(state, principal)
    try:
        result, provider = await state.router.chat(chat_req, choice.chain)
    except ProviderError as exc:
        raise _map_error(exc) from exc

    cost = state.prices.price(provider, result.model, result.usage)
    resp = ChatResponse(
        id=uuid.uuid4().hex,
        model=result.model,
        provider=provider,
        content=result.content,
        usage=result.usage,
        cost_usd=cost,
        cached=False,
        refusal=result.refusal,
        finish_reason=result.finish_reason,
    )
    out = _generate_output(resp, choice.model_class)
    # Not cached: a refusal (a property of the safety layer's mood, not of the
    # input; re-asking may succeed) or an answer cut off at the token limit
    # (half a JSON object; caching it would hand every retry the same broken
    # answer instantly, so the caller could never recover by asking again).
    if body.cache and out.finish_reason not in ("refusal", "length"):
        await state.cache.set(key, resp)

    MODEL_TOKENS.labels(provider.value, result.model, Action.GENERATE.value).inc(
        result.usage.total_tokens
    )
    MODEL_COST.labels(provider.value, result.model).inc(cost)
    await _record_spend(state, principal, cost)
    _audit(
        principal,
        Action.GENERATE,
        _choice_label(choice),
        provider.value,
        result.model,
        result.usage.total_tokens,
        cost,
        cached=False,
        finish_reason=out.finish_reason,
    )
    return out


async def open_generate_stream(
    state: State,
    principal: Principal,
    body: GenerateInput,
    choice: GenerateChoice,
    request_id: str,
) -> AsyncIterator[str]:
    """Everything that can fail before the first token happens here, so it
    surfaces as an ordinary HTTP error: the cache lookup, the budget, and
    opening the stream on the first rung that answers (failover, retries). The
    returned iterator yields Server-Sent Events from then on."""
    chat_req = _chat_request(body)
    key = _key(principal, choice, chat_req)
    cached: ChatResponse | None = await state.cache.get(key) if body.cache else None
    if cached is not None:
        hit = _generate_output(
            cached.model_copy(update={"cached": True, "cost_usd": 0.0}), choice.model_class
        )
        return _replay(principal, choice, hit, request_id)

    await _check_budget(state, principal)
    try:
        opened, provider = await state.router.open_chat_stream(chat_req, choice.chain)
    except ProviderError as exc:
        raise _map_error(exc) from exc
    return _relay(state, principal, body, choice, key, opened, provider, request_id)


async def _replay(
    principal: Principal, choice: GenerateChoice, out: GenerateOutput, request_id: str
) -> AsyncIterator[str]:
    """A cache hit as a stream: start, the whole answer as one delta, done."""
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        yield _sse(
            "start", StreamStart(id=out.id, model=out.model, provider=out.provider, cached=True)
        )
        if out.content:
            yield _sse("delta", StreamDelta(content=out.content))
        _audit(
            principal,
            Action.GENERATE,
            _choice_label(choice),
            out.provider.value,
            out.model,
            out.usage.total_tokens,
            0.0,
            cached=True,
            finish_reason=out.finish_reason,
        )
        STREAMS.labels("completed").inc()
        yield _sse("done", GenerateSummary.model_validate(out.model_dump(exclude={"content"})))


async def _relay(
    state: State,
    principal: Principal,
    body: GenerateInput,
    choice: GenerateChoice,
    key: str,
    opened: OpenedStream,
    provider: Provider,
    request_id: str,
) -> AsyncIterator[str]:
    """Relay an opened provider stream as Server-Sent Events.

    The gateway's bookkeeping (pricing, spend, cache store, metrics, audit)
    happens when the provider's final item arrives, before `done` is sent, so it
    is recorded even if the caller leaves right after. A failure after the first
    token can no longer fail over: it becomes an `error` event, and counts toward
    the rung's breaker. If the caller disconnects, the provider stream is closed
    (the provider stops generating) and the call is audited as
    `client_disconnected`; its usage is unknown, so its spend isn't counted.
    """
    stream_id = uuid.uuid4().hex
    idle = state.settings.gateway.stream_idle_timeout_seconds
    parts: list[str] = []
    outcome = "client_disconnected"
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        try:
            yield _sse(
                "start",
                StreamStart(id=stream_id, model=opened.model, provider=provider, cached=False),
            )
            item = opened.first
            while not isinstance(item, ProviderResult):
                parts.append(item)
                yield _sse("delta", StreamDelta(content=item))
                item = await asyncio.wait_for(anext(opened.rest), idle)
            out = await _finish(
                state, principal, body, choice, key, provider, item, stream_id, "".join(parts)
            )
            outcome = "completed"
            yield _sse("done", GenerateSummary.model_validate(out.model_dump(exclude={"content"})))
        except (ProviderError, TimeoutError, StopAsyncIteration) as exc:
            outcome = "error"
            failure = _stream_failure(provider, exc, idle)
            state.router.record_stream_failure(provider, opened.model, failure)
            log.warning(
                "stream.failed", provider=provider.value, model=opened.model, detail=failure.detail
            )
            yield _sse("error", _problem(_map_error(failure), request_id))
        except Exception:
            outcome = "error"
            log.exception("stream.unexpected_error", provider=provider.value, model=opened.model)
            yield _sse("error", _problem(AppError("unexpected error"), request_id))
        finally:
            STREAMS.labels(outcome).inc()
            if outcome != "completed":
                await opened.rest.aclose()
                log.info(
                    "model.call",
                    service=principal.service,
                    operation=Action.GENERATE.value,
                    model_choice=_choice_label(choice),
                    provider=provider.value,
                    model=opened.model,
                    tokens=0,
                    cost_usd=0.0,
                    cached=False,
                    finish_reason=None,
                    stream_outcome=outcome,
                    streamed_chars=sum(len(p) for p in parts),
                )


async def _finish(
    state: State,
    principal: Principal,
    body: GenerateInput,
    choice: GenerateChoice,
    key: str,
    provider: Provider,
    final: ProviderResult,
    stream_id: str,
    content: str,
) -> GenerateOutput:
    """The bookkeeping of a completed stream, identical to a non-streamed call."""
    model = final.model
    cost = state.prices.price(provider, model, final.usage)
    resp = ChatResponse(
        id=stream_id,
        model=model,
        provider=provider,
        content="" if final.refusal else content,
        usage=final.usage,
        cost_usd=cost,
        cached=False,
        refusal=final.refusal,
        finish_reason=final.finish_reason,
    )
    out = _generate_output(resp, choice.model_class)
    if body.cache and out.finish_reason not in ("refusal", "length"):
        await state.cache.set(key, resp)
    MODEL_TOKENS.labels(provider.value, model, Action.GENERATE.value).inc(final.usage.total_tokens)
    MODEL_COST.labels(provider.value, model).inc(cost)
    await _record_spend(state, principal, cost)
    _audit(
        principal,
        Action.GENERATE,
        _choice_label(choice),
        provider.value,
        model,
        final.usage.total_tokens,
        cost,
        cached=False,
        finish_reason=out.finish_reason,
    )
    return out


def _stream_failure(provider: Provider, exc: BaseException, idle: float) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, TimeoutError):
        return ProviderError(provider, f"stream went silent for {idle:g}s", retryable=True)
    return ProviderError(provider, "stream ended before completion", retryable=True)


def _sse(event: str, data: BaseModel) -> str:
    """One Server-Sent Event: `event:` names it, `data:` is one line of JSON."""
    return f"event: {event}\ndata: {data.model_dump_json()}\n\n"


def _problem(error: AppError, request_id: str) -> Problem:
    return Problem(
        title=error.title,
        status=error.status,
        code=error.code,
        detail=error.detail,
        request_id=request_id,
    )


async def embed(state: State, principal: Principal, body: EmbedInput) -> EmbedOutput:
    chain, spec = choose_embed(state, principal, body)
    await _check_budget(state, principal)
    try:
        result, provider = await state.router.embeddings(EmbeddingsRequest(input=body.input), chain)
    except ProviderError as exc:
        raise _map_error(exc) from exc
    vectors = result.embeddings or []
    # The published name promises a dimension; a model answering with another
    # one (a misconfigured name) must never reach an index.
    wrong = {len(v) for v in vectors} - {spec.dimensions}
    if wrong:
        raise ProviderUnavailable(
            f"'{body.model}' returned {sorted(wrong)}-dimension vectors, not {spec.dimensions}"
        )

    cost = state.prices.price(provider, result.model, result.usage)
    MODEL_TOKENS.labels(provider.value, result.model, Action.EMBED.value).inc(
        result.usage.total_tokens
    )
    MODEL_COST.labels(provider.value, result.model).inc(cost)
    await _record_spend(state, principal, cost)
    _audit(
        principal,
        Action.EMBED,
        body.model,
        provider.value,
        result.model,
        result.usage.total_tokens,
        cost,
        cached=False,
        finish_reason=None,
    )
    return EmbedOutput(
        id=uuid.uuid4().hex,
        embedding_model=body.model,
        dimensions=spec.dimensions,
        model=result.model,
        provider=provider,
        embeddings=vectors,
        usage=result.usage,
        cost_usd=cost,
    )


def normalize_finish_reason(raw: str | None, *, refusal: bool = False) -> FinishReason | None:
    if refusal:
        return "refusal"
    if raw is None:
        return None
    value = raw.lower()
    if value in _LENGTH:
        return "length"
    if value in _STOP:
        return "stop"
    if value == "refusal":
        return "refusal"
    return "other"


def _choice_label(choice: GenerateChoice) -> str:
    """The caller's choice as the audit line records it: the class, or `pin`."""
    return choice.model_class.value if choice.model_class is not None else "pin"


def _generate_output(resp: ChatResponse, model_class: ModelClass | None) -> GenerateOutput:
    return GenerateOutput(
        id=resp.id,
        model_class=model_class,
        model=resp.model,
        provider=resp.provider,
        content=resp.content,
        usage=resp.usage,
        cost_usd=resp.cost_usd,
        cached=resp.cached,
        refusal=resp.refusal,
        finish_reason=normalize_finish_reason(resp.finish_reason, refusal=resp.refusal),
        provider_finish_reason=resp.finish_reason,
    )


def _audit(
    principal: Principal,
    operation: Action,
    model_choice: str,
    provider: str,
    model: str,
    tokens: int,
    cost: float,
    *,
    cached: bool,
    finish_reason: FinishReason | None,
) -> None:
    """One audit line per served call — cache hits included, so the log sink
    can account for every request a service made, not just the ones that
    reached a provider."""
    log.info(
        "model.call",
        service=principal.service,
        operation=operation.value,
        model_choice=model_choice,
        provider=provider,
        model=model,
        tokens=tokens,
        cost_usd=cost,
        cached=cached,
        finish_reason=finish_reason,
    )


async def _check_budget(state: State, principal: Principal) -> None:
    budget = state.policy.daily_budget_usd(principal)
    if budget is None:
        return
    spent = await state.ledger.spent_today(principal.service)
    if spent >= budget:
        BUDGET_REJECTED.labels(principal.service).inc()
        raise BudgetExceeded(
            f"'{principal.service}' has spent ${spent:.2f} of its ${budget:.2f} daily budget",
            headers={"Retry-After": str(seconds_until_next_utc_day())},
        )


async def _record_spend(state: State, principal: Principal, cost: float) -> None:
    if cost > 0:
        SERVICE_COST.labels(principal.service).inc(cost)
        await state.ledger.add(principal.service, cost)


def _map_error(exc: ProviderError) -> ProviderUnavailable | RateLimited:
    if exc.rate_limited:
        return RateLimited(exc.detail)
    return ProviderUnavailable(exc.detail)
