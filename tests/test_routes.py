from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from air_model_gateway.api.v1 import model_calls
from air_model_gateway.config import PolicySettings, SecuritySettings, Settings
from air_model_gateway.constants import Environment, Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.main import create_app
from air_model_gateway.schemas.models import Usage

SENT = {"X-API-Key": "t-sent"}


class _FakeRouter:
    """Stands in for ModelRouter so the endpoint can be tested without providers."""

    def __init__(
        self,
        result: ProviderResult | None = None,
        error: Exception | None = None,
        provider: Provider = Provider.OLLAMA,
    ) -> None:
        self._result = result
        self._error = error
        self._provider = provider
        self.last_json_schema: dict[str, Any] | None = None
        self.last_chain: Any = None
        self.calls = 0

    async def chat(self, req: Any, chain: Any) -> tuple[ProviderResult, Provider]:
        self.last_chain = chain
        self.calls += 1
        self.last_json_schema = req.json_schema
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, self._provider

    async def embeddings(self, req: Any, chain: Any) -> tuple[ProviderResult, Provider]:
        self.last_chain = chain
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, self._provider

    async def health(self) -> dict[str, bool]:
        return {}

    async def aclose(self) -> None:
        return None


def _settings(environment: Environment = Environment.LOCAL) -> Settings:
    return Settings(
        environment=environment,
        security=SecuritySettings(
            service_tokens={"t-sent": "air-classifier-service", "t-ghost": "unlisted-service"}
        ),
        policy=PolicySettings(config_path="deploy/policy/policy.local.yaml"),
    )


