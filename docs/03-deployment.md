# AIR Model Gateway — Deployment and Operations

**Companion docs:** [Overview](overview.md) · [Plan](00-plan.md) · [HLD](01-hld.md) · [LLD](02-lld.md)

How air-model-gateway is deployed to UAT and production on GKE, what this repository
owns in that, and how the gateway is operated once it runs.

## 1. Where the deployment code lives

The Helm chart, the Terraform and the deployment scripts for every AIR core service
are kept together in the **air-infrastructure-service** repository, under `gke/`, where
DevOps maintains them. That repository's `docs/03-gke-runbook.md` is the step-by-step
runbook (provisioning, secrets, first deploy, pipelines), and its
`docs/02-gcp-deployment.md` explains what each service needs.

| Piece | Where |
|---|---|
| The gateway's Helm chart | air-infrastructure-service, `gke/helm/air-model-gateway/` |
| Values per environment | air-infrastructure-service, `gke/values/{uat,prod,kind}/air-model-gateway.yaml` |
| Terraform (Memorystore, secrets, tokens, Workload Identity; optional cluster) | air-infrastructure-service, `gke/terraform/` |
| Token and provider-key bootstrap, smoke test, kind rehearsal | air-infrastructure-service, `gke/scripts/`, `gke/kind/` |
| **The image** (`Dockerfile`) | this repository |
| **Routing, pricing and policy** (`deploy/routing.yaml`, `deploy/pricing.yaml`, `deploy/policy/policy.<env>.yaml`) | this repository. They are the gateway's configuration; the chart receives them at deploy time, so `deploy/` here stays their single source |
| **The build-and-promote pipeline** (`.github/workflows/deploy.yml`) | this repository |

## 2. Target shape

A GKE Autopilot cluster per environment. The gateway runs in its own namespace as 2 to
10 pods behind an in-cluster Service: no Ingress or load balancer. Consumers call
`http://air-model-gateway.air-model-gateway.svc.cluster.local` with their token in
`X-API-Key`, and a NetworkPolicy admits only their namespaces. Secrets come from Secret
Manager through the CSI add-on and Workload Identity; shared state is Memorystore Redis
with AUTH and TLS; provider APIs are reached through Cloud NAT. Model servers (Ollama,
llama.cpp, vLLM) are separate deployments: the gateway only gets their URLs.

## 3. Environment names

Deployments are named `uat` and `prod`. **This service's own name for its
pre-production environment is `staging`**: `AIR_MODEL_GATEWAY_ENVIRONMENT=staging`,
`deploy/policy/policy.staging.yaml`, and `"environment": "staging"` in
`/v1/capabilities`. A UAT deployment runs with those. The deploy commands take `uat`
and map it for you.

## 4. Deploying from this repository

The sibling checkout is needed (`INFRA_DIR`, default `../air-infrastructure-service`).

```bash
export IMAGE_REPOSITORY=<region>-docker.pkg.dev/<project>/air/air-model-gateway
make image-push                       # linux/amd64, tagged with this commit
make deploy DEPLOY_ENV=uat KUBE_CONTEXT=$(kubectl config current-context) GCP_PROJECT=<project>
make deploy-smoke DEPLOY_ENV=uat KUBE_CONTEXT=$(kubectl config current-context) GCP_PROJECT=<project>
```

| Target | Does |
|---|---|
| `make image` / `make image-push` | Build the image locally; build for `linux/amd64` and push it |
| `make deploy-render` | Print the manifests an environment would get (`SAMPLE=1` fills the project placeholders) |
| `make deploy-validate` | Lint the charts, schema-check the manifests and validate Terraform, for every service |
| `make deploy` | `helm upgrade --install` into `KUBE_CONTEXT`, with this checkout's routing, pricing and policy |
| `make deploy-smoke` | The authenticated in-cluster smoke test and the NetworkPolicy check |
| `make kind-up`, `make kind-test`, `make kind-down` | The production chart on a local kind cluster, with the other four services |

`.github/workflows/deploy.yml` runs on a `v*` tag or manually: it builds once, deploys
UAT by digest and smoke-tests it, waits for approval on the `prod` environment, copies
the same digest into prod's registry, deploys and smoke-tests. It checks
air-infrastructure-service out beside this repository.

First-time setup of an environment (Terraform, secrets, values, consumers) is in
air-infrastructure-service's runbook, §5.

## 5. Day-2 operations

| Task | How |
|---|---|
| Change routing, pricing or a grant | Edit the file in `deploy/`, merge, run the Deploy workflow (or `make deploy` with the current image). The ConfigMap checksum rolls the pods. |
| Set provider quotas, a service's daily budget or its model-choice grants | Edit `provider_quotas`, `daily_budget_usd`, `model_classes`, `embed_models`, `preferred_providers` or `pin` in `deploy/policy/policy.<env>.yaml` (commented examples inside), then deploy as for routing. Watch `provider_quota_rejected_total` and `budget_rejected_total`. |
| Rotate a provider key | Add a version (`printf %s "$KEY" \| gcloud secrets versions add air-model-gateway-gemini-api-key --data-file=-`), then `kubectl -n air-model-gateway rollout restart deploy/air-model-gateway`. Secrets are read at boot. |
| Rotate consumer tokens, no downtime | air-infrastructure-service's `gke/scripts/bootstrap-gateway-secrets.sh <p> <env> --rotate` (the gateway accepts old and new), restart the gateway, restart each consumer, then `--retire-old` and restart the gateway. |
| Roll back | `helm rollback air-model-gateway -n air-model-gateway` (config and image together). |
| Long streams and rollouts | A rollout drains each pod for `gracefulShutdownSeconds` (70 s). A stream still running after that is cut off and its caller must retry. If consumers stream long answers, raise `gracefulShutdownSeconds` and `terminationGracePeriodSeconds` together. The Service is in-cluster with no proxy, so nothing buffers streams; any proxy added later must not buffer `text/event-stream` (the gateway sends `X-Accel-Buffering: no`). |
| Scale | HPA on CPU, 3–10 replicas in prod. The gateway mostly waits on providers, so move to a request-rate metric once there is a baseline. |
| Redis maintenance or outage | Nothing to do. Pods stay ready; the cache misses, and rate limits, quotas and budgets fall back to per replica (budgets undercount meanwhile). `/v1/ready` reports `"redis": false`. |
| A model provider outage | Breakers skip the broken model and chains fail over. If every rung of every provider fails, readiness turns 503 and pods leave the Service. |

