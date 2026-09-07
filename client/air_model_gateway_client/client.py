from __future__ import annotations

import os
from typing import Any

import httpx


class AirModelGatewayClient:
    """Consumer entry point for air-model-gateway.

    Configured via ``AIR_MODEL_GATEWAY_URL`` / ``AIR_MODEL_GATEWAY_TOKEN`` (or explicit args) —
    identical shape in local and production, because air-model-gateway is reached
    directly, in every environment, with no brokered indirection through
    air-infra (docs/00-plan.md §1, §6).
    """

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        # 8083, not 8080: 8080 is the container-internal port, never reachable
        # from the host under docker-compose.yml's own "8083:8080" mapping —
        # this default is only ever a fallback for a quick ad hoc script, and
        # the one port that actually answers without AIR_MODEL_GATEWAY_URL set is 8083.
        self._base_url = base_url or os.environ.get(
            "AIR_MODEL_GATEWAY_URL", "http://localhost:8083"
        )
        self._token = token or os.environ.get("AIR_MODEL_GATEWAY_TOKEN", "")
        self._headers = {"X-API-Key": self._token}
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=90.0)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        cache_prefix: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task": "chat",
            "messages": messages,
            "cache_prefix": cache_prefix,
        }
        if model is not None:
            payload["model"] = model
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if json_schema is not None:
            payload["json_schema"] = json_schema
        if schema_name is not None:
            payload["schema_name"] = schema_name
        return await self._post(payload)

    async def embeddings(self, input: list[str], *, model: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"task": "embeddings", "input": input}
        if model is not None:
            payload["model"] = model
        return await self._post(payload)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post("/v1/inference", json=payload, headers=self._headers)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def aclose(self) -> None:
        await self._http.aclose()
