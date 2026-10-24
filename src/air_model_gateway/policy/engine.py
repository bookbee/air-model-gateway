from __future__ import annotations

from pathlib import Path

import yaml

from air_model_gateway.constants import Action, ModelClass, Provider
from air_model_gateway.observability.logging import get_logger
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.policy import Decision, PolicyDoc

log = get_logger("policy")


class PolicyEngine:
    """RBAC over ``(service, resource, action)``.

    air-model-gateway has exactly one resource (``inference``) plus ``admin`` for the
    policy-dump endpoint. The rule shape is general (any resource, any action
    list), so adding a resource is a policy-file change, not a code change.

    Fails open only when ``default_allow`` is set *and* the environment permits
    it — a local convenience. Outside ``local`` the flag is overridden to false
    at load (HLD §5, §7: strict, fail closed), so a local policy file mounted
    into staging/prod by mistake cannot quietly open the gate to any caller
    holding a valid token.
    """

    def __init__(self, doc: PolicyDoc) -> None:
        self.doc = doc

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        default_rate_per_minute: int = 120,
        permit_default_allow: bool = True,
    ) -> PolicyEngine:
        p = Path(path)
        if not p.exists():
            # No file -> deny-by-default empty policy (safe if a prod path is wrong).
            return cls(
                PolicyDoc(default_allow=False, default_rate_per_minute=default_rate_per_minute)
            )
        raw = yaml.safe_load(p.read_text()) or {}
        raw.setdefault("default_rate_per_minute", default_rate_per_minute)
        doc = PolicyDoc.model_validate(raw)
        if doc.default_allow and not permit_default_allow:
            log.warning(
                "policy.default_allow_ignored",
                path=str(p),
                reason="default_allow is honoured only in the local environment",
            )
            doc = doc.model_copy(update={"default_allow": False})
        return cls(doc)

    def decide(self, principal: Principal, resource: str, action: Action) -> Decision:
        sp = self.doc.services.get(principal.service)
        if sp is None:
            return self._fallback(f"no policy for service '{principal.service}'")

        allowed = sp.resources.get(resource)
        if allowed is None:
            return self._fallback(f"'{principal.service}' has no grant on '{resource}'")
        if action in allowed:
            return Decision(allow=True, reason="explicit grant")
        return self._fallback(f"'{principal.service}' may not '{action.value}' on '{resource}'")

    def may_use_class(self, principal: Principal, model_class: ModelClass) -> bool:
        return self._allows(principal, "model_classes", model_class)

    def may_use_embed_model(self, principal: Principal, name: str) -> bool:
        return self._allows(principal, "embed_models", name)

    def may_prefer(self, principal: Principal, provider: Provider) -> bool:
        return self._allows(principal, "preferred_providers", provider)

    def may_pin(self, principal: Principal) -> bool:
        sp = self.doc.services.get(principal.service)
        return sp.pin if sp is not None else self.doc.default_allow

    def _allows(self, principal: Principal, field: str, value: object) -> bool:
        """A choice is allowed unless the service's policy lists the field and
        leaves the value out. An unlisted service gets default_allow."""
        sp = self.doc.services.get(principal.service)
        if sp is None:
            return self.doc.default_allow
        allowed = getattr(sp, field)
        return allowed is None or value in allowed

    def daily_budget_usd(self, principal: Principal) -> float | None:
        sp = self.doc.services.get(principal.service)
        return sp.daily_budget_usd if sp else None

    def provider_quota(self, provider: Provider) -> int | None:
        return self.doc.provider_quotas.get(provider)

    def rate_per_minute(self, principal: Principal) -> int:
        sp = self.doc.services.get(principal.service)
        if sp and sp.rate_per_minute is not None:
            return sp.rate_per_minute
        return self.doc.default_rate_per_minute

    def _fallback(self, reason: str) -> Decision:
        if self.doc.default_allow:
            return Decision(allow=True, reason=f"default-allow ({reason})")
        return Decision(allow=False, reason=reason)
