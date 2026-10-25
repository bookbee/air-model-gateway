# AIR Model Gateway — Consumer Integration Guide

**Companion docs:** [Overview](overview.md) · [Deployment](03-deployment.md) ·
[OpenAPI contract](openapi.json)

For a team whose service needs a model. It covers how to get access, how to call the
gateway, and how to handle its answers and failures robustly. The machine-readable
contract is [`docs/openapi.json`](openapi.json) (OpenAPI 3.1). Generate a client from
it, or read it in the gateway's own `/docs` page.

## 1. Getting access

The gateway team adds a new consumer; ask for:

| You provide | The gateway team changes |
|---|---|
| Service name (= your repo name) | Token issued per environment (`bootstrap-secrets.sh`, `make-secrets.sh`) |
| Operations: `generate`, `embed` | A grant in `policy.local.yaml`, `policy.staging.yaml`, `policy.prod.yaml` |
| Expected calls per minute | `rate_per_minute` in those files |
| Which model choices you need (classes; embedding model) and any special needs (latency, quality, no fallback) | Grants in the policy files; if needed, your own tuning of what a class means (`overrides` in `deploy/routing.yaml`); pinned by `tests/test_gateway_config.py` |
| Your Kubernetes namespace | `networkPolicy.allowedNamespaces` in the GKE values |

The full checklist the gateway team follows is in this repo's `AGENTS.md`.

You never name a model. You choose **where** it runs (a class) for text, or **which
embedding space** for vectors; the gateway team decides which models serve each
choice and can change them without touching your code (§3, "Choosing a model").

## 2. Where to call

| Environment | Base URL | Token |
|---|---|---|
| Laptop (`make run` / `make up`) | `http://localhost:8083` (`host.docker.internal:8083` from containers) | `<service>-dev`, from `.env.example` |
| Laptop integration stack (`make integration-up`) | `http://localhost:9083` | `deploy/local/integration/tokens.env` |
| GKE staging / prod | `http://air-model-gateway.air-model-gateway.svc.cluster.local` | Your `<service>-model-gateway-token` secret in Secret Manager |

The URL and the token are configuration in your service, never code. Use one token per
service per environment, never share it, and never send it to a browser. A web UI
calls its own backend, and the backend calls the gateway.

## 3. Calls

Two routes, both `POST` with `X-API-Key: <token>`:

| Route | For | Body | Policy action |
|---|---|---|---|
| `/v1/generate` | Text from messages: chat, classification, summaries, extraction; optionally JSON matching a schema; optionally streamed | `messages`; optional `model` (class), `provider`, `max_tokens`, `temperature`, `json_schema`, `schema_name`, `stream`, `cache` | `generate` |
| `/v1/embed` | One vector per input text | `model` (embedding name, required), `input` | `embed` |

### Choosing a model

**`/v1/generate`: `model` is a class**, `local`, `auto` (the default) or `cloud`:

| Class | Means | Use it when |
|---|---|---|
| `local` | Our own model servers only. The prompt never leaves the network; free | The data must stay in, or a small model is enough |
| `cloud` | Cloud providers only (Gemini, Anthropic, OpenAI) | You need stronger models |
| `auto` | The gateway's choice: local first today, cloud as fallback | You have no constraint; let the owner decide |

`auto` may send the prompt to a cloud provider: if your data must not leave the
network, send `local`. Each class is a chain of models with failover; which models,
and in what order, is the owner's configuration, possibly tuned for your service.
`GET /v1/capabilities` shows the chain behind each class **for your service**, which
classes you are granted (`allowed`), and which this deployment can serve at all
(`available`: some provider in the chain has a key). Pick from those with both true.

- **`provider`** (optional) is a preference within the class: `ollama` or `llama.cpp`
  for `local`; `gemini`, `anthropic` or `openai` for `cloud`; any of them for `auto`.
  That provider's models are tried first and the rest stay as fallback. A provider the
  class doesn't offer is a 422.
