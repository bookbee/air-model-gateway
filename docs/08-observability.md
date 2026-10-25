# AIR Model Gateway — Production Observability (Proposal)

**Status:** proposed, for the owner's review. Nothing here is implemented yet; §11
lists the decisions to confirm before development starts.

**Companion docs:** [Plan](00-plan.md) · [LLD](02-lld.md) (§11 lists every exported
metric) · [Deployment runbook](03-deployment.md) (§5 logs, metrics and alerts) ·
[Handover](05-handover.md) · [AI firewall proposal](07-ai-firewall.md) (§9, its own
metrics)

The aim: DevOps and support can see, without reading code, whether the gateway is
**up, fast, affordable and correct**, and are told in time to act, by the right
channel and with a runbook. The gateway already records most of the raw signal. What
is missing is the operating layer on top of it (SLOs, alerts, dashboards, runbooks,
routing), a handful of signals specific to a model gateway, tracing, and a long-term
cost ledger.

## 1. What exists today

| Signal | Where | What it answers |
|---|---|---|
| Request metrics | `requests_total{route,method,status}`, `request_duration_seconds{route,method}` (to the start of the response, so time to first token for a stream), from `api/middleware.py` | Traffic, errors and latency per route |
| Cost and usage | `model_tokens_total`, `model_cost_usd_total` (per provider and model), `service_cost_usd_total` (per service), `model_unpriced_total` | Spend and its attribution; unpriced spend |
| Provider health | `rung_failures_total{reason}`, `provider_timeouts_total`, `chain_retries_total`, `circuit_skips_total`, `circuit_open` | Which provider or model is failing, and why |
| Governance | `ratelimited_total`, `provider_quota_rejected_total`, `budget_rejected_total` | Limits being hit |
| Degradation | `redis_errors_total` | Running on per-replica fallbacks |
| Streams | `streams_total{outcome}` | Streams failing after their first token |
| Build identity | `build_info{version,revision}`; `revision` on `/v1/health` and `/v1/capabilities` | Which code answered; mixed rollouts |
| Process | The `prometheus_client` default collectors (CPU, memory, file descriptors, GC) | Saturation of a replica |
| Logs | JSON lines with a Cloud Logging `severity`; `request_id` bound on every line of a request; one `model.call` audit line per served call (service, operation, model choice, provider, model, tokens, cost, cached, finish reason). Prompts and answers are never logged. | Who called, what answered, what it cost |
| Probes | `/v1/health` (liveness), `/v1/ready` (per provider, auth-aware; Redis reported, never gating) | Whether a pod should take traffic |
| Collection | `PodMonitoring` in the chart: Managed Prometheus scrapes `/metrics` on every replica | — |
| Alerts | Eleven rules, as prose in [03-deployment.md](03-deployment.md) §5 | — |

## 2. Findings

Ordered by how much they leave the team blind.

| # | Finding | Effect in production |
|---|---|---|
| O-1 | The alerts exist only as prose. No rule, policy or notification channel is created by the chart or Terraform (`00-plan.md` §6 item 2). | Nobody is told about anything. A revoked key, an open breaker or a spent budget is found by a consumer. |
| O-2 | No SLOs with numbers (`05-handover.md` §7 leaves them to the owner). | Latency and error alerts have no principled threshold; no error budget to decide "freeze or ship". |
| O-3 | No dashboards, and none as code. | Support has nothing to open during an incident; each person builds their own queries. |
| O-4 | Request metrics carry no `service` label and no model class. | "Is the classifier failing?" or "is `cloud` slow?" can only be answered from logs. Per-class SLOs are impossible. |
| O-5 | No latency per provider call (only end-to-end), no time-to-first-token per provider. | A slow provider hides behind failover and retries; you see the route slow down, not why. |
| O-6 | No record of which rung answered. | Silent failover to a pricier model (the cost risk failover creates) shows only as rising spend. |
| O-7 | No cache hit or miss metric. | The cache is a direct cost lever with no way to see whether it works. |
| O-8 | Budgets are visible only once spent (`budget_rejected_total`). | No warning before a service starts getting 429 `budget_exceeded`. |
| O-9 | No output-quality rates: refusals and cut-off answers (`finish_reason` `refusal`, `length`) per model. | A model change that starts refusing or truncating is invisible until consumers complain. |
| O-10 | The audit line has no latency, attempt count, rung or outcome, and a failed call writes no audit line. | Per-call forensics and cost reconciliation are incomplete; failed calls are invisible in the log sink. |
| O-11 | No tracing (`00-plan.md` §6 item 5). | One slow request can't be followed consumer → gateway → provider. |
| O-12 | No outside-in (synthetic) check. The Service is in-cluster, so a Cloud Monitoring uptime check can't reach it. | A fault that passes the pods' own probes (a broken NetworkPolicy, a token or policy mistake) goes unnoticed until real traffic fails. |
| O-13 | Cost lives only in Prometheus counters (resets per pod, limited retention). | No month-long, per-service ledger for chargeback, no reconciliation against provider invoices. |
| O-14 | No runbooks per alert, no owner, no severity, no tested route to on-call. | An alert that fires at 3 a.m. with no owner or runbook is noise. |
| O-15 | No cost-anomaly signal, only hard budgets. | A runaway consumer loop spends up to its whole daily budget before anything happens. |

