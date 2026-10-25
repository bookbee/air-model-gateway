# AIR Model Gateway — Handover and Assurance

**Companion docs:** [Overview](overview.md) · [Plan](00-plan.md) · [HLD](01-hld.md) ·
[LLD](02-lld.md) · [Deployment](03-deployment.md) · [Integration](04-integration.md)

What a team taking over needs beyond the design docs: how confidence in this code is
established, the gates every change passes, where the spec lives, what has been
verified, and what is still open. Written to work with any editor, coding agent or
spec-driven workflow.

## 1. How assurance works here

A review that "finds nothing more" is not evidence; each deeper pass finds something.
Confidence instead comes from **gates that fail loudly and keep failing until a defect
is fixed**, so every finding becomes a permanent check:

1. **The spec is written down.** Requirements and decisions (`00-plan.md`), design
   (`01-hld.md`, `02-lld.md`), the wire contract (`openapi.json`) and behaviour
   configuration (`deploy/*.yaml`).
2. **The tests are the executable spec.** Each requirement and each guarded risk has a
   test. Sections 4 and 6 map them.
3. **The docs can't drift.** `tests/test_openapi.py` regenerates the contract and
   compares it. `tests/test_repo_consistency.py` checks every env var, make target,
   metric and link the docs name.
4. **Builds are reproducible.** `uv.lock` plus hash-checked exports; the image ships
   exactly what was tested.
5. **Runtime behaviour is checked where it runs.** Smoke tests on the production chart
   (kind) and the local integration stack, against real providers and real consumers.
6. **Consumers check the contract from outside.** air-web-console's `gateway-check` make target
   sends its contract scenarios through its own proxy to a running
   gateway. Its `bugs/model-gateway/` folder holds one file per finding, with a
   scenario that keeps failing until the fix lands.
7. **Every result names its build.** `/v1/health` reports the git `revision`
   (`-dirty` for uncommitted code). A result without one proves nothing: during review
   an old process on the same port answered a test run while the files had changed.

Each finding becomes a test here, or a scenario in the console, or both. A fix also
creates new edges (MG-009 came out of the MG-005 fix), so a release is decided by the
gate in §2a, run on one frozen commit. "The last review found nothing" doesn't count.

## 2. The gates

| Gate | Command | Proves | Runs in |
|---|---|---|---|
| Lint, types | `make check` | ruff clean, mypy `--strict` clean | CI, laptop |
| Tests + coverage | `make check` | Every behaviour below; coverage ≥ 90% (floor in `pyproject.toml`) | CI, laptop |
| Contract | `make check` (`test_openapi.py`) | `docs/openapi.json` equals the code and is valid OpenAPI 3.1 | CI, laptop |
| Docs consistency | `make check` (`test_repo_consistency.py`) | Docs name only real settings, targets, metrics; links resolve; image context is an allowlist | CI, laptop |
| Lock | `make lock-check` | `uv.lock` and its exports match `pyproject.toml` | CI, laptop |
| Supply chain | `make audit` | No known vulnerabilities in the locked runtime set | CI, laptop |
| Scripts, image | `make lint-scripts` | shellcheck and hadolint clean | CI, laptop |
| Deployment config | `make gke-validate` | Chart lints; staging/prod/kind manifests pass schemas; Terraform validates | CI, laptop |
| **All static gates** | **`make verify`** | All of the above, in one command | CI, laptop |
| Production chart | `make kind-up kind-test` | Production values on Kubernetes: TLS Redis, prod policy, NetworkPolicy, grants, model-choice rules | Laptop |
| Integration | `make integration-up integration-test` | Two replicas, shared Redis, real providers, and running consumers end to end | Laptop |
| Deployed | `make gke-smoke` | The same checks inside GKE, after each deploy | CI (Deploy workflow) |

**Definition of done for any change:** `make verify` passes; if the change affects
runtime behaviour, deployment or a consumer-visible contract, the kind and integration smoke
tests pass too; the docs describing the change are updated in the same commit.

## 2a. Release and handover gate

Run in order on one commit (tag it, e.g. `v0.2.0-rc1`), with one session per repository.
Any failing step stops the release.

