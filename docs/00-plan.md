# AIR Model Gateway — Scope and Decisions

**Companion docs:** [Overview](overview.md) · [HLD](01-hld.md) · [LLD](02-lld.md) · [Deployment](03-deployment.md) · [Integration](04-integration.md) · [Handover](05-handover.md)

## 1. Problem

Every AIR service needs model access: local models for cheap, private, high-volume work,
cloud models for quality. Wired into each service separately, that means provider SDKs
and API keys in every repo, failover and retry logic written five times, no
platform-wide view of cost, and a single local Ollama process as a silent point of
failure.

air-model-gateway puts that logic in one independently deployed service that every
consumer reaches directly, in every environment.

## 2. Goals

| # | Goal | How it is met |
|---|---|---|
| G1 | One gateway for every model call | `POST /v1/generate` and `POST /v1/embed`, the same for every provider |
| G2 | Routing and failover | A model class or embedding name → ordered `(provider, model)` chain with a per-attempt deadline |
| G3 | Resilience | Circuit breaker per rung; jittered retry for transient failures |
| G4 | Cost visibility | Every call priced from a DevOps-owned table; unpriced spend counted |
| G5 | Own control plane | Service tokens, RBAC policy, rate limits, all in-process |
| G6 | Observability | Structured logs, audit line per call, Prometheus metrics |
| G7 | Horizontal scale | Cache, rate limits, quotas and spend shared through Redis |
| G8 | Standalone | No runtime dependency on any other AIR service, in any environment |
| G9 | Multi-project governance | Provider quotas, per-service daily budgets, grants for model classes, embedding models, providers and pins, per-service cache, all in the policy file |

## 3. Non-goals

- **Hosting models.** The gateway calls model servers (Ollama, llama.cpp, vLLM, cloud
  APIs); it does not run them.
- **Brokering other infrastructure.** Databases and queues are out of scope.
- **Prompt management or evaluation.** Prompts belong to the consumers.
- **Adaptive routing.** Chains are static per configuration; no latency- or
  cost-driven reordering.
- **Multi-region routing.** One deployment per environment.

