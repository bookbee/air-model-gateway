from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.gateway.providers._common import is_transient
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage


class OllamaProvider:
    """Functional provider: plain HTTP to a local Ollama runtime."""

    name = Provider.OLLAMA

    def __init__(self, base_url: str, timeout: float = 60.0, keep_alive: str = "30m") -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._keep_alive = keep_alive

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        data = await self._post("/api/chat", self._chat_payload(req, model, stream=False))
        return _chat_result(data, model, content=data.get("message", {}).get("content", ""))

    async def chat_stream(
        self, req: ChatRequest, model: str
    ) -> AsyncGenerator[str | ProviderResult, None]:
        """Ollama streams newline-delimited JSON: one object per token batch, the
        last with `done: true` carrying the counts and `done_reason`."""
        payload = self._chat_payload(req, model, stream=True)
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.is_error:
                    await resp.aread()
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise ProviderError(self.name, f"/api/chat: {data['error']}")
                    text = data.get("message", {}).get("content", "")
                    if text:
                        yield text
                    if data.get("done"):
                        yield _chat_result(data, model, content="")
                        return
        except httpx.HTTPStatusError as exc:
            rate_limited = exc.response.status_code == httpx.codes.TOO_MANY_REQUESTS
            raise ProviderError(
                self.name, str(exc), rate_limited=rate_limited, retryable=is_transient(exc)
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, str(exc), retryable=is_transient(exc)) from exc
        except ValueError as exc:
            raise ProviderError(self.name, "/api/chat: stream line is not JSON") from exc
        raise ProviderError(self.name, "/api/chat: stream ended before done", retryable=True)

    def _chat_payload(self, req: ChatRequest, model: str, *, stream: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model,
            "messages": [m.model_dump() for m in req.messages],
            "stream": stream,
            # Without this, Ollama's server-default keep_alive (5m) applies and
            # every AIR service sharing this instance pays a reload cold-start
            # on the next call past that window — see config.py::OllamaSettings.
            "keep_alive": self._keep_alive,
        }
        # A JSON Schema becomes a decoding grammar: the model physically cannot
        # emit prose around the object, which is what makes a small local model
        # usable for structured output at all.
        if req.json_schema is not None:
            payload["format"] = req.json_schema

        options: dict[str, float | int] = {}
        # Structured calls want determinism; default to greedy + fixed seed
        # unless the caller asked for a specific temperature.
        if req.json_schema is not None and req.temperature is None:
            options["temperature"] = 0.0
            options["seed"] = 0
        elif req.temperature is not None:
            options["temperature"] = req.temperature
        if req.max_tokens is not None:
            options["num_predict"] = req.max_tokens
        if options:
            payload["options"] = options
        return payload

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        data = await self._post(
            "/api/embed", {"model": model, "input": req.input, "keep_alive": self._keep_alive}
        )
        tokens = data.get("prompt_eval_count", 0)
        return ProviderResult(
            model=model,
            embeddings=data.get("embeddings", []),
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    async def _post(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        """POST and decode, with every failure as a ProviderError, a body that
        isn't JSON included (a proxy's HTML error page): anything else would
        escape the failover chain."""
        try:
            resp = await self._client.post(path, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            rate_limited = exc.response.status_code == httpx.codes.TOO_MANY_REQUESTS
            raise ProviderError(
                self.name, str(exc), rate_limited=rate_limited, retryable=is_transient(exc)
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, str(exc), retryable=is_transient(exc)) from exc
        except ValueError as exc:
            raise ProviderError(self.name, f"{path}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(self.name, f"{path}: unexpected response shape")
        return data

    async def health(self) -> bool:
        # Short deadline of its own: this backs /v1/ready, which a probe polls on a
        # far tighter budget than the completion timeout this client is built with.
        try:
            resp = await self._client.get("/api/tags", timeout=2.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


def _chat_result(data: dict[str, Any], model: str, *, content: str) -> ProviderResult:
    """A chat answer from Ollama's final object (the whole response, or the
    stream's `done` line)."""
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)
    return ProviderResult(
        model=str(data.get("model") or model),
        content=content,
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        finish_reason=data.get("done_reason"),
    )
