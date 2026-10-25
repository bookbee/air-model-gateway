# AIR Model Gateway — Low-Level Design

**Companion docs:** [Overview](overview.md) · [Scope](00-plan.md) · [HLD](01-hld.md) · [Integration](04-integration.md) · [Handover](05-handover.md)

The implementation contract: settings, wire and internal contracts, and how each module
behaves. The purpose of every file is listed in [Overview §4](overview.md#4-project-structure).

## 1. Composition

`main.py::create_app(settings)` builds each collaborator once and attaches it to
`app.state`. Endpoints reach collaborators through `request.app.state`, never module
globals.

| `app.state.` | Type | Built from |
|---|---|---|
| `settings` | `Settings` | env vars, `.env`, `SECRETS_DIR` |
| `policy` | `PolicyEngine` | `policy.config_path` (fails closed unless `local`) |
| `verifier` | `ApiKeyVerifier` | `security.service_tokens` |
| `redis` | `RedisStore \| None` | `redis.url` (None when unset) |
| `limiter` | `MemoryRateLimiter` or `RedisRateLimiter` | Redis when configured |
| `cache` | `MemoryResponseCache` or `RedisResponseCache` | Redis when configured |
| `ledger` | `MemorySpendLedger` or `RedisSpendLedger` | Redis when configured |
| `catalog` | `ModelCatalog` | `routing.yaml`: classes, per-service overrides, embedding models |
| `router` | `ModelRouter` | five adapters, resilience settings, and an `admit` callback enforcing `provider_quotas` |
| `prices` | `PriceBook` | `pricing.yaml` |

Before building anything, outside `local` `create_app` raises `ConfigError` if:
- the policy, routing or pricing file is missing, or no service tokens are configured;
- the policy grants an embedding name routing doesn't publish;
- a chain names a paid model pricing doesn't list;
- a service that can reach a paid model has no `daily_budget_usd`
  (`main.py::_unbudgeted_services`: a class whose chain has a paid rung, a paid
  embedding model, or `pin`).

It also sets `build_info{version,revision}`. The
lifespan closes the provider clients and the Redis connection on shutdown. Outside
`local`, booting without Redis logs `redis.not_configured`, because cache, limits and
spend are then per replica.

## 2. Configuration (`config.py`)

pydantic-settings, prefix `AIR_MODEL_GATEWAY_`, nested delimiter `__`. Source
precedence, highest first:

1. constructor arguments;
2. environment variables;
3. files in `AIR_MODEL_GATEWAY_SECRETS_DIR` (one file per variable, named like it, value
   trimmed; dot-entries skipped);
4. `.env`;
5. defaults.

`get_settings()` is cached. At boot the gateway logs `config.unknown_keys` with the
names (never the values) of any `AIR_MODEL_GATEWAY_*` variable, in the environment or
`.env`, that no setting reads: settings ignore extra keys, so a misspelt or retired one
would otherwise do nothing silently (`config.py::unknown_keys`).

| Group | Key | Default | Meaning |
|---|---|---|---|
| — | `environment` | `local` | `local`\|`staging`\|`prod`; only `local` honours policy `default_allow` |
| — | `revision` | `unknown` | The build's git revision; the image bakes it in (`REVISION` build arg), `make run` sets it. Shown by `/v1/health` |
| `log` | `level`, `render` | `INFO`, `console` | `render`: `console`\|`json` |
| `security` | `service_tokens` | `{}` | `token:service` CSV or mapping |
| `ollama` | `base_url`, `keep_alive` | `http://localhost:11434`, `30m` | `keep_alive` sent on every call |
| `self_hosted` | `base_url`, `api_key` | `http://localhost:8181/v1`, `""` | One OpenAI-compatible backend |
| `providers` | `anthropic_api_key`, `openai_api_key`, `gemini_api_key` | `""` | Empty key ⇒ that provider fails over |
| `gateway` | `routing_config_path`, `pricing_config_path` | `deploy/…` | Missing file ⇒ built-in table locally; startup error elsewhere |
| `gateway` | `request_timeout_seconds` | `60` | Deadline per provider attempt (for a stream: to its first token) |
| `gateway` | `stream_idle_timeout_seconds` | `30` | Longest silence allowed in a stream after its first token |
| `gateway` | `retry_attempts`, `retry_backoff_seconds` | `1`, `0.5` | Extra chain passes on transient failure |
| `gateway` | `breaker_failure_threshold`, `breaker_reset_seconds` | `5`, `30` | Per `(provider, model)` breaker |
| `gateway` | `cache_enabled`, `cache_ttl_seconds`, `cache_max_entries` | `true`, `300`, `10000` | Max entries bounds the memory cache |
| `redis` | `url`, `key_prefix`, `socket_timeout_seconds` | `""`, `air-model-gateway:`, `0.25` | Empty URL ⇒ in-process state |
| `policy` | `config_path`, `default_rate_per_minute` | `deploy/policy/policy.local.yaml`, `120` | |

There is no bind host/port setting; the listen address belongs to `uvicorn --host/--port`.

## 3. API

Every route except `/v1/health`, `/v1/ready` and `/metrics` requires `X-API-Key`.
The published contract is `docs/openapi.json` (OpenAPI 3.1, `make openapi`); the
consumer-facing explanation is [04-integration.md](04-integration.md).

| Method | Path | Purpose | Guards |
|---|---|---|---|
| POST | `/v1/generate` | Text from messages, optionally structured JSON | authN, policy(`inference`, `generate`), rate limit, model access, daily budget |
| POST | `/v1/embed` | One vector per input text | authN, policy(`inference`, `embed`), rate limit, model access, daily budget |
| GET | `/v1/health` | Liveness, with `version` and the build's `revision` | none |
| GET | `/v1/ready` | Readiness: 200 if any provider answers, else 503; reports Redis. A provider whose key was refused on its last 3 calls reports false | none |
| GET | `/v1/capabilities` | Version, environment, configured providers, and for the caller: each class's chain, whether it is granted (`allowed`) and servable here (`available`: some provider behind it is configured), preferred providers, `pin`, embedding models (same two flags) | authN |
| GET | `/v1/admin/policy` | Effective policy document | authN, policy(`admin`, `admin_read`) |
| GET | `/metrics` | Prometheus exposition | none |

Both model routes are thin: `require_principal`, then `api/v1/model_calls.py`'s
`admit` (policy, rate limit, model access) and `generate` or `embed` (cache, budget,
router, pricing, spend, metrics, audit), so the two can't drift apart.

Every `/v1` router uses `api/deps.py::AuthenticatedRoute`: for a route that depends on
`require_principal`, it checks the key before FastAPI reads the body, so an anonymous
request is 401 whatever its body (FastAPI parses and validates the body before running
any dependency). Routes without the dependency are untouched.

### Wire contracts (`schemas/generate.py`, `schemas/embed.py`)

```python
class GenerateInput(BaseModel):
    model: ModelClass = AUTO                  # local | auto | cloud
    provider: Provider | None = None          # preference within the class
    pin: str | None = None                    # provider:model; granted services only
    messages: list[Message]                   # required, 1..500, 800,000 characters in all
    max_tokens: int | None = None             # >= 1
    temperature: float | None = None          # 0..2; not forwarded to Anthropic
    json_schema: dict[str, Any] | None = None # structured output
    schema_name: str | None = None
    cache_prefix: bool = True                 # provider prompt-cache hint (system + history)
    cache: bool = True                        # false: skip the response cache both ways
    stream: bool = False                      # true: Server-Sent Events (below)

class GenerateSummary(BaseModel):             # also the stream's `done` event
    id: str
    model: str                                # concrete model that answered
    provider: Provider                        # provider that answered
    usage: Usage                              # prompt/completion/total + cache read/write
    cost_usd: float = 0.0                     # what this call cost; 0 for cache hits
    cached: bool = False
    refusal: bool = False                     # the provider's safety layer declined
    finish_reason: Literal["stop", "length", "refusal", "other"] | None
    provider_finish_reason: str | None        # the provider's own value, unmapped

class GenerateOutput(GenerateSummary):
    content: str = ""

class EmbedInput(BaseModel):
    model: str                                # <local|cloud>-<dims>-v<version>, required
    input: list[str]                          # required, 1..2048 texts of <= 32,000 characters

class EmbedOutput(BaseModel):
    id: str
    embedding_model: str                      # the published name: store it with the vectors
    dimensions: int                           # every vector's length
    model: str                                # the provider's model behind the name
    provider: Provider
    embeddings: list[list[float]]             # one per input, in order
    usage: Usage                              # total_tokens == prompt_tokens
    cost_usd: float = 0.0
```

Both inputs (and `Message`) forbid unknown fields: an option the gateway doesn't
implement (`tools`, `top_p`, `stream_options`, a message `name`) is a 422 naming the field, not
silently ignored. `Message.role` is `system`, `user` or `assistant`. Out-of-range values are the caller's
422, not a provider 400 that would walk the whole chain. So are the size limits
(`MAX_MESSAGES`, `MAX_TOTAL_CHARS`, `MAX_EMBED_INPUTS`, `MAX_EMBED_TEXT_CHARS`): an
oversized request fails at once rather than tying up a provider before it fails. The
422 `detail` lists `field: message` clauses (a body that isn't JSON: `body is not valid
JSON (at character N)`) and never echoes the input (it may hold prompts). A caller's
`X-Request-ID` is kept only if it is 1–128 characters of `[A-Za-z0-9._:-]`; otherwise a
fresh id is used.

### Streaming (`stream: true`)

`api/v1/model_calls.py::open_generate_stream` does everything that can fail before
the first token while the response is still an ordinary one: the cache lookup, the
budget check, and `router.open_chat_stream` (failover and retries, with the
per-attempt deadline acting as the time-to-first-token deadline). Any failure there
is a normal problem+json response. It then returns an iterator of Server-Sent Events,
served with `Cache-Control: no-cache` and `X-Accel-Buffering: no`:

| Event | Payload | When |
|---|---|---|
| `start` | `StreamStart`: `id`, `model`, `provider`, `cached` | Once, first |
| `delta` | `StreamDelta`: `content` | Each text slice (for `json_schema`, slices of the JSON text) |
| `done` | `GenerateSummary` | The provider's final item arrived; bookkeeping is done |
| `error` | `Problem` | The stream failed after `start` |

- **Bookkeeping** (pricing, spend, cache store, metrics, audit) runs when the
  provider's final item arrives, before `done` is sent, through the same code as a
  non-streamed call: identical cost, cache and audit behaviour.
- **After the first token** no failover is possible. A `ProviderError`, a silence
  longer than `gateway.stream_idle_timeout_seconds`, or a stream ending without its
  final item becomes an `error` event and is reported to the rung's breaker
  (`router.record_stream_failure`).
- **A cache hit** replays as `start` (`cached: true`), one `delta`, `done`.
- **A refusal** is known only when the final item arrives, after the model's text
  has gone out as `delta`s; `done` then has `refusal: true`, and the caller discards
  the deltas (the non-streamed answer's `content` is empty).
- **Client disconnect:** the response task is cancelled; the generator's `finally`
  closes the provider stream (the provider stops generating) and logs `model.call`
  with `stream_outcome=client_disconnected` and `streamed_chars`. Usage is unknown
  then, so the call's spend isn't counted (a known under-count; the provider still
  bills what it produced).
- `streams_total{outcome}` counts `completed`, `error` and `client_disconnected`.
  `requests_total` and the access log record a stream when its headers are sent, not
  when it ends.
- The stream binds `request_id` into the log context itself, because the middleware's
  binding ends when the headers are sent.

### OpenAPI document (`api/openapi.py`)

FastAPI infers paths and models from the routes. `api/openapi.py` adds what it can't
infer, so the document matches what the gateway really does:
- Routes declare their error statuses with `problems(...)`, rendered as
  `application/problem+json` with the `Problem` schema. That includes 422, replacing
  FastAPI's default `HTTPValidationError`.
- `X-API-Key` is the `ServiceToken` API-key security scheme (`APIKeyHeader` with
  `auto_error=False`, so a missing key still reaches the verifier and returns our 401).
- Every operation documents the optional `X-Request-ID` header and echoes it in
  responses.
- System routes have typed responses (`schemas/system.py`): `Health`, `Readiness`
  (also the 503 body) and `Capabilities`.
- `/v1/generate`'s 200 lists both `application/json` and `text/event-stream`; the
  event payloads (`StreamStart`, `StreamDelta`, `GenerateSummary`) are published as
  components so clients can generate types for them.

`make openapi` writes `docs/openapi.json`; `tests/test_openapi.py` fails when the
committed file differs from the code, or when the document stops being valid
OpenAPI 3.1.

### Internal contract (`schemas/models.py`)

The router and adapters speak `ChatRequest`, `EmbeddingsRequest` and `ProviderResult`,
named after the provider APIs they map onto: `/v1/generate` becomes
`router.chat(ChatRequest)` → `adapter.chat()`, and `/v1/embed` becomes
`router.embeddings(EmbeddingsRequest)` → `adapter.embeddings()`.
`api/v1/model_calls.py` translates between the two layers, so the wire can change
without touching adapters.

## 4. Control plane

- **AuthN** (`security/apikeys.py`): `X-API-Key` → `Principal(service, token_id)`;
  unknown → 401. `token_id` is a short SHA-256 of the token, never the token itself. A
  service may hold several tokens at once (rotation).
- **Policy** (`policy/engine.py`): `decide(principal, resource, action)`. A service's
  `resources` map grants actions per resource: `inference: [generate, embed]`,
  `admin: [admin_read]`. Actions are typed (`constants.py::Action`), so an unknown one
  fails the file at load. With no matching rule, the outcome is
  `default_allow`, which `load()` forces to false unless `environment` is `local`
  (logging `policy.default_allow_ignored`). A missing file is an empty deny-all policy
  locally; outside local the app refuses to start (`ConfigError`), as it does with no
  service tokens or no routing/pricing file.
- **Policy file** (`schemas/policy.py`), per service: `resources`, `rate_per_minute`,
  `model_classes`, `embed_models`, `preferred_providers` (each unset = every choice
  routing offers), `pin` (default false), `daily_budget_usd`; across services:
  `provider_quotas` (calls per minute per provider).
- **Model choice** (`api/v1/model_calls.py::choose_generate`, `choose_embed`): the
  policy decides whether the service may make the choice (403 `policy_denied`; `pin` is
  allowed for any service locally, by grant elsewhere), then the catalog resolves it
  (422 `invalid_model_choice` if it means nothing). A pin to a paid model the price
  table doesn't list is also 422: it would bill at the provider and count $0 against
  the budget. Outside local, startup refuses a policy that grants an embedding name
  routing doesn't publish, a routing file naming a paid model pricing doesn't list,
  and a service that can reach a paid model without a `daily_budget_usd` (§1).
- **Rate limits** (`policy/ratelimit.py`): token bucket of capacity `rpm`, refilling at
  `rpm/60` per second, keyed by `(service, resource)`. Each call checks
  `(service, "inference")`. Provider quotas use the same buckets under the service
  `*` (`("*", "provider:<name>")`), checked by the router before each rung
  (`main.py::_provider_quota`); a rung over quota is skipped like a 429.
  - `MemoryRateLimiter`: per process.
  - `RedisRateLimiter`: one Lua script per check, reading Redis `TIME` so replicas with
    skewed clocks agree. It stores `{tokens, ts}` in a hash at
    `<prefix>ratelimit:<service>:<resource>`, which expires once a full bucket would have
    refilled. While Redis is unreachable it falls back to a per-replica
    `MemoryRateLimiter`.
- **Budgets** (`policy/budget.py`): spend per service per UTC day, in
  `<prefix>spend:<service>:<date>` (`INCRBYFLOAT`, expires after two days) or in
  memory. Checked before a paid call (cache hits pass), added after. Over budget:
  429 `budget_exceeded` with `Retry-After` to the next UTC day. Soft: in-flight calls
  still complete and count, so the overshoot is at most the service's concurrent paid
  calls times the cost of one.

## 5. Routing and resilience (`gateway/router.py`, `gateway/resilience.py`)

**Model catalog** (`gateway/models.py::ModelCatalog`, from `deploy/routing.yaml`,
validated by `schemas/gateway.py::RoutingDoc`). The router never resolves names; it
runs the chain the catalog returns:
- **A class** → the service's override for that class if `overrides` has one, else the
  global chain. With a `provider` preference, that provider's rungs move to the front
  and the rest follow ("prefer", not "only"); a provider absent from the chain is a
  `ModelSelectionError` (422).
- **A pin** → one rung, `provider:model`, no fallback.
- **An embedding name** → its one model; the response must hold one vector per input
  text, each of the name's dimension (else 502; the call's spend still counts).

At load, every class must have a chain, a `local` chain may only use local providers
(`ollama`, `llama.cpp`) and a `cloud` chain only cloud ones, and each embedding name
must match `<local|cloud>-<dims>-v<version>` with its provider's location and its
declared dimension. `model_settings`, keyed `provider:model`, says how a model is
called wherever it runs (any chain, override or pin); today only `thinking_budget`,
for Gemini, where an unknown field, a malformed key or another provider fails at load.
The response cache keys on the resolved chain, so changing what a
class means makes earlier answers miss instead of being served from the old models.

**Walking a chain (one pass).** For each rung:

1. **Unregistered provider:** recorded as a failure.
2. **Breaker open:** the rung is skipped (`circuit_skips_total`).
3. **Provider quota spent** (`admit` returns false): the rung is skipped as a throttle
   (`provider_quota_rejected_total`), and the breaker slot is released.
4. **Call:** the call runs under `asyncio.wait_for(request_timeout_seconds)`. A timeout
   counts as a transient failure (`provider_timeouts_total`).
5. **`ProviderError`:**
   - transient and not throttled: counts against the breaker;
   - a 429 (`rate_limited`) or a permanent error (400, missing key, refused key):
     releases the breaker without counting, since neither says the provider is
     unhealthy.
   Each carries the provider's HTTP `status` and one of these (`ProviderError.reason`):
   - `auth`: the provider refused the gateway's own key or account (401, 402, 403, or a
     message `_common.is_auth_failure` recognises: Google's 400 `API_KEY_INVALID`,
     Anthropic's 400 "credit balance is too low", OpenAI's 429 `insufficient_quota`).
     Never rejected, throttled or retried; logged at error level. After 3 in a row
     (`AUTH_FAILURES_UNHEALTHY`) `/v1/ready` reports the provider false, until a call
     to it succeeds.
   - `not_configured`: no key or SDK for the provider.
   - `rejected`: any other 400, 413 or 422. The request itself is invalid for that
     model.
   - `rate_limited`, `not_found` (404), `unavailable` (transient), `other`.
6. **Any other exception** (an adapter bug, an SDK change): logged with its traceback
   (`provider.unexpected_error`), recorded as a non-retryable failure with reason
   `adapter`, breaker released.
7. **Success:** resets the breaker and the provider's auth-failure count, and returns.

Every rung that is called and fails increments
`rung_failures_total{provider,model,reason}` (plus `timeout` from step 4). Failover hides
these failures from callers, so this counter is where they show.

**After a failed pass:**
- If retries remain and any failure was retryable, sleep
  `backoff_seconds(base, n) = base · 2^(n-1) · U[0.5, 1.5)` and walk again
  (`chain_retries_total`).
- Otherwise raise `ProviderError`. It is `rate_limited` (→ 429) only if every rung
  failed with a rate limit, and `rejected` (→ 422 `provider_rejected`) only if every
  rung rejected the request; anything else is 502. A pin whose provider answers 404 is
  422 `invalid_model_choice` (the caller named the model); a 404 on a class's chain is
  the owner's misconfiguration, so 502.

**Transient** (`providers/_common.py::is_transient`):
- HTTP status 408, 409, 425, 429 or 5xx, read from `status_code`, `code` or an httpx
  response (`http_status`). Adapters build every `ProviderError` from an SDK or HTTP
  failure with `provider_error`, so status, `retryable` and `rate_limited` are read the
  same way everywhere.
- Or a transport, connection or timeout error with no status.

Provider SDKs are built with `max_retries=0` so the router is the only retry layer.

**Circuit breaker** (`CircuitBreaker`): one per `(provider, model)`, per replica.
- **CLOSED → OPEN** after `breaker_failure_threshold` consecutive failures.
- **OPEN → HALF_OPEN** after `breaker_reset_seconds`, admitting one trial call.
- **The trial's outcome:** success → CLOSED, failure → OPEN. A trial that ends neutral
  (429 or permanent error) goes back to OPEN so the breaker never sticks.
- `circuit_open{provider,model}` is 1 while a breaker is open.

**Streams:** `router.open_chat_stream` walks the chain with the same pass, breakers,
quotas and retries, but a rung's "call" is opening its stream and receiving the first
item within the per-attempt deadline (`_open_stream`, which closes the provider stream
on any failure or cancellation). It returns an `OpenedStream` (`model`, `first`,
`rest`), after which the stream is committed to that rung. `record_stream_failure`
lets a later failure count toward the rung's breaker.

**Health:** `router.health()` probes every adapter concurrently. Each adapter probe has
its own ~2 s deadline and makes a single attempt. A cloud adapter's probe can only say
its key is set, so a provider whose key was refused on its last 3 calls reports false
whatever its probe says.

## 6. Provider adapters (`gateway/providers/`)

Every adapter implements `ModelProvider` (`chat`, `chat_stream`, `embeddings`, `health`,
`aclose`) and returns `ProviderResult` with normalized `Usage`. Cache tokens are kept
disjoint from `prompt_tokens` so they price at cache rates; embeddings set
`total_tokens` equal to `prompt_tokens`; a refusal keeps its usage, because the prompt
was still billed. Cloud SDKs are imported lazily, so a missing package or key is a
clean, non-retryable `ProviderError`.

`chat_stream` is an async generator: each text delta as a `str`, then exactly one final
`ProviderResult` (empty `content`; model, usage, finish reason, refusal), sharing its
request building and result parsing with `chat`. Every failure, at open or mid-stream,
is a `ProviderError`.

| Adapter | How it streams |
|---|---|
| `ollama` | `"stream": true`: newline-delimited JSON; the `done: true` line carries the counts. A stream ending without it is a retryable error |
| `anthropic` | `messages.stream`: `text_delta` events, or `input_json_delta` (the forced tool's JSON text) for structured output; usage from the final message |
| `openai`, `llama.cpp` | `stream=True` with `stream_options={"include_usage": True}` (`_common.openai_stream`); usage arrives on a last chunk with no choices |
| `gemini` | `generate_content_stream`: text per chunk; usage (thinking tokens included) and finish reason from the last chunk |

| Adapter | Structured output | Notes |
|---|---|---|
| `ollama` | `format` = the JSON Schema | Plain HTTP; sends `keep_alive` on every call; an error's detail is the status plus Ollama's own reason from the body (`404 model 'x' not found`); a non-JSON body is a `ProviderError`; health = `/api/tags` |
| `anthropic` | Forced tool whose `input_schema` is the schema | System messages become cacheable system blocks (`cache_prefix`); in a conversation (an assistant turn present) the last turn is also a cache breakpoint, so the next turn reads the history at the cache rate (one-shot calls aren't marked: a cache write bills above input); `system` sent only when set; `temperature` never sent (API version `2026-10-21` rejects sampling parameters); no embeddings |
| `openai` | JSON mode plus schema in the system prompt | `max_completion_tokens` (gpt-5 and o-series reject `max_tokens`); `temperature` only when set; cached prompt tokens split out of `prompt_tokens`; `content_filter` is a refusal |
| `gemini` | `response_json_schema`: standard JSON Schema, union types and `$defs`/`$ref` included (not `response_schema`, Gemini's OpenAPI subset, on which the SDK crashes for `"type": ["string", "null"]`) | `thinking_config.thinking_budget` from `model_settings` when set (0 for 2.5 Flash), and that budget added to `max_output_tokens`, so the caller's `max_tokens` bounds the visible answer: Gemini counts thinking against the cap (MG-011). Thinking tokens billed as completion; embeddings priced via a concurrent `count_tokens` call |
| `llama.cpp` (`self_hosted`) | Schema in the system prompt only | Any OpenAI-compatible server; no `response_format`, because servers disagree on its dialect |

`Provider.SELF_HOSTED` has the value `"llama.cpp"`, naming the backend actually
configured. It is a separate class from `OpenAIProvider` because pricing treats
`ollama` and `llama.cpp` as free by provider identity.

## 7. Cache (`gateway/cache.py`)

Chat only. The key is a SHA-256 over `service, model, messages, max_tokens, temperature,
json_schema, schema_name, cache_prefix`: everything that changes what a correct answer
looks like, plus the calling service, so an answer is only reused by the service that
paid for it. Refusals and cut-off answers (`finish_reason: length`) are not stored. A
request with `cache: false` skips both the lookup and the store.

A hit returns `cached: true` and `cost_usd: 0.0`. Cost and token metrics count only calls
that reached a provider.

| Implementation | Storage | Bound |
|---|---|---|
| `MemoryResponseCache` | Process dict | TTL plus `cache_max_entries` (expired entries purged first, then the one nearest expiry evicted) |
| `RedisResponseCache` | `<prefix>cache:<sha256>` with `EX ttl` | Redis TTL and eviction policy; falls back to a memory cache while Redis is unreachable; unreadable entries are a miss |

## 8. Redis (`redis_store.py`)

One `redis.asyncio` client, created lazily, so boot never waits on Redis. It uses
`socket_timeout_seconds` for both connect and read, because Redis is on the request
path.

`RedisStore.run(op, fn)` turns any Redis or OS error into `RedisUnavailable` and counts
it (`redis_errors_total{operation}`). After 3 consecutive failures its own breaker
refuses calls locally for 5 s. A dead Redis therefore costs three timeouts, not one per
request. `ping()` backs the `redis` field of `/v1/ready`. The response cache, the rate
limiter (service limits and provider quotas) and the spend ledger all go through it.

## 9. Pricing (`gateway/cost.py`)

`deploy/pricing.yaml` holds USD per **million** tokens per model, plus two ratios applied
to the input rate: `cache_read_ratio` (0.1) and `cache_write_ratio` (1.25, Anthropic's 5-minute cache write).

A call is free when either:
- its provider is `ollama` or `llama.cpp`, or
- its model name starts with a `local_prefixes` entry.

A model missing from the table prices at 0 and increments
`model_unpriced_total{provider,model}`. Outside local, startup refuses a routing file
naming an unpriced paid model, and a pin to one is 422, so the counter catches what
those checks can't see: a local run on such a routing file, or a provider reporting a
model name other than the one asked for.

## 10. Errors

RFC 9457 `application/problem+json` with `type`, `title`, `status`, `code`, `detail` and
`request_id`.

| Status | `code` | When |
|---|---|---|
| 401 | `unauthorized` | Missing or unknown `X-API-Key` |
| 403 | `policy_denied` | No grant for this operation, or a class, embedding model, preferred provider or `pin` outside the service's grant |
| 404 | `not_found` | No such path (the router's own error, as problem+json) |
| 405 | `method_not_allowed` | The path exists, the method doesn't; `Allow` lists the methods |
| 422 | `invalid_model_choice` | An unpublished embedding name, a provider the class doesn't offer, a malformed `pin`, a pin to an unpriced paid model or to one its provider doesn't have |
| 422 | `validation_error` | Malformed body (for example, `/v1/generate` without `messages`, or over a size limit) |
| 422 | `provider_rejected` | Every rung's provider refused the request as invalid (400, 413, 422: too long, a parameter the model won't take); `detail` has their reasons |
| 429 | `rate_limited` | The service's bucket is empty, or every rung was throttled (provider 429 or gateway quota) |
| 429 | `budget_exceeded` | The service's `daily_budget_usd` is spent; `Retry-After` is the next UTC day |
| 502 | `provider_unavailable` | The chain failed for any other reason (outage, timeout, missing or refused provider key, open breaker), or an embedding answer didn't match its texts or its name's dimension |
| 500 | `internal_error` | Unhandled error (rendered by the middleware, so it carries `X-Request-ID` and is counted) |

`/v1/ready`'s 503 is a plain readiness body, not a problem document.

## 11. Observability

- **Logs.** structlog with `request_id` bound per request. Console locally, JSON
  elsewhere; JSON lines also carry `severity` (the upper-cased level), which Cloud
  Logging reads. In Kubernetes uvicorn runs with `--no-access-log`. Key events:
  - `model.call`: the audit line, one per served call, with service, operation
    (`generate` or `embed`), provider, model, tokens, cost_usd, cached and finish_reason;
  - provider events: `provider.failed` (with `reason`; error level for `auth`),
    `provider.timeout`, `chain.retry`, `circuit.transition`;
  - `air-model-gateway.start`, with `version` and `revision`;
  - Redis and policy: `redis.failed`, `redis.not_configured`,
    `policy.default_allow_ignored`.
- **Metrics** (`/metrics`), all prefixed `air_model_gateway_`:

| Metric | Labels | Meaning |
|---|---|---|
| `requests_total` | route, method, status | HTTP requests |
| `request_duration_seconds` (histogram) | route, method | Time to the start of the response (for a stream, to its first token); what latency SLOs use |
| `build_info` (gauge) | version, revision | Always 1: the build each replica runs |
| `model_tokens_total` | provider, model, kind | Tokens; `kind` is `generate` or `embed` |
| `model_cost_usd_total` | provider, model | Spend |
| `model_unpriced_total` | provider, model | Calls priced at 0 for lack of a price |
| `provider_timeouts_total` | provider | Attempts cut off by the deadline |
| `rung_failures_total` | provider, model, reason | Rungs called that failed (§5): `auth` and `adapter` each need a person; the rest, rising, is failover eating the budget |
| `chain_retries_total` | primary_provider | Extra passes over a chain |
| `circuit_skips_total` | provider, model | Rungs skipped by an open breaker |
| `circuit_open` (gauge) | provider, model | 1 while a breaker is open on this replica |
| `ratelimited_total` | service, resource | Calls refused by the per-service rate limit |
| `redis_errors_total` | operation | Redis operations that fell back |
| `service_cost_usd_total` | service | Spend by calling service (what budgets count) |
| `provider_quota_rejected_total` | provider | Rungs skipped because the provider's gateway quota was spent |
| `budget_rejected_total` | service | Calls refused because the service's daily budget was spent |
| `streams_total` | outcome | Streamed calls by end: `completed`, `error`, `client_disconnected` |

## 12. Reference client (`src/air_model_gateway_client/`)

```python
async with AirModelGatewayClient() as gateway:  # AIR_MODEL_GATEWAY_URL / _TOKEN
    reply = await gateway.generate([{"role": "user", "content": "hi"}], model="local")
    vecs = await gateway.embed(["a", "b"], model="local-768-v1")
```

`generate()` (POST /v1/generate) and `embed()` (POST /v1/embed) take an optional
`request_id`, sent as `X-Request-ID`. A non-2xx response raises
`AirModelGatewayError(status, code, detail, request_id)`, with `retryable` true for 429,
502 and 503, except a 429 `budget_exceeded`. A non-JSON body (a proxy's HTML page)
still raises it, with `code="http_error"`. The `transport=` argument lets tests drive the client
in-process.

## 13. Tests

Run by `make check` alongside ruff and mypy `--strict`, with a 90% coverage floor
(`pyproject.toml`). None needs network, a server or keys:
- providers are faked at the SDK boundary;
- the app runs in-process through `httpx.ASGITransport`;
- Redis is `fakeredis`, which executes the real Lua script.

Coverage by area:

- **End to end** (`test_client_end_to_end.py`): reference client → app → router →
  shipped `routing.yaml`.
- **Routes** (`test_routes.py`): `/v1/generate` and `/v1/embed`: dispatch, cache, cost,
  error mapping, validation, audit.
- **Streaming** (`test_streaming.py`): the event sequence, errors before the first
  token as HTTP errors, failover only before it, mid-stream errors and the idle
  deadline as `error` events, cache replay, bookkeeping, the reference client; each
  adapter's `chat_stream` in its own provider test file.
- **Router** (`test_gateway.py`): failover, deadlines, the 429-vs-502 rule, concurrent
  health, failure reasons (provider account failures are `auth`, never `rejected`) and
  readiness after refused keys.
- **Resilience** (`test_resilience.py`): breaker states, backoff, retry only on transient
  errors, throttles not tripping breakers.
- **Shared state** (`test_cache.py`, `test_ratelimit.py`): keys, bounds, cross-replica
  sharing and Redis fallbacks.
- **Control plane** (`test_policy.py`, `test_config.py`, `test_readiness.py`,
  `test_system.py`).
- **Configuration contract** (`test_gateway_config.py`): shipped YAML loads, every
  reachable cloud model is priced (every class, override and embedding model), every
  class and embedding model each consumer depends on exists and is granted in every
  policy file, each class keeps its location promise, embedding names are well-formed,
  and the classifier keeps its tier semantics.
- **Model catalog** (`test_models.py`): class chains, overrides, provider preference,
  pins, embedding names, `describe`.
- **Logging** (`test_logging.py`): JSON lines carry a Cloud Logging severity.
- **Governance and review regressions** (`test_governance.py`): model choices against
  grants (classes, providers, pins, embedding names), capabilities, budgets (memory and Redis), provider quotas, per-service cache, normalized
  `finish_reason`, counted 500s, bounded metric labels, startup checks.
- **Repository consistency** (`test_repo_consistency.py`): settings, make targets,
  metrics and links named in docs exist; every source file is in the overview tree;
  the image build context is an allowlist.
- **Published contract** (`test_openapi.py`): committed `docs/openapi.json` matches
  the code and validates; exact path surface; token scheme and problem+json errors on
  every authenticated route; each route takes only its own body; no route is
  deprecated.
- **Adapters:** one file per provider.