async def _client(
    router: _FakeRouter, environment: Environment = Environment.LOCAL
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings(environment))
    app.state.router = router
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_generate_passes_schema_and_returns_content() -> None:
    router = _FakeRouter(
        result=ProviderResult(
            model="llama3.2:3b",
            content='{"label":"positive"}',
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    )
    async for client in _client(router):
        r = await client.post(
            "/v1/generate",
            headers=SENT,
            json={
                "pin": "ollama:llama3.2:3b",
                "messages": [{"role": "user", "content": "great!"}],
                "json_schema": {"type": "object"},
                "schema_name": "Verdict",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == '{"label":"positive"}'
    assert body["usage"]["total_tokens"] == 15
    assert body["cost_usd"] == 0.0  # local model is free
    assert router.last_json_schema == {"type": "object"}


async def test_generate_prices_a_cloud_model() -> None:
    router = _FakeRouter(
        result=ProviderResult(
            model="claude-haiku-4-5",
            content="{}",
            usage=Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000),
        ),
        provider=Provider.ANTHROPIC,
    )
    async for client in _client(router):
        r = await client.post(
            "/v1/generate",
            headers=SENT,
            json={
                "model": "cloud",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
    assert r.status_code == 200
    # (1000 * 1.00 + 1000 * 5.00) / 1e6
    assert r.json()["cost_usd"] == pytest.approx(0.006)


async def test_refusal_is_reported_not_cached() -> None:
    router = _FakeRouter(result=ProviderResult(model="claude-haiku-4-5", refusal=True))
    async for client in _client(router):
        r = await client.post(
            "/v1/generate",
            headers=SENT,
            json={"messages": [{"role": "user", "content": "x"}]},
        )
    assert r.status_code == 200
    assert r.json()["refusal"] is True


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens", "MAX_TOKENS"])
async def test_an_answer_cut_off_at_the_token_limit_is_not_cached(finish_reason: str) -> None:
    """Otherwise every retry gets the same half-finished JSON back from cache
    and the caller can never recover by asking again."""
    router = _FakeRouter(
        result=ProviderResult(model="m", content='{"label": "pos', finish_reason=finish_reason)
    )
    payload = {"messages": [{"role": "user", "content": "x"}]}
    async for client in _client(router):
        await client.post("/v1/generate", headers=SENT, json=payload)
        second = (await client.post("/v1/generate", headers=SENT, json=payload)).json()
    assert second["cached"] is False


async def test_upstream_rate_limit_maps_to_429() -> None:
    router = _FakeRouter(error=ProviderError(Provider.ANTHROPIC, "throttled", rate_limited=True))
    async for client in _client(router):
        r = await client.post(
            "/v1/generate",
            headers=SENT,
            json={"messages": [{"role": "user", "content": "x"}]},
        )
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"


async def test_provider_down_maps_to_502() -> None:
    router = _FakeRouter(error=ProviderError(Provider.ANTHROPIC, "connection refused"))
    async for client in _client(router):
        r = await client.post(
            "/v1/generate",
            headers=SENT,
            json={"messages": [{"role": "user", "content": "x"}]},
        )
    assert r.status_code == 502
    assert r.json()["code"] == "provider_unavailable"


VECTOR = [0.1] * 768
EMBED = {"model": "local-768-v1", "input": ["hello"]}


async def test_embed_returns_the_vectors_with_their_model_and_dimensions() -> None:
    router = _FakeRouter(result=ProviderResult(model="nomic-embed-text", embeddings=[VECTOR]))
    async for client in _client(router):
        r = await client.post("/v1/embed", headers=SENT, json=EMBED)
    assert r.status_code == 200
    body = r.json()
    assert body["embeddings"] == [VECTOR]
    assert (body["embedding_model"], body["dimensions"]) == ("local-768-v1", 768)
    assert router.last_chain == [(Provider.OLLAMA, "nomic-embed-text")]  # one model, no fallback


async def test_vectors_of_the_wrong_dimension_never_reach_the_caller() -> None:
    """A published name promises a dimension; a model answering with another
    (a misconfigured name) would corrupt the caller's index."""
    router = _FakeRouter(result=ProviderResult(model="nomic-embed-text", embeddings=[[0.1] * 1024]))
    async for client in _client(router):
        r = await client.post("/v1/embed", headers=SENT, json=EMBED)
    assert r.status_code == 502
    assert "1024" in r.json()["detail"]


async def test_generate_without_messages_is_a_validation_error() -> None:
    router = _FakeRouter()
    async for client in _client(router):
        r = await client.post("/v1/generate", headers=SENT, json={})
    assert r.status_code == 422


async def test_embed_without_input_is_a_validation_error() -> None:
    router = _FakeRouter()
    async for client in _client(router):
        r = await client.post("/v1/embed", headers=SENT, json={})
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"input": ["hi"]}, 422),  # required: the name is the index's identity
        ({"model": "my-embedder", "input": ["hi"]}, 422),  # not <local|cloud>-<dims>-v<n>
        ({"model": "local-1024-v1", "input": ["hi"]}, 422),  # well-formed, not published
    ],
)
async def test_embed_needs_a_published_embedding_model(body: dict[str, Any], status: int) -> None:
    async for client in _client(_FakeRouter()):
        r = await client.post("/v1/embed", headers=SENT, json=body)
    assert r.status_code == status


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": -1}, "max_tokens"),
        ({"temperature": 2.5}, "temperature"),
        ({"temperature": -0.1}, "temperature"),
        ({"messages": [{"role": "wizard", "content": "hi"}]}, "messages.0.role"),
    ],
)
async def test_out_of_contract_generate_fields_are_rejected(
    patch: dict[str, Any], field: str
) -> None:
    """Each of these would otherwise reach a provider: Ollama reads max_tokens
    <= 0 as "no cap" and answers past an unknown role; Anthropic and Gemini
    400, which would come back as a retryable 502 after walking the chain."""
    router = _FakeRouter()
    body = {"messages": [{"role": "user", "content": "hi"}], **patch}
    async for client in _client(router):
        r = await client.post("/v1/generate", headers=SENT, json=body)
    assert r.status_code == 422
    assert r.json()["detail"].startswith(f"{field}: ")


async def test_a_validation_error_names_the_field_without_echoing_the_body() -> None:
    router = _FakeRouter()
    secret_prompt = "my account number is 4111"
    async for client in _client(router):
        r = await client.post(
            "/v1/embed",
            headers=SENT,
            json={"messages": [{"role": "user", "content": secret_prompt}]},
        )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail == (
        "model: Field required; input: Field required; messages: Extra inputs are not permitted"
    )
    assert secret_prompt not in detail


