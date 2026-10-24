from __future__ import annotations

import hashlib

from air_model_gateway.schemas.common import Principal


class ApiKeyVerifier:
    """Maps an ``X-API-Key`` to a Principal.

    Tokens come from ``security.service_tokens``: in every environment the same
    setting, delivered as an env var locally and as a SECRETS_DIR file (Secret
    Manager on GKE) elsewhere. A service may hold more than one token at a time,
    which is what lets a rotation run without downtime.
    """

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = dict(tokens)

    def verify(self, token: str | None) -> Principal | None:
        if not token:
            return None
        service = self._tokens.get(token)
        if service is None:
            return None
        return Principal(service=service, token_id=token_id(token))


def token_id(token: str) -> str:
    """A short, non-reversible id for a token: tells two tokens of one service
    apart (mid-rotation) without putting any part of the secret in a log."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]
