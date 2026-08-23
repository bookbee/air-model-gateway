VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
UVICORN := $(VENV)/bin/uvicorn
COMPOSE ?= docker compose

HOST ?= 0.0.0.0
PORT ?= 8083
OLLAMA_MODEL ?= llama3.2:3b
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

export AIR_LLM_OLLAMA_URL := $(OLLAMA_URL)

SOURCES := src client tests

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@echo "air-llm — make <target>"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Overridable: HOST=$(HOST) PORT=$(PORT) OLLAMA=$(OLLAMA) OLLAMA_MODEL=$(OLLAMA_MODEL)"
	@echo "Ollama defaults to native ($(OLLAMA_URL)) — 'OLLAMA=container make up' to use the bundled container instead."

# ---- local stack -----------------------------------------------------------
.PHONY: up
up: ## Start air-llm [, ollama]
ifeq ($(OLLAMA),native)
	@curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 || { \
		echo ""; \
		echo "OLLAMA=native (the default) but nothing answers on 127.0.0.1:11434."; \
		echo ""; \
		echo "  Option 1 — start native ollama, then re-run this command:"; \
		echo "      ollama serve"; \
		echo ""; \
		echo "  Option 2 — use air-llm's bundled ollama container instead:"; \
		echo "      OLLAMA=container make up"; \
		echo ""; \
		exit 1; \
	}
	@echo "ollama: native on the host — stopping the container copy if it is up"
	@$(COMPOSE) rm -sf ollama >/dev/null 2>&1 || true
endif
	$(COMPOSE) $(OLLAMA_PROFILE) up -d --build
	@echo
	@echo "air-llm:  http://localhost:$(PORT)/v1/health"
	@echo "ollama:   $(OLLAMA) at $(OLLAMA_URL)"
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
logs: ## Tail air-llm logs (SVC=<name> to pick another)
	$(COMPOSE) logs -f $(or $(SVC),air-llm)

.PHONY: ollama-pull
ollama-pull: ## Pull the default model into whichever ollama is configured
ifeq ($(OLLAMA),native)
	ollama pull $(OLLAMA_MODEL)
else
	$(COMPOSE) exec ollama ollama pull $(OLLAMA_MODEL)
endif

.PHONY: ollama-warm
ollama-warm: ## Load the model at OLLAMA_NUM_CTX so the first real request is not a reload
	@echo "warming $(OLLAMA_MODEL) at num_ctx=$(OLLAMA_NUM_CTX) ..."
	@curl -sf -m 300 http://127.0.0.1:11434/api/chat -o /dev/null -w '  loaded in %{time_total}s\n' \
		-d '{"model":"$(OLLAMA_MODEL)","stream":false,"keep_alive":"30m","options":{"num_ctx":$(OLLAMA_NUM_CTX)},"messages":[{"role":"user","content":"ok"}]}' \
		|| { echo "  failed — is ollama serving on :11434?"; exit 1; }

# ---- self-hosted backend (llama.cpp), local exploration only --------------
# Not part of `make up` — a separate, opt-in thing to point AIR_LLM_SELF_HOSTED
# at, the way LM Studio was used earlier. `brew install llama.cpp` first.
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
	@echo "log: /tmp/llama-server.log · point AIR_LLM_SELF_HOSTED__BASE_URL at http://localhost:$(LLAMACPP_PORT)/v1"

.PHONY: llamacpp-down
llamacpp-down: ## Stop llama-server
	@pkill -f "llama-server.*--port $(LLAMACPP_PORT)" && echo "stopped" || echo "not running"

# ---- dev inner loop --------------------------------------------------------
$(PY):
	python3.12 -m venv $(VENV)

.PHONY: install
install: $(PY) ## Create .venv and install the package with the providers extra
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[providers]"

.PHONY: dev
dev: install ## Add the development toolchain (pytest, ruff, mypy)
	$(PIP) install -e ".[providers,dev]"

.PHONY: run
run: ## Run air-llm on the host against a local ollama
	AIR_LLM_OLLAMA__BASE_URL=http://localhost:11434 \
	$(UVICORN) air_llm.main:app --reload --host $(HOST) --port $(PORT)

.PHONY: test
test: ## Run tests
	$(PYTEST) -q

.PHONY: lint
lint: ## Ruff lint
	$(PY) -m ruff check $(SOURCES)

.PHONY: fmt
fmt: ## Ruff format and autofix
	$(PY) -m ruff format $(SOURCES)
	$(PY) -m ruff check --fix $(SOURCES)

.PHONY: typecheck
typecheck: ## Mypy
	$(PY) -m mypy src client

.PHONY: check
check: lint typecheck test ## Lint + typecheck + test

.PHONY: clean
clean: ## Remove the venv and tool caches
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
