---
project: air-model-gateway
tagline: Central model gateway for the AIR platform — one HTTP entry point for text generation and embeddings across Ollama, Anthropic, OpenAI, Gemini, and any self-hosted OpenAI-compatible backend.
status: Feature-complete. Verified live against Ollama, llama.cpp, Gemini and Anthropic with all five AIR consumers. Production packaging for GKE (Helm, Terraform, CI/CD) validated on a local kind cluster; not yet deployed to GCP.
last_updated: 2026-10-25
companion_docs: [00-plan.md, 01-hld.md, 02-lld.md, 03-deployment.md, 04-integration.md, openapi.json]
---

# air-model-gateway — Project Overview

The one-stop guide for engineers adopting or working on the gateway: what it is, how a
request flows, what every file and folder is for, and how to run, test and operate it.
The design documents go deeper: [scope and decisions](00-plan.md), [high-level
design](01-hld.md), [low-level design](02-lld.md), [GKE deployment runbook](03-deployment.md).
Teams calling the gateway start with the [integration guide](04-integration.md) and the
[OpenAPI contract](openapi.json).

## 1. What it is

Every AIR service needs models: some local and free, some cloud and billed. Without a
gateway, each service would carry its own provider SDKs, keys, failover, retry and cost
logic, and a single local Ollama process would be a silent point of failure for all of
them.