- **`pin`** (operator tools only, with a grant) names one exact model,
  `provider:model` (e.g. `anthropic:claude-haiku-4-5`), with no fallback. It replaces
  `model` and `provider`; applications never need it. The model must be in the
  gateway's price table (or local), so its spend counts toward your budget, and one its
  provider has; otherwise the pin is 422 `invalid_model_choice`.
- The response's `model_class`, `provider` and `model` say what actually answered.

**`/v1/embed`: `model` is a published embedding model**, named
`<local|cloud>-<dimensions>-v<version>`:

| Name | Model | Dimensions |
|---|---|---|
| `local-768-v1` | nomic-embed-text (Ollama) | 768 |
| `cloud-1536-v1` | text-embedding-3-small (OpenAI) | 1536 |

- **It is required:** the name is your index's identity. Put it in your service's
  configuration, once.
- **A name never changes meaning.** Vectors from the same name stay comparable forever.
  When the owner moves to a new model, it is published as a new version (`…-v2`); you
  re-index to move.
- **One model, no fallback.** If it is down, the call fails rather than returning
  vectors from another model that would corrupt your index.
- The response's `dimensions` lets you check, at startup, that it matches your index.
  `GET /v1/capabilities` lists the published names, which you are granted (`allowed`)
  and which this deployment can serve (`available`).

**Size limits**, checked before any model is called (422 `validation_error`):

| Route | Limit |
|---|---|
| `/v1/generate` | At most 500 `messages`, and 800,000 characters of content in all (about 200k tokens) |
| `/v1/embed` | At most 2,048 texts in `input`, each at most 32,000 characters (about 8k tokens) |

Split a larger embedding batch into several calls. A prompt within the limit can still
be too long for a particular model; then the provider's refusal comes back as 422
`provider_rejected`.

```bash
# Generate
curl -X POST $GW/v1/generate -H "X-API-Key: $TOKEN" -H "X-Request-ID: $RID" \
  -H "Content-Type: application/json" \
  -d '{"model":"cloud","max_tokens":300,
       "messages":[{"role":"system","content":"Be brief."},{"role":"user","content":"Hi"}]}'

# Structured output: content is a JSON string that should match the schema
curl -X POST $GW/v1/generate ... -d '{"model":"cloud","provider":"anthropic","max_tokens":300,
  "messages":[{"role":"user","content":"Classify: I love it"}],
  "json_schema":{"type":"object","properties":{"label":{"type":"string"}},"required":["label"]},
  "schema_name":"sentiment"}'

# Embed: one vector per input text, in order
curl -X POST $GW/v1/embed ... -d '{"model":"local-768-v1","input":["first","second"]}'
```

Response fields that matter:

| Field | Use it for |
|---|---|
| `content` (generate) / `embeddings` (embed) | The answer (with `stream: true`, assembled from `delta` events; see §4) |
| `model`, `provider` | Which model actually answered, after failover (log it; for embeddings, store it with the vectors) |
| `finish_reason` (generate) | `stop`, `length` (cut off at `max_tokens`: treat structured output as invalid), `refusal` or `other`, the same for every provider; `provider_finish_reason` has the raw value |
| `refusal` (generate) | The provider's safety layer declined; not an error status |
| `usage`, `cost_usd` | Tokens and USD for this call; 0 for local models and cache hits |
| `cached` (generate) | Served from the gateway's cache |

## 4. Making it robust

### Errors

Every error is `application/problem+json` with a stable `code` and the `request_id`:

```json
{"type":"about:blank","title":"Forbidden","status":403,"code":"policy_denied",
 "detail":"…","request_id":"…"}
```