class _AuditLog:
    """Records the endpoint's structlog calls, so the audit contract can be
    asserted without depending on how logging is rendered."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def calls(self) -> list[dict[str, Any]]:
        return [fields for event, fields in self.events if event == "model.call"]


async def test_a_cache_hit_costs_nothing_and_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = _AuditLog()
    monkeypatch.setattr(model_calls, "log", audit)
    router = _FakeRouter(
        result=ProviderResult(
            model="claude-haiku-4-5",
            content="hello",
            usage=Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000),
        ),
        provider=Provider.ANTHROPIC,
    )
    payload = {"model": "cloud", "messages": [{"role": "user", "content": "x"}]}
    async for client in _client(router):
        first = (await client.post("/v1/generate", headers=SENT, json=payload)).json()
        second = (await client.post("/v1/generate", headers=SENT, json=payload)).json()

    assert first["cached"] is False
    assert first["cost_usd"] == pytest.approx(0.006)
    # Same answer, but this call spent nothing — echoing the first call's cost
    # would double-count it against a consumer's budget.
    assert second["cached"] is True
    assert second["content"] == "hello"
    assert second["cost_usd"] == 0.0

    calls = audit.calls()
    assert [c["cached"] for c in calls] == [False, True]
    assert [c["cost_usd"] for c in calls] == [pytest.approx(0.006), 0.0]
    assert all(c["service"] == "air-classifier-service" for c in calls)


async def test_embed_calls_are_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = _AuditLog()
    monkeypatch.setattr(model_calls, "log", audit)
    router = _FakeRouter(
        result=ProviderResult(
            model="nomic-embed-text",
            embeddings=[VECTOR],
            usage=Usage(prompt_tokens=8, total_tokens=8),
        ),
    )
    async for client in _client(router):
        r = await client.post("/v1/embed", headers=SENT, json=EMBED)
    assert r.status_code == 200
    [call] = audit.calls()
    assert (call["operation"], call["model_choice"]) == ("embed", "local-768-v1")
    assert call["provider"] == "ollama"
    assert call["service"] == "air-classifier-service"


async def test_unlisted_service_is_allowed_locally_but_denied_in_prod(tmp_path: Path) -> None:
    """The shipped local policy sets default_allow: true. In prod that flag is
    ignored, so a valid token for a service with no explicit grant gets 403."""
    router = _FakeRouter(result=ProviderResult(model="m", content="ok"))
    ghost = {"X-API-Key": "t-ghost"}
    payload = {"messages": [{"role": "user", "content": "x"}]}

    async for client in _client(router, Environment.LOCAL):
        assert (await client.post("/v1/generate", headers=ghost, json=payload)).status_code == 200

    # The local file as it stands, plus the budgets prod requires (main.py).
    doc = yaml.safe_load(Path("deploy/policy/policy.local.yaml").read_text())
    for service in doc["services"].values():
        service["daily_budget_usd"] = 100.0
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump(doc))
    settings = _settings(Environment.PROD)
    settings.policy.config_path = str(policy)
    app = create_app(settings)
    app.state.router = router
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/v1/generate", headers=ghost, json=payload)
        assert r.status_code == 403
        assert r.json()["code"] == "policy_denied"
        # An explicitly granted service is unaffected.
        assert (await client.post("/v1/generate", headers=SENT, json=payload)).status_code == 200


@pytest.mark.parametrize(
    ("route", "body", "field"),
    [
        ("/v1/generate", {"stream_options": {"include_usage": True}}, "stream_options"),
        ("/v1/generate", {"tools": []}, "tools"),
        ("/v1/generate", {"top_p": 0.9}, "top_p"),
        (
            "/v1/generate",
            {"messages": [{"role": "user", "content": "hi", "name": "a"}]},
            "messages.0.name",
        ),
        ("/v1/embed", {"dimensions": 256}, "dimensions"),
    ],
)
async def test_an_option_the_gateway_does_not_implement_is_rejected(
    route: str, body: dict[str, Any], field: str
) -> None:
    """Ignored, `tools` would get a model that can't call any and an unsupported
    option would silently change nothing; the caller must find out at once."""
    base: dict[str, Any] = (
        {"messages": [{"role": "user", "content": "hi"}]}
        if route == "/v1/generate"
        else {"model": "local-768-v1", "input": ["x"]}
    )
    async for client in _client(_FakeRouter()):
        r = await client.post(route, headers=SENT, json={**base, **body})
    assert r.status_code == 422
    assert r.json()["detail"] == f"{field}: Extra inputs are not permitted"


async def test_cache_false_always_asks_the_provider() -> None:
    router = _FakeRouter(
        result=ProviderResult(model="m", content="a", usage=Usage(), finish_reason="stop")
    )
    payload = {"messages": [{"role": "user", "content": "x"}], "cache": False}
    async for client in _client(router):
        first = (await client.post("/v1/generate", headers=SENT, json=payload)).json()
        second = (await client.post("/v1/generate", headers=SENT, json=payload)).json()
        # ... and stores nothing: a later cached call still misses.
        third = await client.post("/v1/generate", headers=SENT, json={**payload, "cache": True})
    assert (first["cached"], second["cached"], third.json()["cached"]) == (False, False, False)
    assert router.calls == 3


GEN = {"messages": [{"role": "user", "content": "x"}]}
JSON = {"Content-Type": "application/json"}


async def test_a_request_every_provider_rejected_is_the_callers_422_not_a_502() -> None:
    error = ProviderError(Provider.OLLAMA, "all providers failed: 400 too long", status=400)
    async for client in _client(_FakeRouter(error=error)):
        r = await client.post("/v1/generate", headers=SENT, json=GEN)
    assert (r.status_code, r.json()["code"]) == (422, "provider_rejected")
    assert "too long" in r.json()["detail"]


@pytest.mark.parametrize(
    ("choice", "status", "code"),
    [
        ({"pin": "ollama:no-such-model"}, 422, "invalid_model_choice"),  # the caller named it
        ({"model": "local"}, 502, "provider_unavailable"),  # the owner's chain is broken
    ],
)
async def test_a_model_the_provider_doesnt_have_is_the_callers_only_when_pinned(
    choice: dict[str, Any], status: int, code: str
) -> None:
    error = ProviderError(Provider.OLLAMA, "404 model 'no-such-model' not found", status=404)
    async for client in _client(_FakeRouter(error=error)):
        r = await client.post("/v1/generate", headers=SENT, json={**GEN, **choice})
    assert (r.status_code, r.json()["code"]) == (status, code)


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("POST", "/v1/inference", 404, "not_found"),
        ("GET", "/v1/generate", 405, "method_not_allowed"),
    ],
)
async def test_routing_errors_are_problem_documents(
    method: str, path: str, status: int, code: str
) -> None:
    async for client in _client(_FakeRouter()):
        r = await client.request(method, path, headers=SENT)
    assert r.status_code == status
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert body["code"] == code
    assert body["request_id"] == r.headers["X-Request-ID"]
    if status == 405:
        assert r.headers["allow"] == "POST"


@pytest.mark.parametrize("path", ["/v1/generate", "/v1/embed"])
async def test_no_key_is_401_even_when_the_body_is_not_json(path: str) -> None:
    broken = b'{"messages": ['
    async for client in _client(_FakeRouter()):
        anonymous = await client.post(path, content=broken, headers=JSON)
        keyed = await client.post(path, content=broken, headers={**SENT, **JSON})
    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthorized")
    assert keyed.status_code == 422
    assert keyed.json()["detail"] == "body is not valid JSON (at character 14)"


async def test_routes_without_a_key_requirement_still_answer_anonymously() -> None:
    async for client in _client(_FakeRouter()):
        assert (await client.get("/v1/health")).status_code == 200


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        ("/v1/embed", {"model": "local-768-v1", "input": ["t"] * 2049}, "input"),
        ("/v1/embed", {"model": "local-768-v1", "input": ["t" * 32_001]}, "input.0"),
        ("/v1/generate", {"messages": [{"role": "user", "content": "t"}] * 501}, "messages"),
        (
            "/v1/generate",
            {"messages": [{"role": "user", "content": "t" * 400_001}] * 2},
            "800,000",
        ),
    ],
    ids=["embed-count", "embed-text", "generate-count", "generate-chars"],
)
async def test_oversized_requests_are_refused_before_any_provider_call(
    path: str, body: dict[str, Any], field: str
) -> None:
    router = _FakeRouter(ProviderResult(model="m", embeddings=[[0.0]]))
    async for client in _client(router):
        r = await client.post(path, headers=SENT, json=body)
    assert (r.status_code, r.json()["code"]) == (422, "validation_error")
    assert field in r.json()["detail"]
    assert router.last_chain is None  # no provider was asked
