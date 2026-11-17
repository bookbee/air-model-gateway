VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
UVICORN := $(VENV)/bin/uvicorn
COMPOSE ?= docker compose

HOST ?= 0.0.0.0
PORT ?= 8083
OLLAMA_MODEL ?= llama3.2:3b
# Every Ollama model deploy/routing.yaml names: the `local` chain's two rungs and
# the `local-768-v1` embedding model. Pulling only the first would leave the
# second rung and every embed call failing on a fresh machine.
OLLAMA_MODELS ?= llama3.2:3b ministral-3:3b nomic-embed-text
OLLAMA_NUM_CTX ?= 8192

# ---- ollama backend --------------------------------------------------------
# native | container.
#
#   native     talks to the ollama already running on the laptop (`ollama
#              serve`) — no container, no image pull, full host performance
#              (GPU on macOS via Metal, which a containerised ollama on Docker
#              Desktop cannot reach — CPU only). Default: a dev laptop that
#              runs this Makefile typically already has ollama running day to
#              day, so this is the zero-extra-cost path, not the exception.
#   container  runs ollama in this stack instead. No host setup needed; the
#              fallback for a fresh checkout with nothing native running.
#
# `make up` checks native reachability before choosing either path, and if
# nothing answers it prints both next steps and stops — it never silently
# falls back to a different backend than the one asked for.
OLLAMA ?= native

ifeq ($(OLLAMA),container)
OLLAMA_PROFILE := --profile ollama
OLLAMA_URL     := http://ollama:11434
else ifeq ($(OLLAMA),native)
OLLAMA_PROFILE :=
OLLAMA_URL     := http://host.docker.internal:11434
else
$(error OLLAMA must be `native` or `container`, got `$(OLLAMA)`)
endif

export AIR_MODEL_GATEWAY_OLLAMA_URL := $(OLLAMA_URL)

# ---- redis (shared cache, rate limits, quotas, spend) ----------------------
# none | infra.
#
#   none   in-process cache and rate limits. Right for one replica, and the
#          zero-dependency default.
#   infra  the Redis that air-infrastructure-service's `make up` runs on
#          :6379. A local convenience only: in shared environments DevOps
#          provides Redis and sets AIR_MODEL_GATEWAY_REDIS__URL directly.
#
# Either way the gateway serves without Redis; if `infra` is chosen but Redis
# is down, /v1/ready reports `"redis": false` and calls degrade gracefully.
REDIS ?= none
REDIS_PASSWORD ?= localdevpass

ifeq ($(REDIS),infra)
REDIS_HOST_URL      := redis://:$(REDIS_PASSWORD)@localhost:6379/0
REDIS_CONTAINER_URL := redis://:$(REDIS_PASSWORD)@host.docker.internal:6379/0
else ifeq ($(REDIS),none)
REDIS_HOST_URL      :=
REDIS_CONTAINER_URL :=
else
$(error REDIS must be `none` or `infra`, got `$(REDIS)`)
endif

export AIR_MODEL_GATEWAY_REDIS_URL := $(REDIS_CONTAINER_URL)

# ---- build identity ----------------------------------------------------------
# The checkout's revision (`-dirty` with uncommitted changes), baked into every
# image this Makefile builds (the Dockerfile's REVISION build arg) and read by
# `make run`. /v1/health reports it, so a test result can name the code that
# produced it.
REVISION ?= $(shell git describe --always --dirty --abbrev=12 2>/dev/null || echo unknown)
export AIR_MODEL_GATEWAY_REVISION := $(REVISION)

SOURCES := src tests docs/design

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@echo "air-model-gateway — make <target>"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Overridable: HOST=$(HOST) PORT=$(PORT) OLLAMA=$(OLLAMA) OLLAMA_MODEL=$(OLLAMA_MODEL) REDIS=$(REDIS)"
	@echo "Redis defaults to none (in-process state) — REDIS=infra uses air-infrastructure-service's Redis on :6379."
	@echo "Ollama defaults to native ($(OLLAMA_URL)) — 'OLLAMA=container make up' to use the bundled container instead."

