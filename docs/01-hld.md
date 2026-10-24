# AIR Model Gateway — High-Level Design

**Companion docs:** [Overview](overview.md) · [Scope](00-plan.md) · [LLD](02-lld.md) · [Deployment](03-deployment.md)

## 1. System context

```
  air-orchestrator-service   air-classifier-service   air-tools   air-rag   air-web-console
              │                         │                 │          │             │
              └────────────────────┬────┴─────────────────┴──────────┴─────────────┘
                                   │ POST /v1/generate · /v1/embed · X-API-Key
                                   ▼
                 ┌──────────────────────────────────┐
                 │        air-model-gateway         │◀────▶ Redis (optional)
                 │ AuthN · Policy · Limits · Budget │
                 │  Router · Cost · Cache · Audit   │
                 └──┬─────────┬──────────┬──────────┘
                    │         │          │
                Ollama   llama.cpp    Anthropic · OpenAI · Gemini
               (local)   (or vLLM/TGI) (cloud)
```

Consumers reach the gateway directly, configured with its URL and their own service
token. The gateway depends on no other AIR service. Redis, when configured, is its own
dependency, provisioned like any other.

## 2. Principles

1. **On the path for every model call.** Routing, failover, cost attribution and caching
   are only possible where every call passes.
2. **No hidden runtime dependency.** The gateway owns its authN, policy, rate limits and
   secrets loading. Redis is optional and its loss degrades, never fails, the service.
3. **One image, many environments.** Behaviour differs by configuration
   (`AIR_MODEL_GATEWAY_ENVIRONMENT`, keys, Redis URL), never by code path.
4. **Configuration over code.** Routing, pricing and permissions are DevOps-owned files.
5. **Every remote call is guarded.** Per-attempt deadline, failover, circuit breaker and a
   transient-only retry on every provider call; a short timeout and a breaker on Redis.
6. **Every call is accounted for.** Who called, which provider and model, tokens, cost and
   cache hit, as metrics and as one audit line per call.

## 3. Components

| Component | Responsibility |
|---|---|
| **API** | `POST /v1/generate` (JSON, or Server-Sent Events with `stream: true`) and `POST /v1/embed`, on one shared call path, so the control plane is identical for every route |
| **AuthN** | Resolves the `X-API-Key` service token to a service name |
| **Policy engine** | RBAC on `(service, resource, action)`: resource `inference` (actions `generate`, `embed`, named after the routes) or `admin` (`admin_read`). Also holds the governance settings: which model classes, embedding models and preferred providers a service may use, `pin`, `daily_budget_usd`, `provider_quotas`. Fails closed outside `local` |
| **Rate limiter** | Token buckets: one per service for `inference`, and one per provider shared by all services for `provider_quotas` (checked by the router before each rung). In memory, or shared in Redis |
| **Spend ledger** | Each service's spend for the current UTC day, for budgets. In memory, or shared in Redis |
| **Model catalog** | Turns a caller's choice into a chain from `routing.yaml`: a class (`local`, `auto`, `cloud`; per-service overrides; a preferred provider moved first), a granted `pin`, or a published embedding name (one model). Rejects choices that mean nothing (422) |
| **Router** | Walks a chain of `(provider, model)` rungs with a per-attempt deadline, per-rung circuit breakers, provider quotas and a transient-only retry |
| **Provider adapters** | One per provider behind the `ModelProvider` protocol: Ollama, Anthropic, OpenAI, Gemini, and a self-hosted OpenAI-compatible slot |
| **Cost book** | Prices token usage (including prompt-cache reads and writes) from `pricing.yaml` |
| **Response cache** | Chat responses keyed on the calling service and everything that shapes the answer. In memory, or shared in Redis with a memory fallback |
| **Observability** | Request IDs, access and audit logs, Prometheus metrics |
| **Reference client** | `air_model_gateway_client`: typed Python client for the wire contract |

## 4. Request flow

```
POST /v1/generate {"model": "cloud", "provider": "anthropic", "messages": [...]}
  │
  ├─ authN ─────────────── unknown token ──────────────────────────▶ 401
  ├─ policy(service, inference, generate) ── no grant ─────────────▶ 403
  ├─ rate limit: (service, inference) ──────────────────────────────▶ 429
  ├─ class / provider / pin outside the service's grant ─────────────▶ 403
  ├─ choice → chain (catalog: class, service override, preference) ── meaningless ▶ 422
  ├─ cache lookup (this service's entries) ── hit ──▶ cached=true, cost 0
  ├─ daily budget spent ─────────────────────── 429 budget_exceeded
  │
  └─ router: for each (provider, model) in the chain
        ├─ breaker open, or provider quota spent? ── skip
        ├─ call under the per-attempt deadline
        │     ├─ answer ──▶ price → spend → cache store → audit + metrics ──▶ 200
        │     └─ error / timeout ──▶ next rung
        └─ chain exhausted:
              ├─ some failure transient and retries left ── back off, walk again
              ├─ every rung rate-limited ─────────────────────────────▶ 429
              └─ otherwise ──────────────────────────────────────────▶ 502
```

