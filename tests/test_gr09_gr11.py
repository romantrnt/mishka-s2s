from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.foundation.config import Settings, SettingsError
from app.foundation.simulator import SimulatorAcceptanceHarness
from app.foundation.stand import HardwareIntegrationStand, create_stand_app

ROOT = Path(__file__).parents[1]


def run(coro):
    return asyncio.run(coro)


def test_exhibition_configuration_is_explicit_and_rejects_invalid_policy():
    settings = Settings(profile="mock", runtime_mode="exhibition", hardware_mode="simulator").validate()
    snapshot = settings.operational_snapshot()
    assert snapshot["mode"] == "exhibition"
    assert snapshot["scenario_id"] == "canonical_product"
    assert snapshot["graph_backend"] == "langgraph"
    assert snapshot["graph"] == "langgraph"
    assert snapshot["audio_policy"] == "recorded_catalog"
    assert snapshot["hid_policy"] == "space_lifecycle"
    assert snapshot["lighting_policy"] == "canonical_effects"
    assert set(snapshot["provenance"]) == {"config_source", "entrypoint", "python"}
    assert "stand_access_token" not in snapshot
    with pytest.raises(SettingsError, match="MISHKA_AUDIO_POLICY"):
        Settings(profile="mock", audio_policy="legacy_text").validate()


def test_declared_env_file_is_loaded_and_process_values_win(monkeypatch, tmp_path):
    from scripts import run_stand

    env_file = tmp_path / "explicit.env"
    env_file.write_text("MISHKA_AUD00_PROBE=from-file\nMISHKA_AUD00_PROCESS=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MISHKA_ENV_FILE", str(env_file))
    monkeypatch.setenv("MISHKA_AUD00_PROCESS", "from-process")
    monkeypatch.delenv("MISHKA_AUD00_PROBE", raising=False)
    run_stand._load_runtime_env()
    assert os.environ["MISHKA_AUD00_PROBE"] == "from-file"
    assert os.environ["MISHKA_AUD00_PROCESS"] == "from-process"

def test_operational_health_is_machine_readable_and_redacted():
    stand = HardwareIntegrationStand(Settings(profile="mock", runtime_mode="exhibition", hardware_mode="simulator"))
    try:
        health = stand.health_snapshot()
        assert health["status"] in {"ready", "degraded"}
        assert health["mode"] == "exhibition"
        assert health["canonical_state"] == "idle"
        assert health["handset"] == "down"
        assert health["session"]["active"] is False
        assert "session_id" not in json.dumps(health)
        assert "transcript" not in json.dumps(health).lower()
        assert "profile_db" not in json.dumps(health).lower()
        diagnostics = stand.diagnostics_snapshot()
        assert set(diagnostics) == {"health", "trace", "diagnostics", "physical_lighting"}
        assert "session_id" not in json.dumps(diagnostics)
        assert "audio_buffer" not in json.dumps(diagnostics).lower()
    finally:
        stand.close()


def test_stand_health_routes_are_read_only_and_webpanel_optional():
    app = create_stand_app(Settings(profile="mock", hardware_mode="simulator", webpanel_enabled=False))
    try:
        paths = {route.path for route in app.routes}
        assert "/health" in paths and "/stand/health" in paths and "/stand/diagnostics" in paths
        assert "/console" not in paths
        assert "/stand/health" not in {"/stand/handset", "/stand/button", "/stand/text"}
        endpoint = next(route.endpoint for route in app.routes if route.path == "/stand/health")
        assert endpoint()["mode"] == "exhibition"
    finally:
        app.state.stand.close()


def test_simulator_harness_is_deterministic_for_welcome_reset_and_stale_hid():
    async def scenario():
        harness = SimulatorAcceptanceHarness()
        try:
            assert harness.health()["canonical_state"] == "idle"
            first = await harness.space_released(1)
            assert first["reason"] == "exhibition_handset_lifted"
            assert sum(item.get("type") == "lighting_wave_intent" for item in first["outputs"]) == 1
            due = await harness.advance(5.0)
            assert len(due) == 1
            assert sum(item.get("phrase_id") == "PHRASE_001" for item in due[0]["outputs"]) == 1
            down = await harness.space_pressed(2)
            assert down["reason"] == "exhibition_reset"
            assert harness.health()["canonical_state"] == "idle"
            harness.disconnect_hid()
            assert harness.health()["hid"]["status"] == "simulated"
            harness.reconnect_hid()
            assert harness.health()["session"]["active"] is False
        finally:
            harness.close()

    run(scenario())


def test_clean_process_smoke_from_other_cwd_has_bounded_output():
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/smoke_test.py")],
        cwd="/tmp", env=env, text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["personal_data_output"] is False
    assert "session_id" not in result.stdout
    assert "transcript" not in result.stdout.lower()