| Status | `code` | Meaning | Retry? |
|---|---|---|---|
| 401 | `unauthorized` | Token missing, wrong, or from another environment (checked before the body) | No: fix config |
| 403 | `policy_denied` | No grant for this operation, or a class, embedding model, provider or `pin` outside your grant | No: ask for a grant |
| 404 | `not_found` | No such path (e.g. a route that was removed) | No: fix the URL |
| 405 | `method_not_allowed` | Wrong method for the path; `Allow` lists the right one | No: fix the call |
| 422 | `invalid_model_choice` | An unpublished embedding name, a provider the class doesn't offer, a malformed `pin`, or a pin to an unpriced model or one its provider doesn't have | No: fix the request (`/v1/capabilities` lists your choices) |
| 422 | `validation_error` | Bad body (e.g. `/v1/generate` without `messages`, invalid JSON, or over a size limit) | No: fix the request |
| 422 | `provider_rejected` | Every model in the chain refused the request as invalid (e.g. too long for it); `detail` has the providers' reasons | No: change the request |
| 429 | `rate_limited` | Your rate limit, or every rung of the chain throttled (by a provider or the gateway's provider quota) | Yes, with backoff |
| 429 | `budget_exceeded` | Your service's daily budget is spent | No: stop until `Retry-After` (next UTC day); cache hits still work |
| 502 | `provider_unavailable` | Every model in the chain failed for a reason other than your request (outage, timeout); `detail` lists each | Yes, with backoff |
| 503 | (no body contract) | A proxy or a gateway with no reachable provider | Yes, with backoff |
| 500 | `internal_error` | A gateway bug: report it with the `request_id` | Once at most |

**Retry policy.** Retry 429 (except `budget_exceeded`), 502 and 503 at most twice, with jittered exponential
backoff starting around 1 s. Retrying is safe because inference has no side effects,
but each retry costs money. The gateway already retries transient provider failures
once and fails over across the chain, so a 502 has been tried hard before it reaches
you. Don't add tight retry loops on top.

### Timeouts

The gateway has no per-request deadline parameter. Each model attempt has its own
deadline (60 s by default), and a chain can try several models plus one retry. Set your
HTTP client's timeout to your own latency budget. If the budget is tight (an
interactive path), ask the owner to tune your class so its first model is a fast one. When you time out, the
gateway may still finish the call, and it is billed.

A stream has two gateway-side deadlines instead: the first token within the same
per-attempt deadline (or the next model is tried), then no more than 30 s of silence
between tokens. Give your client a read timeout above that idle limit, not a total
timeout: a long answer can legitimately stream for minutes. Closing the stream stops
the generation.

### Structured output

The gateway forwards your schema and returns the model's JSON as `content`; it doesn't
validate it. Parse and validate on your side. Treat a cut-off `finish_reason` or
`refusal: true` as "no answer", and leave `max_tokens` at 300 or more, because thinking
models spend part of it before answering.

### Conversations and history

The gateway is stateless: it keeps no conversation. Your service owns the history and
sends every turn the model should see, oldest first, on every `/v1/generate` call:

```json
{"model": "cloud",
 "messages": [
   {"role": "system", "content": "You are a helpful assistant."},
   {"role": "user", "content": "Hello, my name is Alex."},
   {"role": "assistant", "content": "Hi Alex! How can I help you today?"},
   {"role": "user", "content": "What is my name?"}]}
```

- **Any replica can answer any turn.** Nothing ties a conversation to one instance.
- **Context length is yours to manage.** The gateway doesn't trim history. Watch
  `usage.prompt_tokens` in each response, and summarize or drop old turns before you
  approach the model's context limit.
- **Long histories cost less on repeat.** With `cache_prefix` (on by default), once the
  conversation has an assistant turn the gateway asks Anthropic to cache the history, so
  each new turn re-reads earlier turns at the cache rate. OpenAI and Gemini cache
  prefixes on their own.

### Caching

Identical `/v1/generate` requests from your service within the cache TTL (300 s by
default) come back with `cached: true` at no cost; other services never get your
cached answers. The key covers the model, messages, parameters and schema. Refusals,
cut-off answers and `/v1/embed` calls are never cached. Send `"cache": false` to always
ask the provider, for example for a fresh sample of a creative answer.

### Streaming

Send `"stream": true` to `/v1/generate` to receive the answer as it is generated, as
Server-Sent Events (`Content-Type: text/event-stream`). Each event is an `event:` line
and one `data:` line of JSON, followed by a blank line:

