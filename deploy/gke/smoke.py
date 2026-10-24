"""In-cluster smoke checks for a deployed gateway (run by deploy/gke/smoke.sh).

Runs inside a pod in a consumer-labelled namespace, so it reaches the gateway
the way a consumer does: through the Service, past the NetworkPolicy. Tokens
arrive as AIR_<SERVICE>_TOKEN env vars from a short-lived Secret. Standard
library only, so it runs in the gateway's own image. Prints no tokens.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ["GATEWAY_URL"].rstrip("/")
EXPECT_REDIS = os.environ.get("EXPECT_REDIS", "true") == "true"
GENERATE = {
    "model": "local",
    "messages": [{"role": "user", "content": "Reply with OK"}],
    "max_tokens": 5,
}
EMBED = {"model": "local-768-v1", "input": ["smoke"]}
results: list[bool] = []


def call(path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-API-Key"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name: str, want: object, got: object) -> None:
    ok = want == got
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name:<52} {got if ok else f'got {got}, want {want}'}")


def token(service: str) -> str:
    return os.environ[service.upper().replace("-", "_") + "_TOKEN"]


if sys.argv[1:] == ["--expect-unreachable"]:
    # Run from a namespace the NetworkPolicy doesn't allow: connecting must fail.
    try:
        urllib.request.urlopen(BASE + "/v1/health", timeout=5)
    except OSError:
        sys.exit(0)
    sys.exit(1)

print(f"gateway ({BASE})")
status, ready = call("/v1/ready")
check("ready", 200, status)
if EXPECT_REDIS:
    check("redis connected", True, ready.get("redis"))
check("dev token refused", 401, call("/v1/generate", "air-classifier-dev", GENERATE)[0])
check("classifier: generate", 200, call("/v1/generate", token("air-classifier-service"), GENERATE)[0])
check("orchestrator: generate", 200, call("/v1/generate", token("air-orchestrator-service"), GENERATE)[0])
check("tools: generate", 200, call("/v1/generate", token("air-tools"), GENERATE)[0])
check("rag: embed", 200, call("/v1/embed", token("air-rag"), EMBED)[0])
check("rag: generate not granted", 403, call("/v1/generate", token("air-rag"), GENERATE)[0])
pin = {"pin": "ollama:llama3.2:3b", "messages": GENERATE["messages"], "max_tokens": 5}
check("classifier: pin refused (no grant)", 403, call("/v1/generate", token("air-classifier-service"), pin)[0])
unpublished = {"model": "local-1024-v1", "input": ["x"]}
check("classifier: unpublished embedding model", 422, call("/v1/embed", token("air-classifier-service"), unpublished)[0])
other = {"model": "cloud-1536-v1", "input": ["x"]}
check("rag: embedding model outside its grant", 403, call("/v1/embed", token("air-rag"), other)[0])
status, body = call("/v1/generate", token("air-tools"), GENERATE)
check("finish_reason normalized", True, body.get("finish_reason") in {"stop", "length"})
check("web-console: embed not granted", 403, call("/v1/embed", token("air-web-console"), EMBED)[0])
stream_body = {**GENERATE, "stream": True, "cache": False}
req = urllib.request.Request(
    BASE + "/v1/generate",
    data=json.dumps(stream_body).encode(),
    headers={"Content-Type": "application/json", "X-API-Key": token("air-tools")},
)
with urllib.request.urlopen(req, timeout=120) as r:
    events = [line[7:] for line in r.read().decode().splitlines() if line.startswith("event: ")]
streamed = events[:2] == ["start", "delta"] and events[-1] == "done"
check("stream: start, deltas, done", True, streamed)
check("web-console: policy probe", 200, call("/v1/admin/policy", token("air-web-console"))[0])
check("classifier: policy probe not granted", 403, call("/v1/admin/policy", token("air-classifier-service"))[0])
status, body = call("/v1/capabilities", token("air-classifier-service"))
check("environment", os.environ.get("EXPECT_ENVIRONMENT", "prod"), body.get("environment"))

print(f"passed {sum(results)}, failed {len(results) - sum(results)}")
sys.exit(0 if all(results) else 1)
