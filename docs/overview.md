---
project: air-llm
tagline: Central LLM gateway for the AIR platform — one HTTP entry point for chat and embeddings across Ollama, Anthropic, OpenAI, Gemini, and any self-hosted OpenAI-compatible backend.
status: Built and locally verified; not yet committed, not yet integrated by any consumer, not yet deployed
last_updated: 2026-08-23
companion_docs: [00-plan.md, 01-hld.md, 02-lld.md]
---

# air-llm — Project Overview

This document is the one-stop summary: what air-llm is for, how it's built, how to
demo it, and what's left to do. For the full design record — the decisions, the
tradeoffs, the things that turned out to be wrong and were amended — see
`00-plan.md` / `01-hld.md` / `02-lld.md` in this same folder. This file is the
digest; those are the source of truth when the two disagree.

## Need

Every AIR service (air-classifier, air-platform, air-client) needs to call LLMs —
some local and free, some cloud and billed — without hardcoding which provider,
without duplicating routing/failover/cost logic per service, and without a laptop's
single local Ollama process being a silent point of failure for the whole platform.

That logic used to live inside `air-infra`, tangled up with its store-broker
responsibilities. Two decisions forced it out on its own:

1. **`air-infra` is local-development-only and will never run in production.**
   Nothing that must work in prod — models included — can depend on it, even
   transitively.
2. **A single local Ollama process is a single point of failure by construction** —
   every model it serves shares one runtime. Development, QA, and production
   legitimately want *different* model choices, not one hardcoded default.

air-llm is the answer to both: a standalone, independently-deployed gateway that
every AIR service reaches directly, in every environment, over one HTTP contract —
never through air-infra.

## Architecture

```
   air-platform      air-classifier       air-client        (any future consumer)
        │                   │                   │                    │
        └───────────────────┴── air_llm_client (SDK) ─────────────────┘
                              │ HTTPS + X-API-Key
                              ▼
                 ┌──────────────────────────────┐
                 │            air-llm             │
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

- **One entry point**: `POST /v1/inference` — chat *and* embeddings, chosen by a
  `task` field, over HTTP today and (carrying the same fields) gRPC later.
- **Five provider adapters**, each with its own `Provider` identity so pricing and
  observability never have to guess: `ollama`, `anthropic`, `openai`, `gemini`,
  and `llama.cpp` — the last one a generic slot for *any* OpenAI-wire-compatible
  self-hosted backend, proven against both LM Studio and llama.cpp's
  `llama-server`, with vLLM/TGI as the real production candidates.
- **DevOps-owned routing** (`deploy/routing.yaml`): each alias (`fast`, `smart`,
  `embed`, and air-classifier's own `classify-local`/`classify-default`/
  `classify-arbiter`) is an ordered failover chain, edited without a code release.
- **Own control plane**: authN (service tokens), RBAC policy, and rate limiting
  all live in-process — air-llm assumes nothing else is running to front it.
- **No runtime dependency on air-infra, anywhere, ever** — this is the one design
  constraint everything else was built against.

## Design

The key decisions, and why, in one line each — full rationale is in `00-plan.md`
§4 and `02-lld.md`:

- **Ported, not redesigned**, from air-infra's original gateway — the router,
  failover, and cost logic were already proven; a bug found there (see Gaps) was
  never actually correct even in the original.
- **Direct connection, not a broker hop.** Because air-infra never reaches
  production, it can't be the thing handing out air-llm endpoints/credentials —
  every consumer is configured with `AIR_LLM_URL`/`AIR_LLM_TOKEN` directly,
  identically in every environment.
- **`llama.cpp` is a concrete name, not a category.** The self-hosted provider
  identity is named after whatever's actually configured (matching `ollama`),
  not a generic `self_hosted` label — a real trade-off: it means a rename is
  needed if a different backend takes that slot later.
- **One self-hosted slot, not a map.** Covers "one alternate local/self-hosted
  runtime per environment," which is what was asked for; a named multi-backend
  map is explicitly deferred until a real concurrent-backend need shows up.
- **No response_format sent to self-hosted backends.** Self-hosted servers
  disagree on the dialect (verified live: LM Studio 400s on the same payload
  llama.cpp accepts); the schema-in-prompt instruction is the actual mechanism,
  response_format would only add a backend-specific way to fail.
- **`keep_alive` is a config default, not a per-request field.** It's an
  operational concern about a shared local runtime, not something that changes
  any individual answer — callers shouldn't need to know they're talking to
  Ollama at all.
- **No circuit breaker, deliberately not claimed as one.** A per-attempt deadline
  plus a static failover chain is what's shipped; retry/circuit-breaking is a
  named follow-on, not something the README overstates.

## Implementation

- **Stack**: Python 3.12, FastAPI, Pydantic v2, httpx, structlog, Prometheus
  client. `pyproject.toml` extras: `providers` (cloud SDKs), `dev` (ruff/mypy/pytest).
- **Footprint**: 40 source files (`src/air_llm` + `client/air_llm_client`), 10 test
  files, 71 tests — all passing, ruff and mypy `--strict` both clean.
- **Repo layout** (full detail in `02-lld.md` §1):
  - `src/air_llm/gateway/` — the router, cost book, cache, and five provider adapters
  - `src/air_llm/api/v1/inference.py` — the one endpoint
  - `src/air_llm/policy/`, `security/` — the in-process control plane
  - `client/air_llm_client/` — the consumer SDK
  - `deploy/routing.yaml`, `deploy/pricing.yaml`, `deploy/policy/policy.local.yaml` —
    DevOps-owned config
  - `Makefile`, `docker-compose.yml`, `Dockerfile` — standalone local stack, no
    dependency on air-infra's
- **Verified live**, not just unit-tested: real inference calls through Ollama,
  Gemini, and llama.cpp via the actual running Docker container; structured
  JSON output; cost accounting on a real cloud call; a provider coming online
  mid-session with no restart required; `keep_alive` confirmed against Ollama's
  own `/api/ps`.

## Demo Guide

A ~10-minute walkthrough for other engineers. Requires a laptop with Ollama
installed (`ollama serve` running) and Docker; a Gemini API key makes the cloud
half real rather than simulated.

```bash
# 1. Stand it up — native Ollama by default, zero extra setup
cp .env.example .env   # then fill in a real GEMINI_API_KEY
make up
curl http://localhost:8083/v1/health

