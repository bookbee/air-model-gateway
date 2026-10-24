#!/bin/sh
# Generates the local integration stack's secrets, once. Re-running keeps existing
# tokens, so consumers configured with them keep working; delete
# deploy/local/integration/secrets/ (or `make integration-rotate`) to rotate everything.
#
#   secrets/             mounted read-only into the gateway as SECRETS_DIR:
#                        one file per setting, named like its env var
#   tokens.env           the per-consumer tokens, for configuring consumers
#
# Provider keys are copied from the repo's .env if it has them. Nothing here is
# printed. Both paths are gitignored.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
secrets="$here/secrets"
tokens="$here/tokens.env"
redis_url=${STAGING_REDIS_URL:-redis://:localdevpass@host.docker.internal:6379/0}

mkdir -p "$secrets"

if [ ! -f "$tokens" ]; then
  : > "$tokens"
  for service in air-classifier-service air-orchestrator-service air-web-console air-tools air-rag; do
    var=$(echo "$service" | tr 'a-z-' 'A-Z_')_TOKEN
    echo "$var=amg_int_$(openssl rand -hex 20)" >> "$tokens"
  done
fi

# token:service pairs, rebuilt from tokens.env every run
# (AIR_CLASSIFIER_SERVICE_TOKEN=t  ->  t:air-classifier-service).
pairs=""
while IFS='=' read -r var token; do
  [ -n "$var" ] || continue
  service=$(echo "${var%_TOKEN}" | tr 'A-Z_' 'a-z-')
  pairs="${pairs:+$pairs,}$token:$service"
done < "$tokens"
printf '%s' "$pairs" > "$secrets/AIR_MODEL_GATEWAY_SECURITY__SERVICE_TOKENS"
printf '%s' "$redis_url" > "$secrets/AIR_MODEL_GATEWAY_REDIS__URL"

for key in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY; do
  name="AIR_MODEL_GATEWAY_PROVIDERS__$key"
  value=$(grep -E "^$name=" "$root/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' || true)
  if [ -n "$value" ]; then
    printf '%s' "$value" > "$secrets/$name"
  else
    rm -f "$secrets/$name"
  fi
done

# The gateway runs as a non-root user inside the container; on a laptop the
# mount keeps host ownership, so the files must be world-readable to it.
chmod 755 "$secrets"
chmod 644 "$secrets"/* "$tokens"

echo "integration secrets: $(find "$secrets" -type f | wc -l | tr -d ' ') files in deploy/local/integration/secrets/"
echo "consumer tokens: deploy/local/integration/tokens.env"