air-model-gateway is that logic, once. Services call `POST /v1/generate` for text
(chat, classification, summaries, structured JSON; streamed on request) or
`POST /v1/embed` for vectors. Callers never name a model: for text they choose a
**class**, `local` (our own servers; the prompt stays in), `cloud`, or `auto` (the
gateway's choice, the default), optionally preferring a provider; for vectors they
choose a **published embedding model** such as `local-768-v1`. The gateway:

- **Authenticates and authorizes the caller.** Each service has its own token, and a
  policy file says which operations (`generate`, `embed`) it may run, how fast, and how
  much it may spend per day, and which classes, embedding models and providers it may
  choose.
- **Resolves the choice to an ordered failover chain** of `(provider, model)` pairs (the
  owner decides which models serve each class, per service if needed) and
  walks it. Each attempt has a deadline, rungs that keep failing are skipped by a
  circuit breaker, and the chain is retried with backoff when the failure was transient.
- **Prices every call** from a DevOps-owned price table, so cost is reported per call,
  per service, per model.
- **Caches identical chat requests per service,** so a repeat costs nothing.
- **Protects shared provider accounts** with per-provider quotas across all services.
- **Emits metrics and one audit line per call.**

Routing, pricing and permissions are YAML files owned by DevOps. Adding a model,
reordering a chain or repricing is a file edit and a restart, never a code release.

## 2. Architecture

```
  air-orchestrator-service   air-classifier-service   air-tools   air-rag   air-web-console
              │                         │                 │          │             │
              └────────────────────┬────┴─────────────────┴──────────┴─────────────┘
                                   │ POST /v1/generate · /v1/embed · X-API-Key
                                   ▼
                 ┌──────────────────────────────────┐        ┌───────────────────────┐
                 │        air-model-gateway         │◀──────▶│ Redis (optional):      │
                 │ AuthN · Policy · Limits · Budget │        │ cache, limits, quotas, │
                 │  Router · Cost · Cache · Audit   │        │ spend                  │
                 │                                  │        └───────────────────────┘
                 └──┬─────────┬──────────┬──────────┘
                    │         │          │
                Ollama   llama.cpp    Anthropic · OpenAI · Gemini
               (local)   (any OpenAI-  (cloud)
                         compatible
                         server)
```

**Request flow** for one call:

1. **Request ID.** Middleware takes a well-formed `X-Request-ID` or assigns one, and
   counts the request.
2. **AuthN.** `X-API-Key` is resolved to a service name (401 if unknown).
3. **Policy.** Is this service granted this operation (`generate` or `embed`)? (403 if
   not.)
4. **Rate limit.** The service's token bucket (429 `rate_limited` if empty).
5. **Model choice.** The class, preferred provider, `pin` or embedding name must be within
   the service's grant (403 if not); the catalog turns it into a chain from
   `routing.yaml` (422 if it means nothing, e.g. an unpublished embedding name).
6. **Cache (`/v1/generate` only).** On a hit for this service, return the stored answer with
   `cached: true` and `cost_usd: 0`.
7. **Budget.** If the service has spent its `daily_budget_usd`, 429 `budget_exceeded`.
8. **Router.** Try each rung of the chain under the per-attempt
   deadline:
   - a rung whose circuit breaker is open, or whose provider quota is spent, is skipped;
   - the first answer wins;
   - if every rung failed and at least one failure was transient, back off and walk the
     chain again.
9. **Pricing, spend, cache store and audit.** Price the usage and add it to the
   service's daily spend, store the answer (unless it was a refusal or cut off), emit a
   `model.call` log line and metrics, then reply with a normalized `finish_reason`.

If the whole chain fails, the caller gets 502, or 429 when every rung was throttled.

With `stream: true`, steps 1–8 are the same, and a rung counts as answering once its
first token arrives. The answer then streams as Server-Sent Events (`start`, `delta`s,
`done`); step 9 runs when the provider finishes, before `done`. A failure after the
first token can't fail over: it ends the stream with an `error` event.

## 3. Consumers and their model choices

| Service | Token (local) | Grant | Uses |
|---|---|---|---|
| air-classifier-service | `air-classifier-dev` | generate, embed · 300/min | `local` (Tier 2, summaries), `auto` (Tier 3), `cloud` (arbitration), each tuned for it |
| air-orchestrator-service | `air-orchestrator-service-dev` | generate, embed · 600/min | `cloud` (customer and business answers), tuned for it |
| air-tools | `air-tools-dev` | generate · 300/min | `cloud` (agent steps) |
| air-rag | `air-rag-dev` | embed · 600/min | `local-768-v1` only (its index) |
| air-web-console | `air-client-dev` | generate, admin_read · 60/min | any class, and `pin` for exact models (operator console) |

The classes mean the same for every service unless `deploy/routing.yaml` tunes them
for one (`overrides`): the classifier's `local` never fails over and its `auto` is
cloud-only, because its own ladder already tried local; the orchestrator's `cloud`
falls back to a stronger model than the default. Tuning one service never moves
another's. `GET /v1/capabilities` shows each service its own chains.
`tests/test_gateway_config.py` pins this table: removing a grant, a class or an
embedding model a consumer uses fails the build.

Consumers call the gateway through their own small HTTP clients, which plug into their
own metrics and request-ID propagation. The wire contract is the
integration point; `air_model_gateway_client` in this repo is a reference client.

## 4. Project structure

```
air-model-gateway/
├── README.md                     Entry point: what it is, quick start, links
├── AGENTS.md                     Rules for changing the code, for engineers and any agent
├── CLAUDE.md                     Imports AGENTS.md for Claude Code
├── pyproject.toml                Package metadata, dependency ranges, ruff/mypy/pytest/coverage
├── uv.lock                       The locked dependency set (source of truth; `make lock`)
├── requirements.lock             Hash-pinned runtime export of uv.lock; the image installs it
├── requirements-dev.lock         The same plus dev tools; `make dev` installs it
├── .dockerignore                 Allowlist: only what the image needs reaches the build
├── Makefile                      Every dev/ops command (`make help` lists them)
├── Dockerfile                    Production image: multi-stage, non-root, health-checked
├── docker-compose.yml            Local stack: gateway + optional Ollama and Prometheus
├── .env.example                  Every setting, documented; copy to .env
├── .gitignore
├── .github/workflows/
│   ├── ci.yml                    make check, make gke-validate, image build on every push/PR
│   └── deploy.yml                Build → staging (deploy + smoke) → promote digest → prod
│
├── deploy/                       DevOps-owned configuration (edit + restart, no release)
│   ├── routing.yaml              What each class (local, auto, cloud) and embedding model
│   │                             means: ordered chains, per-service overrides
│   ├── pricing.yaml              USD per million tokens per model; local models are free
│   ├── policy/
│   │   ├── policy.local.yaml     Who may call what, and per-service rate limits (local)
│   │   ├── policy.staging.yaml   The same, with explicit grants only (fail-closed)
│   │   └── policy.prod.yaml      Production grants (same as staging)
│   │
│   │  ── what gets deployed (DevOps-owned) ──
│   ├── helm/air-model-gateway/   The Helm chart (one chart, every Kubernetes environment)
│   │   ├── values.yaml           Production-safe defaults, every knob commented
│   │   └── templates/            Deployment, Service, ServiceAccount, ConfigMap (routing,
│   │                             pricing, policy), SecretProviderClass (Secret Manager),
│   │                             HPA, PDB, NetworkPolicy, PodMonitoring, `helm test` pod
│   ├── gke/                      The real environments: per-environment values and scripts
│   │   ├── values-staging.yaml   Staging on GKE (fill the <placeholders> from Terraform)
│   │   ├── values-prod.yaml      Production on GKE
│   │   ├── bootstrap-secrets.sh  Issues consumer tokens into Secret Manager; rotation
│   │   └── smoke.sh, smoke.py    Authenticated in-cluster smoke test + NetworkPolicy check
│   ├── terraform/                GCP resources
│   │   ├── modules/air-model-gateway/  Memorystore, Secret Manager secrets, Workload
│   │   │                               Identity IAM: what the gateway owns
│   │   ├── modules/gke-platform/       VPC, Cloud NAT, Autopilot cluster, Artifact
│   │   │                               Registry: only for a project with no platform yet
│   │   └── environments/{staging,prod}/  One root per environment (GCS state)
│   │
│   │  ── laptop test harnesses (never deployed) ──
│   └── local/
│       ├── integration/          Compose: 2 replicas + nginx on :9083 with the staging
│       │   │                     config, for consumers on this laptop (`make integration-*`)
│       │   ├── compose.yml       The stack (ENVIRONMENT=staging, shared Redis)
│       │   ├── nginx.conf        Round-robin load balancer; X-Served-By shows the replica
│       │   ├── make-secrets.sh   Generates tokens + SECRETS_DIR files (gitignored)
│       │   └── smoke.sh          Smoke test: gateway, then any running consumers
│       ├── kind/                 The production Helm chart on a kind cluster (`make kind-*`)
│       │   ├── cluster.yaml      1 control plane + 2 workers in two "zones"
│       │   ├── redis-tls.yaml    TLS + AUTH Redis standing in for Memorystore
│       │   ├── values-kind.yaml  Production values minus what kind can't provide
│       │   └── up.sh             Build, load, secrets, helm install
│       └── prometheus/
│           └── prometheus.yml    Local Prometheus scrape config (`make up-observability`)
│
├── docs/
│   ├── overview.md               This guide
│   ├── 00-plan.md                Scope: goals, non-goals, key decisions, delivery status
│   ├── 01-hld.md                 High-level design: components, flows, failure behaviour
│   ├── 02-lld.md                 Low-level design: settings, contracts, module internals
│   ├── 03-deployment.md          GKE runbook: provisioning, deploy, rotate, operate
│   ├── 04-integration.md         For consuming teams: access, calls, errors, retries
│   ├── 05-handover.md            Assurance: gates, traceability, findings, open items
│   ├── 06-consumer-migration.md  Per-service steps to the new routes and streaming
│   ├── air-model-gateway-design.pdf  Visual design, printable (`make design`)
│   ├── design/                   Its source: diagrams.py draws diagrams/*.svg,
│   │                             design.html lays out the pages
│   └── openapi.json              The API contract, generated (`make openapi`), tested
│
├── src/air_model_gateway_client/  Reference Python client: its own import package (no
│   │                             server imports), shipped in the same wheel
│   ├── __init__.py               Exports AirModelGatewayClient, AirModelGatewayError
│   └── client.py                 generate() / embed() over the two routes; typed errors
│
├── src/air_model_gateway/        The service (import name = project name, underscored)
│   ├── __init__.py               Package version
│   ├── main.py                   Composition root: create_app() builds every component once
│   │                             and hangs it on app.state; refuses to start on bad config
│   │                             outside local; lifespan opens/closes clients
│   ├── config.py                 Settings (env vars, .env, SECRETS_DIR files) by group
│   ├── constants.py              Enums: Environment, Action (policy actions = operations:
│   │                             generate, embed, admin_read), Provider
│   ├── redis_store.py            The one Redis connection, with a breaker so a dead Redis
│   │                             is skipped cheaply; used by cache, limiter, spend ledger
│   │
│   ├── api/                      HTTP layer
│   │   ├── middleware.py         Request ID, access log, request metrics, last-resort 500
│   │   ├── deps.py               Guards: require_principal, enforce_policy, enforce_rate
│   │   ├── errors.py             AppError types → RFC 9457 problem+json responses
│   │   ├── openapi.py            What the published OpenAPI adds: problem+json errors,
│   │   │                         the ServiceToken scheme, X-Request-ID
│   │   └── v1/
│   │       ├── router.py         Mounts the v1 routers under /v1
│   │       ├── generate.py       POST /v1/generate (thin: auth, then model_calls)
│   │       ├── embed.py          POST /v1/embed (thin: auth, then model_calls)
│   │       ├── model_calls.py    The path every model call takes: policy, rate limit, model
│   │       │                     access, cache, budget, router, pricing, spend, audit,
│   │       │                     finish_reason normalization; streams as Server-Sent Events
│   │       ├── system.py         /v1/health, /v1/ready, /v1/capabilities
│   │       └── admin.py          GET /v1/admin/policy (effective policy, read-only)
│   │
│   ├── schemas/                  Pydantic models
│   │   ├── generate.py           Wire contract of /v1/generate: GenerateInput / Output,
│   │   │                         and the stream's events (StreamStart, StreamDelta,
│   │   │                         GenerateSummary)
│   │   ├── embed.py              Wire contract of /v1/embed: EmbedInput / EmbedOutput
│   │   ├── models.py             Internal contract router ↔ adapters (named after provider
│   │   │                         APIs): ChatRequest, EmbeddingsRequest, Usage, …
│   │   ├── gateway.py            routing.yaml and pricing.yaml file shapes
│   │   ├── policy.py             Policy file shape and policy decisions
│   │   ├── system.py             Health, Readiness, Capabilities response shapes
│   │   └── common.py             Principal (the authenticated caller), Problem
│   │
│   ├── security/
│   │   └── apikeys.py            Service-token → service-name verification
│   ├── policy/
│   │   ├── engine.py             RBAC decisions; loads the policy file; fails closed off-local
│   │   ├── ratelimit.py          Token buckets: in-memory, or Redis (atomic Lua script);
│   │   │                         per-service limits and provider quotas
│   │   └── budget.py             Daily spend per service (memory or Redis) for budgets
│   │
│   ├── gateway/                  Everything about reaching models
│   │   ├── base.py               ModelProvider protocol, ProviderResult, ProviderError
│   │   ├── models.py             Model catalog: a class, preference, pin or embedding name
│   │   │                         → the chain to run (from routing.yaml)
│   │   ├── router.py             Runs a chain: failover walk, retries, circuit breakers,
│   │   │                         provider-quota admission
│   │   ├── resilience.py         CircuitBreaker and jittered exponential backoff
│   │   ├── cache.py              Response cache: in-memory, or Redis with memory fallback
│   │   ├── cost.py               PriceBook: usage → USD from pricing.yaml
│   │   └── providers/            One adapter per provider, all behind ModelProvider
│   │       ├── _common.py        Shared plumbing: provider errors, transient-error test, schema prompt, …
│   │       ├── ollama_provider.py       Ollama over plain HTTP (keep_alive on every call)
│   │       ├── anthropic_provider.py    Claude: cached system blocks, forced-tool JSON
│   │       ├── openai_provider.py       OpenAI: JSON mode plus schema instruction
│   │       ├── gemini_provider.py       Gemini: native response_json_schema; token-counted embeddings
│   │       └── self_hosted_provider.py  Any OpenAI-compatible server (llama.cpp, vLLM, TGI)
│   │
│   └── observability/
│       ├── logging.py            structlog setup (console locally; JSON with a Cloud
│       │                         Logging `severity` in shared envs)
│       └── metrics.py            Every Prometheus metric the gateway exports
│
└── tests/                        Unit and in-process end-to-end; no network, server or keys
    ├── conftest.py               Shared app/client fixtures (ASGI transport, test tokens)
    ├── test_client_end_to_end.py Reference client → real app → real router + routing.yaml
    ├── test_routes.py            /v1/generate, /v1/embed: dispatch, cache, cost, errors, audit
    ├── test_streaming.py         stream: true: events, failover before the first token,
    │                             mid-stream errors, idle deadline, cache replay, client
    ├── test_system.py, test_readiness.py  Health, readiness (providers + Redis), capabilities
    ├── test_gateway.py           Router: failover, deadlines, 429-vs-502 rule
    ├── test_models.py            Catalog: classes, overrides, preference, pins, embed names
    ├── test_resilience.py        Circuit breaker, backoff, retries, transient classification
    ├── test_cache.py             Cache keys, memory bounds, Redis sharing and fallback
    ├── test_ratelimit.py         Token buckets in memory and in Redis (shared across replicas)
    ├── test_policy.py            Grants, default-allow only in local, missing file denies
    ├── test_gateway_config.py    Shipped YAML loads, every model priced, consumer contract
    │                             (classes, embedding models, grants in every policy file)
    ├── test_config.py            Settings from env vars and SECRETS_DIR files
    ├── test_logging.py           JSON logs carry a Cloud Logging severity
    ├── test_openapi.py           docs/openapi.json matches the code and is valid OpenAPI
    ├── test_governance.py        Model choice vs grants, budgets, quotas, cache scope,
    │                             500s, metric labels, startup checks
    ├── test_repo_consistency.py  Docs name only real settings/targets/metrics; links resolve
    └── test_<provider>_provider.py  One file per adapter, against faked SDK clients
```

**Where to start reading the code:** `main.py` (how the pieces fit), then
`api/v1/generate.py` and `api/v1/model_calls.py` (one request end to end), then
`gateway/router.py` (failover, retries and breakers).

## 5. Running it locally

Requirements: Python 3.12. Optional: Docker, a native Ollama (`ollama serve`), cloud
API keys.

```bash
make dev                    # .venv with the exact locked runtime, provider SDKs and dev tools
cp .env.example .env        # then add any cloud keys you have
ollama serve & make ollama-pull   # the Ollama models routing.yaml uses

make run                    # host process on :8083, auto-reload, native Ollama
make run REDIS=infra        # same, with shared state in air-infrastructure-service's Redis
make up                     # the production image under Docker Compose instead
```

| | `make run` | `make up` |
|---|---|---|
| Runs | uvicorn from `.venv`, auto-reload | the production Docker image |
| Best for | day-to-day development, debugging | checking the shippable image; containerised callers |
| Callers reach it at | `http://localhost:8083` | `http://localhost:8083`, or `http://host.docker.internal:8083` from containers |

Options for both: `OLLAMA=native|container` and `REDIS=none|infra`. With `REDIS=infra`,
Redis must be reachable on `localhost:6379` (run `make up` in air-infrastructure-service).
If it isn't, the gateway still serves and `/v1/ready` reports `"redis": false`.

### Try it

```bash
curl localhost:8083/v1/ready                         # which providers (and Redis) answer
curl -H "X-API-Key: air-classifier-dev" localhost:8083/v1/capabilities

# Free local call (cost_usd 0)
curl -X POST localhost:8083/v1/generate -H "X-API-Key: air-classifier-dev" \
  -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"Say hi"}],"max_tokens":50}'

# Structured output on a cloud chain. Leave max_tokens at 300 or more: Gemini Flash
# spends part of the budget on thinking tokens before the visible JSON.
curl -X POST localhost:8083/v1/generate -H "X-API-Key: air-classifier-dev" \
  -H "Content-Type: application/json" \
  -d '{"model":"cloud","max_tokens":300,
       "messages":[{"role":"user","content":"Classify sentiment: I love this product!"}],
       "json_schema":{"type":"object","properties":{"label":{"type":"string"}},"required":["label"]}}'

# Embed: one vector per input text
curl -X POST localhost:8083/v1/embed -H "X-API-Key: air-rag-dev" \
  -H "Content-Type: application/json" \
  -d '{"model":"local-768-v1","input":["hello","world"]}'

curl -s localhost:8083/metrics | grep ^air_model_gateway   # cost, tokens, breakers, retries
```

## 6. Testing

| Level | Command | Needs |
|---|---|---|
| Every static gate (definition of done) | `make verify` ([05-handover.md](05-handover.md) §2) | uv, Docker, helm, kubeconform, terraform |
| Unit + in-process end to end | `make check` (ruff, mypy strict, pytest) | nothing |
| Real process, no providers | the smoke run below | nothing |
| Live | `make run`, then the calls above | Ollama and/or cloud keys |
| Integration with consumers | `make integration-up`, `make integration-test` (§7) | Docker, Redis |
| Production packaging | `make gke-validate` (chart lint, schemas, Terraform) | helm, kubeconform, terraform |
| Production chart, running | `make kind-up`, `make kind-test` (§7) | Docker, kind, helm |

Every unit test runs without network access. Providers are faked at the SDK boundary,
and Redis is faked with `fakeredis`, which runs the real Lua rate-limit script.
`tests/test_client_end_to_end.py` is the best single file to read: it drives the
reference client through the real app, router and shipped `routing.yaml`.

Smoke-testing the real process with nothing behind it:

```bash
AIR_MODEL_GATEWAY_OLLAMA__BASE_URL=http://127.0.0.1:1 \
AIR_MODEL_GATEWAY_SELF_HOSTED__BASE_URL=http://127.0.0.1:1/v1 \
AIR_MODEL_GATEWAY_PROVIDERS__GEMINI_API_KEY= AIR_MODEL_GATEWAY_PROVIDERS__ANTHROPIC_API_KEY= \
AIR_MODEL_GATEWAY_SECURITY__SERVICE_TOKENS=smoke:air-orchestrator-service \
  .venv/bin/uvicorn air_model_gateway.main:app --port 18083 &
curl -s localhost:18083/v1/health        # 200
curl -s localhost:18083/v1/ready         # 503, every provider false
curl -s -X POST localhost:18083/v1/generate -H 'X-API-Key: smoke' \
  -H 'Content-Type: application/json' \
  -d '{"model":"local","messages":[{"role":"user","content":"hi"}]}'   # 502 naming each rung
```

## 7. Running it in a shared environment

One image, configured entirely by environment variables or secret files:

| Setting | Value |
|---|---|
| `AIR_MODEL_GATEWAY_ENVIRONMENT` | `staging` or `prod`. The policy then fails closed: no `default_allow`. |
| `AIR_MODEL_GATEWAY_POLICY__CONFIG_PATH` | A policy file with an explicit grant for every consumer; it also sets provider quotas, daily budgets and each service's model-choice grants. |
| `AIR_MODEL_GATEWAY_SECURITY__SERVICE_TOKENS` | Real tokens, from the secrets store. |
| `AIR_MODEL_GATEWAY_PROVIDERS__*_API_KEY` | Provider keys, from the secrets store. |
| `AIR_MODEL_GATEWAY_REDIS__URL` | Required before scaling past one replica. |
| `AIR_MODEL_GATEWAY_LOG__RENDER` | `json` |
| `AIR_MODEL_GATEWAY_SECRETS_DIR` | Optional. A mounted directory of secret files (Vault Agent, Kubernetes secrets, cloud secret-store CSI), one file per variable. |

Outside `local` the gateway refuses to start (`ConfigError`, naming what is missing) if
the policy, routing or pricing file is absent, no service tokens are configured, the
policy grants an embedding name routing doesn't publish, or routing names a paid model
pricing doesn't list. At boot it also logs `config.unknown_keys` for any
`AIR_MODEL_GATEWAY_*` variable no setting reads (a typo or a retired key).

Probes: liveness `GET /v1/health`, readiness `GET /v1/ready`. Readiness is 503 only when
no provider answers; Redis being down is reported but never takes a replica out of
rotation. Scrape `GET /metrics`.

**Alerting starting points:**
- `air_model_gateway_circuit_open == 1` for minutes: a model is down.
- Rising `air_model_gateway_redis_errors_total`: the gateway is running degraded.
- Any `air_model_gateway_model_unpriced_total`: spend that isn't being counted.
- Any `air_model_gateway_rung_failures_total` with `reason="auth"` (a provider refused
  the gateway's key or account) or `reason="adapter"` (an adapter bug): failover hides
  both from callers.
- p95 of `air_model_gateway_request_duration_seconds` above its SLO.
- A rising 5xx share of `air_model_gateway_requests_total`: failing chains or gateway bugs.
- Any `air_model_gateway_budget_rejected_total` or
  `air_model_gateway_provider_quota_rejected_total`: a budget or quota is being hit.

### Local integration stack

`deploy/` separates what gets deployed (`helm/`, `gke/`, `terraform/`, plus the
routing, pricing and policy files every target reads) from laptop test harnesses
(`local/`), which are never deployed. Real staging is GKE.

`make integration-up` runs the production image with the staging configuration, so
consumers running on the same laptop can be integration-tested against a
production-shaped gateway (kind can't serve them: its gateway is in-cluster only):
- two replicas behind nginx on `:9083`;
- `ENVIRONMENT=staging`, so the policy fails closed, with `policy.staging.yaml`;
- generated `amg_int_…` tokens and the provider keys delivered as `SECRETS_DIR` files;
- shared state in air-infrastructure-service's Redis on `:6379`, under its own key
  prefix;
- JSON logs.

| Command | What it does |
|---|---|
| `make integration-up` / `integration-down` | Start (generating secrets on first run) / stop |
| `make integration-test` | Gateway checks (readiness, Redis, both replicas, 401/403/200 per grant, model-choice rules, `finish_reason`), plus a classifier and an orchestrator turn when those services are running |
| `make integration-rotate` | Issue new tokens and restart the gateways on them |
| `make integration-logs` / `integration-ps` | JSON logs / status |

Each consumer's token is in `deploy/local/integration/tokens.env`
(`AIR_CLASSIFIER_SERVICE_TOKEN`, `AIR_WEB_CONSOLE_TOKEN`, …). To point a consumer at the
stack, start it with the gateway URL `http://localhost:9083` and its token, under the
setting names its own `.env.example` documents:

```bash
set -a; . ../air-model-gateway/deploy/local/integration/tokens.env; set +a
# then, from the consumer's repo, its gateway URL and token settings, e.g.
<CONSUMER>_GATEWAY_URL=http://localhost:9083 <CONSUMER>_GATEWAY_TOKEN=$AIR_CLASSIFIER_SERVICE_TOKEN make run
```

### Production on GKE

The production target is GKE, inside the cluster only: consumers in the same cluster
call `http://air-model-gateway.air-model-gateway.svc.cluster.local` with their token.
There is no Ingress, and a NetworkPolicy admits only the consumer namespaces. The full
runbook is [03-deployment.md](03-deployment.md); in short:

| Piece | Where | What it provides |
|---|---|---|
| Helm chart | `deploy/helm/air-model-gateway` | Hardened Deployment (non-root, read-only root FS, drain on shutdown), HPA, PDB, zone spread, NetworkPolicy, Managed Prometheus scrape |
| Values | `deploy/gke/values-{staging,prod}.yaml` | Per-environment image, model-server URLs, Redis TLS, Secret Manager project, consumer namespaces |
| Config | `deploy/routing.yaml`, `pricing.yaml`, `policy/policy.<env>.yaml` | Passed with `--set-file` into a ConfigMap; a change rolls the pods |
| Secrets | Secret Manager → GKE add-on (CSI) → `SECRETS_DIR` | Tokens, provider keys, Redis URL and CA; no keys in Kubernetes or GitHub |
| GCP | `deploy/terraform` | Memorystore (AUTH + TLS), secrets + IAM via Workload Identity, optional VPC/Autopilot/Artifact Registry |
| CI/CD | `.github/workflows/deploy.yml` | One build, staging deploy + smoke, the same digest promoted to prod after approval |

Ollama and the self-hosted slot are separate model-server deployments; the gateway only
gets their in-cluster URLs.

**On a laptop**, `make kind-up` runs the same chart with production values on a local
kind cluster (two workers in two "zones"), with a TLS + AUTH Redis standing in for
Memorystore, `policy.prod.yaml`, and secrets as a Kubernetes Secret. `make kind-test`
runs the same smoke test CI runs against GKE, plus a check that a pod outside the
consumer namespaces can't connect. `make kind-down` removes the cluster.

## 8. Known limits and next steps

- **A stream the caller abandons isn't billed to its budget.** The gateway stops the
  provider, but usage is unknown at that point; the provider still bills what it
  generated. `streams_total{outcome="client_disconnected"}` shows how often.
- **One self-hosted slot.** One OpenAI-compatible backend per environment; becomes a map
  only if two are ever needed at once.
- **Policy is read-only at runtime.** `/v1/admin/policy` shows the effective policy;
  changing it is a file edit and a restart.
- **No distributed tracing.** Request IDs propagate and are logged, but there is no
  OpenTelemetry export.
- **`gemini-3.1-pro-preview` is a preview model.** It leads the classifier's `cloud`
  class; replace it with the GA name when Google ships one (routing.yaml and
  pricing.yaml).
- **Untested live:** the OpenAI provider (no key yet) and vLLM/TGI on real GPU hosts.
- **No per-request deadline.** Each attempt has its own deadline; a caller bounds the
  whole call with its own client timeout (future work in `00-plan.md` §6).
- **Next step:** provision staging on GCP by following [03-deployment.md](03-deployment.md):
  Terraform, then `bootstrap-secrets.sh`, then the Deploy workflow. The chart, values,
  secrets layout and smoke test have run on kind; what hasn't run yet is anything that
  needs a real GCP project (Terraform apply, the Secret Manager add-on, Memorystore's
  CA, Managed Prometheus, Workload Identity Federation for CI).

## 9. Troubleshooting

Start from the `request_id` (in every error body and the `X-Request-ID` response
header). Search the logs for it; the `model.call` line or the `provider.failed` lines
tell you what happened.

| Symptom | Usual cause | Fix |
|---|---|---|
| 401 `unauthorized` | Token from another environment, or not in `SERVICE_TOKENS`; on GKE, a rotated token the gateway hasn't loaded | Check the consumer's token config; after rotation, `rollout restart` the gateway |
| 403 `policy_denied` | No grant for that operation in this environment's policy file. Locally, unlisted services pass only through `default_allow` | Add the grant to all three `policy.*.yaml` files and the `CONSUMERS` table in `tests/test_gateway_config.py` |
| 429 `rate_limited` | The service's `rate_per_minute`, or every rung throttled (provider 429s or the gateway's `provider_quotas`) | `ratelimited_total` / `provider_quota_rejected_total`; raise the grant's rate or quota, or slow the caller |
| 429 `budget_exceeded` | The service spent its `daily_budget_usd` | `service_cost_usd_total`; raise the budget in the policy file or wait for the next UTC day |
| 403 "may not use the … class" / "may not prefer" / "may not pin" | The choice is outside the service's grant | Grant it in the policy files (`model_classes`, `preferred_providers`, `pin`), or choose within the grant (`/v1/capabilities` shows it) |
| 422 `invalid_model_choice` | An unpublished embedding name, a provider the class doesn't offer, a malformed `pin`, a pin to an unpriced model or one its provider doesn't have | `/v1/capabilities` lists what exists; publish a new embedding name in `routing.yaml`, or price the model in `pricing.yaml`, if needed |
| 422 `provider_rejected` | Every rung's provider refused the request as invalid (too long, a parameter the model won't take) | `detail` has each provider's reason; the caller changes the request |
| Pod crash-loops with `ConfigError` | Missing policy/routing/pricing file, no service tokens, an unpublished embedding grant, or an unpriced paid model in routing | The message names what is missing |
| 502 `provider_unavailable` | Every rung failed for a reason other than the request; `detail` names each one | A missing key, Ollama unreachable, a retired model name, or a breaker open (`air_model_gateway_circuit_open`). Fix the cause, or reorder the chain in `routing.yaml` |
| `/v1/ready` 503 | No provider answers at all | Check the Ollama / self-hosted URLs and that at least one cloud key is set |
| `"redis": false` in `/v1/ready` | Wrong URL or password, TLS CA not mounted, or Redis down | Harmless meanwhile (degraded cache, per-replica rate limits); `redis.failed` log lines name the error |
| Structured output doesn't parse | Answer cut off (`finish_reason: length`); thinking models use part of `max_tokens` | Raise `max_tokens` (300+); treat cut-off answers as no answer |
| Calls slower than expected | A slow first rung, or failover after a deadline | The response's `provider`/`model` shows who answered; `air_model_gateway_provider_timeouts_total` counts deadlines |
| `cost_usd` is 0 for a cloud model | The provider reported a model `pricing.yaml` doesn't list (configured rungs and pins are checked) | `air_model_gateway_model_unpriced_total` counts these; add the price |
| A local setting seems ignored | A misspelt or retired key (e.g. a `.env` copied from an old `.env.example`) | The `config.unknown_keys` warning at startup names it; re-copy `.env.example` |
| GKE pod stuck in `ContainerCreating` | A secret listed in `secrets.gcp.items` has no version, or Workload Identity IAM is missing | `kubectl describe pod` shows the CSI mount error; see [03-deployment.md](03-deployment.md) §4.2 |
| Consumer times out connecting on GKE | Its namespace isn't allowed by the NetworkPolicy | Add it to `networkPolicy.allowedNamespaces` (or label the namespace) |
| A consumer's answers change after a routing change | The models behind its class changed | Intended if the change was; tune that service alone with an `overrides` entry if not |