# 2. Show it knows what's available right now
curl -H "X-API-Key: air-classifier-dev" http://localhost:8083/v1/capabilities
curl http://localhost:8083/v1/ready

# 3. A free local call — note cost_usd: 0.0
curl -X POST http://localhost:8083/v1/inference \
  -H "X-API-Key: air-classifier-dev" -H "Content-Type: application/json" \
  -d '{"task":"chat","model":"fast","messages":[{"role":"user","content":"Say hi in five words"}],"max_tokens":50}'

# 4. A real billed cloud call — note the actual cost_usd this time
curl -X POST http://localhost:8083/v1/inference \
  -H "X-API-Key: air-classifier-dev" -H "Content-Type: application/json" \
  -d '{"task":"chat","model":"classify-default","messages":[{"role":"user","content":"Say hi in five words"}],"max_tokens":50}'

# 5. Structured output — the exact pattern air-classifier depends on
# max_tokens:300, not lower — Gemini 2.5 Flash spends part of its budget on
# internal "thinking" tokens before the visible JSON, and a tighter budget
# here reliably truncates mid-object (verified: 100 truncates, 300 does not).
curl -X POST http://localhost:8083/v1/inference \
  -H "X-API-Key: air-classifier-dev" -H "Content-Type: application/json" \
  -d '{"task":"chat","model":"classify-default","messages":[{"role":"user","content":"Classify sentiment: I love this product!"}],"max_tokens":300,"json_schema":{"type":"object","properties":{"label":{"type":"string"}},"required":["label"]}}'