| # | Step | Pass when |
|---|---|---|
| 1 | Freeze: commit and tag; `git status` clean | `/v1/health` `revision` has no `-dirty` |
| 2 | `make verify` | "verify: all gates passed" |
| 3 | `make kind-up kind-test` (prod values) and `make integration-up integration-test` (staging config) | All gateway checks pass; consumer checks pass for every migrated consumer |
| 4 | One gateway on `:8083` (`lsof -nP -iTCP:8083 -sTCP:LISTEN`), started without `--reload`; then air-web-console's `gateway-check` make target | Contract all pass; its report header shows the tag's revision |
| 5 | Bugs: air-web-console `bugs/model-gateway/README.md` | No open High; each Medium `verified` or `wont-fix` with a reason |
| 6 | Owner decisions (§7) | Committed values, not starting values |
| 7 | Record evidence | §5 below updated with the date, revision and results; the console's `last-run-*.txt` committed |

The console's `bugs/model-gateway/READINESS.md` is the consumer's copy of this gate,
with where each step stands.

## 3. Working with any harness or spec-driven workflow

The knowledge is in plain files, not in any tool's memory:

| Need | Where |
|---|---|
| Rules for changing the code (any agent or engineer) | `AGENTS.md` (`CLAUDE.md` imports it) |
| Requirements, non-goals, decisions with reasons | `docs/00-plan.md` §2–4 |
| Components, flows, failure behaviour | `docs/01-hld.md` |
| Settings, contracts, module internals, metrics | `docs/02-lld.md` |
| The API contract | `docs/openapi.json` |
| Behaviour that is configuration | `deploy/routing.yaml`, `pricing.yaml`, `policy/*.yaml` (inline rationale) |
| Why a line of code looks odd | The comment above it; this codebase explains decisions where they are made |

A spec-driven change runs in this order:
1. **Spec:** update `00-plan.md` (requirement or decision) and the HLD or LLD section.
2. **Contract:** change `schemas/`, then `make openapi`.
3. **Tests:** write the failing test.
4. **Code:** make it pass.
5. **Docs and checklists:** follow `AGENTS.md`'s checklist for the kind of change.
6. **Verify:** `make verify`.

If step 1 is skipped, `test_repo_consistency.py` and `test_openapi.py` still catch the
mechanical drift, but not the reasoning. Write the decision down.

**One agent per repository at a time.** Two sessions editing the same files silently
overwrite each other. A change another repository needs goes to that repository's own
session.

**Anything learned goes into a file and a check**, never only a chat, an agent's memory
or a session transcript. If a lesson can't be a test, it goes in the comment where the
code makes the choice, or in §8.

## 4. Traceability

| Goal (`00-plan.md`) | Where | Proven by |
|---|---|---|
| G1 One gateway for every model call | `api/v1/generate.py`, `embed.py`, `model_calls.py`; `schemas/generate.py`, `embed.py` | `test_routes.py`, `test_streaming.py`, `test_openapi.py`, `test_client_end_to_end.py` |
| G2 Routing and failover | `gateway/router.py`, `deploy/routing.yaml` | `test_gateway.py`, `test_gateway_config.py` |
| G3 Resilience | `gateway/resilience.py`, router | `test_resilience.py`, `test_gateway.py` (unexpected errors fail over) |
| G4 Cost visibility | `gateway/cost.py`, adapters' usage | `test_gateway_config.py` (every model priced), provider tests (refusals billed, embedding totals) |
| G5 Own control plane | `api/deps.py`, `policy/`, `security/` | `test_policy.py`, `test_governance.py`, `test_ratelimit.py` |
| G6 Observability | `api/middleware.py`, `observability/` | `test_governance.py` (bounded labels, counted 500s), `test_logging.py` |
| G7 Horizontal scale | `redis_store.py`, Redis cache/limiter/ledger | `test_cache.py`, `test_ratelimit.py`, `test_governance.py`; integration smoke (two replicas) |
| G8 Standalone | `main.py`, deploy | No AIR imports or URLs in `src/`; kind runs with nothing else |
| G9 Multi-project governance | `schemas/policy.py`, `policy/budget.py`, router admission | `test_governance.py`, `test_models.py`, `test_gateway_config.py`; kind and integration smoke (model-choice rules) |

## 5. Last verification (2026-10-26, commit `40ee62b`)

On a laptop, with real Ollama, llama.cpp, Gemini and Anthropic, against `40ee62b`
(revision `40ee62beb778`, clean), the release candidate. Tag that commit; a tag on a
docs-only follow-up needs no re-run.
- **`make verify`** on the committed tree: all gates passed; 354 tests, 95.21% coverage.
- **air-web-console's `gateway-check`** (one gateway on `:8083`, no reload): contract
  42/42; the MG-011 known gap (`gap-gemini-thinking-cap`) passing. On `4dd28b7`, before
  the MG-011 fix: 42/42, and the MG-006 to MG-010 manual repros all passed.
