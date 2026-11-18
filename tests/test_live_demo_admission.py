"""Test-only host diagnostics prove all ingress shares the live-demo guard.

No device, audio, production model or TLS acceptance is simulated as evidence.
"""
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.foundation.config import Settings
from app.foundation.lifecycle import RuntimeState, StartupLifecycle
from app.foundation.stand import create_stand_app

TOKEN = "synthetic-admission-test-only"
HEADERS = {"x-stand-token": TOKEN}


class Client:
    event = None
    clears = 0
    def start(self): return True
    def poll(self):
        event, self.event = self.event, None
        return event
    def patient(self): raise AssertionError("unavailable runtime read patient")
    def clear(self): self.clears += 1
    def close(self): return True


@pytest_asyncio.fixture
async def rig(monkeypatch):
    client = Client()
    api = create_stand_app(Settings(identity_mode="max_json", stand_access_token=TOKEN), phone_client=client)
    stand = api.state.stand
    # The test changes only DI diagnostics after constructing fake adapters.
    # Normal startup validates and opens real physical dependencies instead.
    status = dict(hid=True, level=True, lighting=True, playback=True, capture=True)
    try:
        with monkeypatch.context() as patch:
            settings = replace(stand.settings, max_client_source="local_demo", max_synthetic_data=True)
            patch.setattr(stand, "settings", settings)
            patch.setattr(stand.application, "settings", settings)
            patch.setattr(stand, "hardware_mode", "physical")
            patch.setattr(stand, "_hid_status", {"state": "active", "physical_ready": True})
            patch.setattr(stand, "input_adapter", SimpleNamespace(diagnostics=lambda: {
                "ready": status["hid"], "physical_ready": status["hid"] and status["level"],
            }))
            patch.setattr(stand, "lighting_serial", SimpleNamespace(diagnostics=lambda: {"ready": status["lighting"]}))
            patch.setattr(stand, "_lighting_status", {"state": "available"})
            patch.setattr(stand.playback, "readiness", lambda: {"ready": status["playback"]})
            patch.setattr(stand, "audio_input", SimpleNamespace(is_ready=lambda: status["capture"], diagnostics=lambda: {"ready": status["capture"]}))
            patch.setattr(stand, "_headless_audio_status", {"state": "active"})
            # Explicit unit-test seam, never used by normal run_stand.py.
            api.state.lifecycle._state = RuntimeState.READY
            yield api, stand, client, status
    finally:
        await stand.aclose()


def test_guard_is_dynamic_fail_closed_and_never_overrides_shutdown():
    state = {"ready": False}
    gate = StartupLifecycle(admission_guard=lambda: state["ready"])
    gate._state = RuntimeState.READY
    assert not gate.admits_users()
    state["ready"] = True
    assert gate.admits_users()
    gate.begin_shutdown()
    assert not gate.admits_users()
    def broken(): raise RuntimeError("diagnostics unavailable")
    gate = StartupLifecycle(admission_guard=broken)
    gate._state = RuntimeState.READY
    assert not gate.admits_users()


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["hid", "level", "lighting", "playback", "capture"])
async def test_loss_recloses_hid_json_and_http_without_waiting_for_health_poll(rig, dependency):
    api, stand, client, status = rig
    assert stand.physical_hardware_ready()
    assert api.state.lifecycle.admits_users()
    status[dependency] = False
    assert not api.state.lifecycle.admits_users()
    assert not api.state.lifecycle.snapshot()["ready"]
    assert not stand.physical_hardware_ready()
    result = await stand.inject_physical({"type": "SpaceReleased"})
    assert result["reason"] == "runtime_unavailable"
    client.event = {"kind": "patient_ready"}
    boundary = stand.application._max_client_boundary
    assert (await boundary.step())["reason"] == "runtime_unavailable"
    assert client.clears == 0  # An automatic rejection cannot erase a concurrent Max scan.
    assert not stand.application._canonical_profiles
    assert not stand.application._identity_requests and boundary.facts is None
    assert await boundary.step() is None  # No silent reauthorization from Max's retained card.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as http:
        response = await http.post("/stand/handset", headers=HEADERS, json={"state": "up"})
        assert response.status_code == 503
        health = (await http.get("/health")).json()
        assert health["status"] == "degraded"
        if dependency in {"hid", "level"}:
            assert not health["hid"]["physical_ready"]
        assert not health["physical_hardware_ready"]
        assert not health["production_ready"]
        assert health["identity"]["source"] == "local_demo"
        ready = (await http.get("/ready")).json()
        assert not ready["ready"] and not ready["physical_hardware_ready"]
        assert not ready["production_ready"]
    # Cleanup is accepted even when new interactions are closed.
    assert (await stand.inject_physical({"type": "SpacePressed"})).get("reason") != "runtime_unavailable"
    status[dependency] = True
    assert api.state.lifecycle.admits_users()


@pytest.mark.asyncio
async def test_reader_unavailable_does_not_disable_anonymous_admission(rig):
    api, stand, client, status = rig
    boundary = stand.application._max_client_boundary
    assert not boundary._reader_ready
    assert api.state.lifecycle.admits_users()
    assert stand.physical_hardware_ready()


@pytest.mark.asyncio
async def test_existing_generic_websocket_rechecks_hardware_before_each_message(rig):
    api, stand, client, status = rig
    with TestClient(api).websocket_connect("/ws/stand", headers=HEADERS) as socket:
        status["lighting"] = False
        socket.send_json({"type": "handset_up"})
        response = socket.receive_json()
        assert response["error"]["code"] == "runtime_unavailable"
        assert not stand.application._canonical_profiles


@pytest.mark.asyncio
async def test_panel_observer_remains_available_but_mutation_is_denied(rig):
    api, stand, client, status = rig
    status["lighting"] = False
    with TestClient(api).websocket_connect("/stand/ws", headers=HEADERS) as socket:
        assert socket.receive_json()["type"] == "stand_state"
        socket.send_json({"type": "handset", "state": "up"})
        assert socket.receive_json()["error"]["code"] == "runtime_unavailable"
