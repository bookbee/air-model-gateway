from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from air_model_gateway.constants import Action
from air_model_gateway.policy.engine import PolicyEngine
from air_model_gateway.schemas.common import Principal
from air_model_gateway.schemas.policy import PolicyDoc, ServicePolicy


def _engine(default_allow: bool) -> PolicyEngine:
    doc = PolicyDoc(
        default_allow=default_allow,
        default_rate_per_minute=100,
        services={
            "air-classifier-service": ServicePolicy(
                resources={"inference": ["generate", "embed"]},
                rate_per_minute=300,
            )
        },
    )
    return PolicyEngine(doc)


def test_explicit_grant_allows() -> None:
    p = Principal(service="air-classifier-service")
    assert _engine(False).decide(p, "inference", Action.GENERATE).allow


def test_missing_grant_denies_when_default_deny() -> None:
    p = Principal(service="air-classifier-service")
    d = _engine(False).decide(p, "admin", Action.ADMIN_READ)
    assert not d.allow
    assert "admin" in d.reason


def test_unknown_service_denied_when_default_deny() -> None:
    assert not _engine(False).decide(Principal(service="ghost"), "inference", Action.GENERATE).allow


def test_default_allow_opens_the_gate() -> None:
    d = _engine(True).decide(Principal(service="ghost"), "inference", Action.GENERATE)
    assert d.allow
    assert "default-allow" in d.reason


def test_rate_override_and_default() -> None:
    e = _engine(False)
    assert e.rate_per_minute(Principal(service="air-classifier-service")) == 300
    assert e.rate_per_minute(Principal(service="ghost")) == 100


def test_local_policy_yaml_loads() -> None:
    e = PolicyEngine.load("deploy/policy/policy.local.yaml")
    assert e.decide(Principal(service="air-web-console"), "inference", Action.GENERATE).allow


def test_default_allow_is_overridden_where_not_permitted() -> None:
    """HLD §5/§7: outside `local` the gateway fails closed. The shipped local
    policy sets default_allow: true, so mounting it in prod by mistake must not
    open the gate to every token holder — only explicit grants survive."""
    e = PolicyEngine.load("deploy/policy/policy.local.yaml", permit_default_allow=False)
    assert e.doc.default_allow is False
    assert not e.decide(Principal(service="ghost"), "inference", Action.GENERATE).allow
    # air-web-console is granted chat only; embeddings rode on default_allow locally.
    assert e.decide(Principal(service="air-web-console"), "inference", Action.GENERATE).allow
    assert not e.decide(Principal(service="air-web-console"), "inference", Action.EMBED).allow


def test_missing_policy_file_denies_by_default(tmp_path: Path) -> None:
    e = PolicyEngine.load(tmp_path / "absent.yaml")
    assert not e.decide(Principal(service="air-web-console"), "inference", Action.GENERATE).allow


def test_an_unknown_action_fails_the_policy_file_at_load(tmp_path: Path) -> None:
    """A stale or misspelt action (e.g. the pre-route name `chat`) must stop the
    load loudly, not grant nothing and 403 a consumer at runtime."""
    bad = tmp_path / "policy.yaml"
    bad.write_text("services:\n  svc:\n    resources:\n      inference: [chat]\n")
    with pytest.raises(ValidationError):
        PolicyEngine.load(bad)
