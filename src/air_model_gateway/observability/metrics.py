from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter(
    "air_model_gateway_requests_total", "HTTP requests", ["route", "method", "status"]
)
# What latency SLOs are set on. Measured to the response's start, so for a
# stream it is the time to the first token. Buckets span a cache hit (ms) to a
# long cloud completion near the 60s per-attempt deadline.
REQUEST_SECONDS = Histogram(
    "air_model_gateway_request_duration_seconds",
    "HTTP request duration, to the start of the response",
    ["route", "method"],
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 60, 120),
)
# The build each replica runs: constant 1, labelled. During a rollout, or when
# a test run must prove which code answered it, this names the revision.
BUILD_INFO = Gauge(
    "air_model_gateway_build_info", "The running build (value is always 1)", ["version", "revision"]
)
MODEL_TOKENS = Counter(
    "air_model_gateway_model_tokens_total", "Model tokens", ["provider", "model", "kind"]
)
MODEL_COST = Counter(
    "air_model_gateway_model_cost_usd_total", "Model cost in USD", ["provider", "model"]
)
# A model served but absent from the price table. Its cost is reported as 0, so
# without this counter unpriced spend is indistinguishable from free local spend.
MODEL_UNPRICED = Counter(
    "air_model_gateway_model_unpriced_total",
    "Model calls priced at zero because the model is not in the price table",
    ["provider", "model"],
)
PROVIDER_TIMEOUTS = Counter(
    "air_model_gateway_provider_timeouts_total",
    "Provider calls abandoned at the gateway timeout",
    ["provider"],
)
# Every rung that was tried and didn't answer, by why. Failover hides these
# from callers (the next rung answers, often at a higher price), so this is how
# they get noticed: `auth` (the gateway's key or account was refused) and
# `adapter` (an unexpected exception inside an adapter: a gateway defect) each
# need a person; a rising rate of the rest is failover eating the budget.
RUNG_FAILURES = Counter(
    "air_model_gateway_rung_failures_total",
    "Failover-chain rungs that were called and failed, by reason: auth, not_configured, "
    "rate_limited, rejected, not_found, timeout, unavailable, adapter, other",
    ["provider", "model", "reason"],
)
CHAIN_RETRIES = Counter(
    "air_model_gateway_chain_retries_total",
    "Extra passes over a failover chain after every rung failed transiently",
    ["primary_provider"],
)
CIRCUIT_SKIPS = Counter(
    "air_model_gateway_circuit_skips_total",
    "Rungs skipped without a call because their circuit breaker was open",
    ["provider", "model"],
)
CIRCUIT_OPEN = Gauge(
    "air_model_gateway_circuit_open",
    "1 while a (provider, model) circuit breaker is open on this replica",
    ["provider", "model"],
)
REDIS_ERRORS = Counter(
    "air_model_gateway_redis_errors_total",
    "Redis operations that failed and fell back to degraded behaviour",
    ["operation"],
)
RATELIMITED = Counter(
    "air_model_gateway_ratelimited_total",
    "Requests rejected by the rate limiter",
    ["service", "resource"],
)


SERVICE_COST = Counter(
    "air_model_gateway_service_cost_usd_total",
    "Model cost in USD by calling service",
    ["service"],
)
QUOTA_REJECTED = Counter(
    "air_model_gateway_provider_quota_rejected_total",
    "Rungs skipped because the deployment's quota for the provider was spent",
    ["provider"],
)
BUDGET_REJECTED = Counter(
    "air_model_gateway_budget_rejected_total",
    "Requests refused because the service's daily budget was spent",
    ["service"],
)


STREAMS = Counter(
    "air_model_gateway_streams_total",
    "Streamed /v1/generate calls by how they ended: completed, error, client_disconnected",
    ["outcome"],
)


def render_latest() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