## 4. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Routes | One route per operation: `/v1/generate` (text, optionally structured) and `/v1/embed` (vectors); both share one code path (`api/v1/model_calls.py`) | Each contract carries only its own fields, so generated clients and docs are precise; the shared path keeps the control plane identical |
| Streaming | `stream: true` on `/v1/generate`, as Server-Sent Events (`start`, `delta`, `done`, `error`); failover only before the first token; bookkeeping at the end | The convention callers know (OpenAI, Ollama); a model switch mid-answer would splice two answers, so a failure after the first token is reported, not hidden |
| Vocabulary | Routes, policy actions, metric labels and the audit line use the same names (`generate`, `embed`) | A grant reads the same as the call it allows; an unknown action fails the policy file at load |
| Two schemas | Public wire contracts (`schemas/generate.py`, `schemas/embed.py`) separate from the internal router/adapter contract (`schemas/models.py`) | The wire can evolve (or gain a gRPC transport) without touching adapters |
| Routing config | DevOps-owned YAML (`routing.yaml`, `pricing.yaml`, policy) | A model change is an edit and a restart, not a release |
| Model choice | Callers choose a class (`local`, `auto`, `cloud`; default `auto`) plus an optional preferred provider, never a model name; the owner maps classes to chains, per service through `overrides` | Consumers state where the model may run, which is their concern; which model is the owner's. Tuning one service never moves another's |
| Embedding models | Permanent versioned names, `<local|cloud>-<dims>-v<version>`, one model each, required on `/v1/embed` | Dimensions alone don't make vectors comparable; a name that never changes meaning keeps every index valid, and a model change is a new version plus a re-index |
| Exact models | `pin: provider:model`, granted per service (the operator console) | Testing a physical model without opening model names to applications |
| Consumer access | Direct HTTPS with a service token; consumers may use their own client | Same shape in every environment; the wire contract is the integration point |
| Self-hosted identity | A distinct `Provider.SELF_HOSTED` (labelled `llama.cpp`), not OpenAI with another URL | Pricing keys off provider identity, so local models can never be billed at cloud rates |
| Policy outside local | `default_allow` is honoured only when `environment=local` | A local policy file mounted in prod by mistake still fails closed |
| Retry ownership | Provider SDK retries off; one router-level retry, transient errors only | One observable retry layer instead of hidden retries multiplying per rung |
| Breaker scope | Per `(provider, model)`, per replica | A broken model doesn't take down its provider's healthy ones; no network hop per attempt |
| Shared state | Redis optional; in-process when unset; degrade (never fail) when unreachable | A laptop needs nothing extra, and a Redis outage costs cache hits, not availability |
| Secrets | Env vars, or files in `SECRETS_DIR` named like the variables | Covers Vault Agent, Kubernetes secrets and cloud CSI drivers without a vendor SDK |
| Local stack | Own Docker Compose; optionally borrow air-infrastructure-service's Redis; laptop-only harnesses in `deploy/local/`, apart from what gets deployed | Runnable standalone; shared infrastructure stays a local convenience; nobody mistakes a harness for an environment |
| Production platform | GKE (Autopilot by default), one GCP project per environment | The platform's target; Autopilot leaves node management to Google |
| Packaging | One Helm chart, one values file per environment | Same templates for staging, prod and the local kind cluster |
| Exposure | In-cluster ClusterIP plus a NetworkPolicy; no Ingress | Every consumer runs in the cluster; nothing to expose publicly |
| Model servers | Separate deployments; the gateway gets their URLs | Keeps "the gateway calls models, it doesn't host them" |
| Production secrets | Secret Manager through the GKE add-on into `SECRETS_DIR`; direct Workload Identity | No keys in Kubernetes, CI or Terraform state, except the Redis URL Terraform creates |
| Production Redis | Memorystore with AUTH and TLS | Managed and private; the gateway already degrades through its maintenance |
| Config delivery | `deploy/` files into a ConfigMap via `--set-file` | One source for routing, pricing and policy; a change is a deploy, not a build |
| Release flow | Build once, deploy staging by digest, promote the same digest to prod after approval | Prod runs exactly what staging tested |
| Dependencies | `uv.lock`; image and dev installs from hash-checked exports | The image ships exactly what the tests ran against |
| Spend control | Per-service daily budget (soft, 429 `budget_exceeded`) and provider quotas across services | Shared provider accounts and budgets survive many projects on one gateway |
| Budgets required | Outside local, a service that can reach a paid model must have a `daily_budget_usd`, or the gateway won't start | On a shared gateway one uncapped service is unlimited spend on everyone's provider accounts |
| Provider account failures | A refused key or account is the gateway's fault (502), counted per reason, and shown in readiness | Failover otherwise hides it, at a higher price, and a pin would blame the caller (MG-009) |
| Build identity | Every build reports its git revision (`/v1/health`, `build_info`) | A test or an incident must be able to name the code that answered (MG-010) |
| Cache scope | Per calling service | No project sees another's answers, at some cost in hit rate |
| `finish_reason` | Normalized `stop/length/refusal/other`, raw kept | Consumers branch on one vocabulary whichever provider answered |
| Misconfiguration | Refuse to start outside local | A crash-looping pod with a reason beats every call answering 401/403 |

## 5. Status

| Area | State |
|---|---|
| Gateway (G1–G9) | Implemented; verified live against Ollama, llama.cpp, Gemini and Anthropic, with all five AIR consumers |
| Contract | `docs/openapi.json`, generated and drift-tested |
| Production packaging | Helm chart, GKE values, Terraform, CI/CD: validated on a laptop (kind, schema and Terraform validation); not yet applied to GCP |
| Assurance | `make verify` gates, runtime smoke tests; see [05-handover.md](05-handover.md) |
| Not yet verified live | The OpenAI provider (no key), vLLM/TGI on GPU hosts, anything needing a real GCP project |

## 6. Future work

In rough priority order:

1. Stand up staging, then prod, on GCP following [03-deployment.md](03-deployment.md).
   Proves what a laptop can't: Terraform apply, the Secret Manager add-on, Memorystore
   TLS, Managed Prometheus and CI's Workload Identity Federation.
2. Alerting on breaker, Redis-error, unpriced-model, rung-failure (`auth`, `adapter`), budget and quota metrics, and latency SLOs.
3. Replace `gemini-3.1-pro-preview` with its GA successor when available.
4. Validate the self-hosted slot against vLLM or TGI on GPU hosts.
5. OpenTelemetry trace export.
6. Policy write endpoints, if policy must change without a restart.
7. Per-environment routing files (the setting already supports a different path per
   environment).
8. A per-request deadline (`timeout_ms` on the wire) that bounds the whole chain.
   Today each attempt has its own deadline, so a consumer with a tight budget relies on
   its own client timeout, and the gateway may finish and bill a call nobody awaits.
