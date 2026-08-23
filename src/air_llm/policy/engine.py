from __future__ import annotations

from pathlib import Path

import yaml

from air_llm.constants import Action
from air_llm.schemas.common import Principal
from air_llm.schemas.policy import Decision, PolicyDoc


class PolicyEngine:
    """RBAC over ``(service, resource, action)``.

    air-llm has exactly one resource (``inference``) plus ``admin`` for the
    policy-dump endpoint — the shape carries over from air-infra's broader
    multi-resource policy unchanged, since a single-resource policy is just the
    degenerate case of the same rules.

    Fails open only when ``default_allow`` is set (a local convenience). Prod
    policy documents set ``default_allow: false`` and every access needs an
    explicit rule.
    """

    def __init__(self, doc: PolicyDoc) -> None:
        self.doc = doc

    @classmethod
    def load(cls, path: str | Path, *, default_rate_per_minute: int = 120) -> PolicyEngine:
        p = Path(path)
        if not p.exists():
            # No file -> deny-by-default empty policy (safe if a prod path is wrong).
            return cls(
                PolicyDoc(default_allow=False, default_rate_per_minute=default_rate_per_minute)
            )
        raw = yaml.safe_load(p.read_text()) or {}
        raw.setdefault("default_rate_per_minute", default_rate_per_minute)
        return cls(PolicyDoc.model_validate(raw))

    def decide(self, principal: Principal, resource: str, action: Action) -> Decision:
        sp = self.doc.services.get(principal.service)
        if sp is None:
            return self._fallback(f"no policy for service '{principal.service}'")

        allowed = sp.resources.get(resource)
        if allowed is None:
            return self._fallback(f"'{principal.service}' has no grant on '{resource}'")
        if action.value in allowed:
            return Decision(allow=True, reason="explicit grant")
        return self._fallback(f"'{principal.service}' may not '{action.value}' on '{resource}'")

    def rate_per_minute(self, principal: Principal) -> int:
        sp = self.doc.services.get(principal.service)
        if sp and sp.rate_per_minute is not None:
            return sp.rate_per_minute
        return self.doc.default_rate_per_minute

    def _fallback(self, reason: str) -> Decision:
        if self.doc.default_allow:
            return Decision(allow=True, reason=f"default-allow ({reason})")
        return Decision(allow=False, reason=reason)
