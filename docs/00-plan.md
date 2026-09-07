# AIR Model Gateway — Plan

**Status:** Draft for review · **Date:** 2026-08-22
**Companion docs:** [HLD](01-hld.md) · [LLD](02-lld.md)

---

## 1. Problem statement

Model access for the AIR platform currently lives *inside* `air-infra`, as an on-path
gateway (`src/air_infra/gateway/`): provider adapters, routing/failover, cost accounting,
and a response cache, fronted by `air-infra`'s own authN/policy/rate-limit stack. That
design was the right first cut — it kept model traffic on the one path where routing,
failover, cost, and cache are possible (`air-infra` HLD §5) — but it also means model
hosting is scoped to whatever else `air-infra` happens to be (a store broker), and every
consumer of models is, transitively, a consumer of `air-infra`.

`air-model-gateway` extracts that gateway into its own service: a **central place to host and call
models**, independent of the store-broker concerns `air-infra` also carries. The
provider adapters, router, cost book, and cache already implemented and tested in
`air-infra` are the starting point, not a rewrite — they move, they don't get redesigned.

This extraction is sharpened by a separate decision about `air-infra` itself:
**`air-infra` is being scoped down to a local-development convenience only — a
one-command way to stand up Redis/Postgres/Mongo (and a local Ollama) for a laptop — and
will never be promoted to production.** In production, DevOps provisions those
dependencies directly; nothing in the platform's runtime path depends on `air-infra`
being up. `air-model-gateway` cannot inherit anything from `air-infra` at runtime — no brokered
creds, no shared Redis, no shared authN — because in production `air-infra` simply is not
there. `air-model-gateway` must stand on its own as a real, independently-deployed production
service from day one.

## 2. Goals

| # | Goal | Acceptance signal |
|---|---|---|
| G1 | Unified model API | One request/response contract for chat + embeddings across Ollama, Anthropic, OpenAI, Gemini, and any OpenAI-compatible backend |
| G2 | Intelligent routing & failover | Alias → ordered provider chain, per-attempt deadline, automatic failover on error/timeout — ported from `air-infra` unchanged |
| G3 | Cost attribution | Every call priced from a DevOps-owned table; an unpriced model is a metric, not silent free spend |
| G4 | Own control plane | `air-model-gateway` authenticates, authorizes (RBAC), and rate-limits its own callers — it does not require `air-infra` to be in front of it |
| G5 | Full observability | Structured logs, Prometheus metrics, audit trail — same shape as `air-infra`'s, so a shared dashboard/runbook works across both |
| G6 | Environment-agnostic | Local: Ollama + whatever cloud keys are set. Prod: full provider set, same image, config-selected |
| G7 | Clean migration | `air-infra` stops hosting model logic; existing consumers (`air-platform`, `air-classifier`, `air-client`) switch to a dedicated `air-model-gateway` client, configured identically in local and prod |
| G8 | Stand-alone production readiness | `air-model-gateway` runs in production with no runtime dependency on `air-infra` — its own authN, policy, rate limiting, secrets, and (eventually) its own Redis |

## 3. Non-goals (v1)

- **No store brokering.** Redis/Postgres/Mongo access stays `air-infra`'s job. `air-model-gateway`
  is models only.
- **No training or fine-tuning.** `air-model-gateway` serves inference; it does not manage model
  weights, training jobs, or evaluation pipelines.
- **No UI.**
- ~~No self-hosted inference runtime beyond Ollama in v1~~ — **landed ahead of
  schedule.** The original plan deferred a second local/self-hosted runtime to
  v2, on the assumption Ollama alone was enough for local dev. It wasn't: a
  single local Ollama process is a single point of failure by construction
  (every model it serves shares one runtime), and separately, being able to
  pick the right model per environment — dev/QA/deployment — matters
  independent of that. `Provider.SELF_HOSTED` (any OpenAI-wire-compatible
  backend — LM Studio locally, vLLM/TGI in prod) shipped in response, verified
  live against a running LM Studio instance (chat, structured output,
  embeddings). See LLD §5 for the one real incompatibility that surfaced doing
  so: self-hosted servers do not agree on `response_format` dialects the way
  real OpenAI cloud does.
- **No multi-region routing.** A single deployment per environment, same as `air-infra`
  today.

## 4. Decisions taken

| Decision | Choice | Rationale |
|---|---|---|
| Starting point | **Port, don't redesign** `ModelProvider` protocol, `ModelRouter`, `PriceBook`, `ResponseCache`, and the routing/pricing schema shapes from `air-infra` | Already implemented, tested (`test_gateway.py`, `test_gateway_config.py`), and proven against a real failover/deadline scenario. Redesigning now would just re-introduce bugs already fixed once |
| On-path position | `air-model-gateway` **is** the on-path proxy for models, reached **directly** by every consumer in every environment — never through `air-infra` | `air-infra` is local-dev-only and is not present in production, so nothing that must work in prod can route through it, even transitively |
| Control plane | `air-model-gateway` owns its **own** authN (service tokens), RBAC policy, rate limiting, and audit — ported pattern from `air-infra`'s `api/deps.py` + `policy/` | "Host models centrally" implies callers beyond just the current `air-infra`-fronted set; `air-model-gateway` cannot assume a trusted front door, and cannot assume `air-infra` is even running |
| Consumer access | A dedicated `air_model_gateway_client` SDK, configured via `AIR_MODEL_GATEWAY_URL` / `AIR_MODEL_GATEWAY_TOKEN` (or equivalent), identical shape local and prod | Mirrors `air-infra`'s own "one image, many environments" principle — the difference between a developer laptop and production is config, not code, and now not a broker hop either |
| Language / runtime | Python 3.12, FastAPI, Pydantic v2 | Matches `air-infra`; the ported modules import cleanly with no adaptation |
| Config | pydantic-settings, `AIR_MODEL_GATEWAY_*` env, `environment` selector | Same one-image-many-environments shape as `air-infra` |
| Secrets | Pluggable backend (`env`/`file` local, Vault/cloud SM prod) | Same protocol `air-infra` defines; provider API keys are exactly the kind of secret it already handles |
| Routing / pricing config | DevOps-owned YAML (`deploy/routing.yaml`, `deploy/pricing.yaml`), ported verbatim | Already the right shape — a model or price change is a config edit, not a release |