## 3. Principles

1. **SLOs first, alerts from them.** Page on symptoms users feel (error-budget burn);
   open tickets for causes (an open breaker, Redis degraded). A cause that users don't
   feel must not page.
2. **Every alert has an owner, a severity and a runbook,** and every alert is fired
   once on purpose before release (a game day), as the MG-009 repro did by hand.
3. **Golden signals per route, per service and per model class**: traffic, errors,
   latency (and time to first token), saturation; plus the two a model gateway adds,
   **cost** and **output quality**.
4. **Bounded label sets.** `service`, `provider`, `model`, `route`, `operation` and
   model class are bounded by policy and routing. Request ids, users and prompts are
   never metric labels: they belong in logs and traces.
5. **Observability as code.** Alerts, dashboards, log sinks and SLOs are reviewed,
   versioned and deployed with the gateway, and alert rules have unit tests.
6. **No content in telemetry.** Metrics, logs and traces carry identifiers, counts,
   timings and money, never prompts, answers or vectors (consistent with
   [07-ai-firewall.md](07-ai-firewall.md)).
7. **Build-aware.** Every signal can be split by `revision`, so a regression is tied
   to the rollout that caused it.

## 4. SLOs

Starting values, to be confirmed against a week of staging traffic (decision 1). A
30-day rolling window.

| SLO | SLI | Starting target |
|---|---|---|
| Availability, per route | Good = any response except 5xx. Caller errors (4xx), `budget_exceeded` and rate-limit 429s are excluded from both sides: they are policy working, not the gateway failing. An all-rungs-throttled 429 counts as bad. | 99.5% |
| Latency, `/v1/embed` | p95 of request duration | ≤ 1 s |
| Latency, `/v1/generate` unary, per model class | p95 of model-call duration (new histogram, §6) | `local` ≤ 10 s, `auto`/`cloud` ≤ 15 s |
| Time to first token, streamed `/v1/generate` | p95 of request duration for streams | ≤ 3 s |
| Stream completion | `streams_total{outcome="completed"}` / all, excluding `client_disconnected` | 99% |

Alerting on them uses the multi-window burn-rate method (Google SRE workbook):

| Burn | Windows (both must hold) | Budget consumed | Severity |
|---|---|---|---|
| 14.4× | 1 h and 5 min | 2% in an hour | P1, page |
| 6× | 6 h and 30 min | 5% in six hours | P1, page |
| 1× | 3 days and 6 h | 10% in three days | P2, ticket |

## 5. Alert catalogue

P1 pages the on-call engineer at any hour. P2 creates a ticket or posts in the team
channel for the same working day. Each row gets a runbook section in
[03-deployment.md](03-deployment.md) §5 (what it means, first queries, fixes), linked
from the alert.

| Alert | Condition (metric names without the `air_model_gateway_` prefix) | Severity | Owner |
|---|---|---|---|
| Availability burn | §4 burn-rate rules on `requests_total` | P1 / P2 | Gateway on-call |
| Latency burn | §4 on `request_duration_seconds` and the model-call histogram | P1 / P2 | Gateway on-call |
| Synthetic probe failing | Two consecutive failed probe runs (§9) | P1 | Gateway on-call |
| Too few replicas | `kube_deployment_status_replicas_available` < 2 for 5 min | P1 | Platform |
| Every rung of a class failing | Model-call outcome `failed` share > 50% for a class, 10 min | P1 | Gateway on-call |
| Provider key or account refused | `increase(rung_failures_total{reason="auth"}[15m]) > 0` | P2, same hour | Gateway owner (holds the provider accounts) |
| Adapter defect | `increase(rung_failures_total{reason="adapter"}[1h]) > 0` | P2 | Gateway developers |
| Breaker open | `circuit_open == 1` for 5 min | P2 | Gateway on-call |
| Failover share rising | Share of calls answered by a rung other than the first > 20% for 30 min (§6) | P2 | Gateway on-call |
| Redis degraded | `rate(redis_errors_total[5m]) > 0` for 10 min | P2 | Platform |
| Unpriced spend | `increase(model_unpriced_total[1h]) > 0` | P2 | Gateway owner |
| Budget nearly spent | Budget-used ratio ≥ 0.8 (§6) | P2 | Consuming team, gateway owner copied |
| Budget spent | `increase(budget_rejected_total[15m]) > 0` | P2 | Consuming team |
| Provider quota hit | `increase(provider_quota_rejected_total[15m]) > 0` | P2 | Gateway owner |
| Cost anomaly | Hourly `service_cost_usd_total` per service > 3× the same hour's 7-day average, and > $1 | P2 | Gateway owner |
| Streams failing | `streams_total{outcome="error"}` share > 5% for 15 min | P2 | Gateway on-call |
| Mixed builds | More than one `revision` in `build_info` 15 min after a rollout | P2 | Release owner |
| Pods restarting | `kube_pod_container_status_restarts_total` increase > 3 in 30 min, or an OOM kill | P2 | Platform |
| Quality shift | Refusal or `length` share per model doubles against its 7-day baseline | P2 | Gateway developers |
| Gateway errors in logs | Log-based metric on `severity>=ERROR` above its baseline | P2 | Gateway developers |

