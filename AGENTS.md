# AGENTS.md

Guidance for anyone changing this repository: engineers, and coding agents of any
kind (Claude Code reads it through `CLAUDE.md`; Codex, Cursor and others read it
directly). Humans start with `docs/overview.md`; `docs/05-handover.md` explains how
the project is verified and what is open.

## What this is

`air-model-gateway` is a standalone model gateway (FastAPI, Python 3.12):
`POST /v1/generate` (text, optionally structured) and `POST /v1/embed` (vectors) across
Ollama, Anthropic, OpenAI, Gemini and one self-hosted OpenAI-wire-compatible slot.
It carries its own control plane (service tokens, RBAC policy, per-service rate limits, provider quotas,
daily budgets, model-choice grants), model classes with failover, retries and circuit
breakers, cost accounting, a per-service response cache, and observability.
`docs/overview.md` §4 explains every file.

Hard constraint: **no runtime dependency on any other AIR service, in any
environment.** Redis is optional and owned by the gateway's deployment; locally
`REDIS=infra` borrows air-infrastructure-service's Redis as a convenience only.

## Definition of done

**`make verify` passes** (lint, mypy strict, tests with a 90% coverage floor, OpenAPI
drift, lock freshness, dependency audit, shellcheck, hadolint, chart and Terraform
validation). It is exactly what CI runs. For changes that touch runtime behaviour,
deployment or a consumer-visible contract, also run the runtime gates:
`make kind-up kind-test` and `make integration-up integration-test`. `docs/05-handover.md` §2
lists what each gate proves.

## Commands

`make help` lists everything. `make dev` builds `.venv` from `requirements-dev.lock`
(hash-checked). Dependencies are locked: edit `pyproject.toml`, then `make lock`
(needs uv); the image installs `requirements.lock`, never resolves versions.

- `make run` (host, reload) / `make up` (production image under Compose). `OLLAMA=native|container`,
  `REDIS=none|infra`. The Makefile exports `AIR_MODEL_GATEWAY_OLLAMA_URL`, a compose
  interpolation var that `docker-compose.yml` maps onto `AIR_MODEL_GATEWAY_OLLAMA__BASE_URL`.
- `deploy/` = what gets deployed (`helm/`, `gke/`, `terraform/`, and the routing,
  pricing and policy files) plus `local/`, laptop harnesses that are never deployed.
- `make integration-up` / `integration-test`: two replicas behind nginx on `:9083` with
  the staging config, for consumers on the laptop; generated tokens (gitignored).
- `make kind-up` / `kind-test`: the Helm chart with production values on a local kind cluster.
- `make gke-render|gke-deploy|gke-smoke`: GKE, see `docs/03-deployment.md`.
- `make openapi`: regenerate `docs/openapi.json` after any wire change.

Tests: `asyncio_mode = "auto"`. Endpoint tests use `httpx.ASGITransport` and build the
app with `create_app(settings)` (`tests/conftest.py`), never the module-level `app`.

## Architecture

Request path: `api/middleware.py` (bounded request id, metrics, last-resort 500) →
`api/v1/generate.py` or `embed.py` (`require_principal`) → `api/v1/model_calls.py`
(`admit`: policy, rate limit, model-access rule; then per-service cache, budget check) →
`gateway/router.py` (provider quota, breaker, deadline, failover, retry) → adapter →
`gateway/cost.py` → spend ledger, metrics, `model.call` audit line.

- **`main.py::create_app` is the composition root.** Every collaborator is built once
  and hung on `app.state`; endpoints reach them through `request.app.state`. Outside
  `local` it refuses to start (`ConfigError`) when the policy, routing or pricing file
  is missing or no service tokens are set.
- **Two contracts, deliberately.** `schemas/generate.py` and `schemas/embed.py` are
  the public wire contracts; `schemas/models.py` is the internal router/adapter
  contract, named after the provider APIs (`ChatRequest`, `adapter.chat()`,
  `EmbeddingsRequest`); `api/v1/model_calls.py` translates. Every route goes through
  `model_calls` (`admit`, then `generate` or `embed`), so routes can't drift apart. The
  wire `finish_reason` is normalized (`stop|length|refusal|other`), the raw value is
  `provider_finish_reason`.
- **Streaming** (`stream: true` on `/v1/generate`): Server-Sent Events from
  `model_calls.open_generate_stream`. Everything before the first token (cache,
  budget, `router.open_chat_stream` with failover) can still fail as plain HTTP; after
  it, failures are `error` events and no failover happens. Adapters implement
  `chat_stream` (text deltas, then one final `ProviderResult`).
- **One vocabulary.** The routes, the policy actions (`constants.py::Action`:
  `generate`, `embed`, `admin_read`), the metric `kind` label and the audit line's
  `operation` all use the same names. Policy files are typed: an unknown action fails
  at load.
