from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import State

from air_llm.api.deps import enforce_policy, enforce_rate, require_principal
from air_llm.api.errors import ProviderUnavailable, RateLimited
from air_llm.constants import Action
from air_llm.gateway.base import ProviderError
from air_llm.observability.logging import get_logger
from air_llm.observability.metrics import MODEL_COST, MODEL_TOKENS
from air_llm.schemas.common import Principal
from air_llm.schemas.inference import InferenceRequest, InferenceResponse
from air_llm.schemas.models import ChatRequest, ChatResponse, EmbeddingsRequest

router = APIRouter(tags=["inference"])
log = get_logger("gateway")

# The one resource air-llm has. "chat"/"embeddings" (Action.CHAT/EMBEDDINGS,
# also InferenceRequest.task) are both the policy action and the wire
# discriminator — see constants.py.
RESOURCE = "inference"


@router.post("/inference", response_model=InferenceResponse)
async def inference(
    request: Request,
    body: InferenceRequest,
    principal: Principal = Depends(require_principal),
) -> InferenceResponse:
    """The single entry point every AIR service calls for model access — chat and
    embeddings both route through here, distinguished by ``task``. HTTP today;
    a future gRPC transport carries the same request/response shapes
    (docs/00-plan.md)."""
    enforce_policy(request, principal, RESOURCE, body.task)
    enforce_rate(request, principal, RESOURCE)

    state = request.app.state
    # Per-provider control: rate-limit against the provider this model routes to
    # first, so one service cannot exhaust a shared upstream via a busy chain.
    primary = state.router.primary(body.model)
    enforce_rate(request, principal, f"provider:{primary.value}")

    if body.task == Action.EMBEDDINGS:
        return await _embeddings(state, body)
    return await _chat(state, body, principal)


async def _chat(state: State, body: InferenceRequest, principal: Principal) -> InferenceResponse:
    chat_req = ChatRequest(
        model=body.model,
        messages=body.messages or [],
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        json_schema=body.json_schema,
        schema_name=body.schema_name,
        cache_prefix=body.cache_prefix,
    )
    default_model = state.settings.gateway.default_model
    cache_key = state.cache.key(body.model or default_model, chat_req)

    cached: ChatResponse | None = state.cache.get(cache_key)
    if cached is not None:
        return _to_inference_response(Action.CHAT, cached.model_copy(update={"cached": True}))

    try:
        result, provider = await state.router.chat(chat_req)
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
    # A refusal is not cacheable — it is a property of the safety layer's mood,
    # not of the input, and re-asking may succeed.
    if not result.refusal:
        state.cache.set(cache_key, resp)

    MODEL_TOKENS.labels(provider.value, result.model, "chat").inc(result.usage.total_tokens)
    MODEL_COST.labels(provider.value, result.model).inc(cost)
    log.info(
        "model.call",
        service=principal.service,
        task="chat",
        provider=provider.value,
        model=result.model,
        tokens=result.usage.total_tokens,
        cost_usd=cost,
        cached=False,
    )
    return _to_inference_response(Action.CHAT, resp)


async def _embeddings(state: State, body: InferenceRequest) -> InferenceResponse:
    embed_req = EmbeddingsRequest(model=body.model, input=body.input or [])
    try:
        result, provider = await state.router.embeddings(embed_req)
    except ProviderError as exc:
        raise _map_error(exc) from exc

    cost = state.prices.price(provider, result.model, result.usage)
    MODEL_TOKENS.labels(provider.value, result.model, "embeddings").inc(
        result.usage.total_tokens or result.usage.prompt_tokens
    )
    MODEL_COST.labels(provider.value, result.model).inc(cost)
    return InferenceResponse(
        id=uuid.uuid4().hex,
        task=Action.EMBEDDINGS,
        model=result.model,
        provider=provider,
        embeddings=result.embeddings or [],
        usage=result.usage,
        cost_usd=cost,
    )


def _to_inference_response(task: Action, resp: ChatResponse) -> InferenceResponse:
    return InferenceResponse(
        id=resp.id,
        task=task,
        model=resp.model,
        provider=resp.provider,
        content=resp.content,
        usage=resp.usage,
        cost_usd=resp.cost_usd,
        cached=resp.cached,
        refusal=resp.refusal,
        finish_reason=resp.finish_reason,
    )


def _map_error(exc: ProviderError) -> ProviderUnavailable | RateLimited:
    if exc.rate_limited:
        return RateLimited(exc.detail)
    return ProviderUnavailable(exc.detail)