# ---- local stack -----------------------------------------------------------
.PHONY: up
up: ## Start air-model-gateway [, ollama]
ifeq ($(OLLAMA),native)
	@curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 || { \
		echo ""; \
		echo "OLLAMA=native (the default) but nothing answers on 127.0.0.1:11434."; \
		echo ""; \
		echo "  Option 1 — start native ollama, then re-run this command:"; \
		echo "      ollama serve"; \
		echo ""; \
		echo "  Option 2 — use air-model-gateway's bundled ollama container instead:"; \
		echo "      OLLAMA=container make up"; \
		echo ""; \
		exit 1; \
	}
	@echo "ollama: native on the host — stopping the container copy if it is up"
	@$(COMPOSE) rm -sf ollama >/dev/null 2>&1 || true
endif
	$(COMPOSE) $(OLLAMA_PROFILE) up -d --build
	@echo
	@echo "air-model-gateway:  http://localhost:$(PORT)/v1/health"
	@echo "ollama:   $(OLLAMA) at $(OLLAMA_URL)"
	@echo "redis:    $(REDIS)"
ifeq ($(OLLAMA),container)
	@echo "run 'make ollama-pull' once ollama is up"
endif

.PHONY: down
down: ## Stop the stack (keep volumes)
	$(COMPOSE) --profile ollama --profile observability down

.PHONY: nuke
nuke: ## Stop the stack and DELETE all volumes (data loss)
	$(COMPOSE) --profile ollama --profile observability down -v

.PHONY: up-observability
up-observability: ## Also start prometheus :9090
	$(COMPOSE) --profile observability up -d
	@echo "prometheus: http://localhost:9090/targets"

.PHONY: ps
ps: ## Show stack status
	$(COMPOSE) --profile ollama --profile observability ps

.PHONY: logs
logs: ## Tail air-model-gateway logs (SVC=<name> to pick another)
	$(COMPOSE) logs -f $(or $(SVC),air-model-gateway)

.PHONY: ollama-pull
ollama-pull: ## Pull every Ollama model the routing uses (OLLAMA_MODELS)
ifeq ($(OLLAMA),native)
	@for m in $(OLLAMA_MODELS); do ollama pull $$m || exit 1; done
else
	@for m in $(OLLAMA_MODELS); do $(COMPOSE) exec ollama ollama pull $$m || exit 1; done
endif

.PHONY: ollama-warm
ollama-warm: ## Load the model at OLLAMA_NUM_CTX so the first real request is not a reload
	@echo "warming $(OLLAMA_MODEL) at num_ctx=$(OLLAMA_NUM_CTX) ..."
	@curl -sf -m 300 http://127.0.0.1:11434/api/chat -o /dev/null -w '  loaded in %{time_total}s\n' \
		-d '{"model":"$(OLLAMA_MODEL)","stream":false,"keep_alive":"30m","options":{"num_ctx":$(OLLAMA_NUM_CTX)},"messages":[{"role":"user","content":"ok"}]}' \
		|| { echo "  failed — is ollama serving on :11434?"; exit 1; }

# ---- self-hosted backend (llama.cpp), local exploration only --------------
# Not part of `make up` — a separate, opt-in thing to point AIR_MODEL_GATEWAY_SELF_HOSTED
# at. `brew install llama.cpp` first.
#
# -c 4096 --parallel 1 is load-bearing, not cosmetic: llama-server's default
# context (the model's full trained window) times the default 4 parallel
# slots overflowed this M1 Pro's Metal GPU memory budget on first boot
# ("Insufficient Memory" / kIOGPUCommandBufferCallbackErrorOutOfMemory,
# followed by every real request 500ing with "Compute error") — it *loads*
# fine either way, so this only surfaces once a real request is sent, not at
# boot. Bump -c if you need a longer context and have the memory for it.
LLAMACPP_MODEL ?= bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M
LLAMACPP_ALIAS ?= llama-3.2-3b-gguf
LLAMACPP_PORT ?= 8181
LLAMACPP_CTX ?= 4096

.PHONY: llamacpp-up
llamacpp-up: ## Start llama-server (LLAMACPP_MODEL, downloaded/cached on first run)
	@pgrep -f "llama-server.*--port $(LLAMACPP_PORT)" >/dev/null \
		&& { echo "already running on :$(LLAMACPP_PORT)"; exit 0; } || true
	llama-server -hf $(LLAMACPP_MODEL) --port $(LLAMACPP_PORT) --alias $(LLAMACPP_ALIAS) \
		-c $(LLAMACPP_CTX) --parallel 1 > /tmp/llama-server.log 2>&1 &
	@echo "starting — first run downloads the model, can take a few minutes"
	@echo "log: /tmp/llama-server.log · point AIR_MODEL_GATEWAY_SELF_HOSTED__BASE_URL at http://localhost:$(LLAMACPP_PORT)/v1"

