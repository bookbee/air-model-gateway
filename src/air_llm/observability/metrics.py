from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

REQUESTS = Counter("air_llm_requests_total", "HTTP requests", ["route", "method", "status"])
MODEL_TOKENS = Counter("air_llm_model_tokens_total", "Model tokens", ["provider", "model", "kind"])
MODEL_COST = Counter("air_llm_model_cost_usd_total", "Model cost in USD", ["provider", "model"])
# A model served but absent from the price table. Its cost is reported as 0, so
# without this counter unpriced spend is indistinguishable from free local spend.
MODEL_UNPRICED = Counter(
    "air_llm_model_unpriced_total",
    "Model calls priced at zero because the model is not in the price table",
    ["provider", "model"],
)
PROVIDER_TIMEOUTS = Counter(
    "air_llm_provider_timeouts_total",
    "Provider calls abandoned at the gateway timeout",
    ["provider"],
)
RATELIMITED = Counter(
    "air_llm_ratelimited_total",
    "Requests rejected by the rate limiter",
    ["service", "resource"],
)


def render_latest() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