- **MG-011, live:** `pin` `gemini:gemini-2.5-flash` at `max_tokens` 120, 300 and 600
  and uncapped: every answer complete (`stop`), about 30 output tokens and $0.00008
  (uncapped was 850 tokens and $0.0021); streamed the same; a structured `cloud` call
  ~1.1 s. Re-run on `40ee62b` with the same result.
- **MG-009, live** (on `4dd28b7`, invalid Gemini key): a pin is 502 naming the key
  problem; `cloud` fails over to Anthropic; `/v1/ready` reports `gemini: false`;
  `rung_failures_total{reason="auth"}` counts every refused call.
- **`make integration-test`** and **`make kind-test`** ran on the same tree just before
  the commit (revision `4dd28b76537b-dirty`), which differed only in the `.cache/` tool
  config:
- **`make integration-test`** (staging policy with budgets, two replicas, Redis): all 18
  gateway checks passed.
- **`make kind-test`** (prod policy with budgets, TLS Redis, NetworkPolicy): 17 of 17,
  and an unlisted namespace can't connect.
- **Resilience,** last exercised on the same stacks before these fixes and not re-run:
  - a rolling restart under continuous load failed 0 of 1,331 requests;
  - with Redis frozen, calls were still served, readiness was unaffected, and recovery
    was automatic;
  - failover to llama.cpp worked, and breakers opened on a dead Ollama.

Not verifiable without GCP: Terraform apply, the Secret Manager add-on, Memorystore's
CA, Managed Prometheus, and CI's Workload Identity Federation
([03-deployment.md](03-deployment.md) §7). Re-run the gate in §2a and update this
section before each handover or release.

## 6. Risks and their guards

The failure modes this codebase is built to prevent, each with the guard and the test
that fails if the guard is removed. Read this before "simplifying" any of them.

| Risk | Guard | Pinned by |
|---|---|---|
| Generated secrets (`deploy/local/*`) baked into an image | Allowlist `.dockerignore`; Dockerfile copies config files by name | `test_repo_consistency.py` |
| Consumers misreading truncation because providers spell `finish_reason` differently | Normalized `finish_reason`, raw value in `provider_finish_reason` | `test_governance.py` |
| The image shipping untested or vulnerable dependency versions | `uv.lock` plus hash-checked installs; `make lock-check`, `make audit` | CI `check`, `supply-chain` |
| An adapter bug or SDK change crashing a request instead of failing over | The router treats any exception as a rung failure; adapters parse inside their error boundary | `test_gateway.py`, `test_ollama_provider.py` |
| Gateway bugs invisible to alerts | The middleware renders and counts unhandled 500s, with the request id | `test_governance.py` |
| Unbounded metric series (random URLs, caller-chosen model names) | Fixed route labels with one `<unmatched>`; callers choose classes, not models (`pin` is granted) | `test_governance.py` |
| One busy service exhausting a shared provider account | `provider_quotas`, one bucket per provider across services | `test_governance.py`, `test_gateway.py` |
| Runaway spend | `daily_budget_usd` per service, required outside local for any service that can reach a paid model; unpriced models counted | `test_governance.py`, `test_gateway_config.py` |
| A revoked or invalid provider key silently moving traffic to a pricier provider, or blamed on the caller | Account failures are `auth` (502, never `rejected`), counted in `rung_failures_total`, and turn the provider false in `/v1/ready` | `test_gateway.py` |
| Embedding vectors filed under the wrong text | One vector per input text is checked before the answer leaves | `test_governance.py` |
| Not knowing which code answered a test or an incident | `revision` in `/v1/health`, `/v1/capabilities`, the startup log and `build_info` | `test_system.py` |
| A misconfigured deploy that "works" but answers 401/403 to everyone | `ConfigError` at startup outside local | `test_governance.py` |
| A routing or policy change silently breaking a consumer | `CONSUMERS` table: every class, embedding model and grant each consumer uses, in every policy file | `test_gateway_config.py` |
| A class quietly sending data somewhere it promised not to | `local` chains may only use local providers, `cloud` only cloud ones; checked when routing loads | `test_gateway_config.py` |
| An index corrupted by a changed or mismatched embedding model | Permanent versioned names, one model each, no fallback; vectors checked against the name's dimension | `test_gateway_config.py`, `test_routes.py` |
| Projects seeing each other's answers | Cache key includes the calling service | `test_governance.py` |
| Spend under-reported | Refusals keep their usage; embeddings set `total_tokens` | Provider tests |
| A streamed answer silently spliced from two models, or a provider left generating for nobody | Failover only before the first token; later failures are `error` events; a disconnect closes the provider stream | `test_streaming.py` |
| An option the gateway doesn't implement silently ignored (e.g. `stream` answered with one JSON body) | Wire inputs and `Message` forbid unknown fields: 422 naming the field | `test_routes.py` |
| Hostile or oversized input reaching providers or logs | Bounded `X-Request-ID`; `role`, `temperature`, `max_tokens` validated; 422 never echoes input | `test_routes.py`, `test_openapi.py` |
| Docs drifting from the code | Contract regenerated and compared; docs' settings, targets, metrics, links and source tree checked | `test_openapi.py`, `test_repo_consistency.py` |

