from __future__ import annotations

import httpx

from air_model_gateway.constants import Provider
from air_model_gateway.gateway.base import ProviderError, ProviderResult
from air_model_gateway.schemas.models import ChatRequest, EmbeddingsRequest, Usage


class OllamaProvider:
    """Functional provider: plain HTTP to a local Ollama runtime."""

    name = Provider.OLLAMA

    def __init__(self, base_url: str, timeout: float = 60.0, keep_alive: str = "30m") -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._keep_alive = keep_alive

    async def chat(self, req: ChatRequest, model: str) -> ProviderResult:
        payload: dict[str, object] = {
            "model": model,
            "messages": [m.model_dump() for m in req.messages],
            "stream": False,
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

        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            rate_limited = exc.response.status_code == httpx.codes.TOO_MANY_REQUESTS
            raise ProviderError(self.name, str(exc), rate_limited=rate_limited) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, str(exc)) from exc

        data = resp.json()
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)
        return ProviderResult(
            model=str(data.get("model") or model),
            content=data.get("message", {}).get("content", ""),
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason=data.get("done_reason"),
        )

    async def embeddings(self, req: EmbeddingsRequest, model: str) -> ProviderResult:
        try:
            resp = await self._client.post(
                "/api/embed",
                json={"model": model, "input": req.input, "keep_alive": self._keep_alive},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, str(exc)) from exc

        data = resp.json()
        return ProviderResult(
            model=model,
            embeddings=data.get("embeddings", []),
            usage=Usage(prompt_tokens=data.get("prompt_eval_count", 0)),
        )

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
