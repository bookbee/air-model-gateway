#!/bin/sh
# Fills the gateway's Secret Manager secrets for one environment. The secrets
# themselves (empty containers + IAM) come from deploy/terraform; values never
# go through Terraform state.
#
#   deploy/gke/bootstrap-secrets.sh <project> <staging|prod> [--rotate | --retire-old]
#
#   (no flag)     First run: issue a token per consumer into
#                 <service>-model-gateway-token, and the token:service map into
#                 air-model-gateway-service-tokens. Re-runs keep existing tokens.
#   --rotate      Phase 1 of a no-downtime rotation: new token per consumer;
#                 the gateway map holds old AND new. Then restart the gateway,
#                 then the consumers (they pick up their new token).
#   --retire-old  Phase 2: the map holds only the consumers' current tokens.
#                 Restart the gateway; old tokens stop working.
#
# Provider keys are added when present in this shell's environment, under the
# setting's own name (AIR_MODEL_GATEWAY_PROVIDERS__GEMINI_API_KEY=... ). They
# are piped to gcloud on stdin, never passed as arguments. Prints no values.
set -eu

project=${1:?usage: bootstrap-secrets.sh <project> <staging|prod> [--rotate|--retire-old]}
env=${2:?usage: bootstrap-secrets.sh <project> <staging|prod> [--rotate|--retire-old]}
mode=${3:-}
case "$env" in staging|prod) ;; *) echo "environment must be staging or prod" >&2; exit 2;; esac
case "$mode" in ""|--rotate|--retire-old) ;; *) echo "unknown option $mode" >&2; exit 2;; esac

services="air-classifier-service air-orchestrator-service air-web-console air-tools air-rag"
map_secret=air-model-gateway-service-tokens

read_latest() { gcloud secrets versions access latest --project "$project" --secret "$1" 2>/dev/null || true; }
add_version() { gcloud secrets versions add "$1" --project "$project" --data-file=- >/dev/null; }

current_map=$(read_latest "$map_secret")
pairs=""
for service in $services; do
  secret="$service-model-gateway-token"
  token=$(read_latest "$secret")
  if [ -z "$token" ] || [ "$mode" = --rotate ]; then
    old=$token
    token="amg_${env}_$(openssl rand -hex 20)"
    printf '%s' "$token" | add_version "$secret"
    echo "  $secret: new version"
    # During a rotation the old token stays valid until --retire-old.
    [ "$mode" = --rotate ] && [ -n "$old" ] && pairs="${pairs:+$pairs,}$old:$service"
  else
    echo "  $secret: kept"
  fi
  pairs="${pairs:+$pairs,}$token:$service"
done

if [ "$pairs" != "$current_map" ]; then
  printf '%s' "$pairs" | add_version "$map_secret"
  echo "  $map_secret: new version (restart the gateway to load it)"
else
  echo "  $map_secret: unchanged"
fi

for key in ANTHROPIC_API_KEY GEMINI_API_KEY OPENAI_API_KEY; do
  var="AIR_MODEL_GATEWAY_PROVIDERS__$key"
  secret="air-model-gateway-$(echo "$key" | tr 'A-Z_' 'a-z-')"
  value=$(printenv "$var" || true)
  if [ -n "$value" ]; then
    printf '%s' "$value" | add_version "$secret"
    echo "  $secret: new version"
  elif [ -z "$(read_latest "$secret")" ]; then
    echo "  $secret: EMPTY. Add a key, or drop it from secrets.gcp.items (a listed secret with no version stops pods starting)"
  fi
done