# 6. Prometheus metrics — cost/tokens/timeouts per provider, out of the box
curl http://localhost:8083/metrics | grep ^air_llm
```

**The strongest live moment**: point `AIR_LLM_OLLAMA__BASE_URL` at a port with
nothing listening, `make up`, show `/v1/ready` reporting `ollama: false` — then
start Ollama and hit `/v1/ready` again on the *same, unrestarted* container. It
flips to `true` and the very next inference call just works. No redeploy, no
restart — makes the "no circuit breaker, no cached failure state" design choice
tangible instead of abstract.

If llama.cpp is set up (`make llamacpp-up`), showing the same alias pick between
two genuinely independent local processes — and failing over between them when
one is down — is the second-best moment: it's the concrete answer to "what
happens when your one local model process dies."

## Current Gaps

Honestly stated, not glossed over — most of these were found by deliberately
trying to break things, not just left unconsidered:

- **No circuit breaker, no retry/backoff.** Per-attempt deadline + static
  failover only. The README used to overclaim this; it no longer does, but the
  capability itself still doesn't exist.
- **Routing is static, not adaptive.** No latency/cost/SLA-based selection —
  aliases are a hand-edited ordered list.
- **Single-replica only.** `ResponseCache` and `RateLimiter` are in-process
  dicts; nothing is shared across replicas yet (Phase 5: Redis).
- **No in-app secrets abstraction.** Provider keys come straight from
  env-populated `Settings` — a deliberate v1 choice, but it means key rotation
  or per-request key selection isn't possible without building one.
- **`smart` / `classify-arbiter` are currently broken end-to-end** in a
  no-cloud-keys dev environment: `gemini-2.5-pro` is deprecated by Google
  ("no longer available to new users") and Anthropic/OpenAI have no key
  configured. Needs a real model-choice decision, not just a config edit.
- **The self-hosted slot only serves chat right now** — the running llama.cpp
  instance has no embedding model loaded (`llama-server` serves one or the
  other per process).
- **Unpriced-model spend has a metric but no alert.** `air_llm_model_unpriced_total`
  exists; nothing watches it yet.
- **Some real behavior is unverified, only assumed reasonable**: whether every
  provider adapter cancels its underlying connection cleanly on a timeout
  (proven at the router level with a fake provider, not against real SDKs); the
  empty-routing-config fallback path and its log warning have no test.
- **Policy is read-only.** `/v1/admin/policy` dumps the effective policy; there's
  no write endpoint to change it without editing the YAML and restarting.
- **No consumer has actually cut over yet.** air-classifier's `InfraGatewayProvider`
  still hardcodes `/v1/models/chat` (air-infra's old path, not air-llm's
  `/v1/inference`) and its `gateway.enabled` flag is off — the fix is small
  (documented, one line) but it hasn't landed.
- **`ApiSettings` (`api.host`/`.port`) is declared but never read** — the real
  bind comes from the `uvicorn` CLI args in the Dockerfile/Makefile. Documented
  as reserved rather than silently wrong, but still not wired up.

## Roadmap & Future Extensions

Roughly in the order they'd likely matter to a new owner:

1. **Land the air-classifier cutover** — the `_CHAT_PATH` fix, `gateway.enabled`,
   and the `BASE_URL`/networking decision are all already scoped; this is the
   fastest way to get a real consumer proving the design under real traffic.
2. **Resolve `smart`/`classify-arbiter`'s dead Gemini model** — pick the
   replacement (`gemini-3.1-pro-preview` or otherwise) and add its price row.
3. **Phase 5: Redis-backed cache + rate limiter**, so this survives running as
   more than one replica.
4. **Retry + circuit breaker** on top of the existing per-attempt deadline —
   the README no longer claims this exists; building it is still valuable.
5. **Environment-scoped routing.yaml** (`routing.dev.yaml`, `routing.prod.yaml`,
   ...) so dev/QA/prod can genuinely differ in model choice without touching code
   — the mechanism (`AIR_LLM_GATEWAY__ROUTING_CONFIG_PATH`) already supports this.
6. **Validate vLLM or TGI against real GPU infrastructure** for the eventual
   UAT/Prod self-hosted tier — llama.cpp locally proved the integration point,
   not the production performance characteristics.
7. **Wire the unpriced-model metric to an actual alert.**
8. **Policy write endpoints**, if/when policy needs to change without a
   deploy-and-restart cycle.
9. **OTel trace export** — mentioned throughout the design docs as a marked
   TODO, not yet started.
10. **Decide the `ApiSettings` question** — wire it up (change how the app
    binds) or remove it; it shouldn't stay declared-but-inert indefinitely.

For the full, itemized version of the marked TODOs with file-level pointers,
see `02-lld.md` §10.
