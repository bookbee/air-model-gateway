from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Literal

import httpx

#: POST /v1/generate classes: where the model runs.
ModelClass = Literal["local", "auto", "cloud"]


class AirModelGatewayError(Exception):
    """A non-2xx answer from air-model-gateway, carrying its RFC 9457 problem body.

    ``code`` is the stable machine-readable part (``unauthorized``,
    ``policy_denied``, ``rate_limited``, ``budget_exceeded``, ``provider_unavailable``,
    ``provider_rejected``, ``validation_error``, ...) — branch on it, not on ``detail``,
    which is human-readable and may change. ``request_id`` is what to quote when asking
    the gateway's owners to look at a failed call.
    """

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        self.request_id = request_id
        super().__init__(f"{status} {code}: {detail}" if detail else f"{status} {code}")

    @property
    def retryable(self) -> bool:
        """Throttled or every provider down: worth retrying later, unlike a
        4xx caused by the request itself. A spent daily budget is a 429 too,
        but it won't clear before the next UTC day, so it is not retryable."""
        if self.code == "budget_exceeded":
            return False
        return self.status in (429, 502, 503)

    @classmethod
    def from_response(cls, resp: httpx.Response) -> AirModelGatewayError:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            # Not a problem document — something between the caller and the
            # gateway (proxy, load balancer) answered instead.
            return cls(resp.status_code, "http_error", resp.text[:500] or None)
        return cls(
            status=int(body.get("status", resp.status_code)),
            code=str(body.get("code", "http_error")),
            detail=body.get("detail"),
            request_id=body.get("request_id") or resp.headers.get("X-Request-ID"),
        )


class AirModelGatewayClient:
    """Consumer entry point for air-model-gateway.

    Configured via ``AIR_MODEL_GATEWAY_URL`` / ``AIR_MODEL_GATEWAY_TOKEN`` (or explicit
    args), the same way in every environment: the gateway is always reached
    directly, never through another service.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 8083, not 8080: 8080 is the container-internal port, never reachable
        # from the host under docker-compose.yml's own "8083:8080" mapping —
        # this default is only ever a fallback for a quick ad hoc script, and
        # the one port that actually answers without AIR_MODEL_GATEWAY_URL set is 8083.
        self._base_url = base_url or os.environ.get(
            "AIR_MODEL_GATEWAY_URL", "http://localhost:8083"
        )
        self._token = token or os.environ.get("AIR_MODEL_GATEWAY_TOKEN", "")
        self._headers = {"X-API-Key": self._token}
        # 90s: above the gateway's own 60s per-attempt deadline, so a slow
        # first rung that fails over still has time to answer from the next.
        # `transport` lets a test drive the client against the app in-process
        # (httpx.ASGITransport) with no server or network involved.
        self._http = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, transport=transport
        )

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        model: ModelClass | None = None,
        provider: str | None = None,
        pin: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        cache_prefix: bool = True,
        cache: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/generate: messages in, one answer out. `model` is the class
        (`local`, `auto`, `cloud`; the gateway's default is `auto`), `provider`
        an optional preference within it, `pin` an exact `provider:model` for
        services granted it (instead of `model`/`provider`)."""
        payload = _generate_payload(
            messages,
            model=model,
            provider=provider,
            pin=pin,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
            schema_name=schema_name,
            cache_prefix=cache_prefix,
            cache=cache,
        )
        return await self._post("/v1/generate", payload, request_id)

    async def generate_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: ModelClass | None = None,
        provider: str | None = None,
        pin: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        cache_prefix: bool = True,
        cache: bool = True,
        request_id: str | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """POST /v1/generate with `stream: true`. Yields `(event, data)` pairs as
        they arrive: one `("start", ...)`, then `("delta", {"content": ...})` for
        each slice of text, then `("done", summary)`. An error before the stream
        starts, or an `error` event after, raises AirModelGatewayError.

        If `done` has `refusal: true`, discard the deltas: they are what the
        model wrote before its safety layer stopped it, which the non-streamed
        call would have replaced with empty `content`."""
        payload = _generate_payload(
            messages,
            model=model,
            provider=provider,
            pin=pin,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
            schema_name=schema_name,
            cache_prefix=cache_prefix,
            cache=cache,
        )
        payload["stream"] = True
        async with self._http.stream(
            "POST", "/v1/generate", json=payload, headers=self._with_request_id(request_id)
        ) as resp:
            if resp.is_error:
                await resp.aread()
                raise AirModelGatewayError.from_response(resp)
            event: str | None = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line.removeprefix("event:").strip()
                elif line.startswith("data:") and event is not None:
                    data: dict[str, Any] = json.loads(line.removeprefix("data:"))
                    if event == "error":
                        raise AirModelGatewayError(
                            status=int(data.get("status", 502)),
                            code=str(data.get("code", "stream_error")),
                            detail=data.get("detail"),
                            request_id=data.get("request_id"),
                        )
                    yield event, data

    async def embed(
        self, input: list[str], *, model: str, request_id: str | None = None
    ) -> dict[str, Any]:
        """POST /v1/embed: texts in, one vector per text out. `model` is a
        published embedding name (e.g. `local-768-v1`): your index's identity."""
        payload: dict[str, Any] = {"model": model, "input": input}
        return await self._post("/v1/embed", payload, request_id)

    async def _post(
        self, path: str, payload: dict[str, Any], request_id: str | None
    ) -> dict[str, Any]:
        resp = await self._http.post(path, json=payload, headers=self._with_request_id(request_id))
        if resp.is_error:
            raise AirModelGatewayError.from_response(resp)
        data: dict[str, Any] = resp.json()
        return data

    def _with_request_id(self, request_id: str | None) -> dict[str, str]:
        # The caller's own request id, so one id follows the call through the
        # caller's logs and the gateway's audit line.
        return {**self._headers, "X-Request-ID": request_id} if request_id else self._headers

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AirModelGatewayClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def _generate_payload(
    messages: list[dict[str, str]],
    *,
    model: ModelClass | None,
    provider: str | None,
    pin: str | None,
    max_tokens: int | None,
    temperature: float | None,
    json_schema: dict[str, Any] | None,
    schema_name: str | None,
    cache_prefix: bool,
    cache: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"messages": messages, "cache_prefix": cache_prefix, "cache": cache}
    optional = {
        "model": model,
        "provider": provider,
        "pin": pin,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "json_schema": json_schema,
        "schema_name": schema_name,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    return payload
