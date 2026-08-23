from __future__ import annotations

from air_llm.schemas.common import Principal


class ApiKeyVerifier:
    """Maps an ``X-API-Key`` to a Principal.

    Local: tokens come from settings. Prod: swap this for a verifier backed by
    DevOps-injected secrets or an mTLS identity — the ``verify`` contract stays
    the same.
    """

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = dict(tokens)

    def verify(self, token: str | None) -> Principal | None:
        if not token:
            return None
        service = self._tokens.get(token)
        if service is None:
            return None
        return Principal(service=service, token_id=token[:8])
