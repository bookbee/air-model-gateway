# AIR Model Gateway — Consumer Migration Notes

**Companion docs:** [Integration guide](04-integration.md) · [Handover](05-handover.md) ·
[OpenAPI contract](openapi.json)

What each AIR service changes to move from `POST /v1/inference` to
`POST /v1/generate` and `POST /v1/embed`, and how streaming fits in. Each section is
self-contained: hand it to that service's own session. Paths are relative to the
service's repository.

**`POST /v1/inference` has been removed from air-model-gateway** (it now answers 404),
and so has the mapping of old alias names. A service works against the current
gateway only once it has made the changes below. This note is temporary: it is
deleted once every service has moved.

## Common to every service

| Before (`/v1/inference`) | After |
|---|---|
| `{"task": "chat", "model": "<alias>", "messages": …}` | `POST /v1/generate`, same body **without `task`**, `model` a class (table below) or omitted (`auto`) |
| `{"task": "embeddings", "model": "rag-embed", "input": …}` | `POST /v1/embed`, same body **without `task`**, `model: "local-768-v1"` |
| Response with `task` | Same response without `task`; generate has no `embeddings`, embed no `content`/`cached`/`refusal`/`finish_reason` |

Rules the routes enforce:
- **Unknown fields are 422.** A leftover `task` is the most likely one: several
  services set it through a default on their request model, so remove the field from
  the model, not just from call sites.
- **An old alias name is 422** (`invalid_model_choice` or a validation error naming
  `model`); nothing maps them any more.
- **`messages[].role`** must be `system`, `user` or `assistant`; `max_tokens` ≥ 1;
  `temperature` 0–2.
