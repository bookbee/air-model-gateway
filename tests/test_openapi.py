"""The published contract: docs/openapi.json.

Other suites ask whether the gateway behaves; this one asks whether the document
consumers generate clients from says so. Pinned:

- the committed file is exactly what the code generates (`make openapi` after
  any wire change), and it is a valid OpenAPI 3.1 document;
- the path surface is exactly the /v1 routes (no accidental export, and
  /metrics stays out: it is Prometheus text, not JSON);
- every authenticated route declares the token scheme and its errors as
  problem+json, which is what api/errors.py really sends;
- /v1/generate and /v1/embed each take only their own body, and no route is
  deprecated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from openapi_spec_validator import validate

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"

EXPECTED_PATHS = {
    "/v1/generate": {"post"},
    "/v1/embed": {"post"},
    "/v1/health": {"get"},
    "/v1/ready": {"get"},
    "/v1/capabilities": {"get"},
    "/v1/admin/policy": {"get"},
}
AUTHENTICATED_ERRORS = {
    ("/v1/generate", "post"): {"401", "403", "422", "429", "502"},
    ("/v1/embed", "post"): {"401", "403", "422", "429", "502"},
    ("/v1/capabilities", "get"): {"401"},
    ("/v1/admin/policy", "get"): {"401", "403"},
}


@pytest.fixture
def spec(app: FastAPI) -> dict[str, Any]:
    return app.openapi()


def test_the_committed_document_matches_the_code(spec: dict[str, Any]) -> None:
    committed = json.loads(SPEC_PATH.read_text())
    assert committed == spec, "docs/openapi.json is stale: run `make openapi` and commit it"


def test_the_document_is_valid_openapi(spec: dict[str, Any]) -> None:
    assert spec["openapi"].startswith("3.1")
    validate(spec)


def test_the_path_surface_is_exactly_the_v1_routes(spec: dict[str, Any]) -> None:
    assert {p: set(ops) for p, ops in spec["paths"].items()} == EXPECTED_PATHS


@pytest.mark.parametrize(("path", "method"), sorted(AUTHENTICATED_ERRORS))
def test_authenticated_routes_declare_the_token_and_problem_errors(
    spec: dict[str, Any], path: str, method: str
) -> None:
    op = spec["paths"][path][method]
    assert op["security"] == [{"ServiceToken": []}]
    errors = {code: r for code, r in op["responses"].items() if code.startswith(("4", "5"))}
    assert set(errors) == AUTHENTICATED_ERRORS[(path, method)]
    for response in errors.values():
        schema = response["content"]["application/problem+json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/Problem"}


def test_the_token_scheme_is_the_header_the_gateway_reads(spec: dict[str, Any]) -> None:
    assert spec["components"]["securitySchemes"]["ServiceToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "The calling service's token, issued per service and environment.",
    }


def test_each_route_takes_only_its_own_body(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    generate, embed = schemas["GenerateInput"], schemas["EmbedInput"]
    assert generate["required"] == ["messages"] and "input" not in generate["properties"]
    assert embed["required"] == ["model", "input"] and "messages" not in embed["properties"]
    assert generate["properties"]["model"]["default"] == "auto"


def test_no_route_is_deprecated(spec: dict[str, Any]) -> None:
    assert not [op for ops in spec["paths"].values() for op in ops.values() if op.get("deprecated")]


async def test_a_missing_token_is_still_a_problem_401(client: httpx.AsyncClient) -> None:
    """The security scheme must not swap our 401 for FastAPI's own 403."""
    r = await client.post("/v1/generate", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


async def test_the_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/health", headers={"X-Request-ID": "trace-123"})
    assert r.headers["X-Request-ID"] == "trace-123"


@pytest.mark.parametrize("bad", ["a" * 129, 'a"b<script>', "has space"])
async def test_an_unbounded_request_id_is_replaced(client: httpx.AsyncClient, bad: str) -> None:
    """Echoed into headers, logs and error bodies, so only bounded ids survive."""
    r = await client.get("/v1/health", headers={"X-Request-ID": bad})
    echoed = r.headers["X-Request-ID"]
    assert echoed != bad
    assert len(echoed) == 32


def test_generate_declares_its_stream_and_the_event_payloads(spec: dict[str, Any]) -> None:
    ok = spec["paths"]["/v1/generate"]["post"]["responses"]["200"]["content"]
    assert set(ok) == {"application/json", "text/event-stream"}
    schemas = spec["components"]["schemas"]
    assert {"StreamStart", "StreamDelta", "GenerateSummary"} <= set(schemas)
    assert "content" not in schemas["GenerateSummary"]["properties"]
    assert schemas["GenerateInput"]["properties"]["stream"]["default"] is False
