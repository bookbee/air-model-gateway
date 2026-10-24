"""Multi-project governance and the contract guarantees found in review.

Each test pins a behaviour a consuming team relies on:

- services choose a class (and optionally a preferred provider) within their
  grants; only `pin`-granted services may name an exact model outside local;
- per-service daily budgets refuse paid calls once spent (429
  `budget_exceeded` + Retry-After), while cache hits stay free and allowed;
- provider quotas are shared by every service;
- a cached answer is only reused by the service that paid for it;
- `finish_reason` means the same thing whichever provider answered;
- an unhandled error is still a problem+json 500 with the request id, and is
  counted;
- outside local the gateway refuses to start on configuration that can only
  be a mistake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
import yaml
from prometheus_client import REGISTRY
from pydantic import ValidationError

from air_model_gateway.api.v1.model_calls import normalize_finish_reason
from air_model_gateway.config import ConfigError, PolicySettings, SecuritySettings, Settings
from air_model_gateway.constants import Environment, Provider
from air_model_gateway.gateway.base import ProviderResult
from air_model_gateway.main import _provider_quota, create_app
from air_model_gateway.policy.budget import MemorySpendLedger, RedisSpendLedger
from air_model_gateway.policy.engine import PolicyEngine
from air_model_gateway.policy.ratelimit import MemoryRateLimiter
from air_model_gateway.redis_store import RedisStore
from air_model_gateway.schemas.models import Usage
from air_model_gateway.schemas.policy import PolicyDoc

CLASSIFIER = {"X-API-Key": "t-classifier"}
CONSOLE = {"X-API-Key": "t-console"}


class _Router:
    """A router stand-in: answers every call with `result` from `provider`."""

    def __init__(self, result: ProviderResult, provider: Provider = Provider.ANTHROPIC) -> None:
        self.result = result
        self.provider = provider
        self.calls = 0
        self.error: Exception | None = None
        self.last_chain: Any = None

    async def chat(self, req: Any, chain: Any) -> tuple[ProviderResult, Provider]:
        self.calls += 1
        self.last_chain = chain
        if self.error is not None:
            raise self.error
        return self.result, self.provider

    async def embeddings(self, req: Any, chain: Any) -> tuple[ProviderResult, Provider]:
        return await self.chat(req, chain)

    async def health(self) -> dict[str, bool]:
        return {}

    async def aclose(self) -> None:
        return None


def _policy(tmp_path: Path, **classifier: Any) -> str:
    doc = {
        "default_allow": False,
        "services": {
            "air-classifier-service": {"resources": {"inference": ["generate"]}, **classifier},
            "air-web-console": {"resources": {"inference": ["generate"]}, "pin": True},
        },
    }
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(doc))
    return str(path)


def _settings(policy_path: str, environment: Environment = Environment.STAGING) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=environment,
        security=SecuritySettings(
            service_tokens={
                "t-classifier": "air-classifier-service",
                "t-console": "air-web-console",
            }
        ),
        policy=PolicySettings(config_path=policy_path),
    )


def _paid(content: str = "ok", finish: str = "end_turn") -> ProviderResult:
    # 1M input tokens on claude-haiku-4-5: exactly $1.00 per call.
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
    return ProviderResult(
        model="claude-haiku-4-5", content=content, usage=usage, finish_reason=finish
    )


async def _client(settings: Settings, router: _Router) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    app.state.router = router
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _gen(text: str = "hi", **choice: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}], **choice}


# ---- model choice -----------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "choice", "status"),
    [
        (CLASSIFIER, {}, 200),  # the default class, auto
        (CLASSIFIER, {"model": "local"}, 200),
        (CLASSIFIER, {"model": "cloud", "provider": "anthropic"}, 200),  # a preference
        (CLASSIFIER, {"model": "local", "provider": "anthropic"}, 422),  # not in the class
        (CLASSIFIER, {"model": "fast"}, 422),  # not a class
        (CLASSIFIER, {"pin": "ollama:llama3.2:3b"}, 403),  # no pin grant
        (CONSOLE, {"pin": "ollama:llama3.2:3b"}, 200),  # pin: true
        (CONSOLE, {"pin": "gpt-4o"}, 422),  # a pin names its provider
        (CONSOLE, {"pin": "openai:gpt-4o", "model": "cloud"}, 422),  # pin stands alone
    ],
)
async def test_outside_local_services_choose_within_their_grants(
    tmp_path: Path, headers: dict[str, str], choice: dict[str, Any], status: int
) -> None:
    async for c in _client(_settings(_policy(tmp_path)), _Router(_paid())):
        r = await c.post("/v1/generate", headers=headers, json=_gen(**choice))
        assert r.status_code == status, r.text
        if status == 403:
            assert r.json()["code"] == "policy_denied"


async def test_a_preferred_provider_goes_first_and_the_class_is_reported(tmp_path: Path) -> None:
    router = _Router(_paid())
    async for c in _client(_settings(_policy(tmp_path)), router):
        r = await c.post(
            "/v1/generate", headers=CONSOLE, json=_gen(model="cloud", provider="openai")
        )
    assert router.last_chain[0][0] is Provider.OPENAI
    assert r.json()["model_class"] == "cloud"


@pytest.mark.parametrize(
    ("grants", "choice"),
    [
        ({"model_classes": ["local"]}, {"model": "cloud"}),
        ({"preferred_providers": ["gemini"]}, {"model": "cloud", "provider": "anthropic"}),
    ],
)
async def test_a_choice_outside_the_grant_is_refused(
    tmp_path: Path, grants: dict[str, Any], choice: dict[str, Any]
) -> None:
    async for c in _client(_settings(_policy(tmp_path, **grants)), _Router(_paid())):
        r = await c.post("/v1/generate", headers=CLASSIFIER, json=_gen(**choice))
    assert (r.status_code, r.json()["code"]) == (403, "policy_denied")


async def test_locally_any_service_may_pin(tmp_path: Path) -> None:
    settings = _settings(_policy(tmp_path), Environment.LOCAL)
    async for c in _client(settings, _Router(_paid())):
        r = await c.post("/v1/generate", headers=CLASSIFIER, json=_gen(pin="openai:gpt-4o"))
        assert r.status_code == 200
        assert r.json()["model_class"] is None


async def test_capabilities_shows_a_service_its_choices(tmp_path: Path) -> None:
    settings = _settings(_policy(tmp_path, model_classes=["local", "auto"]))
    async for c in _client(settings, _Router(_paid())):
        caps = (await c.get("/v1/capabilities", headers=CLASSIFIER)).json()
    assert caps["generate"]["local"]["allowed"] and not caps["generate"]["cloud"]["allowed"]
    assert caps["generate"]["local"]["chain"] == ["ollama:llama3.2:3b"]  # its override
    assert caps["embed"]["local-768-v1"]["dimensions"] == 768
    assert caps["may_pin"] is False


# ---- budgets -----------------------------------------------------------------


async def test_a_spent_budget_refuses_paid_calls_but_not_cache_hits(tmp_path: Path) -> None:
    router = _Router(_paid())
    settings = _settings(_policy(tmp_path, daily_budget_usd=1.5))
    async for c in _client(settings, router):
        first = await c.post("/v1/generate", headers=CLASSIFIER, json=_gen("one"))
        second = await c.post("/v1/generate", headers=CLASSIFIER, json=_gen("two"))
        assert (first.json()["cost_usd"], second.status_code) == (1.0, 200)  # $2 spent now

        refused = await c.post("/v1/generate", headers=CLASSIFIER, json=_gen("three"))
        assert refused.status_code == 429
        assert refused.json()["code"] == "budget_exceeded"
        assert 0 < int(refused.headers["Retry-After"]) <= 86_400

        hit = await c.post("/v1/generate", headers=CLASSIFIER, json=_gen("one"))
        assert (hit.status_code, hit.json()["cached"]) == (200, True)
        assert router.calls == 2

        other = await c.post("/v1/generate", headers=CONSOLE, json=_gen("three"))
        assert other.status_code == 200  # budgets are per service


async def test_spend_is_shared_across_replicas_through_redis() -> None:
    server = fakeredis.FakeServer()
    a = RedisSpendLedger(RedisStore(fakeredis.FakeAsyncRedis(server=server), "t:"))
    b = RedisSpendLedger(RedisStore(fakeredis.FakeAsyncRedis(server=server), "t:"))
    await a.add("svc", 0.75)
    await b.add("svc", 0.5)
    assert await a.spent_today("svc") == pytest.approx(1.25)
    assert await b.spent_today("other") == 0.0


async def test_the_memory_ledger_keeps_only_today() -> None:
    ledger = MemorySpendLedger()
    ledger._spent[("svc", "2000-01-01")] = 99.0
    await ledger.add("svc", 1.0)
    assert await ledger.spent_today("svc") == 1.0
    assert ("svc", "2000-01-01") not in ledger._spent


# ---- provider quotas ---------------------------------------------------------


async def test_a_provider_quota_is_one_bucket_for_every_service() -> None:
    policy = PolicyEngine(PolicyDoc(provider_quotas={Provider.GEMINI: 2}))
    admit = _provider_quota(policy, MemoryRateLimiter())
    assert [await admit(Provider.GEMINI) for _ in range(3)] == [True, True, False]
    assert await admit(Provider.ANTHROPIC)  # no quota set: unlimited


def test_provider_quotas_must_name_real_providers() -> None:
    with pytest.raises(ValidationError):
        PolicyDoc.model_validate({"provider_quotas": {"gemnii": 10}})


# ---- cache isolation ---------------------------------------------------------


async def test_a_cached_answer_is_reused_only_by_the_service_that_paid(tmp_path: Path) -> None:
    router = _Router(_paid())
    async for c in _client(_settings(_policy(tmp_path), Environment.LOCAL), router):
        await c.post("/v1/generate", headers=CLASSIFIER, json=_gen("same"))
        again = await c.post("/v1/generate", headers=CLASSIFIER, json=_gen("same"))
        other = await c.post("/v1/generate", headers=CONSOLE, json=_gen("same"))
    assert again.json()["cached"] is True
    assert other.json()["cached"] is False
    assert router.calls == 2


# ---- finish_reason -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("stop", "stop"),
        ("end_turn", "stop"),
        ("STOP", "stop"),
        ("tool_use", "stop"),
        ("length", "length"),
        ("max_tokens", "length"),
        ("MAX_TOKENS", "length"),
        ("refusal", "refusal"),
        ("MALFORMED_FUNCTION_CALL", "other"),
        (None, None),
    ],
)
def test_finish_reason_is_one_vocabulary(raw: str | None, normalized: str | None) -> None:
    assert normalize_finish_reason(raw) == normalized


async def test_the_wire_carries_both_the_normalized_and_the_raw_reason(tmp_path: Path) -> None:
    router = _Router(_paid(finish="MAX_TOKENS"), Provider.GEMINI)
    async for c in _client(_settings(_policy(tmp_path), Environment.LOCAL), router):
        body = (await c.post("/v1/generate", headers=CLASSIFIER, json=_gen())).json()
    assert (body["finish_reason"], body["provider_finish_reason"]) == ("length", "MAX_TOKENS")


# ---- unhandled errors ----------------------------------------------------------


async def test_an_unhandled_error_is_a_counted_problem_with_the_request_id(tmp_path: Path) -> None:
    router = _Router(_paid())
    router.error = RuntimeError("bug")

    def served_500s() -> float:
        labels = {"route": "/v1/generate", "method": "POST", "status": "500"}
        return REGISTRY.get_sample_value("air_model_gateway_requests_total", labels) or 0.0

    before = served_500s()
    async for c in _client(_settings(_policy(tmp_path), Environment.LOCAL), router):
        r = await c.post(
            "/v1/generate", headers={**CLASSIFIER, "X-Request-ID": "rid-9"}, json=_gen()
        )
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["X-Request-ID"] == "rid-9"
    assert r.json()["request_id"] == "rid-9"
    assert served_500s() == before + 1


# ---- startup checks ------------------------------------------------------------


def test_outside_local_a_missing_policy_file_stops_startup() -> None:
    with pytest.raises(ConfigError, match="policy file not found"):
        create_app(_settings("deploy/policy/nope.yaml"))


def test_outside_local_no_service_tokens_stops_startup(tmp_path: Path) -> None:
    settings = _settings(_policy(tmp_path))
    settings.security = SecuritySettings()
    with pytest.raises(ConfigError, match="no service tokens"):
        create_app(settings)


def test_locally_missing_files_fall_back_to_built_in_tables() -> None:
    create_app(_settings("deploy/policy/nope.yaml", Environment.LOCAL))


async def test_request_metrics_use_full_paths_and_one_label_for_unknown_urls() -> None:
    def count(route: str, status: str) -> float:
        labels = {"route": route, "method": "GET", "status": status}
        return REGISTRY.get_sample_value("air_model_gateway_requests_total", labels) or 0.0

    health, unknown = count("/v1/health", "200"), count("<unmatched>", "404")
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/v1/health")
        await c.get("/wp-admin/x")
        await c.get("/random/y")
    assert count("/v1/health", "200") == health + 1
    assert count("<unmatched>", "404") == unknown + 2


def test_outside_local_a_grant_of_an_unpublished_embedding_model_stops_startup(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "air-rag": {
                        "resources": {"inference": ["embed"]},
                        "embed_models": ["local-1024-v9"],
                    }
                }
            }
        )
    )
    with pytest.raises(ConfigError, match="local-1024-v9"):
        create_app(_settings(str(policy)))