### Logs

Every line is JSON with a Cloud Logging `severity`. One audit line per call:

```
resource.type="k8s_container"
resource.labels.namespace_name="air-model-gateway"
jsonPayload.event="model.call"
```

Filter on `jsonPayload.service`, `jsonPayload.model` or `jsonPayload.request_id`.

### Metrics and alerts

Managed Prometheus scrapes `/metrics` (`PodMonitoring`). Every metric is listed in
[02-lld.md](02-lld.md) §11. Alerts worth creating first:
- `air_model_gateway_circuit_open == 1` for 5 minutes: a model is down.
- A rise in `rate(air_model_gateway_redis_errors_total[5m])`: running degraded.
- `increase(air_model_gateway_model_unpriced_total[1h]) > 0`: uncounted spend.
- `increase(air_model_gateway_rung_failures_total{reason="auth"}[15m]) > 0`: a provider
  refused the gateway's key or account (revoked, expired, out of credit). Failover hides
  it from callers, usually at a higher price; `/v1/ready` shows the provider false.
- `increase(air_model_gateway_rung_failures_total{reason="adapter"}[1h]) > 0`: an
  adapter bug (often an SDK change), hidden by failover in the same way.
- p95 of `air_model_gateway_request_duration_seconds` per route above its SLO.
- More than one `revision` in `air_model_gateway_build_info` after a rollout finished:
  replicas running different builds.
- A 5xx ratio from `air_model_gateway_requests_total` per route.
- A rising share of `air_model_gateway_streams_total{outcome="error"}`: streams
  failing after their first token.
- Any `air_model_gateway_budget_rejected_total` or
  `air_model_gateway_provider_quota_rejected_total`: a budget or quota is being hit.
- `kube_deployment_status_replicas_available` below 2.

## 6. Security posture

- **Identity:** each consumer has its own token, checked against `policy.<env>.yaml`.
  Anything not granted gets 403, and the policy fails closed outside `local`. The
  NetworkPolicy is a second, network-level gate.
- **Secrets:** they live only in Secret Manager. Pods read them as files through
  Workload Identity. Nothing is in Kubernetes Secrets, GitHub, or the image.
- **Pod:**
  - runs as non-root uid 10001 with a read-only root filesystem;
  - drops all capabilities, with no privilege escalation;
  - uses the `RuntimeDefault` seccomp profile;
  - has no Kubernetes API token mounted.
- **Redis:** AUTH plus TLS, verified against Memorystore's CA. Hostname checking is off
  because the client dials an IP.
- **Supply chain:** tags are immutable, deploys are by digest, and prod runs exactly
  what UAT tested.
- **In-cluster traffic is plain HTTP.** If your policy requires encryption between pods,
  enable GKE inter-node transparent encryption or a service mesh. The gateway needs no
  change for either.
- **Egress isn't restricted by the chart.** Provider APIs are public hostnames. To pin
  egress, use GKE FQDN network policies (`api.anthropic.com`, `api.openai.com`,
  `generativelanguage.googleapis.com`) or a Cloud NAT firewall policy.

## 7. What has been verified, and what hasn't

Verified on a laptop (`make deploy-validate`, `make kind-up`, `make kind-test`), most recently
on 2026-11-17 from the chart's new home in air-infrastructure-service:
- **Chart:** lints, and UAT, prod and kind manifests pass Kubernetes 1.33 and CRD
  schema validation (`SecretProviderClass`, `PodMonitoring`).
- **Terraform:** both roots pass `fmt` and `validate` against google provider 7.x.
- **On kind, with production values:**
  - two replicas spread over two zones;
  - the read-only root filesystem works;
  - routing, pricing and policy come from the ConfigMap;
  - Redis over TLS with AUTH and CA verification;
  - all smoke checks pass, and the NetworkPolicy blocks an unlisted namespace;
  - a rolling restart under continuous traffic dropped 0 of 1,331 requests.
- **The bootstrap script:** first run, re-run, `--rotate` and `--retire-old`, against
  a stand-in for `gcloud` (before the move; it has since changed only in the environment
  names it accepts).

Not verifiable without a GCP project:
- `terraform apply`;
- the Secret Manager add-on mounting real secrets;
- Memorystore's real CA;
- Managed Prometheus scraping;
- Autopilot admission of the pod spec;
- the Deploy workflow's Workload Identity Federation.

The first UAT rollout is where these get proven; air-infrastructure-service's runbook
is written to make that a single pass.
