from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

REQUESTS = Counter(
    "air_model_gateway_requests_total", "HTTP requests", ["route", "method", "status"]
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
