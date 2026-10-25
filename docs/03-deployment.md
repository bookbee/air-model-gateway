# AIR Model Gateway — GKE Deployment Runbook

**Companion docs:** [Overview](overview.md) · [Plan](00-plan.md) · [HLD](01-hld.md) · [LLD](02-lld.md)

How air-model-gateway runs in staging and production on GCP, and everything DevOps
needs to stand it up and operate it. Every file named here is in this repo.

## 1. Target shape

```
                 GKE cluster (Autopilot, private nodes)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ ns air-classifier-service ─┐                                         │
 │ ns air-orchestrator-service├─► Service air-model-gateway (ClusterIP)  │
 │ ns air-web-console ────────┤      │  NetworkPolicy: these only       │
 │ ns air-tools, air-rag ─────┘      ▼                                  │
 │                    ns air-model-gateway: Deployment, 2–10 pods       │
 │                      (HPA, PDB, zone spread, non-root, RO root FS)   │
 │                      │ SECRETS_DIR ◄── Secret Manager add-on (CSI)   │
 │                      │ config      ◄── ConfigMap (routing, pricing,  │
 │                      │                 policy from deploy/)          │
 │                      ├──► ns air-model-servers: Ollama, llama.cpp/vLLM│
 │                      │    (owned separately; URLs in values)         │
 └──────────────────────┼───────────────────────────────────────────────┘
                        ├──► Memorystore Redis (AUTH + TLS, private IP)
                        └──► Cloud NAT ──► Anthropic, OpenAI, Gemini APIs
```

- **In-cluster only.** No Ingress or load balancer. Consumers call
  `http://air-model-gateway.air-model-gateway.svc.cluster.local` with their token in
  `X-API-Key`.
- **One project per environment** (staging, prod). Secret names are fixed, so two
  environments can't share a project.
- **Model servers are not part of this deployment.** The gateway calls them; it doesn't
  host them (a non-goal in [00-plan.md](00-plan.md)). `values-<env>.yaml` holds their
  URLs. Until they exist, the local rungs fail fast and chains fail over to cloud models.
  The exceptions are `local-768-v1` (embeddings) and the classifier's `local` class,
  which are local-only by design.

## 2. What is where

| Path | Owner action |
|---|---|
| `deploy/terraform/environments/<env>/` | `terraform apply` once per environment; re-apply on change |
| `deploy/terraform/modules/air-model-gateway/` | Memorystore, Secret Manager secrets, Workload Identity IAM |
| `deploy/terraform/modules/gke-platform/` | VPC, Cloud NAT, Autopilot cluster, Artifact Registry. Skip (`create_platform = false`) if the platform team already runs a cluster |
| `deploy/gke/bootstrap-secrets.sh` | Issue consumer tokens, load provider keys, rotate tokens |
| `deploy/gke/values-<env>.yaml` | Fill the `<placeholders>` once; edit model-server URLs and consumer namespaces |
| `deploy/helm/air-model-gateway/` | The chart. `values.yaml` documents every knob |
| `deploy/routing.yaml`, `pricing.yaml`, `policy/policy.<env>.yaml` | Config changes: edit, then deploy (no image build) |
| `.github/workflows/deploy.yml` | Build, staging deploy + smoke, approved promotion to prod |
| `make gke-validate`, `gke-render`, `gke-deploy`, `gke-smoke` | The same commands locally and in CI |

## 3. Prerequisites

- Tools: `gcloud`, `kubectl`, `helm` (v3.14+ or v4), `terraform` ≥ 1.5, and
  `kubeconform` for `make gke-validate`.
- A GCS bucket for Terraform state (one bucket; each environment uses its own prefix).
- Decide the region (default `us-central1`) and the project per environment.
- The image registry: the platform module creates `air` in Artifact Registry (tags are
  immutable). If the platform has its own registry, use that path instead.

## 4. First-time setup (per environment)

