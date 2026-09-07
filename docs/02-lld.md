# AIR Model Gateway — Low-Level Design

**Status:** Draft for review · **Companion docs:** [Plan](00-plan.md) · [HLD](01-hld.md)

This document is the implementation contract. Code follows it; where it turns out to be
wrong, the document is amended rather than silently diverged from.

Modules marked **[ported]** below are lifted from `air-infra`'s
`src/air_infra/gateway/` near-verbatim — same class shapes, same behavior, imports
rewritten from `air_infra.*` to `air_model_gateway.*`. They are not being redesigned as part of
this move; see [Plan §4](00-plan.md#4-decisions-taken).

---

## 1. Repository layout

```
air-model-gateway/
├── pyproject.toml                 # deps, ruff, mypy, pytest config
├── Dockerfile                     # multi-stage, non-root runtime
├── docker-compose.yml             # air-model-gateway + local Ollama (+ optional local Redis for Phase 5 dev)
├── Makefile                       # up / down / test / lint / run targets
├── .env.example                   # every settings key, documented
├── deploy/
│   ├── prometheus/prometheus.yml
│   ├── policy/policy.local.yaml
│   ├── routing.yaml                # alias -> failover chain (DevOps-owned)   [ported]
│   └── pricing.yaml                # USD per Mtok per model (DevOps-owned)    [ported]
├── docs/                           # 00-plan · 01-hld · 02-lld
├── client/air_model_gateway_client/  # the SDK consumers import
│   ├── __init__.py                 # AirModelGatewayClient
│   └── client.py                   # chat() / embeddings(), both POST /v1/inference under the hood
└── src/air_model_gateway/
    ├── __init__.py                 # __version__
    ├── main.py                     # create_app(), lifespan, middleware wiring
    ├── config.py                   # Settings (pydantic-settings), get_settings()
    ├── constants.py                # Environment / Action / Provider enums
    │
    ├── api/
    │   ├── deps.py                 # authN, request context, policy + rate-limit guards
    │   ├── errors.py                # AppError hierarchy + RFC 9457 handlers
    │   ├── middleware.py            # request-id, access log, timing
    │   └── v1/
    │       ├── router.py             # aggregates the v1 routers
    │       ├── inference.py          # POST /v1/inference — the single entry point (chat + embeddings)
    │       ├── admin.py              # policy read (write = TODO)
    │       └── system.py             # /v1/health · /v1/ready · /v1/capabilities
    │
    ├── schemas/
    │   ├── common.py                # RequestContext, Principal, Problem
    │   ├── inference.py              # InferenceRequest/Response — the /v1/inference wire contract
    │   ├── models.py                # ChatRequest/Response, EmbeddingsRequest/Response, Usage  [ported]
    │   ├── gateway.py                # RoutingDoc, PricingDoc                                   [ported]
    │   └── policy.py                # PolicyDoc, ServicePolicy, Decision
    │
    ├── policy/
    │   ├── engine.py                 # PolicyEngine.decide(); loads policy.*.yaml
    │   └── ratelimit.py              # RateLimiter (token bucket; in-memory now, Redis = Phase 5)
    ├── gateway/
    │   ├── base.py                   # ModelProvider protocol, ProviderResult, ProviderError   [ported]
    │   ├── providers/                # ollama · anthropic · openai · gemini [ported] · self_hosted
    │   ├── router.py                 # ModelRouter: alias -> chain, failover, per-attempt deadline [ported]
    │   ├── cache.py                  # ResponseCache (in-memory now, Redis = Phase 5)            [ported]
    │   └── cost.py                   # PriceBook: price table + cost attribution                [ported]
    ├── observability/
    │   ├── logging.py                 # structlog config
    │   └── metrics.py                 # Prometheus registry + helpers
    └── security/apikeys.py            # service-token verification
```

Deliberately absent, relative to `air-infra`'s layout: `registry/`, `broker/`,
`stores/`, `secrets/`. `air-model-gateway` has no stores and brokers no credentials — it has
exactly one resource (`inference`) and serves it itself. Provider API keys come
straight from `Settings` (env-populated, same as air-infra's own gateway actually
does it today — air-infra's `broker/secrets.py` backs credential *issuance* for
stores, not its gateway's own provider keys). How those env vars get set in
production (Vault agent, cloud secrets manager) is a DevOps/ops concern outside
this app; an in-app `SecretBackend` abstraction is a Marked TODO (§10) if a
concrete need for one shows up, not built ahead of one.

## 2. Configuration (`config.py`)

`Settings` (pydantic-settings), env prefix `AIR_MODEL_GATEWAY_`, nested delimiter `__`.

| Group | Keys | Notes |
|---|---|---|
| top | `environment` | `local`\|`staging`\|`prod`; drives fail-open/closed and default provider set |
| `api` | `host`, `port` | |
| `log` | `level`, `json` | structlog renderer |
| `security` | `service_tokens` | tokens as `token:service` pairs; local file/env, DevOps-injected in prod |
| `ollama` | `base_url`, `keep_alive` | local runtime or an in-cluster one; `keep_alive` sent on every call so a shared instance isn't unloaded between AIR services |
| `self_hosted` | `base_url`, `api_key` | one OpenAI-wire-compatible slot — llama.cpp locally by default, vLLM/TGI in prod; no key needed for most self-hosted servers |
| `providers` | `anthropic_api_key`, `openai_api_key`, `gemini_api_key` | blank locally unless a developer opts in |
| `gateway` | `default_provider`, `default_model`, `cache_enabled`, `cache_ttl_seconds`, `cache_max_entries` | |
| `gateway` | `routing_config_path`, `pricing_config_path` | DevOps-owned YAML; missing file ⇒ built-in table |
| `gateway` | `request_timeout_seconds` | deadline on one provider attempt (default 60) |
| `policy` | `config_path`, `default_rate_per_minute` | |

`get_settings()` is `lru_cache`d, same as `air-infra`. A `redis` group is not
part of v1 — it arrives with Phase 5's Redis-backed cache/rate-limiter and is
documented there, not stubbed in ahead of a consumer.

## 3. API surface (v1)

All routes require `X-API-Key`; errors are RFC 9457 `application/problem+json`.

| Method | Path | Purpose | Guard |
|---|---|---|---|
| GET | `/v1/health` | liveness (process up) | none |
| GET | `/v1/ready` | readiness — 503 if no provider is reachable | none |
| GET | `/v1/capabilities` | env, configured providers, default model, version | authN |
| POST | `/v1/inference` | chat **and** embeddings, chosen by `task` | authN + policy(task) + rate-limit |
| GET | `/v1/admin/policy` | dump effective policy | authN + policy(admin_read) |

A single endpoint rather than air-infra's `/v1/models/chat` + `/v1/models/embeddings`
split, per direct instruction: one entry point every AIR service calls, over HTTP
now and (carrying the same fields) gRPC later. `task` doubles as the policy
action (`Action.CHAT` / `Action.EMBEDDINGS`), so one enum serves both instead of
keeping two vocabularies in sync (`constants.py`).

### Wire contract (`schemas/inference.py`)

```python
class InferenceRequest(BaseModel):
    task: Action = Action.CHAT              # "chat" | "embeddings"
    model: str | None = None                # alias, "provider:model", or a bare name

    # task=chat
    messages: list[Message] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    json_schema: dict[str, Any] | None = None   # structured output
    schema_name: str | None = None
    cache_prefix: bool = True

    # task=embeddings
    input: list[str] | None = None

    # validated at parse time: messages required for chat, input for embeddings

class InferenceResponse(BaseModel):
    id: str
    task: Action
    model: str
    provider: Provider
    content: str = ""                        # chat
    embeddings: list[list[float]] | None = None  # embeddings
    usage: Usage
    cost_usd: float = 0.0
    cached: bool = False                     # chat only — see §5
    refusal: bool = False                    # provider's safety layer declined
    finish_reason: str | None = None
```

The endpoint handler (`api/v1/inference.py`) translates `InferenceRequest` into
the internal `ChatRequest`/`EmbeddingsRequest` shapes below and calls the same
ported `ModelRouter` air-infra uses — the unification is confined to the wire
layer, so the router/provider code needed zero changes to support it. Structured
output (`json_schema`) and prompt-cache hinting (`cache_prefix`) carry over
per-provider exactly as `air-infra`'s LLD §5 describes — Ollama sets `format`,
Anthropic uses a forced tool, OpenAI uses JSON mode; the gateway stays
schema-agnostic.

### Internal contract (`schemas/models.py`) — ported unchanged from `air-infra`

```python
class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[Message]
    max_tokens: int | None = None
    temperature: float | None = None
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    cache_prefix: bool = True

class ChatResponse(BaseModel):
    id: str
    model: str
    provider: Provider
    content: str
    usage: Usage
    cost_usd: float = 0.0
    cached: bool = False
    refusal: bool = False
    finish_reason: str | None = None
```

`EmbeddingsRequest` / `EmbeddingsResponse` mirror `air-infra`'s shapes exactly.

## 4. Control plane (`api/deps.py`, `policy/`, `security/`)

This is the part that is genuinely new relative to `air-infra`'s gateway, because in
`air-infra` this layer lived one level up (`air-infra`'s own `api/deps.py`) and
`air-model-gateway` cannot assume anything sits in front of it.

- `security/apikeys.py::verify_token` — resolves `X-API-Key` to a `Principal(service=…)`
  against `security.service_tokens`. Same shape as `air-infra`'s.
- `PolicyEngine.decide(principal, resource, action) -> Decision` — RBAC over
  `(service, resource, action)`, ported unchanged from `air-infra`. `resource` is
  `"inference"` for every model call and `"admin"` for the policy dump — a
  single-resource policy is just the degenerate case of the same rules, so the
  dimension stayed rather than being special-cased away. `action ∈ {chat,
  embeddings, admin_read}`. `default_allow` governs the no-rule case (true
  local, false prod).
- `RateLimiter.check(principal, resource) -> bool` — token bucket keyed by
  `(service, resource)`, called once for `inference` and once for
  `provider:<primary>` per request (mirrors `air-infra`'s per-provider limiting
  in `api/v1/models.py`, so one service cannot exhaust a shared upstream via a
  busy failover chain). In-memory v1; Redis-backed from Phase 5 so limits hold
  across replicas.

## 5. Model gateway (`gateway/`) — ported, plus one new adapter

- `ModelProvider` protocol: `async chat(req, model) -> ProviderResult`,
  `async embeddings(...)`, `async health()`, `async aclose()`. `ProviderResult` carries
  content, token usage (incl. cache read/write), finish reason, and a `refusal` flag.
- Four adapters ship at Phase 1: `OllamaProvider` (plain HTTP), `AnthropicProvider`,
  `OpenAIProvider`, `GeminiProvider` — each guarded so a missing key/SDK is a clean
  "provider unavailable" (502) rather than an import error; an upstream 429 maps to 429
  (`ProviderError(rate_limited=True)`), a safety-layer decline returns `refusal: true`
  (200, uncached).
- **`OllamaProvider` sends `keep_alive` (`ollama.keep_alive`, default `30m`) on every
  `/api/chat` and `/api/embed` call** — flagged from outside this repo (an
  air-classifier integration session): without it, Ollama's own server default (5m)
  applied, and *every* AIR service sharing that Ollama instance paid a reload
  cold-start on the next call past that window. Fixed centrally, as a config default,
  rather than exposed as a per-request field on `InferenceRequest` — this is an
  operational concern about how long air-model-gateway keeps a model warm in a shared local
  runtime, not something that changes any individual response, and the whole premise
  of a unified gateway is that a caller like air-classifier shouldn't need to know
  it's talking to Ollama at all, let alone tune Ollama-specific memory-management
  flags per call. Verified against a live Ollama instance: `/api/ps`'s reported
  `expires_at` moved from ~5 minutes out to exactly the configured 30 after a real
  call through air-model-gateway, not just asserted from the outgoing payload.
- **`SelfHostedProvider`** (added after Phase 1, originally slated for Phase 5 — see
  Plan §3) covers any OpenAI-wire-compatible self-hosted backend: llama.cpp's
  `llama-server` locally by default (`make llamacpp-up`), vLLM/TGI in prod; LM Studio
  also works unchanged — it's what this was first proven against. Own
  `Provider.SELF_HOSTED` identity (not `OpenAIProvider` pointed at a different
  `base_url`) so `PriceBook.is_free` prices it free unconditionally, the same way it
  already does Ollama — see `cost.py`'s `_FREE_PROVIDERS`. No API key required by
  default (most self-hosted servers, LM Studio and llama-server included, don't check
  one). **One real incompatibility surfaced verifying this live against LM Studio**
  (and confirmed absent from llama.cpp's server, which accepts no `response_format` at
  all cleanly): unlike real OpenAI cloud, LM Studio 400s on
  `response_format={"type": "json_object"}` — it wants `"json_schema"` or `"text"`
  specifically, and self-hosted servers don't agree on this dialect at all.
  `SelfHostedProvider` therefore sends **no** `response_format` at all and relies
  purely on the schema-in-the-system-prompt instruction every JSON-mode-style adapter
  already sends — the least common
  denominator, deliberately, across a backend family defined by not agreeing on much.
- `ModelRouter.resolve(alias)` expands an alias into an ordered `(provider, model)` chain.
  The table comes from `deploy/routing.yaml` via `load_aliases()`; `DEFAULT_ALIASES` is
  the fallback when no file is mounted. A caller may bypass aliases with `provider:model`.
- **Per-attempt deadline.** Each attempt runs under
  `asyncio.wait_for(request_timeout_seconds)`; a timeout becomes a `ProviderError`, so a
  *hang* fails over exactly as an error does. Counted on
  `air_model_gateway_provider_timeouts_total`. Retry/circuit-breaking layer on later.
- `ResponseCache` keys on `(model, normalized messages, max_tokens, temperature,
  json_schema, schema_name, cache_prefix)` — every field that can change what a
  legitimate response looks like, so two structurally different requests
  against the same messages (e.g. one asking for a JSON Schema, one not)
  cannot collide. **This is one place air-model-gateway diverges from a pure port**:
  air-infra's original cache key omitted the structured-output fields, which
  was a live collision bug there too, not a porting regression — caught and
  fixed here (`tests/test_cache.py`). Bounded by `cache_max_entries` in
  addition to TTL: TTL alone only reclaims a key lazily, on its own next
  lookup, so a write-once entry (the common case for a distinct prompt) would
  otherwise never be swept; `set()` purges expired entries and, if still at
  capacity, evicts the one closest to expiry. In-memory now, Redis-backed
  (with Redis's own eviction policy) behind the same interface from Phase 5.
- `PriceBook.price(provider, model, usage)` reads `deploy/pricing.yaml`; a model absent
  from it prices at 0.0 and increments `air_model_gateway_model_unpriced_total`.
- Every call is audited with `{service, model, provider, tokens, cost, cached}`.

## 6. Client SDK (`client/air_model_gateway_client/`)

```python
llm = AirModelGatewayClient(base_url=…, token=…)        # defaults from AIR_MODEL_GATEWAY_URL / AIR_MODEL_GATEWAY_TOKEN
reply = await llm.chat(messages=[…], model="smart")
vecs  = await llm.embeddings(input=[…], model="embed")
```

No lazy driver imports needed (unlike `air_infra_client`, which defers `asyncpg`/`motor`)
— this SDK only ever speaks HTTP to `air-model-gateway`.

## 7. Errors (RFC 9457)

`AppError(status, code, detail)` subclasses — `Unauthorized(401)`, `Forbidden(403)`,
`RateLimited(429)`, `ProviderUnavailable(502)`, `DependencyUnavailable(503)` — render to
`application/problem+json`, same shape as `air-infra`'s:

```json
{ "type": "about:blank", "title": "Forbidden", "status": 403,
  "code": "policy_denied", "detail": "air-classifier may not 'chat'",
  "request_id": "01J…" }
```

## 8. Observability

- **Logs** — structlog; JSON in prod, console locally; `request_id` bound per request.
- **Metrics** — Prometheus at `/metrics`: `air_model_gateway_requests_total{route,status}`,
  `air_model_gateway_model_tokens_total{provider,model,kind}`,
  `air_model_gateway_model_cost_usd_total{provider,model}`,
  `air_model_gateway_model_unpriced_total{provider,model}`,
  `air_model_gateway_provider_timeouts_total{provider}`,
  `air_model_gateway_ratelimited_total{service,resource}`.
- **Audit** — structured `model.call` events to the log sink now; a durable sink is a
  backend swap, same TODO shape as `air-infra`'s.

## 9. Testing

- `tests/test_system.py` — health/capabilities via `httpx.ASGITransport`.
- `tests/test_readiness.py` — readiness follows provider health. **Ported from
  `air-infra`**, simplified: no store-plane distinction, since `air-model-gateway` has no stores.
- `tests/test_policy.py` — allow/deny matrix + default-allow toggle, resource
  renamed `models` → `inference`. **Ported.**
- `tests/test_gateway.py` — router failover, hang-to-failover under the deadline. **Ported
  from `air-infra`'s test of the same name** — the behavior under test does not
  change, only the import path.
- `tests/test_gateway_config.py` — shipped routing/pricing YAML loads, and every cloud
  model a chain can reach has a price. **Ported.**
- `tests/test_inference_endpoint.py` — the `/v1/inference` endpoint against a fake
  router: schema pass-through, cost pricing, refusal-not-cached, rate-limit/provider-down
  error mapping, task-based chat/embeddings dispatch, and the task-appropriate-fields
  validation (422 when `messages`/`input` is missing for its task). **Adapted from
  air-infra's `test_models_endpoint.py`** — same assertions, one endpoint instead of two.
- `tests/test_gemini_provider.py` — dedicated adapter tests for Gemini
  (structurally different request shape — see the file's own docstring). **Ported.**
- `tests/test_self_hosted_provider.py` — dedicated adapter tests for
  `SelfHostedProvider` against a faked `AsyncOpenAI`-shaped client: chat/usage
  mapping, prompt-cache token accounting, the no-`response_format`
  structured-output path, refusal normalization, embeddings, and `health()` not
  raising on an unreachable server. **New.** Written after verifying live
  against both a running LM Studio instance and llama.cpp's `llama-server`,
  not from the OpenAI wire spec alone.
- `tests/test_ollama_provider.py` — dedicated adapter tests for `OllamaProvider`'s
  `keep_alive` behavior (chat and embeddings both send it, configurable, and
  the default matches the Makefile's `ollama-warm` target). **New**, added with
  the fix above — verified live against a real Ollama instance via `/api/ps`
  first, then pinned here.
- `tests/test_cache.py` — regression coverage for the cache-key and eviction
  fixes in §5: two requests differing only in `json_schema` must not collide,
  and `set()` must bound the store regardless of TTL. **New.**
- Anthropic and OpenAI have no adapter-level unit tests of their own — matches
  air-infra's own original test coverage: exercised through `test_gateway.py`'s
  fake-provider router tests and through the real `/v1/inference` call once a
  provider is configured.

## 10. Marked TODOs (fill-in points for later phases)

- `policy/ratelimit.py` — Redis token bucket for multi-replica limits (Phase 5).
- `gateway/cache.py` — Redis-backed cache (Phase 5).
- `gateway/router.py` — retry + circuit breaker on top of the per-attempt deadline.
- `api/v1/admin.py` — policy **writes**.
- `config.py::SelfHostedSettings` — single slot today (one active self-hosted
  backend per environment). Becomes a named map (`{lmstudio: {...}, vllm_prod:
  {...}}`) only if running more than one concurrently becomes a real need, not
  ahead of one.
- An in-app `SecretBackend` abstraction for provider keys — only if a concrete
  need for one shows up (e.g. per-request key rotation); air-infra's own gateway
  doesn't use one for this today, and env-populated `Settings` covers it (§1).
- OTel trace export wiring.
