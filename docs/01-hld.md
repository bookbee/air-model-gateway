# AIR Model Gateway — High-Level Design

**Status:** Draft for review · **Companion docs:** [Plan](00-plan.md) · [LLD](02-lld.md)

---

## 1. System context

```
   air-platform      air-classifier       air-client        (any future consumer)
        │                   │                   │                    │
        └───────────────────┴── air_model_gateway_client (SDK) ─────────────────┘
                              │ HTTPS + X-API-Key
                              ▼
                 ┌──────────────────────────────┐
                 │            air-model-gateway             │
                 │  AuthN · Policy · Rate limit   │
                 │  Router · Cost · Cache · Audit │
                 └──┬─────────┬──────────┬───────┘
                    │         │          │
                Ollama   llama.cpp    Anthropic · OpenAI · Gemini
               (local)   (local; any    (cloud, both environments)
                          OpenAI-compat
                          backend — vLLM/
                          TGI in prod)
```

`air-infra` does not appear in this diagram. It is a separate, local-development-only
project (Redis/Postgres/Mongo/Ollama containers, `make up`) that is never promoted to
production and carries no runtime relationship to `air-model-gateway` — see
[Plan §1](00-plan.md#1-problem-statement). Every consumer reaches `air-model-gateway` the same way,
in every environment: directly, over HTTPS, configured by `AIR_MODEL_GATEWAY_URL` /
`AIR_MODEL_GATEWAY_TOKEN`.

## 2. Architectural principles

1. **On-path by necessity, not by default.** `air-model-gateway` sits in front of every model call
   because that is the only place routing, failover, cost attribution, and caching can
   happen — the same reasoning `air-infra` originally applied to justify a model gateway
   at all. Nothing else about `air-model-gateway` assumes it should be on-path; it just is, for
   models.
2. **No hidden runtime dependency.** `air-model-gateway` never assumes another AIR service is
   available to lean on. It owns its own authN, policy, rate limiting, secrets, and (from
   Phase 5) its own Redis. A consumer that only has `AIR_MODEL_GATEWAY_URL` and a token gets a fully
   working client.
3. **One image, many environments.** A single artifact selects behavior from
   `AIR_MODEL_GATEWAY_ENVIRONMENT`. Local differs from prod only in which providers have keys set
   and which secret/cache backend is wired in — never in code path.
4. **Ported, not reinvented, where something already works.** The provider protocol,
   router, cost book, and cache are lifted from `air-infra`'s gateway essentially
   unchanged (see [LLD](02-lld.md)); the new surface area in this project is the control
   plane (`air-infra` used to supply authN/policy/rate-limit — here that moves in-process)
   and standalone deployability.
5. **Every remote call is guarded.** Timeout + failover on every provider attempt from
   day one (ported); retry and circuit breaking layer on in a later phase.
6. **Controls are observable.** Every call is metered (who, what provider/model, tokens,
   cost, cache hit) and audited as a first-class output, not an afterthought.

## 3. Components

| Component | Responsibility |
|---|---|
| **AuthN** | Per-service API tokens (`X-API-Key`); mTLS-ready for prod. Ported pattern from `air-infra`'s `security/apikeys.py` |
| **Policy engine** | RBAC: `(service, resource, action)` where `resource` is `inference`/`admin` and `action` is `chat` \| `embeddings` \| `admin_read`. Ported from `air-infra`'s `policy/engine.py` |
| **Rate limiter** | Token bucket per `(service, resource)`, where resource includes `inference` and `provider:<name>` so one caller cannot exhaust a shared upstream. In-memory v1, Redis-backed from Phase 5 |
| **Router** | Alias → ordered `(provider, model)` failover chain, per-attempt deadline. Ported from `air-infra`'s `gateway/router.py` |
| **Provider adapters** | `ModelProvider` implementations: Ollama, Anthropic, OpenAI, Gemini (ported), plus `SelfHostedProvider` for any OpenAI-wire-compatible backend — llama.cpp locally by default, vLLM/TGI in prod |
| **Cost book** | Prices token usage from a DevOps-owned table; unpriced models are a metric, not silent free spend. Ported from `air-infra`'s `gateway/cost.py` |
| **Response cache** | TTL cache keyed on normalized request, bounded by size as well as TTL. In-memory v1, Redis-backed from Phase 5. Ported from `air-infra`'s `gateway/cache.py` |
| **Observability** | Structured logs, Prometheus metrics, audit sink — same shape as `air-infra`'s, so shared runbooks/dashboards work across both projects |
| **Client SDK** | `air_model_gateway_client`: `chat()` / `embeddings()` against `air-model-gateway`'s API, configured via `AIR_MODEL_GATEWAY_URL` / `AIR_MODEL_GATEWAY_TOKEN` |

## 4. Request flow

```
app: llm.chat(model="smart", messages=[…])
   └─▶ POST /v1/inference   {"task": "chat", ...}   (X-API-Key)
          authN → policy(service, inference, chat) → rate-limit(service, inference)
          → per-provider rate-limit(service, provider:<primary>)
          → cache lookup ─ hit ─▶ return cached
                         └ miss ─▶ router.resolve(alias) → walk chain:
                                     for each (provider, model):
                                       guarded call (per-attempt deadline)
                                       success → break
                                       error/timeout → next rung
                                    → cost accounting → cache store → audit
          ◀── unified InferenceResponse (+ usage, cost, provider, cached flag)
```

`task: "embeddings"` follows the same path minus the cache step (see
[LLD §3](02-lld.md#3-api-surface-v1)) — one endpoint, one control-plane pass,
dispatched by `task` rather than by route.

This is the exact flow `air-infra`'s gateway already runs in production-shaped tests
(`test_gateway.py`); the only change is where authN/policy/rate-limit happen — in-process
here, rather than in a fronting service.

## 5. Environments

| Concern | `local` | `prod` |
|---|---|---|
| Providers | Ollama + an optional self-hosted backend (llama.cpp's `llama-server` by default — `make llamacpp-up`; LM Studio also works, unchanged) + whatever cloud keys are set | Full provider set: Anthropic, OpenAI, Gemini + self-hosted (vLLM/TGI) where the deployment runs one |
| Secrets | `.env` / file | Vault or cloud secrets manager |
| Cache / rate-limit backing | In-memory | Redis (Phase 5), provisioned by DevOps like any other production dependency |
| Policy | `default_allow` convenience | strict, no default-allow |
| `air-infra` involvement | None required — `air-model-gateway` runs standalone even locally | None. `air-infra` does not exist in this environment |

## 6. Cross-cutting controls

- **AuthN** — per-service API tokens; mTLS-ready.
- **AuthZ** — RBAC evaluated per call, same shape as `air-infra`'s policy engine.
- **Rate limit** — token bucket, per service and per provider.
- **Secrets** — provider API keys come straight from env-populated `Settings`, no
  in-app abstraction (Plan §4 open question 1); how they land in the environment
  (Vault agent, cloud secrets manager) is a DevOps concern outside this app.
- **Audit** — every model call emitted to the audit sink (`service`, `provider`, `model`,
  `tokens`, `cost`, `cached`).
- **Resilience** — per-attempt deadline + failover on every provider call from day one;
  retry/circuit-breaker layer on later.
- **Observability** — request-id propagation, access logs, Prometheus metrics,
  per-call cost attribution.

## 7. Failure behaviour

| Failure | Behaviour |
|---|---|
| A model provider is down | Router fails over to the next provider in the alias's chain; if all fail, 5xx + trace |
| A model provider **hangs** | Per-attempt deadline bounds the call; expiry is treated as a provider failure and fails over — without this a hang stalls the whole chain, not just one rung |
| No provider is reachable at all | `/v1/ready` reports unavailable and returns 503, so the replica leaves rotation |
| Policy backend unreachable | Fail **closed** in prod (deny), fail **open** in local (`default_allow`) |
| Cache (Redis, Phase 5) down | Gateway degrades to no-cache, not to failure |
| `air-infra` is down or absent | No effect. `air-model-gateway` has no runtime dependency on it in any environment |