Shown for staging. Repeat with `prod`.

### 4.1 GCP resources

```bash
cd deploy/terraform/environments/staging
cp terraform.tfvars.example terraform.tfvars          # project_id, region, create_platform…
terraform init -backend-config="bucket=<state-bucket>"
terraform plan -out plan && terraform apply plan
terraform output                                      # cluster, image_repository, secret ids
```

This creates:
- Memorystore (BASIC in staging, STANDARD_HA in prod) with AUTH and TLS.
- Secret containers. Terraform fills the Redis URL and CA itself; the rest stay empty
  until 4.2.
- `secretAccessor` on the gateway's own secrets for the Kubernetes service account
  `air-model-gateway/air-model-gateway`, through direct Workload Identity Federation: no
  Google service account, no key.
- Optionally the VPC, NAT, Autopilot cluster and registry.

The Redis AUTH string ends up in Terraform state (inside the URL secret version), so
the state bucket must be access-restricted like a secret store.

### 4.2 Secrets

```bash
# Provider keys you have, from your password manager; the script pipes them to gcloud on stdin.
export AIR_MODEL_GATEWAY_PROVIDERS__ANTHROPIC_API_KEY=…   # optional
export AIR_MODEL_GATEWAY_PROVIDERS__GEMINI_API_KEY=…      # optional
deploy/gke/bootstrap-secrets.sh <staging-project> staging
```

This issues one token per consumer into `<service>-model-gateway-token`, writes the
gateway's `token:service` map, and adds provider key versions. Re-runs keep existing
tokens. The script prints no values.

**A secret listed in the chart with no version stops pods from starting.** If you have
no OpenAI key, remove it from the chart's list in `values-staging.yaml`:

```yaml
secrets:
  gcp:
    items:
      AIR_MODEL_GATEWAY_PROVIDERS__OPENAI_API_KEY: null
```

### 4.3 Values

In `deploy/gke/values-staging.yaml`:
- Replace `<region>` and `<project>` with real values from `terraform output`. The chart
  refuses to render while a placeholder remains. CI passes both anyway.
- Set `modelServers.*` to the model servers' in-cluster URLs.
- Check `networkPolicy.allowedNamespaces` lists the consumers' real namespaces.

### 4.4 Consumers

For each consumer:
1. Its namespace is in `networkPolicy.allowedNamespaces`, or labelled
   `air-platform/model-gateway-client=true`.
2. Its identity can read its own token secret. Do this in the consumer's Terraform, or:
   ```bash
   gcloud secrets add-iam-policy-binding air-rag-model-gateway-token --project <p> \
     --role roles/secretmanager.secretAccessor \
     --member "principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<p>.svc.id.goog/subject/ns/air-rag/sa/<ksa>"
   ```
