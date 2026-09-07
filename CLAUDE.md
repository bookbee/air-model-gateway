# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`air-model-gateway` is a standalone LLM gateway service (FastAPI, Python 3.12): one HTTP entry point
(`POST /v1/inference`) for chat *and* embeddings across Ollama, Anthropic, OpenAI, Gemini, and
one self-hosted OpenAI-wire-compatible slot. It carries its own control plane (service-token
authN, RBAC policy, rate limiting), routing/failover, cost accounting, and observability.

The hard design constraint: **air-model-gateway has no runtime dependency on `air-infra`, in any
environment.** `air-infra` is local-dev-only and never runs in prod; air-model-gateway serves model
traffic everywhere and is reached directly by consumers. Don't add anything that couples them.

## Commands

`make help` lists every target and the overridable vars. Everything runs out of `.venv`.

**Run `make check` (lint + typecheck + test) before calling work done.**

Single test / single case:

```bash
.venv/bin/pytest tests/test_gateway.py -q
.venv/bin/pytest tests/test_gateway.py::test_alias_failover_walks_the_chain -q
```

`asyncio_mode = "auto"` — async tests need no `@pytest.mark.asyncio`. Endpoint tests go through
`httpx.ASGITransport` with the `client` fixture in `tests/conftest.py` (which builds the app via
`create_app(settings)`, not the module-level `app`).

Ports: host `8083` → container `8080`; ollama `11434`; llama-server `8181`; prometheus `9090`.

## Architecture

Request path: `api/middleware.py` (request-id + metrics) → `api/deps.py`
(`require_principal` → `enforce_policy` → `enforce_rate`) → `api/v1/inference.py` →
`gateway/router.py` → a provider adapter → `gateway/cost.py` pricing → metrics + `model.call` log.

- **`main.py::create_app`** is the composition root. Every collaborator (policy, verifier,
  limiter, router, price book, cache) is built once there and hung on `app.state`; endpoints
  reach them through `request.app.state`, never through module globals. New shared components
  follow that pattern.
- **Two request contracts, deliberately.** `schemas/inference.py` is the *public wire* contract
  (`InferenceRequest`/`InferenceResponse`, `task` discriminating chat vs. embeddings, meant to
  survive a future gRPC transport). `schemas/models.py` is the *internal* contract the router
  and adapters speak (`ChatRequest`/`EmbeddingsRequest`/`ProviderResult`), ported unchanged from
  air-infra. `api/v1/inference.py` translates between them. Keep them separate.
- **`constants.py::Action`** is both the policy action and the wire `task` value — one enum, not
  two vocabularies. air-model-gateway has exactly one policy resource, `inference` (plus `admin`).
- **Router** (`gateway/router.py`) resolves a model string to an ordered chain: an alias from
  `deploy/routing.yaml`, or `"provider:model"` to bypass aliases, or a bare name against the
  default provider. `_run` walks the chain, converting a per-attempt `asyncio.wait_for` timeout
  into the same failover as a `ProviderError`. There is **no retry, backoff, or circuit
  breaker** — don't describe the system as having one.
- **Providers** implement the `ModelProvider` Protocol in `gateway/base.py` (`chat`,
  `embeddings`, `health`, `aclose`). Cloud SDKs are imported lazily in `_ensure()` so a missing
  key or uninstalled SDK surfaces as a clean `ProviderError`, not an import crash. `health()`
  uses a short (~2s) deadline of its own because it backs `/v1/ready`.
- **`Provider.SELF_HOSTED` has value `"llama.cpp"`** — it names today's concrete backend, the
  way `ollama` does, not the generic category. It is a separate class from `OpenAIProvider`
  rather than that class with a different `base_url`, because `PriceBook.is_free()` keys off
  `Provider` identity: conflating them risks billing a free local model at cloud rates. If the
  slot's backend changes (vLLM/TGI), rename the label; the settings slot
  (`config.py::SelfHostedSettings`) stays generic.
- **Errors** are RFC 9457 problem+json. Raise an `AppError` subclass from `api/errors.py`
  (`Unauthorized`, `Forbidden`, `RateLimited`, `ProviderUnavailable`, …); handlers installed in
  `install_error_handlers` render them and attach the request id.

## DevOps-owned config vs. code

`deploy/routing.yaml` (alias → failover chain), `deploy/pricing.yaml` (USD per **million**
tokens), and `deploy/policy/policy.local.yaml` are configuration, not code: adding a model,
reordering a chain, or repricing is a file edit plus a restart — never a release. Each loader
falls back to a built-in table (`router.DEFAULT_ALIASES`, `cost.DEFAULT_PRICING`) when the file
is absent, so a bare checkout still boots; keep the two in rough sync when you change either.
`tests/test_gateway_config.py` asserts the shipped YAML loads and every cloud model reachable
from a chain has a price.

These files carry substantial inline rationale (why a chain is local-only, why an alias isn't
just pointed at `smart`, why a rung was removed). Read the comments before reordering anything —
the ordering is usually a decision, not an accident.

Settings (`config.py`) are pydantic-settings with prefix `AIR_MODEL_GATEWAY_` and `__` nesting
(`AIR_MODEL_GATEWAY_OLLAMA__BASE_URL`). `.env.example` documents every key. Under compose,
`environment:` in `docker-compose.yml` overrides `.env`.

## Conventions

- Comments here explain *why*, at length, especially where a choice looks arbitrary or wrong at
  a glance (the `NoDecode` on `service_tokens`, `-c 4096 --parallel 1` in the Makefile, no
  `response_format` on the self-hosted adapter). Match that register: when you change one of
  those decisions, update the comment that justifies it.
- Known gaps are tracked in `docs/02-lld.md` §10 (Redis-backed cache and rate limiter, retry +
  circuit breaker, policy writes, OTel export). Don't silently implement one as a side effect of
  another task.
- Cache (`gateway/cache.py`) keys on every field that changes what the provider is asked for —
  `json_schema`/`schema_name` included, or a hit hands back JSON shaped for the wrong caller.
  Refusals are never cached.

## Docs

`docs/overview.md` is the digest (need, architecture, demo, gaps); `docs/00-plan.md`,
`01-hld.md`, `02-lld.md` are the source of truth when they disagree with it.