What must **not** alert: individual 4xx, a single provider timeout, a single rung
failure (failover handles it), Redis being absent where it is not configured.

## 6. Gateway instrumentation to add

Code changes, each with tests and a row in [02-lld.md](02-lld.md) §11. Names are
given without the exported prefix.

| Proposed metric | Type and labels | Finding |
|---|---|---|
| `model_calls` (counter) | `service`, `operation`, `model_class` (or `pin`), `provider`, `model`, `rung` (0 = first choice), `outcome` (`served`, `cached`, `failed`, `refused`, `length`) | O-4, O-6, O-9 |
| `model_call_seconds` (histogram) | `service`, `operation`, `model_class`, `outcome`; measured in `model_calls` from admission to the result | O-4, SLOs |
| `provider_call_seconds` (histogram) | `provider`, `model`, `operation`, `outcome`; one observation per rung attempt, in the router | O-5 |
| `stream_first_token_seconds` (histogram) | `provider`, `model` | O-5 |
| `cache_lookups` (counter) | `service`, `operation`, `result` (`hit`, `miss`, `bypass`) | O-7 |
| `service_budget_used_ratio` (gauge) | `service`; set after each spend from the ledger (shared through Redis, per replica in fallback, so dashboards read `max` across replicas) | O-8 |
| `inflight_requests` (gauge) | `route` | Saturation; the request-rate HPA metric `03-deployment.md` §5 asks for |

Cardinality check: about six services × three operations-or-classes × the models in
`deploy/routing.yaml` is a few hundred series per metric, well within budget. The
`service` label comes from the authenticated principal, never from the request body.

**Audit line.** `model.call` gains `latency_ms`, `attempts`, `rung`, `outcome` and,
once tracing exists, `trace_id`. A failed call writes the same event with
`outcome="failed"`, `http_status` and the last rung's failure `reason`, so the log
sink accounts for every call, not only served ones. It still never carries content.

## 7. Dashboards

Defined as JSON in the repository and deployed with Terraform (decision 10).

1. **Operations:** traffic, error ratio and p50/p95/p99 latency per route and per
   service; SLO burn; replicas and revisions; in-flight requests; Redis degradation;
   stream outcomes. Deploy markers from `build_info`.
2. **Providers:** per provider and model: call latency, time to first token, failures
   by reason, timeouts, breaker state, failover share, quota rejections.
3. **Cost and governance:** spend per service, model and provider (hour, day, month to
   date); budget used per service; tokens; cache hit ratio and the spend it avoided;
   unpriced calls; rate-limit and budget rejections.
4. **Quality:** refusal and `length` share per model, against the 7-day baseline.

**Support's triage path**, written into the runbook: a consumer reports a failure
with the `X-Request-ID` from the response → Logs Explorer on
`jsonPayload.request_id` (every line of that request, including the `model.call`
outcome) → the trace by `trace_id` (once tracing exists) → the provider dashboard for
the provider and model it names.

## 8. Tracing

OpenTelemetry, exported over OTLP to Cloud Trace (decision 4).

- Instrument FastAPI (server spans) and the provider SDKs' HTTP clients (client
  spans), plus one span per rung attempt in the router, so failover and retries are
  visible as siblings under one request.
- Attributes follow the OpenTelemetry GenAI semantic conventions:
  `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
  `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
  `gen_ai.response.finish_reasons`, plus the gateway's own service, model class, rung,
  cost and cached. Prompt and completion capture stays off.
- Sampling: parent-based, 10% of new traces at the start; every errored request kept
  needs tail sampling in an OpenTelemetry Collector, so start head-based and add the
  collector if the 10% misses incidents.
- Propagation: accept and emit W3C `traceparent`; keep `X-Request-ID` as the
  human-facing id and put it on the root span. Consumers forwarding both headers is a
  task for their own repositories (the open "forward `X-Request-ID`" items in
  `05-handover.md` §7 grow to include `traceparent`).
