"""Real Stand/PTY/canonical transitions with mock speech providers only."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.foundation.api import create_app
from app.foundation.config import Settings, SettingsError
from app.foundation.lifecycle import RuntimeState
from app.foundation.remote_devices import install_remote_devices
from app.foundation.s2s import AudioChunk
from app.foundation.simulator import SimulatorAcceptanceHarness
from app.foundation.stand import BrowserPlaybackController, create_stand_app


def make_harness():
    harness = SimulatorAcceptanceHarness()
    harness.stand._admits_physical_users = lambda: True
    simulator = install_remote_devices(FastAPI(), harness.stand, admits_users=lambda: True)
    return harness, simulator


@pytest.mark.asyncio
async def test_actions_reach_actual_canonical_events_and_outputs():
    harness, simulator = make_harness()
    try:
        lift = await simulator.action({"action": "handset", "state": "up", "epoch": 0})
        assert lift["canonical_state"]["mode"] == "voice_welcome"
        assert harness.stand.publisher.events[-1]["type"] == "HandsetLifted"
        state = harness.stand.panel_snapshot()["simulator"]
        assert state["devices"]["handset"] == {"state": "up", "space_level": "released"}
        assert any(state["devices"]["lighting"].values())
        assert len(state["devices"]["buttons"]) == 7
        assert state["profile"] == "mock"
        assert state["synthetic_profile"] is False
        assert state["production_ready"] is False
        # All authentic protocol press kinds reach the actual event parser.
        for press, event in [("press", "ButtonPressed"), ("release", "ButtonReleased"),
                             ("hold", "ButtonHeld"), ("long_hold", "ButtonLongHeld")]:
            payload = {"action": "button", "button_id": 4, "press": press}
            if press in {"hold", "long_hold"}:
                payload["duration_ms"] = 700
            await simulator.action(payload)
            assert harness.stand.publisher.events[-1]["type"] == event
        hangup = await simulator.action({"action": "handset", "state": "down", "epoch": 0})
        assert hangup["canonical_state"]["handset"] == "down"
        assert set(harness.snapshot()["physical_lighting"].values()) == {0}
        assert simulator.epoch == 1
        assert any(item["type"] == "audio_stop" for item in simulator.journal()["items"])
        assert simulator.journal()["items"][-1]["cursor"] == simulator.journal()["cursor"]
    finally:
        await harness.stand.aclose()


@pytest.mark.asyncio
async def test_delayed_old_lift_cannot_restart_hung_up_session():
    harness, simulator = make_harness()
    try:
        await simulator.fault({"device": "input", "mode": "delay", "delay_ms": 100})
        delayed = asyncio.create_task(simulator.action({"action": "handset", "state": "up"}))
        while not simulator.pending_inputs:
            await asyncio.sleep(0)
        await simulator.action({"action": "handset", "state": "down"})
        rejected = await delayed
        assert rejected["error"]["code"] == "stale_simulator_epoch"
        assert harness.stand.panel_snapshot()["simulator"]["devices"]["handset"]["state"] == "down"
        await simulator.fault({"device": "input", "mode": "delay", "delay_ms": 0})
        await simulator.action({"action": "handset", "state": "up", "epoch": simulator.epoch})
        assert harness.snapshot()["canonical_state"]["mode"] == "voice_welcome"
        rejected = await simulator.action({"action": "button", "button_id": 1, "epoch": 0})
        assert rejected["error"]["code"] == "stale_simulator_epoch"
    finally:
        await harness.stand.aclose()


@pytest.mark.asyncio
async def test_uart_waiter_is_fenced_before_hangup_can_acquire_transport():
    harness, simulator = make_harness()
    try:
        await simulator.action({"action": "handset", "state": "up"})
        await harness.stand._transport_lock.acquire()
        button = asyncio.create_task(simulator.action({"action": "button", "button_id": 1}))
        while not harness.stand._transport_lock._waiters:
            await asyncio.sleep(0)
        hangup = asyncio.create_task(simulator.action({"action": "handset", "state": "down"}))
        await asyncio.sleep(0)
        assert simulator.epoch == 1
        harness.stand._transport_lock.release()
        stale = await button
        assert stale["reason"] == "stale_session_generation"
        await hangup
        assert harness.snapshot()["canonical_state"]["handset"] == "down"
    finally:
        if harness.stand._transport_lock.locked():
            harness.stand._transport_lock.release()
        await harness.stand.aclose()


@pytest.mark.asyncio
async def test_pcm_envelopes_have_browser_owner_and_stop_fences_remaining_frames():
    started = asyncio.Event()
    release = asyncio.Event()
    class Hub:
        sent = []
        async def publish(self, payload):
            self.sent.append(payload)
            if payload["type"] == "audio_pcm_output":
                started.set()
                await release.wait()
    hub = Hub()
    playback = BrowserPlaybackController(hub)
    playback.active_token = 4
    task = asyncio.create_task(playback.play(AudioChunk((0.1,) * 8192, 24000, 4, session_id="stand")))
    await started.wait()
    await playback.stop()
    release.set()
    await task
    pcm = [item for item in hub.sent if item["type"] == "audio_pcm_output"]
    assert len(pcm) == 1
    assert pcm[0]["session_id"] == "stand" and pcm[0]["generation"] == 0
    assert hub.sent[-1] == {"type": "audio_stop", "generation": 1, "checkpoint_request": None}


@pytest.mark.asyncio
async def test_disconnect_reconnect_does_not_clear_real_safe_stop():
    harness, simulator = make_harness()
    try:
        await simulator.action({"action": "handset", "state": "up"})
        disconnected = await simulator.fault({"device": "input", "mode": "disconnect"})
        assert disconnected["canonical_state"]["mode"] == "safety_stop"
        blocked = await simulator.action({"action": "button", "button_id": 1})
        assert blocked["error"]["code"] == "simulated_input_disconnected"
        # Legacy HTTP/WS ingress shares the same adapter barrier.
        blocked = await harness.stand.inject("button", {"button_id": 1})
        assert blocked["error"]["code"] == "simulated_input_disconnected"
        await simulator.fault({"device": "input", "mode": "reconnect"})
        assert harness.snapshot()["canonical_state"]["mode"] == "safety_stop"
        await simulator.action({"action": "button", "button_id": 1})
        assert harness.snapshot()["canonical_state"]["mode"] == "safety_stop"
        await simulator.action({"action": "handset", "state": "down"})
        await simulator.action({"action": "handset", "state": "up"})
        assert harness.snapshot()["canonical_state"]["mode"] == "voice_welcome"
        # No invented adult dispatch or delivery acknowledgement.
        public = json.dumps(harness.stand.panel_snapshot())
        assert "caregiver_notified" not in public and "incident_id" not in public
    finally:
        await harness.stand.aclose()


@pytest.mark.asyncio
async def test_journal_is_bounded_retains_reset_and_contains_no_speech():
    harness, simulator = make_harness()
    try:
        await simulator.action({"action": "handset", "state": "up"})
        await harness.stand.inject("text", {"text": "SENSITIVE SPEECH FIXTURE"}, wait_for_audio=False)
        for _ in range(simulator.JOURNAL_LIMIT + 5):
            simulator.record("fault", "input_delay", {"delay_ms": 0})
        before = simulator.journal()["cursor"]
        await simulator.action({"action": "reset"})
        journal = simulator.journal()
        assert len(journal["items"]) == simulator.JOURNAL_LIMIT
        assert journal["gap"] is True
        assert journal["cursor"] > before
        assert all(item["cursor"] > before for item in simulator.journal(before)["items"])
        assert "SENSITIVE SPEECH FIXTURE" not in json.dumps(journal)
        assert "reset" in [item["type"] for item in journal["items"]]
    finally:
        await harness.stand.aclose()


@pytest.mark.asyncio
async def test_stale_audio_from_previous_session_is_rejected():
    harness, simulator = make_harness()
    try:
        await simulator.action({"action": "handset", "state": "up"})
        await harness.advance(5.0)
        old = harness.audio_identity("PHRASE_001")
        await simulator.action({"action": "handset", "state": "down"})
        await simulator.action({"action": "handset", "state": "up"})
        await harness.advance(5.0)
        assert harness.audio_identity("PHRASE_001")["playback_lease"] != old["playback_lease"]
        result = await harness.audio_completed("PHRASE_001", identity=old)
        assert result["reason"] == "stale_exhibition_audio_completion"
        assert harness.snapshot()["canonical_state"]["mode"] == "voice_welcome"
        assert harness.stand.panel_snapshot()["simulator"]["prompt"]["text"] == "Привет, я Мишка!"
    finally:
        await harness.stand.aclose()


@pytest.mark.asyncio
async def test_http_auth_admission_validation_and_cleanup():
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator", stand_access_token="unit-operator-secret"))
    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://stand") as client:
            assert (await client.get("/stand/simulator/state")).status_code == 401
            assert (await client.get("/stand/simulator/state?token=unit-operator-secret")).status_code == 401
            client.headers["x-stand-token"] = "unit-operator-secret"
            assert (await client.get("/stand/simulator/state")).status_code == 200
            assert (await client.post("/stand/simulator/action", json={"action": "handset", "state": "up"})).status_code == 503
            assert (await client.post("/stand/simulator/action", json={"action": "handset", "state": "down"})).status_code == 200
            # Explicit test-only admission seam; no model readiness claim.
            api.state.lifecycle._state = RuntimeState.READY
            for payload in ({"action": "button", "button_id": True}, {"action": "button", "button_id": 8},
                            {"action": "button", "button_id": 1, "press": []},
                            {"action": "button", "button_id": 1, "press": "hold", "duration_ms": -1},
                            {"action": "handset", "state": []}, {"action": "rfid", "card": []},
                            {"action": "rfid", "card": "demo-1", "patient_profile": {"name": "forged"}}):
                invalid = await client.post("/stand/simulator/action", json=payload)
                assert invalid.status_code == 422, invalid.text
            scan = await client.post("/stand/simulator/action", json={"action": "rfid", "card": "demo-1"})
            assert scan.status_code == 409 and scan.json()["error"]["code"] == "scan_unavailable"
            assert (await client.post("/stand/simulator/fault", json={"device": "input", "mode": "delay", "delay_ms": 2001})).status_code == 422
            lift = await client.post("/stand/simulator/action", json={"action": "handset", "state": "up"})
            assert lift.status_code == 200
            assert lift.json()["state"]["simulator"]["devices"]["handset"]["state"] == "up"
            assert (await client.get("/stand/simulator/journal?after=-1")).status_code == 422
    finally:
        await api.state.stand.aclose()


@pytest.mark.asyncio
async def test_missing_server_token_is_closed_for_observers_and_mutations():
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://stand") as client:
            assert (await client.get("/stand/simulator/state")).status_code == 503
            assert (await client.get("/stand/simulator/journal")).status_code == 503
            assert (await client.post("/stand/simulator/action", json={"action": "reset"})).status_code == 503
    finally:
        await api.state.stand.aclose()


def test_canonical_mutation_websocket_cannot_bypass_simulator_disconnect():
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator", stand_access_token="unit-operator-secret"))
    with TestClient(api) as client:
        # Explicit test-only control seam, never evidence of model readiness.
        api.state.lifecycle._state = RuntimeState.READY
        client.headers["x-stand-token"] = "unit-operator-secret"
        assert client.post("/stand/simulator/action", json={"action": "handset", "state": "up"}).status_code == 200
        assert client.post("/stand/simulator/fault", json={"device": "input", "mode": "disconnect"}).status_code == 200
        epoch = api.state.remote_devices.epoch
        rejection = None
        try:
            with client.websocket_connect("/ws/stand", headers={"x-stand-token": "unit-operator-secret"}) as socket:
                socket.send_json({"type": "handset_down", "sequence": 10000})
                socket.receive_json()
                socket.send_json({"type": "handset_up", "sequence": 10001})
                socket.receive_json()
        except WebSocketDisconnect as exc:
            rejection = exc
        assert rejection is not None, "canonical WS bypassed the virtual input receiver"
        assert rejection.code == 1008 and rejection.reason == "stand_ingress_required"
        state = api.state.stand.application.canonical_runtime.sessions["stand"]
        assert state.mode == "safety_stop"
        assert api.state.remote_devices.connected is False
        assert api.state.remote_devices.epoch == epoch
        # Official cleanup remains usable while the input adapter is offline.
        cleanup = client.post("/stand/simulator/action", json={"action": "handset", "state": "down"})
        assert cleanup.status_code == 200
        assert api.state.remote_devices.epoch > epoch
        assert api.state.remote_devices.connected is False
        blocked = client.post("/stand/simulator/action", json={"action": "handset", "state": "up"})
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "simulated_input_disconnected"
        assert client.post("/stand/simulator/fault", json={"device": "input", "mode": "reconnect"}).status_code == 200
        assert client.post("/stand/simulator/action", json={"action": "handset", "state": "up"}).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_owner", [False, True])
async def test_canonical_websocket_remains_available_without_remote_devices(physical_owner):
    # Exercise dispatch selection with actual canonical application/PTY outputs.
    # The physical-owner branch is a routing fixture, not real hardware proof.
    harness = SimulatorAcceptanceHarness(Settings(profile="mock", hardware_mode="simulator", stand_access_token="unit-operator-secret"))
    api = create_app(harness.stand.application)
    if physical_owner:
        harness.stand.hardware_mode = "physical"
        api.state.stand = harness.stand
    api.state.lifecycle._state = RuntimeState.READY
    endpoint = next(route.endpoint for route in api.routes if route.path == "/ws/{session_id}")
    class Socket:
        headers = {"x-stand-token": "unit-operator-secret"}
        cookies = {}
        sent = []
        incoming = [{"type": "handset_up", "sequence": 1}]
        async def accept(self): pass
        async def receive_json(self):
            if self.incoming:
                return self.incoming.pop(0)
            raise WebSocketDisconnect()
        async def send_json(self, payload): self.sent.append(payload)
        async def close(self, **kwargs): raise AssertionError(f"unexpected receiver rejection: {kwargs}")
    socket = Socket()
    try:
        await endpoint(socket, "stand")
        assert socket.sent[-1]["canonical_state"]["mode"] == "voice_welcome"
    finally:
        harness.stand.hardware_mode = "simulator"
        await harness.stand.aclose()


def test_real_provider_simulator_configuration_is_explicit_and_synthetic_only(tmp_path):
    valid = Settings(profile="local_gpu", hardware_mode="simulator", identity_mode="max_json",
                     max_client_source="local_demo", max_synthetic_data=True,
                     max_client_bundle=str(tmp_path / "max"), max_demo_state=str(tmp_path / "synthetic"),
                     stand_access_token="unit-operator-secret")
    assert valid.validate() is valid
    for delta in ({"max_synthetic_data": False}, {"stand_access_token": ""}, {"allow_query_token": True},
                  {"max_client_source": "disabled"}, {"profile": "production"}, {"hardware_mode": "fake"}):
        with pytest.raises(SettingsError):
            replace(valid, **delta).validate()
    # The extension does not relax physical demo requirements.
    with pytest.raises(SettingsError, match="explicit devices"):
        replace(valid, hardware_mode="physical").validate()


@pytest.mark.asyncio
async def test_uart_stop_notification_identifies_its_snapshot_command():
    """A delayed UART stop must not cancel the following phrase on the browser."""
    harness, _ = make_harness()
    stand = harness.stand
    notifications = []

    async def capture(payload):
        notifications.append(payload)
        return []

    stand.hub.publish = capture
    try:
        stand.commands.clear()
        stand.adapter.send({"type": "audio_stop", "payload": {"token": 7}})
        stand.adapter.send({"type": "audio_play", "payload": {
            "response_id": "PHRASE_001", "asset_id": "voice/PHRASE_001.wav", "token": 7,
        }})
        await stand._flush_device_commands(timeout=1.0)
        if stand._notification_tasks:
            await asyncio.gather(*tuple(stand._notification_tasks))
        stop = next(item for item in stand.commands if item["type"] == "audio_stop")
        phrase = next(item for item in stand.commands if item["type"] == "audio_play")
        assert phrase["sequence"] > stop["sequence"]
        assert phrase["payload"]["content_kind"] == "phrase"
        notice = next(item for item in notifications if item["type"] == "audio_stop")
        assert notice == {"type": "audio_stop", "generation": stand.playback._generation,
                          "command_sequence": stop["sequence"], "checkpoint_request": None}
    finally:
        await stand.aclose()