3. Its deployment sets the gateway URL and mounts that token, under its own setting
   names (each repo's `.env.example` names them).

### 4.5 First deploy

```bash
gcloud container clusters get-credentials <cluster> --region <region> --project <p>
make gke-deploy GKE_ENV=staging KUBE_CONTEXT=$(kubectl config current-context) \
  GCP_PROJECT=<p> IMAGE_REPOSITORY=<region>-docker.pkg.dev/<p>/air/air-model-gateway IMAGE_TAG=<tag>
helm test air-model-gateway -n air-model-gateway --kube-context <ctx>
make gke-smoke GKE_ENV=staging KUBE_CONTEXT=<ctx> GCP_PROJECT=<p>
```

The smoke test runs in a temporary namespace labelled as a consumer. It checks:
- readiness and Redis;
- 401 for a dev token, and 200/403 for each consumer per its grants;
- the environment name;
- that a pod in an unlisted namespace can't connect.

It deletes its namespaces afterwards.

### 4.6 CI/CD

`.github/workflows/deploy.yml` runs on a `v*` tag or manually:
1. Build the image and push it to staging's registry, tagged by commit.
2. Deploy staging by digest and run the smoke test.
3. Wait for approval on the `prod` environment.
4. Copy the same digest into prod's registry, deploy it, and run the smoke test.

Set up once:
- A Workload Identity Federation pool and provider trusting this GitHub repo.
- A deploy service account per environment with:
  - `roles/artifactregistry.writer`;
  - `roles/container.developer`;
  - `roles/secretmanager.secretAccessor` on the five `*-model-gateway-token` secrets
    (for the smoke test);
  - on prod's account only, `roles/artifactregistry.reader` on staging's repository.
- GitHub environments `staging` and `prod` with variables `GCP_PROJECT_ID`,
  `GCP_REGION`, `GKE_CLUSTER`, `GCP_WIF_PROVIDER` and `GCP_DEPLOY_SA`. Prod also needs
  `STAGING_IMAGE_REPOSITORY`, and a required reviewer.
- With private control planes, the GitHub runners must be allowed through
  `master_authorized_networks`, or use self-hosted runners inside the VPC.

`.github/workflows/ci.yml` runs `make check`, `make gke-validate` and an image build on
every push and pull request.

## 5. Day-2 operations

| Task | How |
|---|---|
| Change routing, pricing or a grant | Edit the file in `deploy/`, merge, run the Deploy workflow (or `make gke-deploy` with the current image). The ConfigMap checksum rolls the pods. |
| Set provider quotas, a service's daily budget or its model-choice grants | Edit `provider_quotas`, `daily_budget_usd`, `model_classes`, `embed_models`, `preferred_providers` or `pin` in `deploy/policy/policy.<env>.yaml` (commented examples inside), then deploy as for routing. Watch `provider_quota_rejected_total` and `budget_rejected_total`. |
| Rotate a provider key | Add a version (`printf %s "$KEY" \| gcloud secrets versions add air-model-gateway-gemini-api-key --data-file=-`), then `kubectl -n air-model-gateway rollout restart deploy/air-model-gateway`. Secrets are read at boot. |
| Rotate consumer tokens, no downtime | `bootstrap-secrets.sh <p> <env> --rotate` (the gateway accepts old and new), restart the gateway, restart each consumer, then `--retire-old` and restart the gateway. |
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
  what staging tested.
- **In-cluster traffic is plain HTTP.** If your policy requires encryption between pods,
  enable GKE inter-node transparent encryption or a service mesh. The gateway needs no
  change for either.
- **Egress isn't restricted by the chart.** Provider APIs are public hostnames. To pin
  egress, use GKE FQDN network policies (`api.anthropic.com`, `api.openai.com`,
  `generativelanguage.googleapis.com`) or a Cloud NAT firewall policy.

## 7. What has been verified, and what hasn't

Verified on a laptop (`make gke-validate`, `make kind-up`, `make kind-test`):
- **Chart:** lints, and staging, prod and kind manifests pass Kubernetes 1.33 and CRD
  schema validation (`SecretProviderClass`, `PodMonitoring`).
- **Terraform:** both roots pass `fmt` and `validate` against google provider 7.x.
- **On kind, with production values:**
  - two replicas spread over two zones;
  - the read-only root filesystem works;
  - routing, pricing and policy come from the ConfigMap;
  - Redis over TLS with AUTH and CA verification;
  - all smoke checks pass, and the NetworkPolicy blocks an unlisted namespace;
  - a rolling restart under continuous traffic dropped 0 of 1,331 requests.
- **`bootstrap-secrets.sh`:** first run, re-run, `--rotate` and `--retire-old`, against
  a stand-in for `gcloud`.

Not verifiable without a GCP project:
- `terraform apply`;
- the Secret Manager add-on mounting real secrets;
- Memorystore's real CA;
- Managed Prometheus scraping;
- Autopilot admission of the pod spec;
- the Deploy workflow's Workload Identity Federation.

The first staging rollout is where these get proven; §4 is written to make that a
single pass.
