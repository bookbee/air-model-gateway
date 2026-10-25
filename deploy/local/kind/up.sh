#!/bin/sh
# Deploys the production Helm chart to a local kind cluster (`make kind-up`).
#
#   1. kind cluster `air-model-gateway` (created once; reused after)
#   2. the production image, built and loaded into the cluster
#   3. a TLS + AUTH Redis standing in for Memorystore (redis-tls.yaml)
#   4. the gateway's secrets as a Kubernetes Secret: generated per-consumer
#      tokens, the rediss:// URL, and provider keys from the repo's .env
#   5. `helm upgrade --install` with values-kind.yaml and deploy/'s routing,
#      pricing and policy.prod.yaml, exactly as CI does for GKE
#
# Generated state (CA, Redis password, tokens.env) lives in .state/, which is
# gitignored; re-running keeps it, so consumers configured with the tokens keep
# working. Every kubectl/helm call names the kind context explicitly, and your
# current context is left as it was. Prints no secrets.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
cluster=air-model-gateway
ctx="kind-$cluster"
ns=air-model-gateway
deps=air-model-gateway-deps
state="$here/.state"
image=air-model-gateway:local
k() { kubectl --context "$ctx" "$@"; }
apply_secret() { # namespace name args...  (create or update)
  n=$1; name=$2; shift 2
  k -n "$n" create secret generic "$name" "$@" --dry-run=client -o yaml | k apply -f - >/dev/null
}

if ! kind get clusters 2>/dev/null | grep -qx "$cluster"; then
  # kind switches the current context to the new cluster; put yours back.
  previous=$(kubectl config current-context 2>/dev/null || true)
  kind create cluster --config "$here/cluster.yaml" --wait 120s
  [ -z "$previous" ] || kubectl config use-context "$previous" >/dev/null
fi

revision=${AIR_MODEL_GATEWAY_REVISION:-$(git -C "$root" describe --always --dirty --abbrev=12 2>/dev/null || echo unknown)}
echo "building $image ($revision)"
docker build -q --build-arg REVISION="$revision" -t "$image" "$root" >/dev/null
kind load docker-image "$image" --name "$cluster" >/dev/null

mkdir -p "$state"
chmod 700 "$state"
if [ ! -f "$state/ca.crt" ]; then
  host="redis.$deps.svc.cluster.local"
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=air-model-gateway-kind-ca" \
    -keyout "$state/ca.key" -out "$state/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$host" \
    -keyout "$state/server.key" -out "$state/server.csr" 2>/dev/null
  printf 'subjectAltName=DNS:%s,DNS:redis.%s.svc,DNS:redis.%s\n' "$host" "$deps" "$deps" > "$state/san.ext"
  openssl x509 -req -in "$state/server.csr" -CA "$state/ca.crt" -CAkey "$state/ca.key" \
    -CAcreateserial -days 30 -extfile "$state/san.ext" -out "$state/server.crt" 2>/dev/null
  openssl rand -hex 24 | tr -d '\n' > "$state/redis-password"
fi
if [ ! -f "$state/tokens.env" ]; then
  : > "$state/tokens.env"
  for service in air-classifier-service air-orchestrator-service air-web-console air-tools air-rag; do
    var=$(echo "$service" | tr 'a-z-' 'A-Z_')_TOKEN
    echo "$var=amg_kind_$(openssl rand -hex 20)" >> "$state/tokens.env"
  done
fi
chmod 600 "$state"/*

for n in "$ns" "$deps"; do
  k create namespace "$n" --dry-run=client -o yaml | k apply -f - >/dev/null
done

echo "redis (TLS stand-in for Memorystore)"
apply_secret "$deps" redis-tls \
  --from-file=ca.crt="$state/ca.crt" --from-file=server.crt="$state/server.crt" \
  --from-file=server.key="$state/server.key"
apply_secret "$deps" redis-auth --from-file=password="$state/redis-password"
k apply -f "$here/redis-tls.yaml" >/dev/null
# Picks up a changed password or certificate.
k -n "$deps" rollout restart deployment/redis >/dev/null
k -n "$deps" rollout status deployment/redis --timeout=120s >/dev/null

# The gateway's SECRETS_DIR files, assembled in a private temp dir.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
pairs=""
while IFS='=' read -r var token; do
  [ -n "$var" ] || continue
  service=$(echo "${var%_TOKEN}" | tr 'A-Z_' 'a-z-')
  pairs="${pairs:+$pairs,}$token:$service"
done < "$state/tokens.env"
printf '%s' "$pairs" > "$tmp/AIR_MODEL_GATEWAY_SECURITY__SERVICE_TOKENS"
printf 'rediss://:%s@redis.%s.svc.cluster.local:6378/0?ssl_cert_reqs=required&ssl_ca_certs=/etc/air-model-gateway/redis-ca/ca.pem' \
  "$(cat "$state/redis-password")" "$deps" > "$tmp/AIR_MODEL_GATEWAY_REDIS__URL"
for key in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY; do
  name="AIR_MODEL_GATEWAY_PROVIDERS__$key"
  value=$(grep -E "^$name=" "$root/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' || true)
  [ -z "$value" ] || printf '%s' "$value" > "$tmp/$name"
done
apply_secret "$ns" air-model-gateway-secrets --from-file="$tmp"
apply_secret "$ns" air-model-gateway-redis-ca --from-file=ca.pem="$state/ca.crt"

existed=$(helm --kube-context "$ctx" -n "$ns" status air-model-gateway >/dev/null 2>&1 && echo yes || echo no)
echo "helm upgrade --install air-model-gateway (values-kind.yaml, policy.prod.yaml)"
helm --kube-context "$ctx" upgrade --install air-model-gateway "$root/deploy/helm/air-model-gateway" \
  -n "$ns" -f "$here/values-kind.yaml" \
  --set-file config.routing="$root/deploy/routing.yaml" \
  --set-file config.pricing="$root/deploy/pricing.yaml" \
  --set-file config.policy="$root/deploy/policy/policy.prod.yaml" \
  --wait --timeout 5m >/dev/null
if [ "$existed" = yes ]; then
  # Secrets and the :local image are read at boot; Helm can't see them change.
  k -n "$ns" rollout restart deployment/air-model-gateway >/dev/null
  k -n "$ns" rollout status deployment/air-model-gateway --timeout=180s >/dev/null
fi

k -n "$ns" get pods -o wide
echo
echo "kind: deployed. Smoke test: make kind-test   (tokens: deploy/local/kind/.state/tokens.env)"