## 7. Open items

**Consumers to update:** each consumer moves to `POST /v1/generate` and
`POST /v1/embed`; per-service steps are in
[06-consumer-migration.md](06-consumer-migration.md).

**Decisions for the owner:**
- `provider_quotas` and `daily_budget_usd` in `policy.staging.yaml` and
  `policy.prod.yaml` hold **starting values** (marked in the files). Set them from each
  provider account's limits and from observed spend. A budget is required for any
  service that can reach a paid model: the gateway won't start without one.
- SLOs: availability and p95 latency per route and class, on
  `requests_total` and `request_duration_seconds`.
- The repository's canonical URL: `pyproject.toml` and the chart point at
  `github.com/bookbee/air-model-gateway`; the GitHub repository should carry that name.

**Other repositories** (for their own sessions):

| Repo | Item |
|---|---|
| air-classifier-service | Forward `X-Request-ID` to the gateway; re-check its T3 budget (8 s) now that Gemini Flash's thinking is off (~1.1 s for a structured `cloud` call, was 7–12 s) |
| air-tools, air-rag | Forward `X-Request-ID` |
| air-web-console | Its local dev token is `air-client-dev`, not its service name. Add a `staging` target pointing at `:9083`, so its `gateway-check` target also runs against the staging config |
| air-orchestrator-service | A failed model call returns HTTP 200 `degraded` with an empty `degraded` list, and stays ready |
| All consumers | Move to `POST /v1/generate` / `POST /v1/embed`; handle 429 `budget_exceeded` as "stop until Retry-After", not as a retryable throttle |

**Gateway future work:** `00-plan.md` §6 (first: stand up GCP staging; a per-request
deadline; alerting).

## 8. Operating notes

Facts about the providers and the tooling that the code and tests already account for,
and that are easy to trip over:
- **Provider behaviour drifts.**
  - Models are retired, so routing names must be checked against what each API serves.
  - Thinking models (Gemini) count thinking against the output cap. Left at Gemini's
    dynamic budget, 2.5 Flash spent a whole 300-token cap thinking (MG-011), so
    `routing.yaml` `model_settings` sets its `thinking_budget` (0) and the adapter adds
    the budget to the cap. A new thinking model in a chain needs the same decision.
  - Anthropic rejects `system: null`, and its API version `2026-10-21` rejects sampling
    parameters, so `temperature` is not sent to it.
  - Verify a change live with `make run` and a real call, not only against fakes.
- **One retry layer.** SDK retries are off; the router owns retries.
- **Cut-off answers and refusals are never cached.** A cached half-JSON answer would
  break every retry.
- **The build context is an allowlist,** because generated secrets live under
  `deploy/local/`.
- **Library upgrades can change behaviour quietly** (FastAPI reports an included
  router's route path without its prefix). Upgrade deliberately: `make lock-upgrade`,
  then `make verify`.
- **Shell portability.**
  - zsh doesn't word-split `$VAR` in commands.
  - macOS `/bin/sh` can't parse `case` patterns inside `$(...)`.
  - Generated secrets must not end in a newline.
- **`kind create` switches the current kubectl context;** `deploy/local/kind/up.sh`
  restores it.
- **Providers disguise account failures.** Google reports an invalid key as
  `400 INVALID_ARGUMENT` (`API_KEY_INVALID`), Anthropic an empty account as a 400, and
  OpenAI no credit as a 429 (`insufficient_quota`). `_common.is_auth_failure` lists
  them. Add any new one there, with a test.
- **Know which process answered.** `--reload` and a second `make run` leave several
  gateways bound to `:8083`, one serving older code. For a test run, start a single
  process without `--reload` and check `/v1/health`'s `revision`.
