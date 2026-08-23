# air-llm

A gateway that isolates AI applications from rapidly changing LLM APIs. Provides a unified interface, chain-based failover, cost tracking, and observability across OpenAI, Anthropic, Gemini, Ollama, and any OpenAI-wire-compatible self-hosted backend (LM Studio, vLLM, TGI). Designed for enterprise workloads requiring vendor independence, reliability, and auditability.

## Why This Exists

LLM providers frequently change:
- API shapes and SDKs
- Rate limits and pricing
- Model availability and SLAs
- Error codes and retry semantics

Embedding provider-specific logic directly into application code creates tight coupling and makes provider switches expensive and risky. This project implements the **air-llm** gateway pattern to decouple application logic from volatile LLM provider SDKs and APIs.

## Key Capabilities

- **Unified API contract** — One entry point (`POST /v1/inference`) for chat and embeddings across all providers
- **Provider adapters** — Ollama, Anthropic, OpenAI, Gemini, and one slot for any OpenAI-wire-compatible self-hosted backend — labeled `llama.cpp` (its `llama-server`, `make llamacpp-up`) after the concrete backend actually configured there, the same way `ollama` is named, not after the generic category; LM Studio also works unchanged if pointed at that slot instead, vLLM/TGI are the real UAT/Prod candidates. Each priced correctly (free for the local/self-hosted ones) via its own `Provider` identity, not just a different URL on the OpenAI adapter
- **Routing** — A DevOps-owned alias → ordered-provider-chain config (`deploy/routing.yaml`); **static per release, not latency/cost/SLA-adaptive** — that's a later phase, not shipped
- **Failover** — A per-attempt deadline plus walking the configured chain on error or timeout; **no circuit breaker** (no failure-history state, no half-open probe, no retry/backoff) — see `docs/02-lld.md` §10 for what's tracked as a follow-on
- **Observability** — Structured request logs, Prometheus metrics, and per-call cost attribution
- **Security** — Per-service API tokens, RBAC policy, and rate limiting (own control plane, no external identity provider yet)

Ideal for teams building multi-agent systems, RAG pipelines, or any AI application that must survive provider switches without code changes — with the routing/failover caveats above in mind for anyone evaluating it against "adaptive routing" or "circuit breaker" as a checklist item today.

## Relationship to `air-infra`

`air-llm` extracts and replaces the model gateway currently embedded in `air-infra`
(`src/air_infra/gateway/`). `air-infra` is scoped to local-development convenience only
(Redis/Postgres/Mongo/Ollama containers) and is never promoted to production; `air-llm`
is the standalone, independently-deployed service that actually serves model traffic in
every environment, reached directly by consumers — never through `air-infra`.

## Docs

- [`docs/overview.md`](docs/overview.md) — need, architecture, design, implementation, demo guide, gaps, and roadmap in one place — start here
- [`docs/00-plan.md`](docs/00-plan.md) — problem, goals, decisions
- [`docs/01-hld.md`](docs/01-hld.md) — high-level design
- [`docs/02-lld.md`](docs/02-lld.md) — implementation contract