With `stream: true`, everything above up to the first token is unchanged: a rung
"answers" once its stream yields a first token within the per-attempt deadline, and
any failure until then fails over or becomes an ordinary HTTP error. The answer then
streams as Server-Sent Events (`start`, `delta`…, `done`); pricing, spend, cache store
and audit run when the provider finishes, before `done`.

`POST /v1/embed` follows the same path with the action `embed` and no cache step.

## 5. Environments

| Concern | `local` | `staging` / `prod` (GKE) |
|---|---|---|
| Runtime | `make run` / `make up`; `make kind-up` for the production chart | Helm chart on GKE Autopilot, 2–10 replicas, in-cluster Service only |
| Providers | Native Ollama, optional llama.cpp, any cloud keys set | Cloud keys from Secret Manager; Ollama and vLLM/llama.cpp as separately owned in-cluster model servers |
| Secrets | `.env` | Secret Manager files in `SECRETS_DIR` (GKE add-on, Workload Identity) |
| Cache and rate limits | In process, or local Redis (`REDIS=infra`) | Memorystore Redis (AUTH + TLS); required for more than one replica |
| Policy | `default_allow` honoured for unlisted services | Fails closed: every consumer needs an explicit grant (`policy.<env>.yaml`) |
| Network | Open on localhost | NetworkPolicy admits only consumer namespaces |
| Logs | Console | JSON with Cloud Logging `severity` |
| Metrics | Optional local Prometheus | Managed Service for Prometheus (`PodMonitoring`) |

The production shape and its operation are in [03-deployment.md](03-deployment.md).

## 6. Cross-cutting controls

- **AuthN:** per-service tokens; in-cluster encryption (GKE inter-node or a mesh) can
  be added without changes to the gateway.
- **AuthZ:** RBAC evaluated on every call.
- **Rate limiting and governance:** per-service rate limits, provider quotas shared by
  all services, per-service daily budgets, grants for which model choices each service
  may make; all
  shared across replicas through Redis and set in the policy file.
- **Secrets:** env vars or mounted files (Secret Manager on GKE); nothing secret in the
  repo, the image, Kubernetes Secrets or CI.
- **Resilience:** deadline, failover, circuit breaker and transient-only retry for
  providers; timeout, breaker and degraded fallbacks for Redis.
- **Audit:** one `model.call` line per served call (service, operation, provider, model,
  tokens, cost, cached, finish_reason).
- **Observability:** request-ID propagation, structured JSON logs, Prometheus metrics.

## 7. Failure behaviour

| Failure | Behaviour |
|---|---|
| A provider errors or is down | Fail over to the next rung. After repeated transient failures its breaker opens and the rung is skipped until a trial call succeeds |
| A provider hangs | The per-attempt deadline turns the hang into a failure and the chain fails over |
| Every rung fails transiently | One jittered retry of the chain, then 502 (429 if every rung was throttled) |
| Every rung fails permanently (bad key, 400) | 502 immediately; no retry |
| An adapter raises something unexpected (a bug, an SDK change) | Treated as that rung's failure: fail over, log the traceback; the breaker is not charged |
| A provider's gateway quota is spent | Its rungs are skipped like a 429; the chain fails over (429 if nothing else answers) |
| A stream fails after its first token (provider error, silence past the idle deadline) | No failover: an `error` event ends the stream; the rung's breaker is told |
| A caller disconnects mid-stream | The provider stream is closed (generation stops); the call is audited as `client_disconnected`, its spend uncounted |
| A service's daily budget is spent | Paid calls get 429 `budget_exceeded` with `Retry-After`; cache hits still answer |
| No provider reachable at all | `/v1/ready` returns 503 and the replica leaves rotation |
| Redis unreachable | Cache, rate limits, quotas and spend fall back to per-replica memory; `/v1/ready` reports `redis: false` but stays 200 |
| Policy, routing or pricing file missing, or no service tokens | Outside `local`: the gateway refuses to start, naming what is missing. Locally: built-in tables (routing, pricing) or deny-all (policy), with a warning |
| An unexpected error in the gateway itself | 500 problem+json with the request id, counted in `requests_total` |