- **`model` is a class or a published embedding name, never an alias or model
  name.** Each old alias maps to the class that serves the same models for that
  service (the gateway's per-service tuning keeps behaviour identical):

| Service | Old alias | New `model` |
|---|---|---|
| air-classifier-service | `classify-local`, `summarize-default` | `local` |
| air-classifier-service | `classify-default` | `auto` |
| air-classifier-service | `classify-arbiter` | `cloud` |
| air-orchestrator-service | `generative` | `cloud` |
| air-tools | `agent_reason`, `agent_interpret` | `cloud` |
| air-rag | `rag-embed` | `local-768-v1` (on `/v1/embed`) |
| air-web-console | any model | `pin: "provider:model"` (granted), or a class |

  `GET /v1/capabilities` shows each service the models behind its classes.

Worth adopting at the same time:
- **Forward `X-Request-ID`** on every call, so one id follows a request through your
  logs and the gateway's audit line.
- **`finish_reason`** is now `stop`, `length`, `refusal` or `other` for every
  provider; `length` means the answer was cut off.
- **429 `budget_exceeded`** (with `Retry-After`) means stop until the next UTC day;
  don't retry it like `rate_limited`.

To verify locally: `make integration-up` in air-model-gateway, point the service at
`http://localhost:9083` with its token from `deploy/local/integration/tokens.env`, run
the service's own tests, then `make integration-test` in air-model-gateway.

## air-classifier-service

- **File:** `src/air_classifier_service/providers/air_llm_provider.py`.
- **Path:** `_INFERENCE_PATH` → `"/v1/generate"`.
- **Body:** delete `task: str = "chat"` from `_InferenceRequest`.
- **Truncation:** the existing check for `finish_reason == "length"` now works for
  every provider (Gemini and Anthropic used to report other spellings).
- **Request id:** it does not forward `X-Request-ID` yet; add it.
- **Timeout:** the T3 budget (8 s) is shorter than Gemini Flash takes with thinking
  (7–12 s). Raise the budget, or have the classifier's `auto` lead with a faster
  model (its override in air-model-gateway's `deploy/routing.yaml`).
- **Streaming:** not needed (structured, machine-read answers).

## air-orchestrator-service

- **File:** `src/air_orchestrator_service/clients/llm.py`.
- **Path:** `_INFERENCE_PATH` → `"/v1/generate"`.
- **Body:** delete `task: str = "chat"` from `_ChatBody`. It already forwards
  `X-Request-ID`.
- **Degraded turns:** a failed model call currently returns HTTP 200 `degraded` with
  an empty `degraded` list and the service stays ready; worth fixing while here.
- **Streaming (the one consumer that benefits):**
  1. Add a streaming method to the client: `POST /v1/generate` with `"stream": true`,
     read the response line by line, and parse `event:`/`data:` pairs. The reference
     client's `generate_stream()` in air-model-gateway
     (`src/air_model_gateway_client/client.py`) is a working example.
  2. Treat an HTTP error status as before (no stream started). After `start`, handle
     `delta` (append, and pass on), `done` (usage, cost, `finish_reason`) and `error`
     (the answer is incomplete).
  3. Stream the orchestrator's own `/v1/chat` to its callers, either relaying the
     gateway's events or translating them to its own event names; the gateway has
     already done failover by the time `start` arrives.
  4. Use a read timeout above the gateway's 30 s idle limit, not a total timeout; if
     the user disconnects, close the gateway stream so generation stops.

## air-tools

- **File:** `src/air_tools/clients/llm.py`.
- **Path:** `_INFERENCE_PATH` → `"/v1/generate"`.
- **Body:** delete `task: str = "chat"` from `_ChatBody`.
- **Request id:** forward `X-Request-ID`.
- **Streaming:** not needed (agents read whole answers).

## air-rag

- **File:** `src/air_rag/clients/llm.py`.
- **Path:** `_INFERENCE_PATH` → `"/v1/embed"`.
- **Body:** drop `"task": "embeddings"` from the request dict in `embed()`; keep
  `model` and `input`. Also update the module docstring, which describes the old
  route.
- **Model:** `local-768-v1`, set once in configuration. It is the index's identity:
  check the response's `dimensions` (768) against the index at startup, and store
  `embedding_model` with the vectors. A future model change arrives as a new name
  (`…-v2`) and means a re-index.
- **Request id:** forward `X-Request-ID`.

## air-web-console

- **Files:** `src/components/llm/LlmTab.tsx`, `src/components/llm/data.ts` (the request
  catalog), `src/components/llm/ScenarioPanel.tsx`, `src/components/sidebar/Sidebar.tsx`.
- **Catalog:** move each case to `/v1/generate` or `/v1/embed`, removing `task` from
  every body. The `task: admin_read` case no longer applies.
- **Catalog cases to add:**
  - an unknown field (for example `stream_options`): expect 422 naming the field;
  - each class (`local`, `auto`, `cloud`), and `cloud` with a `provider` preference;
  - a `provider` the class doesn't offer (`local` + `anthropic`): expect 422;
  - `pin` from a service without the grant: expect 403;
  - an unpublished embedding name: expect 422;
  - `GET /v1/capabilities`, to show what each class means for the console.
- **Removed route:** delete every `/v1/inference` case.
- **Streaming view:** `EventSource` can't send a POST body, so use `fetch` and read
  `response.body` with a reader, splitting on blank lines into `event:`/`data:`
  pairs. Render `delta` text as it arrives; show `done`'s usage, cost and
  `finish_reason`; show `error` as an interrupted answer.
- **Security:** the proxy currently returns the gateway's `X-API-Key` to the browser
  in its request details; strip it.
- **Naming:** the local dev token is still `air-client-dev`; renaming it is a change
  in both repositories (`.env.example` in air-model-gateway, `src/lib/config.ts` here).

## Naming, in every service

The gateway is a general-purpose model gateway named `air-model-gateway`. Services
that still call it `air-llm` or `llm` in their settings, clients, environment variables
and docs (for example `AIR_CLASSIFIER__AIR_LLM__TOKEN`,
`AIR_ORCHESTRATOR_SERVICE__LLM__API_KEY`, `AIR_WEB__TARGETS__<T>__LLM_BASE_URL`, the
`clients/llm.py` modules, the console's LLM tab) should rename them while making the
changes above, and update their deployment manifests and secret names to match.
