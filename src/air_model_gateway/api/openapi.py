"""What docs/openapi.json says beyond what FastAPI infers from the routes.

The published document is the contract consumers generate clients from, so it
must describe the gateway as it behaves, not as FastAPI would by default:

- every error is RFC 9457 `application/problem+json` (api/errors.py), including
  422, which FastAPI would otherwise declare as its own `HTTPValidationError`;
- the service token is an API-key security scheme, not an anonymous header;
- `X-Request-ID` is accepted on every call and always echoed back.

Routes declare which errors they can return with `problems(...)`; `install`
rewrites those responses' media type and adds the request-id header.
`tests/test_openapi.py` keeps the committed docs/openapi.json in step.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from air_model_gateway import __version__
from air_model_gateway.schemas.common import Problem
from air_model_gateway.schemas.generate import GenerateSummary, StreamDelta, StreamStart

_PROBLEM_REF = "#/components/schemas/Problem"
_REQUEST_ID = "X-Request-ID"

_PROBLEM_DESCRIPTIONS: dict[int, str] = {
    401: "Missing or unknown `X-API-Key` (`code: unauthorized`).",
    403: (
        "`code: policy_denied`: the calling service has no grant for this operation, or "
        "chose a model class, embedding model or preferred provider outside its grant, or "
        "sent `pin` without a pin grant (needed outside local). Don't retry."
    ),
    422: (
        "`code: validation_error`: the body failed validation (including the size limits); "
        "`detail` lists each `field: message`. `code: invalid_model_choice`: a choice that "
        "means nothing here (an unpublished embedding model, a provider the class doesn't "
        "offer, a malformed pin, a pin to a model with no price or one its provider doesn't "
        "have). `code: provider_rejected`: every provider refused the request as invalid "
        "(too long for the model, a parameter it won't take); `detail` has their reasons. "
        "Don't retry any of these unchanged."
    ),
    429: (
        "`code: rate_limited`: the service's own rate limit, or every rung of the chain was "
        "throttled (by a provider or the gateway's provider quota). Retry with backoff. "
        "`code: budget_exceeded`: the service's daily budget is spent; don't retry before "
        "`Retry-After` (the next UTC day)."
    ),
    502: (
        "Every rung of the model's chain failed for a reason other than the request "
        "(`code: provider_unavailable`); `detail` names each. Retry with backoff."
    ),
    500: "Unexpected error (`code: internal_error`).",
}

DESCRIPTION = """\
The AIR platform's model gateway, across Ollama, Anthropic, OpenAI, Gemini and a
self-hosted slot: `POST /v1/generate` for text (optionally structured JSON) and
`POST /v1/embed` for vectors. Choose a model class (`local`, `auto`, `cloud`) or a
published embedding model; the gateway picks the models and handles failover, retries,
cost accounting and caching.

**Streaming.** `stream: true` on `/v1/generate` answers with Server-Sent Events:
`start`, `delta`s, then `done` or `error`. The gateway fails over between models only
before the first token; errors before then are ordinary HTTP errors. If `done` has
`refusal: true`, discard the deltas (the non-streamed answer's `content` is empty).

**Authentication.** Every `/v1` call except health and readiness carries the calling
service's token in `X-API-Key`, checked before the body is read (no key is 401 whatever
the body). Each service is granted specific operations (`generate`, `embed`); anything
else is 403.

**Stateless.** The gateway keeps no conversation: send the whole history on every
`/v1/generate` call. Unknown fields are 422.

**Errors.** Every error body is `application/problem+json` (RFC 9457) with a stable
`code` and the `request_id`, an unknown path (`not_found`) or method
(`method_not_allowed`) included. Retry only 429 `rate_limited`, 502 and 503, with
backoff; never 429 `budget_exceeded` or any 4xx.

**Tracing.** Send `X-Request-ID` (up to 128 of `A-Z a-z 0-9 . _ : -`) to have it
logged with the call and echoed back; without one, or with any other value, the
gateway generates it.

The consumer guide is docs/04-integration.md in the air-model-gateway repo.
"""


#: The `stream: true` form of /v1/generate's 200, next to the JSON one.
STREAM_RESPONSE: dict[str, Any] = {
    "description": (
        "Without `stream`: the answer as JSON (GenerateOutput). With `stream: true`: "
        "Server-Sent Events, each `event: <name>` plus one `data:` line of JSON: one "
        "`start` (StreamStart), then `delta` events (StreamDelta) whose `content` "
        "concatenates to the answer, then `done` (GenerateSummary) or, if the stream "
        "fails after it started, `error` (Problem)."
    ),
    "content": {"text/event-stream": {"schema": {"type": "string"}}},
}

# Event payloads of a stream, published as components so clients can generate
# types for them (FastAPI only publishes models a route references directly).
_STREAM_EVENT_MODELS = (StreamStart, StreamDelta, GenerateSummary)


def problems(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries for the error statuses a route can return."""
    return {s: {"model": Problem, "description": _PROBLEM_DESCRIPTIONS[s]} for s in statuses}


def install(app: FastAPI) -> None:
    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        spec = get_openapi(
            title="air-model-gateway",
            version=__version__,
            description=DESCRIPTION,
            routes=app.routes,
            tags=[
                {"name": "generate", "description": "Text generation, optionally structured."},
                {"name": "embed", "description": "Embeddings."},
                {"name": "system", "description": "Liveness, readiness and capabilities."},
                {"name": "admin", "description": "Read-only operator views."},
            ],
        )
        schemas = spec.setdefault("components", {}).setdefault("schemas", {})
        for model in _STREAM_EVENT_MODELS:
            doc = model.model_json_schema(ref_template="#/components/schemas/{model}")
            schemas.update(doc.pop("$defs", {}))
            schemas[model.__name__] = doc
        spec["components"].setdefault("headers", {})[_REQUEST_ID] = {
            "description": "The request's id: the caller's `X-Request-ID`, or a generated one.",
            "schema": {"type": "string"},
        }
        for path_item in spec.get("paths", {}).values():
            for operation in path_item.values():
                _describe(operation)
        app.openapi_schema = spec
        return spec

    app.openapi = openapi  # type: ignore[method-assign]


def _describe(operation: dict[str, Any]) -> None:
    operation.setdefault("parameters", []).append(
        {
            "name": _REQUEST_ID,
            "in": "header",
            "required": False,
            "description": (
                "Optional. Correlates this call across services and logs. A value that "
                "doesn't match the pattern is replaced with a generated id."
            ),
            "schema": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"},
        }
    )
    for response in operation.get("responses", {}).values():
        response.setdefault("headers", {})[_REQUEST_ID] = {
            "$ref": f"#/components/headers/{_REQUEST_ID}"
        }
        content = response.get("content", {})
        json_body = content.get("application/json")
        if json_body and json_body.get("schema", {}).get("$ref") == _PROBLEM_REF:
            content["application/problem+json"] = content.pop("application/json")
