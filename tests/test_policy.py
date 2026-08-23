from __future__ import annotations

from air_llm.constants import Action
from air_llm.policy.engine import PolicyEngine
from air_llm.schemas.common import Principal
from air_llm.schemas.policy import PolicyDoc, ServicePolicy


def _engine(default_allow: bool) -> PolicyEngine:
    doc = PolicyDoc(
        default_allow=default_allow,
        default_rate_per_minute=100,
        services={
            "air-classifier": ServicePolicy(
                resources={"inference": ["chat", "embeddings"]},
                rate_per_minute=300,
            )
        },
    )
    return PolicyEngine(doc)


def test_explicit_grant_allows() -> None:
    p = Principal(service="air-classifier")
    assert _engine(False).decide(p, "inference", Action.CHAT).allow


def test_missing_grant_denies_when_default_deny() -> None:
    p = Principal(service="air-classifier")
    d = _engine(False).decide(p, "admin", Action.ADMIN_READ)
    assert not d.allow
    assert "admin" in d.reason


def test_unknown_service_denied_when_default_deny() -> None:
    assert not _engine(False).decide(Principal(service="ghost"), "inference", Action.CHAT).allow


def test_default_allow_opens_the_gate() -> None:
    d = _engine(True).decide(Principal(service="ghost"), "inference", Action.CHAT)
    assert d.allow
    assert "default-allow" in d.reason


def test_rate_override_and_default() -> None:
    e = _engine(False)
    assert e.rate_per_minute(Principal(service="air-classifier")) == 300
    assert e.rate_per_minute(Principal(service="ghost")) == 100


def test_local_policy_yaml_loads() -> None:
    e = PolicyEngine.load("deploy/policy/policy.local.yaml")
    assert e.decide(Principal(service="air-client"), "inference", Action.CHAT).allow