.PHONY: llamacpp-down
llamacpp-down: ## Stop llama-server
	@pkill -f "llama-server.*--port $(LLAMACPP_PORT)" && echo "stopped" || echo "not running"

# ---- local integration stack (laptop harness, never deployed) --------------
# deploy/local/ holds a test harness only; the Helm chart and Terraform that
# deploy this service live in air-infrastructure-service (gke/). This stack is
# the production image with the
# staging configuration: ENVIRONMENT=staging (fail-closed policy),
# deploy/policy/policy.staging.yaml, generated tokens and provider keys as
# SECRETS_DIR files, Redis, JSON logs, two replicas behind nginx on :9083. It
# exists so consumers running on this laptop can be integration-tested against a
# production-shaped gateway (kind can't serve them: its gateway is in-cluster
# only). Needs air-infrastructure-service's Redis on :6379 and, for local
# models, the host's Ollama / llama-server.
INTEGRATION := $(COMPOSE) -f deploy/local/integration/compose.yml

.PHONY: integration-secrets
integration-secrets: ## Generate integration-stack tokens + secret files (kept on re-run)
	@deploy/local/integration/make-secrets.sh

.PHONY: integration-up
integration-up: integration-secrets ## Start the local integration stack on :9083 (2 replicas + nginx)
	@# --wait: return only once both replicas pass their health check, so an
	@# `integration-test` straight after doesn't hit nginx before they listen.
	$(INTEGRATION) up -d --build --wait --wait-timeout 120
	@echo
	@echo "integration gateway: http://localhost:9083  (tokens: deploy/local/integration/tokens.env)"

.PHONY: integration-test
integration-test: ## Smoke-test the integration stack (+ consumers if running)
	@deploy/local/integration/smoke.sh

.PHONY: integration-rotate
integration-rotate: ## Issue new integration-stack tokens and restart the gateways on them
	rm -rf deploy/local/integration/secrets deploy/local/integration/tokens.env
	@deploy/local/integration/make-secrets.sh
	$(INTEGRATION) restart gateway

.PHONY: integration-down
integration-down: ## Stop the local integration stack
	$(INTEGRATION) down

.PHONY: integration-ps
integration-ps: ## Show integration stack status
	$(INTEGRATION) ps

.PHONY: integration-logs
integration-logs: ## Tail integration-stack gateway logs (JSON)
	$(INTEGRATION) logs -f gateway

# ---- image and deployment (UAT and production) ------------------------------
# This repository owns its image and the gateway's own configuration
# (deploy/routing.yaml, deploy/pricing.yaml, deploy/policy/). The Helm chart,
# the Terraform and the deployment scripts live in air-infrastructure-service
# (gke/), where DevOps keeps them for all five core services. The targets below
# build and push the image here, then hand over to that repository, checked out
# next to this one (INFRA_DIR). Its docs/03-gke-runbook.md is the runbook.
#
#   DEPLOY_ENV    uat | prod. UAT runs with AIR_MODEL_GATEWAY_ENVIRONMENT=staging
#                 and deploy/policy/policy.staging.yaml: `staging` is this
#                 service's own name for its pre-production environment.
#   IMAGE_REPOSITORY  <region>-docker.pkg.dev/<project>/air/air-model-gateway
#   IMAGE_TAG     defaults to this checkout's short commit
#   IMAGE_DIGEST  what CI deploys: a digest can't be re-pointed after review
#   KUBE_CONTEXT  required by deploy / deploy-smoke; never defaulted, so a
#                 deploy can't land on whatever cluster happens to be current
#   GCP_PROJECT   the environment's project: Secret Manager for the chart, and
#                 where deploy-smoke reads the consumer tokens
DEPLOY_ENV ?= uat
INFRA_DIR ?= ../air-infrastructure-service
SERVICE   := air-model-gateway
IMAGE_TAG ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo dev)
INFRA_MAKE = $(MAKE) --no-print-directory -C $(INFRA_DIR) SERVICE=$(SERVICE) DEPLOY_ENV=$(DEPLOY_ENV) \
	GATEWAY_DIR="$(CURDIR)" KUBE_CONTEXT="$(KUBE_CONTEXT)" GCP_PROJECT="$(GCP_PROJECT)" \
	IMAGE_REPOSITORY="$(IMAGE_REPOSITORY)" IMAGE_DIGEST="$(IMAGE_DIGEST)" \
	IMAGE_TAG="$(if $(IMAGE_DIGEST),,$(IMAGE_TAG))"

