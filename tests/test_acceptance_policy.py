
import pytest

from app.foundation.acceptance_policy import (
    POLICY_VERSION,
    default_claim,
    validate_acceptance_claim,
    validate_report,
)


def report(**overrides):
    value = {
        "policy_version": POLICY_VERSION,
        **default_claim("acceptance"),
    }
    value.update(overrides)
    return value


def test_health_only_cannot_claim_ready():
    value = report(
        claim_ready=True,
        readiness_evidence={"health": True, "ready": False, "resource_gate": False,
                            "e2e_warmup": False, "source": "health_only"},
        real_dependencies=True,
        test_doubles=[],
    )
    errors = validate_acceptance_claim(value)
    assert any("health-only" in item for item in errors)
    assert any("resource_gate" in item for item in errors)


def test_fixture_or_mock_cannot_claim_production():
    value = report(acceptance_type="production_runtime", real_dependencies=False,
                   test_doubles=["mock_tts"])
    errors = validate_acceptance_claim(value)
    assert any("production" in item for item in errors)
    assert any("test-double" in item for item in errors)


def test_pty_cannot_claim_physical_hardware():
    value = report(
        acceptance_type="physical_hardware",
        environment={"name": "canonical_runtime"},
        hardware_level="pty",
        real_dependencies=True,
        readiness_evidence={"physical_acceptance": True},
    )
    errors = validate_acceptance_claim(value)
    assert any("physical" in item for item in errors)


def test_valid_runtime_claim_requires_all_evidence():
    value = report(
        acceptance_type="production_runtime",
        environment={"name": "canonical_runtime"},
        real_dependencies=True,
        hardware_level="simulator",
        test_doubles=[],
        claim_ready=True,
        readiness_evidence={
            "health": True, "ready": True, "resource_gate": True,
            "e2e_warmup": True, "source": "controlled_runtime",
        },
    )
    assert validate_acceptance_claim(value) == []


def test_policy_rejects_missing_version():
    with pytest.raises(ValueError, match="policy version"):
        validate_report({"acceptance_type": "unit_regression"})

