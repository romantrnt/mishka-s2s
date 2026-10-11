
"""Acceptance evidence policy: keep health, readiness, fixtures and hardware claims separate."""
from __future__ import annotations

from typing import Any

POLICY_VERSION = "mishka-acceptance/v1"
PRODUCTION_TYPES = {"production_runtime", "physical_hardware"}
HARDWARE_LEVELS = {"none", "simulator", "pty", "physical"}


def default_claim(mode: str) -> dict[str, Any]:
    if mode == "acceptance":
        return {
            "acceptance_type": "runtime_acceptance",
            "environment": {"name": "canonical_runtime"},
            "real_dependencies": False,
            "hardware_level": "simulator",
            "test_doubles": [],
            "claim_ready": False,
            "readiness_evidence": {
                "health": False, "ready": False, "resource_gate": False,
                "e2e_warmup": False, "source": "not_claimed",
            },
        }
    return {
        "acceptance_type": f"{mode}_regression",
        "environment": {"name": "current_pod"},
        "real_dependencies": False,
        "hardware_level": "none",
        "test_doubles": ["fixtures_or_mocks_allowed_in_tests"],
        "claim_ready": False,
        "readiness_evidence": {
            "health": False, "ready": False, "resource_gate": False,
            "e2e_warmup": False, "source": "pytest_or_static_only",
        },
    }


def validate_acceptance_claim(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    acceptance_type = report.get("acceptance_type")
    environment = report.get("environment", {})
    environment_name = environment.get("name") if isinstance(environment, dict) else environment
    real_dependencies = bool(report.get("real_dependencies", False))
    hardware_level = report.get("hardware_level")
    test_doubles = report.get("test_doubles") or []
    claim_ready = bool(report.get("claim_ready", False))
    evidence = report.get("readiness_evidence") or {}
    if hardware_level not in HARDWARE_LEVELS:
        errors.append("hardware_level must be one of none/simulator/pty/physical")
    if not isinstance(test_doubles, list):
        errors.append("test_doubles must be a list")
    if acceptance_type in PRODUCTION_TYPES:
        if not real_dependencies:
            errors.append("production acceptance requires real_dependencies=true")
        if test_doubles:
            errors.append("fixture/mock/test-double evidence cannot claim production")
    if claim_ready:
        if acceptance_type not in {"runtime_acceptance", "production_runtime"}:
            errors.append("regression or unit evidence cannot claim READY")
        if evidence.get("health") and not evidence.get("ready"):
            errors.append("health-only evidence cannot claim READY")
        if not evidence.get("resource_gate"):
            errors.append("READY claim requires resource_gate evidence")
        if not evidence.get("e2e_warmup"):
            errors.append("READY claim requires full E2E warmup evidence")
        if not evidence.get("ready"):
            errors.append("READY claim requires /ready=true evidence")
        if not real_dependencies:
            errors.append("READY claim requires real dependencies")
        if test_doubles:
            errors.append("READY claim cannot contain fixture/mock/test-double evidence")
    if evidence.get("health") and evidence.get("source") == "health_only" and claim_ready:
        errors.append("health-only report is not readiness evidence")
    if evidence.get("physical_acceptance") and hardware_level != "physical":
        errors.append("PTY/simulator evidence cannot be marked physical acceptance")
    if hardware_level == "physical":
        if environment_name != "physical_hardware":
            errors.append("physical hardware acceptance requires physical_hardware environment")
        if not evidence.get("physical_acceptance"):
            errors.append("physical hardware acceptance requires physical_acceptance evidence")
    if evidence.get("ready") and not evidence.get("resource_gate"):
        errors.append("ready=true evidence without resource_gate is invalid")
    return errors


def validate_report(report: dict[str, Any]) -> None:
    if report.get("policy_version") != POLICY_VERSION:
        raise ValueError("missing or unsupported acceptance policy version")
    errors = validate_acceptance_claim(report)
    if errors:
        raise ValueError("; ".join(errors))

