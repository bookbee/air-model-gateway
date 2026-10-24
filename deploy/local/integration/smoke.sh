#!/bin/sh
# Smoke test for the local integration stack (`make integration-test`).
#
# Gateway checks always run: readiness (providers + Redis), both replicas
# serving, the fail-closed policy, and one real call per granted operation
# (POST /v1/generate, POST /v1/embed). Consumer
# checks run only for consumers that are up on their usual ports, configured
# against this stack (see docs/overview.md §7).
#
# Exits non-zero if any check fails. Prints no tokens.
set -u

here=$(cd "$(dirname "$0")" && pwd)
gw=${INTEGRATION_URL:-http://localhost:9083}
. "$here/tokens.env"

pass=0
fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); printf '  ok    %-58s %s\n' "$1" "$3"
  else fail=$((fail + 1)); printf '  FAIL  %-58s got %s, want %s\n' "$1" "$3" "$2"; fi
}
status() { curl -s -o /dev/null -w '%{http_code}' --max-time 90 "$@"; }
call() { # path token body
  status -X POST "$gw$1" -H "X-API-Key: $2" -H 'Content-Type: application/json' -d "$3"
}

gen='{"model":"local","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":5}'
embed='{"model":"local-768-v1","input":["integration smoke"]}'

echo "gateway ($gw)"
ready=$(curl -s --max-time 10 "$gw/v1/ready")
check "ready" 200 "$(status "$gw/v1/ready")"
check "redis connected" true "$(printf '%s' "$ready" | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("redis")).lower())')"

replicas=$(for _ in 1 2 3 4 5 6 7 8; do
  curl -s -D - -o /dev/null "$gw/v1/health" | tr -d '\r' | sed -n 's/^[Xx]-[Ss]erved-[Bb]y: //p'
done | sort -u | wc -l | tr -d ' ')
check "replicas serving (distinct upstreams over 8 calls)" 2 "$replicas"

check "dev token refused" 401 "$(call /v1/generate air-classifier-dev "$gen")"
check "classifier: generate" 200 "$(call /v1/generate "$AIR_CLASSIFIER_SERVICE_TOKEN" "$gen")"
check "orchestrator: generate" 200 "$(call /v1/generate "$AIR_ORCHESTRATOR_SERVICE_TOKEN" "$gen")"
check "tools: generate" 200 "$(call /v1/generate "$AIR_TOOLS_TOKEN" "$gen")"
check "rag: embed" 200 "$(call /v1/embed "$AIR_RAG_TOKEN" "$embed")"
check "rag: generate not granted" 403 "$(call /v1/generate "$AIR_RAG_TOKEN" "$gen")"
pin='{"pin":"ollama:llama3.2:3b","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":5}'
check "classifier: pin refused (no grant)" 403 "$(call /v1/generate "$AIR_CLASSIFIER_SERVICE_TOKEN" "$pin")"
check "web-console: pin allowed" 200 "$(call /v1/generate "$AIR_WEB_CONSOLE_TOKEN" "$pin")"
unpublished='{"model":"local-1024-v1","input":["x"]}'
check "classifier: unpublished embedding model" 422 "$(call /v1/embed "$AIR_CLASSIFIER_SERVICE_TOKEN" "$unpublished")"
other='{"model":"cloud-1536-v1","input":["x"]}'
check "rag: embedding model outside its grant" 403 "$(call /v1/embed "$AIR_RAG_TOKEN" "$other")"
finish=$(curl -s --max-time 90 -X POST "$gw/v1/generate" -H "X-API-Key: $AIR_TOOLS_TOKEN" \
  -H 'Content-Type: application/json' -d "$gen" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("finish_reason"))')
normalized=$finish
if [ "$finish" = stop ] || [ "$finish" = length ]; then normalized=yes; fi
check "finish_reason normalized (stop|length)" yes "$normalized"
check "web-console: embed not granted" 403 "$(call /v1/embed "$AIR_WEB_CONSOLE_TOKEN" "$embed")"
events=$(curl -sN --max-time 90 -X POST "$gw/v1/generate" -H "X-API-Key: $AIR_TOOLS_TOKEN" \
  -H 'Content-Type: application/json' -d '{"model":"local","stream":true,"cache":false,"max_tokens":20,"messages":[{"role":"user","content":"Say hi"}]}' \
  | sed -n 's/^event: //p' | tr '\n' ' ')
case "$events" in "start delta"*" done ") streamed=yes ;; *) streamed="$events" ;; esac
check "stream: start, deltas, done" yes "$streamed"
check "web-console: policy probe" 200 "$(status -H "X-API-Key: $AIR_WEB_CONSOLE_TOKEN" "$gw/v1/admin/policy")"
check "classifier: policy probe not granted" 403 "$(status -H "X-API-Key: $AIR_CLASSIFIER_SERVICE_TOKEN" "$gw/v1/admin/policy")"

up() { curl -s -o /dev/null --max-time 2 "$1"; }
if up http://127.0.0.1:8082/v1/health; then
  echo "air-classifier-service (:8082)"
  check "classify via the gateway (t2)" 200 "$(status -X POST http://127.0.0.1:8082/v1/classify \
    -H 'X-API-Key: airc_local_dev_key' -H 'Content-Type: application/json' \
    -d '{"text":"Great battery, awful camera.","options":{"min_tier":"t2_local_llm","max_tier":"t2_local_llm"}}')"
fi
if up http://127.0.0.1:8081/v1/health; then
  echo "air-orchestrator-service (:8081)"
  # The orchestrator answers 200 even when its model call fails (a canned
  # "degraded" reply), so the turn status is what proves the gateway answered.
  check "customer chat turn answered by the gateway" ok "$(curl -s --max-time 90 \
    -X POST http://127.0.0.1:8081/v1/chat \
    -H 'X-API-Key: airo_local_customer_key' -H 'Content-Type: application/json' \
    -d '{"message":"hello"}' | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))')"
fi

echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
