from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from air_model_gateway.api.deps import AuthenticatedRoute, require_principal
from air_model_gateway.api.openapi import STREAM_RESPONSE, problems
from air_model_gateway.api.v1 import model_calls
from air_model_gateway.constants import Action
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.generate import GenerateInput, GenerateOutput

router = APIRouter(tags=["generate"], route_class=AuthenticatedRoute)

# Proxies must pass each event on as it is written: `no-cache` for any HTTP cache,
# X-Accel-Buffering for nginx (and the many proxies that honour it).
_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post(
    "/generate",
    response_model=GenerateOutput,
    responses={200: STREAM_RESPONSE, **problems(401, 403, 422, 429, 502)},
    summary="Generate text (optionally structured JSON, optionally streamed) from a model class",
)
async def generate(
    request: Request,
    body: GenerateInput,
    principal: Principal = Depends(require_principal),
) -> GenerateOutput | StreamingResponse:
    """Messages in, one answer out: chat, classification, summaries, extraction.
    With `stream: true`, the answer arrives as Server-Sent Events."""
    await model_calls.admit(request, principal, Action.GENERATE)
    state = request.app.state
    choice = model_calls.choose_generate(state, principal, body)
    if body.stream:
        events = await model_calls.open_generate_stream(
            state, principal, body, choice, request.state.request_id
        )
        return StreamingResponse(events, media_type="text/event-stream", headers=_STREAM_HEADERS)
    return await model_calls.generate(state, principal, body, choice)