ifneq ($(filter $(DEPLOY_ENV),uat prod),$(DEPLOY_ENV))
$(error DEPLOY_ENV must be `uat` or `prod`, got `$(DEPLOY_ENV)`)
endif

.PHONY: require-infra-repo
require-infra:
	@test -f $(INFRA_DIR)/gke/scripts/release.sh || { \
		echo "air-infrastructure-service is not at $(INFRA_DIR): the charts and deploy scripts live there."; \
		echo "Check it out next to this repository, or pass INFRA_DIR=<path>."; exit 1; }

.PHONY: require-image-repository
require-image-repository:
	@test -n "$(IMAGE_REPOSITORY)" || { echo "set IMAGE_REPOSITORY, e.g. <region>-docker.pkg.dev/<project>/air/$(SERVICE)"; exit 1; }

.PHONY: image
image: ## Build the image locally as $(SERVICE):$(IMAGE_TAG)
	docker build --build-arg REVISION=$(REVISION) -t $(SERVICE):$(IMAGE_TAG) .

.PHONY: image-push
image-push: require-image-repository ## Build for linux/amd64 and push IMAGE_REPOSITORY:IMAGE_TAG (gcloud auth configure-docker first)
	docker build --platform linux/amd64 --build-arg REVISION=$(REVISION) -t $(IMAGE_REPOSITORY):$(IMAGE_TAG) .
	docker push $(IMAGE_REPOSITORY):$(IMAGE_TAG)

.PHONY: deploy-render
deploy-render: require-infra-repo ## Print the manifests DEPLOY_ENV would get (SAMPLE=1 fills the project placeholders)
	@$(INFRA_MAKE) SAMPLE="$(SAMPLE)" gke-render

.PHONY: deploy-validate
deploy-validate: require-infra-repo ## Lint and schema-check the charts and Terraform for every service (runs in air-infrastructure-service)
	@$(INFRA_MAKE) gke-validate

.PHONY: deploy
deploy: require-infra-repo ## Deploy this service to DEPLOY_ENV (uat|prod) in KUBE_CONTEXT: helm upgrade --install
	@$(INFRA_MAKE) gke-deploy

.PHONY: deploy-smoke
deploy-smoke: require-infra-repo ## Smoke-test this service in DEPLOY_ENV: tokens from GCP_PROJECT
	@$(INFRA_MAKE) gke-smoke

.PHONY: kind-up
kind-up: require-infra-repo ## The production charts on a local kind cluster: this service and the other four
	@$(INFRA_MAKE) kind-up

.PHONY: kind-test
kind-test: require-infra-repo ## Smoke-test the kind deployment (+ NetworkPolicy)
	@$(INFRA_MAKE) kind-test

.PHONY: kind-down
kind-down: require-infra-repo ## Delete the kind cluster
	@$(INFRA_MAKE) kind-down

# ---- dev inner loop --------------------------------------------------------
$(PY):
	python3.12 -m venv $(VENV)

.PHONY: install
install: $(PY) ## .venv with the exact locked runtime (no dev tools)
	$(PIP) install --upgrade pip
	$(PIP) install --require-hashes --no-deps -r requirements.lock
	$(PIP) install --no-deps -e .

.PHONY: dev
dev: $(PY) ## Exact, hash-checked dev environment from requirements-dev.lock
	$(PIP) install --upgrade pip
	$(PIP) install --require-hashes --no-deps -r requirements-dev.lock
	$(PIP) install --no-deps -e .

# Dependencies are locked: uv.lock is the source, the two requirements*.lock
# files are its hash-pinned exports (the image installs requirements.lock, `make
# dev` the -dev one). Change a dependency in pyproject.toml, then `make lock`;
# `make lock-upgrade` moves everything to the newest allowed versions. Both
# need uv (https://docs.astral.sh/uv/). CI fails if the lock is stale.
.PHONY: lock
lock: ## Re-lock after editing dependencies in pyproject.toml
	uv lock
	uv export --frozen --no-dev --extra providers --no-emit-project -o requirements.lock >/dev/null
	uv export --frozen --extra providers --extra dev --no-emit-project -o requirements-dev.lock >/dev/null