```
event: start
data: {"id":"5b40…","model":"claude-haiku-4-5","provider":"anthropic","cached":false}

event: delta
data: {"content":"Hel"}

event: delta
data: {"content":"lo"}

event: done
data: {"id":"5b40…","model":"claude-haiku-4-5","provider":"anthropic","usage":{…},"cost_usd":0.0001,"cached":false,"refusal":false,"finish_reason":"stop","provider_finish_reason":"end_turn"}
```

- **`start`** says which model answers. Failover between models happens only before
  it: until the first token, a failing or slow model is skipped exactly as for a
  normal call.
- **`delta`** events carry slices of text; concatenate their `content` in order. With
  `json_schema`, the slices are the JSON text: parse it once `done` arrives.
- **`done`** has everything a normal response has except `content` (`GenerateSummary`
  in [openapi.json](openapi.json)): usage, cost, `finish_reason`, `refusal`, `cached`.
- **If `done` has `refusal: true`, discard the deltas.** The safety layer's verdict
  arrives with the final item, after the model's text has been streamed; the same call
  without `stream` returns empty `content`. Don't show the text as an answer.
- **`error`** replaces `done` if the stream fails after it started (the model stopped
  answering, or went silent for longer than the gateway's idle limit, 30 s by default).
  Its data is the usual problem body (`code`, `detail`, `request_id`). Text already
  received is incomplete: discard it, or show it as interrupted, and retry if
  appropriate.
- **Errors before the stream starts** (401, 403, 422, 429, 502) are ordinary HTTP
  responses with a problem body, exactly as without `stream`.
- **A cache hit** comes back as a stream too: `start` with `cached: true`, the whole
  answer in one `delta`, then `done`.
- **If you disconnect,** the gateway stops the provider's generation. That call's usage
  is unknown to the gateway, so it isn't counted toward your budget, but the provider
  still bills the tokens it produced.
- **Proxies between you and the gateway** must not buffer: the gateway sends
  `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

The reference client's `generate_stream()` yields `(event, data)` pairs and raises
`AirModelGatewayError` on an `error` event.

### Budgets

A service with a `daily_budget_usd` gets 429 `budget_exceeded` once its spend for the
UTC day reaches it. The check runs before each paid call and the cost is added after,
so calls already in flight complete: spend can pass the budget by up to your concurrent
paid calls times the cost of one. Set the budget with that margin in mind.

### Unsupported options

Both routes reject fields they don't implement with 422 (`detail` names the field), so
nothing is silently ignored: for example `tools`, `top_p`, `stream_options`, or extra
message fields such as `name`.

### Tracing

Send your own request id in `X-Request-ID`. The gateway logs it on the call's audit
line and echoes it back, so one id follows a request through your logs and the
gateway's. Every error body carries it too.

## 5. Clients

- **Generate one** from [`docs/openapi.json`](openapi.json). For example,
  `npx openapi-typescript docs/openapi.json -o gateway.d.ts`, or
  `openapi-python-client generate --path docs/openapi.json` for Python. The token is
  declared as the `ServiceToken` security scheme, and every error as `Problem`.
- **Or write a thin one.** The existing AIR services each own a small client so it
  plugs into their own metrics and request-id propagation. That is the recommended
  shape.
- **The reference client** `air_model_gateway_client` in this repo shows the full
  contract in about 150 lines: auth, `request_id` propagation, errors as exceptions, and
  `retryable` (false for `budget_exceeded`).

When the gateway's contract changes, `docs/openapi.json` changes in the same commit,
and `tests/test_openapi.py` fails if anyone forgets. Watch that file for diffs.

## 6. Testing your integration

1. **Unit tests:** fake the gateway at your client boundary, including the 429, 502
   and cut-off answer paths.
2. **Against a real gateway on a laptop:** `make run` in this repo (local Ollama, any
   cloud keys in `.env`) and your service pointed at `http://localhost:8083` with your
   dev token.
3. **Production-like:** `make integration-up` here, with your service on
   `http://localhost:9083` and its token from `deploy/local/integration/tokens.env`. Add
   a check for your service to `deploy/local/integration/smoke.sh`.
4. **In GKE staging:** the Deploy workflow's smoke test covers the gateway side; your
   own deploy's smoke test should make one real call with the class or embedding
   model it uses.