- **Model choice, then router.** Callers never name models. `/v1/generate` takes a class
  (`local`, `auto`, `cloud`; default `auto`) plus an optional preferred `provider`, or a
  granted `pin` (`provider:model`); `/v1/embed` takes a published embedding name
  (`<local|cloud>-<dims>-v<version>`, required, one model, never a chain).
  `gateway/models.py::ModelCatalog` turns the choice into a chain from
  `deploy/routing.yaml` (global classes, per-service `overrides`); the policy grants
  which choices a service may make. The router only runs chains: `_walk` tries each
  rung under a per-attempt deadline, skipping rungs whose
  `(provider, model)` breaker is open or whose provider quota is spent; any unexpected
  adapter exception is a rung failure, never a 500. `_run` retries the chain once with
  jittered backoff only if some failure was retryable. SDKs use `max_retries=0`; the
  router is the one retry layer. All-failed is 429 only if every rung was throttled,
  else 502.
- **Shared state degrades, never fails.** No Redis URL: in-memory cache, limiter and
  spend ledger. With Redis: the Redis versions, each falling back to memory on
  `RedisUnavailable`. All Redis access goes through `RedisStore.run` (timeout, metric,
  breaker). `/v1/ready` reports Redis, never gates on it.
- **Governance lives in the policy file** (`schemas/policy.py`): grants,
  `rate_per_minute`, `model_classes`, `embed_models`, `preferred_providers`, `pin`,
  `daily_budget_usd` per service; `provider_quotas`
  across services. Policy fails closed outside `local`.
- **Secrets** may be files in `AIR_MODEL_GATEWAY_SECRETS_DIR`, named like the env var
  (`config.py::_SecretsDirSource`). Env beats files beats `.env`.
- **Providers** implement `gateway/base.py::ModelProvider`. Cloud SDKs import lazily in
  `_ensure()`. Every failure becomes a `ProviderError` with `retryable` from
  `_common.is_transient`; refusals keep their usage (they are billed).
- **`Provider.SELF_HOSTED` is `"llama.cpp"`** and a class of its own, because pricing
  keys off provider identity (local models must never bill at cloud rates).
- **Errors** are RFC 9457 problem+json: raise an `AppError` subclass from `api/errors.py`
  and declare it on the route with `responses=problems(...)`.

## Change checklists

- **Wire contract** (`schemas/generate.py`, `schemas/embed.py`, a route, an error):
  `make openapi`; update the reference client (`src/air_model_gateway_client`),
  `docs/04-integration.md`, and tell consuming teams (the diff of `docs/openapi.json`
  is the changelog).
- **New consumer or grant:** all three `deploy/policy/policy.*.yaml`, the `CONSUMERS`
  table in `tests/test_gateway_config.py`, `deploy/local/integration/make-secrets.sh`
  and `smoke.sh`, `deploy/gke/bootstrap-secrets.sh`, `smoke.py`, `deploy/local/kind/up.sh`, the
  Terraform `consumers` variable, and `networkPolicy.allowedNamespaces` in the GKE
  values.
- **Model, chain or price:** `deploy/routing.yaml` / `pricing.yaml` (read the inline
  rationale first; ordering is usually a decision). A `local` chain may only use local
  providers and a `cloud` chain only cloud ones (enforced at load). Never repoint a
  published embedding name: publish a new version. Keep `models.DEFAULT_ROUTING` and
  `cost.DEFAULT_PRICING` roughly in sync (local fallback only).
- **New provider:** an adapter in `gateway/providers/`, a `Provider` member, wiring in
  `main.py`, prices, a test file against a faked SDK, and docs §6 of the LLD.
- **Dependency:** `pyproject.toml`, `make lock`, `make verify`.
- **Chart, values or Terraform:** `make gke-validate`, then `make kind-up kind-test`.
  After changing a Terraform provider version, re-lock for every platform:
  `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64`
  in each `deploy/terraform/environments/<env>`.

## Conventions

- Comments explain *why*, at length, where a choice looks arbitrary (the `NoDecode` on
  `service_tokens`, `-c 4096 --parallel 1`, no `response_format` on the self-hosted
  adapter). When you change such a decision, change its comment.
- Docs describe the current system only, no history. `tests/test_repo_consistency.py`
  keeps env vars, make targets, metrics and links in the docs honest.
- The cache key covers every field that changes the question (schema included) and the
  calling service. Refusals and cut-off answers are never cached.
- Future work is `docs/00-plan.md` §6; don't implement an item as a side effect.
- One agent per repository at a time: two sessions editing the same files overwrite
  each other's work. Cross-repo changes go to that repo's own session.

## Docs

`docs/overview.md` (team guide) · `00-plan.md` (scope, decisions) · `01-hld.md` ·
`02-lld.md` · `03-deployment.md` (GKE runbook) · `04-integration.md` (consumers) ·
`05-handover.md` (assurance, open items) · `openapi.json` (the contract). The plan,
HLD and LLD win when they disagree with the overview.