- Settings go in a new tracing group in `config.py` (off by default, so local and the
  tests need no collector); the standard `OTEL_*` variables are honoured.

## 9. Synthetic probes

The Service has no ingress, so the probe runs in the cluster: a Kubernetes `CronJob`
in the chart, every 5 minutes, running `deploy/gke/smoke.py` (or a slimmer probe) with
a dedicated `gateway-probe` service token (decision 7).

- It checks `/v1/ready`, one `local`-class `/v1/generate` (free) and one local
  `/v1/embed`. A periodic `cloud` call is optional and would need a small budget.
- The probe's principal is granted only the local class and embedding, so it needs no
  daily budget and can't spend. It is a new consumer, so the checklist in
  [AGENTS.md](../AGENTS.md) ("New consumer or grant") applies.
- Failure is visible through `kube_job_status_failed`. Its calls carry
  `service="gateway-probe"` so dashboards can exclude them from consumer traffic.

## 10. Cost ledger and logs

- **Ledger:** a Cloud Logging sink of `jsonPayload.event="model.call"` into a BigQuery
  dataset, partitioned by day and kept 13 months (decision 5). Scheduled queries give
  cost per service, model and day; a Looker Studio report on top serves chargeback.
- **Reconciliation:** monthly, the ledger's total per provider against each provider's
  billing (Anthropic and OpenAI usage pages or APIs, the GCP billing export for
  Gemini). A gap larger than a few percent means a price-table or usage-parsing error;
  it becomes a finding and a test like any other.
- **Price table drift:** `model_unpriced_total` already catches a missing row; the
  reconciliation catches a wrong one.
- **Log retention:** the `_Default` bucket's 30 days for operational logs; the
  BigQuery ledger for the long-term record (decision 9).
- **Error Reporting:** unhandled exceptions already log with their stack; making the
  JSON line carry the exception in the field Error Reporting reads groups them
  automatically.

## 11. Decisions to confirm before development

1. **SLO targets** in §4, and whether to set them now or after a week of staging
   traffic (recommended: after).
2. **On-call and routing:** who holds P1, which tool (PagerDuty, Opsgenie, Google Chat
   or e-mail through Cloud Monitoring notification channels), and the P2 channel.
3. **Alert mechanism:** (a) Prometheus rule files in the chart as the Managed
   Prometheus `Rules` resource, unit-tested with `promtool test rules` in CI
   (recommended: rules live and change with the metrics they read), or (b) Cloud
   Monitoring alert policies with PromQL conditions in Terraform (native incident UI
   and notification channels, but no rule unit tests).
4. **Tracing backend and sampling:** Cloud Trace with 10% head sampling (recommended),
   or a collector with tail sampling from the start.
5. **Cost ledger:** BigQuery sink with 13-month retention, and who reviews the monthly
   reconciliation.
6. **Budget warning:** 80% of the daily budget, sent to the consuming team with the
   gateway owner copied.
7. **Synthetic probe:** a new `gateway-probe` consumer, local-only, every 5 minutes.
8. **The `service` label** on the new model-call metrics (recommended; bounded by
   policy).
9. **Log retention:** 30 days operational, 13 months for the audit ledger.
10. **Dashboards:** Cloud Monitoring dashboards in Terraform (recommended on GKE with
    Managed Prometheus), or Grafana.

## 12. Delivery plan

Each phase ends with its proof, recorded in `05-handover.md` §5 with the revision it
ran against.

| Phase | Scope | Proof |
|---|---|---|
| 0 | Decisions in §11; one week of staging traffic for the latency baseline | Targets written into §4 and `05-handover.md` §7 |
| 1 | Codify the existing alerts (§5 rows that use today's metrics), notification channels, the operations and cost dashboards, runbooks in `03-deployment.md` §5. No application code. | Rule unit tests pass in `make verify`; in staging, each P1 and the auth alert are fired on purpose (an invalid key, a scaled-down deployment) and reach the right person |
| 2 | Instrumentation in §6 and the audit-line additions; the SLO burn-rate, failover-share, budget-warning and quality alerts that need them | Tests per metric; LLD §11 updated (the consistency test enforces it); kind and integration gates pass |
| 3 | BigQuery ledger, scheduled queries, the first monthly reconciliation; the synthetic probe `CronJob` | Ledger total matches `service_cost_usd_total` for a staging day; a broken probe token raises the P1 |
| 4 | OpenTelemetry tracing; consumers forward `traceparent` (their own repositories) | One request's trace shows each rung attempt; `trace_id` is on its `model.call` line |

**Out of scope here:** the firewall's own metrics (`07-ai-firewall.md` §9; they join
the providers and quality dashboards when that work starts), and HPA on request rate
(enabled by `inflight_requests`, decided separately in `03-deployment.md` §5).
