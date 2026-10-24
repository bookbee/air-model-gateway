#!/bin/sh
# Authenticated smoke test of a deployed gateway, on kind or GKE.
#
#   KUBE_CONTEXT   required; e.g. kind-air-model-gateway, or the GKE context
#   NAMESPACE      default air-model-gateway
#   RELEASE        default air-model-gateway (Helm release / Deployment name)
#   TOKENS_FILE    env file of AIR_<SERVICE>_TOKEN=... lines, or
#   GCP_PROJECT    read each consumer's token from Secret Manager instead
#                  (<service>-model-gateway-token, see bootstrap-secrets.sh)
#   EXPECT_ENVIRONMENT  default prod
#
# 1. The Deployment is rolled out with at least 2 ready replicas.
# 2. A pod in a temporary namespace labelled as a consumer runs smoke.py
#    against the Service: readiness, Redis, the fail-closed policy, and one
#    real call per granted operation (generate, embed).
# 3. A pod in a temporary unlabelled namespace must NOT reach the gateway
#    (NetworkPolicy), when the release has one.
#
# Tokens reach the cluster only as a Secret in the temporary namespace, which
# is deleted on exit. Prints no tokens. Exits non-zero on any failure.
set -eu

: "${KUBE_CONTEXT:?set KUBE_CONTEXT}"
ns=${NAMESPACE:-air-model-gateway}
release=${RELEASE:-air-model-gateway}
here=$(cd "$(dirname "$0")" && pwd)
suffix=$(openssl rand -hex 3)
allowed="amg-smoke-$suffix"
denied="amg-smoke-denied-$suffix"
services="air-classifier-service air-orchestrator-service air-web-console air-tools air-rag"
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }

tmp=$(mktemp -d)
cleanup() {
  rm -rf "$tmp"
  k delete namespace "$allowed" "$denied" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ -n "${TOKENS_FILE:-}" ]; then
  cp "$TOKENS_FILE" "$tmp/tokens.env"
elif [ -n "${GCP_PROJECT:-}" ]; then
  : > "$tmp/tokens.env"
  for s in $services; do
    var=$(echo "$s" | tr 'a-z-' 'A-Z_')_TOKEN
    printf '%s=%s\n' "$var" "$(gcloud secrets versions access latest --project "$GCP_PROJECT" --secret "$s-model-gateway-token")" >> "$tmp/tokens.env"
  done
else
  echo "set TOKENS_FILE or GCP_PROJECT" >&2
  exit 2
fi
chmod 600 "$tmp/tokens.env"

echo "deployment $ns/$release"
k -n "$ns" rollout status "deployment/$release" --timeout=180s >/dev/null
ready=$(k -n "$ns" get "deployment/$release" -o jsonpath='{.status.readyReplicas}')
image=$(k -n "$ns" get "deployment/$release" -o jsonpath='{.spec.template.spec.containers[0].image}')
port=$(k -n "$ns" get "service/$release" -o jsonpath='{.spec.ports[0].port}')
url="http://$release.$ns.svc.cluster.local:$port"
if [ "${ready:-0}" -ge 2 ]; then echo "  ok    ready replicas >= 2                                   $ready"
else echo "  FAIL  ready replicas >= 2                                   got ${ready:-0}"; exit 1; fi

# A pod spec both checks share: the gateway's own image, Autopilot-friendly.
pod() { # namespace name command...
  n=$1; name=$2; shift 2
  cmd=$(printf '"%s",' "$@")
  cat <<EOF | k apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $name, namespace: $n}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: smoke
      image: $image
      command: [${cmd%,}]
      env:
        - {name: GATEWAY_URL, value: "$url"}
        - {name: EXPECT_ENVIRONMENT, value: "${EXPECT_ENVIRONMENT:-prod}"}
      envFrom: [{secretRef: {name: smoke-tokens, optional: true}}]
      volumeMounts: [{name: script, mountPath: /smoke}]
      resources: {requests: {cpu: 100m, memory: 128Mi}, limits: {memory: 128Mi}}
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
  volumes:
    - name: script
      configMap: {name: smoke-script}
EOF
}
wait_done() { # namespace name -> prints phase
  i=0
  while [ $i -lt 120 ]; do
    phase=$(k -n "$1" get pod "$2" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    case "$phase" in Succeeded|Failed) echo "$phase"; return;; esac
    sleep 2; i=$((i + 1))
  done
  echo Timeout
}

for n in "$allowed" "$denied"; do
  k create namespace "$n" >/dev/null
  k -n "$n" create configmap smoke-script --from-file=smoke.py="$here/smoke.py" >/dev/null
done
k label namespace "$allowed" air-platform/model-gateway-client=true >/dev/null
k -n "$allowed" create secret generic smoke-tokens --from-env-file="$tmp/tokens.env" >/dev/null

pod "$allowed" smoke python /smoke/smoke.py
phase=$(wait_done "$allowed" smoke)
k -n "$allowed" logs smoke
[ "$phase" = Succeeded ] || { echo "smoke pod: $phase"; exit 1; }

if k -n "$ns" get "networkpolicy/$release" >/dev/null 2>&1; then
  echo "network policy"
  pod "$denied" smoke python /smoke/smoke.py --expect-unreachable
  phase=$(wait_done "$denied" smoke)
  if [ "$phase" = Succeeded ]; then echo "  ok    unlisted namespace cannot connect"
  else echo "  FAIL  unlisted namespace reached the gateway ($phase)"; exit 1; fi
fi
echo "smoke: all checks passed"