### Open questions for review

1. **Secret backend for prod.** Vault vs a cloud secrets manager for provider API keys.
   Resolved for v1: **no in-app abstraction** — provider keys come straight from
   env-populated `Settings`, exactly how `air-infra`'s own gateway reads them today
   (its `broker/secrets.py` backs credential *issuance* for stores, not its
   gateway's own keys). How those env vars land in production (Vault agent,
   cloud secrets manager) is a DevOps concern outside this app. Revisit only if
   a concrete need for in-app key rotation or per-request key selection shows up.
2. **Redis for prod cache/rate-limit (Phase 5).** Since `air-infra` never runs in
   production, `air-model-gateway` cannot rely on it for a shared Redis. Recommendation: **`air-model-gateway`
   takes its own Redis dependency**, provisioned by DevOps the same way any other
   production dependency is (matching how `air-infra`'s local Redis container is *not* a
   stand-in for a production one). Locally, `air-model-gateway` may point at the same Redis
   container `air-infra` stands up for developer convenience — that's a local config
   convenience only, not an architectural dependency.
3. **Local developer story.** With `air-infra` never promoted to prod, its remaining job
   is a one-command local stack. Should local `air-model-gateway` development also run through
   `air-infra`'s `docker-compose` (e.g. `air-infra` keeps hosting a local Ollama container
   that a locally-run `air-model-gateway` points at), or does `air-model-gateway` get its own
   `docker-compose.yml` entirely? Recommendation: **`air-model-gateway` gets its own local compose
   stack** (mirroring `air-infra`'s pattern, not depending on it) so it's runnable and
   testable standalone — matching the fact that it's the piece that actually ships to
   production.
4. **`air-infra` cleanup timeline.** `air-infra`'s `gateway/` module has active
   uncommitted work in flight (Gemini provider, readiness gating, cost accounting). This
   plan does not touch `air-infra`'s code or docs — Phase 4 below (removal) should be a
   separate, deliberate change in that repo once `air-model-gateway` is live and consumers have cut
   over, not a side effect of standing this repo up.

## 5. Phased delivery

| Phase | Deliverable |
|---|---|
| 0 | Docs (this), repo scaffold, pyproject/Makefile/compose skeleton |
| 1 | **Port** provider adapters (Ollama, Anthropic, OpenAI, Gemini), `ModelProvider` protocol, `ModelRouter` (failover + per-attempt deadline), routing/pricing schemas — unit tests ported alongside |
| 2 | API surface — `POST /v1/inference` (chat **and** embeddings, dispatched by `task`; a single entry point for every AIR service, HTTP now and gRPC later over the same fields), `/v1/health`, `/v1/ready`, `/v1/capabilities` — + own authN/RBAC/rate-limit/audit, RFC 9457 errors |
| 3 | Cost accounting (`PriceBook`) + response cache (in-memory TTL) + observability (structlog + Prometheus) |
| 4 | Ship `air_model_gateway_client` SDK; cut `air-platform` / `air-classifier` / `air-client` over to it; **remove** `air-infra`'s `gateway/` module, `schemas/gateway.py`, `api/v1/models.py`, `deploy/gateway/*` as a follow-on change in that repo |
| 5 | `air-model-gateway`'s own Redis-backed cache + rate limiter, Vault/cloud secret backend, hardening |

*`Provider.SELF_HOSTED` (LM Studio/vLLM/TGI — any OpenAI-wire-compatible backend), originally
slated for Phase 5, shipped early — see §3.*

## 6. Migration from `air-infra`

`air-infra`'s existing gateway is the reference implementation, not prior art to diverge
from. Phases 1–3 port it near-verbatim (same protocol, same failover/deadline logic, same
config shapes) so behavior does not regress.

Phase 4 is the cutover, and it is a **direct-connection swap, not a brokered one**:
because `air-infra` never runs in production, it cannot be the thing that hands consumers
an `air-model-gateway` endpoint and credentials — that indirection would work locally and silently
not exist in prod. So consumers are configured with `air-model-gateway`'s endpoint and a service
token directly (env/secrets, provisioned by DevOps in prod, `.env` locally), the same way
they will eventually get store connections once `air-infra` stops being a runtime
dependency for those too. `air-infra`'s own principle — "broker stores, proxy models" —
simplifies further: `air-infra` (local-dev only) brokers local store containers for
developer convenience, and is not on any path — model or store — that production traffic
ever crosses. `air-model-gateway` is the one production proxy, and it exists because being on-path
is what makes routing/failover/cost/cache real (the same rationale `air-infra`'s HLD §5
already gives for models over stores).

Consumer-visible contract does not change in shape — chat/embeddings request and response
schemas carry over unchanged — but the entry point does: `air-platform`, `air-classifier`,
and `air-client` move from `air_infra_client`'s `infra.models.*` to a dedicated
`air_model_gateway_client`, configured independently of whatever `air-infra` they may also use
locally for stores.