.PHONY: lock-upgrade
lock-upgrade: ## Upgrade every locked dependency (then make check)
	uv lock --upgrade
	@$(MAKE) --no-print-directory lock

.PHONY: lock-check
lock-check: ## Fail if uv.lock or the exports are out of date
	uv lock --check
	@tmp=$$(mktemp -d) && cp requirements.lock requirements-dev.lock "$$tmp"/ && \
		$(MAKE) --no-print-directory -s lock >/dev/null 2>&1 && \
		diff -q "$$tmp/requirements.lock" requirements.lock >/dev/null && \
		diff -q "$$tmp/requirements-dev.lock" requirements-dev.lock >/dev/null \
		|| { echo "requirements*.lock are stale: run make lock and commit"; exit 1; }

.PHONY: run
run: ## Run air-model-gateway on the host against a local ollama
	AIR_MODEL_GATEWAY_OLLAMA__BASE_URL=http://localhost:11434 \
	AIR_MODEL_GATEWAY_REDIS__URL=$(REDIS_HOST_URL) \
	$(UVICORN) air_model_gateway.main:app --reload --host $(HOST) --port $(PORT)

.PHONY: test
test: ## Run tests with coverage (fails under the floor in pyproject.toml)
	$(PYTEST) -q --cov --cov-report=term

.PHONY: lint
lint: ## Ruff lint
	$(PY) -m ruff check $(SOURCES)

.PHONY: fmt
fmt: ## Ruff format and autofix
	$(PY) -m ruff format $(SOURCES)
	$(PY) -m ruff check --fix $(SOURCES)

.PHONY: typecheck
typecheck: ## Mypy
	$(PY) -m mypy src

.PHONY: openapi
openapi: ## Write the API contract to docs/openapi.json (commit it)
	@$(PY) -c "import json, logging, pathlib; logging.disable(logging.CRITICAL); \
		from air_model_gateway.config import Settings; from air_model_gateway.main import create_app; \
		spec = create_app(Settings(_env_file=None)).openapi(); \
		pathlib.Path('docs/openapi.json').write_text(json.dumps(spec, indent=2) + '\n')" >/dev/null
	@echo "wrote docs/openapi.json"

# The visual design document. Headless Chrome prints the HTML pages, so the PDF
# matches what a browser shows; CHROME points at any Chrome or Chromium binary.
CHROME ?= $(if $(filter Darwin,$(shell uname -s)),/Applications/Google Chrome.app/Contents/MacOS/Google Chrome,google-chrome)

.PHONY: design
design: ## Redraw docs/design diagrams and print docs/air-model-gateway-design.pdf (commit both)
	$(PY) docs/design/diagrams.py
	@"$(CHROME)" --headless=new --disable-gpu --no-pdf-header-footer \
		--print-to-pdf="$(CURDIR)/docs/air-model-gateway-design.pdf" \
		"file://$(CURDIR)/docs/design/design.html" 2>/dev/null
	@echo "wrote docs/air-model-gateway-design.pdf"

.PHONY: check
check: lint typecheck test ## Lint + typecheck + test

# ---- the gates ---------------------------------------------------------------
# `make verify` is the definition of done for a change: every static gate CI
# runs, in one command. `make kind-up kind-test` (and `integration-up integration-test`)
# are the runtime gates on top. docs/05-handover.md explains each.
.PHONY: lint-scripts
lint-scripts: ## shellcheck every script, hadolint the Dockerfile
	shellcheck -S warning deploy/local/integration/*.sh
	docker run --rm -i hadolint/hadolint:v2.12.0 < Dockerfile

.PHONY: audit
audit: ## Known-vulnerability scan of the locked runtime dependencies
	uvx pip-audit@2.7.3 --require-hashes --disable-pip -r requirements.lock

.PHONY: verify
verify: check lock-check lint-scripts audit ## Every static gate this repository's CI runs (charts: make deploy-validate)
	@echo "verify: all gates passed"

.PHONY: clean
clean: ## Remove the venv and tool caches
	rm -rf $(VENV) .cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
