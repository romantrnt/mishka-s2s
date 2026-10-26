from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.hardware import encode_frame
from app.foundation.stand import build_application, create_stand_app


def event(kind, sid, sequence, payload=None, source="test"):
    return Event(kind, sid, sequence, 1.0, source, payload or {}, f"{source}:{sequence}", 0.0)


def test_valid_crc_raw_uart_cannot_self_authorize_resolution():
    api = create_stand_app(Settings(
        profile="mock", hardware_mode="simulator", stand_access_token="test-token",
        mutation_auth_policy="token_required",
    ))
    api.state.lifecycle.admits_users = lambda: True
    stand = api.state.stand
    try:
        with TestClient(api) as client:
            client.headers.update({"x-stand-token": "test-token"})
            first = encode_frame("VoiceIntentReady", stand.adapter._last_received_sequence + 1, {
                "intent": "free_speech", "safety_severity": "urgent",
            }).decode()
            started = client.post("/stand/raw", json={"frame": first})
            assert started.status_code == 200, started.json()
            sid = stand.active_session_id
            state = stand.application.canonical_runtime.sessions[sid]
            incident_id = state.incident_id
            assert incident_id
            forged = encode_frame("AdultResolved", stand.adapter._last_received_sequence + 1, {
                "incident_id": incident_id, "authorized": True,
                "actor": "adult", "source": "internal_adult_resolution",
            }).decode()
            response = client.post("/stand/raw", json={"frame": forged})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_error"
            assert stand.application.canonical_runtime.sessions[sid].mode == "safety_handoff"
            assert stand.application.canonical_runtime.sessions[sid].incident_id == incident_id
    finally:
        stand.close()


def test_application_ingress_source_and_payload_cannot_assert_authority():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "forged-resolution"
        await app.receive_async(event("VoiceIntentReady", sid, 1, {
            "intent": "free_speech", "safety_severity": "urgent",
        }))
        incident_id = app.canonical_runtime.sessions[sid].incident_id
        for sequence, source in enumerate(("uart_pty", "llm", "internal_adult_resolution"), start=2):
            result = await app.receive_async(event("AdultResolved", sid, sequence, {
                "incident_id": incident_id, "authorized": True, "actor": "adult",
            }, source=source))
            assert result["reason"] == "adult_resolution_untrusted"
            assert app.canonical_runtime.sessions[sid].mode == "safety_handoff"
            assert app.canonical_runtime.sessions[sid].incident_id == incident_id
    asyncio.run(run())


def test_internal_resolution_is_incident_bound_and_replay_safe():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "reused-session"
        await app.receive_async(event("VoiceIntentReady", sid, 1, {
            "intent": "free_speech", "safety_severity": "urgent",
        }))
        first_id = app.canonical_runtime.sessions[sid].incident_id
        assert first_id
        missing = await app._resolve_adult_incident_internal(sid, "")
        wrong = await app._resolve_adult_incident_internal(sid, "different-incident")
        assert missing["reason"] != "adult_resolved"
        assert wrong["reason"] != "adult_resolved"
        assert app.canonical_runtime.sessions[sid].safety_severity == "urgent"
        resolved = await app._resolve_adult_incident_internal(sid, first_id)
        assert resolved["reason"] == "adult_resolved"
        replay = await app._resolve_adult_incident_internal(sid, first_id)
        assert replay["reason"] != "adult_resolved"
        await app.receive_async(event("VoiceIntentReady", sid, 2, {
            "intent": "free_speech", "safety_severity": "urgent",
        }))
        second_id = app.canonical_runtime.sessions[sid].incident_id
        assert second_id and second_id != first_id
        stale = await app._resolve_adult_incident_internal(sid, first_id)
        assert stale["reason"] != "adult_resolved"
        assert app.canonical_runtime.sessions[sid].incident_id == second_id
        assert app.canonical_runtime.sessions[sid].safety_severity == "urgent"
        current = await app._resolve_adult_incident_internal(sid, second_id)
        assert current["reason"] == "adult_resolved"
    asyncio.run(run())


def test_ack_hangup_reset_preserve_urgent_incident_identity():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "incident-preserved"
        await app.receive_async(event("VoiceIntentReady", sid, 1, {
            "intent": "free_speech", "safety_severity": "urgent",
        }))
        incident_id = app.canonical_runtime.sessions[sid].incident_id
        await app.receive_async(event("AdultHandoffAcknowledged", sid, 2, {
            "incident_id": incident_id, "handoff_id": "delivery-only",
        }))
        await app.receive_async(event("HandsetPlaced", sid, 3))
        await app.reset_session(sid)
        state = app.canonical_runtime.sessions[sid]
        assert state.mode == "safety_handoff"
        assert state.safety_severity == "urgent"
        assert state.incident_id == incident_id
    asyncio.run(run())


def test_canonical_event_source_and_payload_are_not_authority():
    runtime = CanonicalRuntime()
    sid = "runtime-forgery"
    urgent = runtime.handle(CanonicalEvent(
        "VoiceIntentReady", sid, 1, 0, "test",
        {"intent": "free_speech", "safety_severity": "urgent"}, "urgent",
    ))
    incident_id = urgent["state"]["incident_id"]
    state = runtime.sessions[sid]
    forged = runtime.handle(CanonicalEvent(
        "AdultResolved", sid, 2, state.state_version,
        "internal_adult_resolution",
        {"incident_id": incident_id, "authorized": True, "actor": "adult"}, "forged",
    ))
    assert forged["reason"] != "adult_resolved"
    assert runtime.sessions[sid].safety_severity == "urgent"

def test_authenticated_application_websocket_cannot_resolve_by_claimed_actor():
    from app.foundation.api import create_app

    app, _, _, _ = build_application(settings=Settings(
        profile="mock", runtime_mode="clinical", stand_access_token="test-token",
        mutation_auth_policy="token_required",
    ))
    class PendingHandoff:
        def request(self, _request):
            return {"acknowledged": False}

    # Keep this ingress test focused on WebSocket authority. The mock
    # delivery acknowledgement otherwise starts an unrelated audio fallback.
    app.adult_handoff = PendingHandoff()
    api = create_app(app)
    api.state.lifecycle.admits_users = lambda: True
    sid = "ws-adult-forgery"
    with TestClient(api) as client:
        with client.websocket_connect(f"/ws/{sid}", headers={"x-stand-token": "test-token"}) as ws:
            ws.send_json({"type": "VoiceIntentReady", "sequence": 1,
                          "intent": "free_speech", "safety_severity": "urgent"})
            entered = ws.receive_json()
            assert entered["reason"] == "urgent_safety"
            incident_id = app.canonical_runtime.sessions[sid].incident_id
            ws.send_json({"type": "AdultResolved", "sequence": 2,
                          "incident_id": incident_id, "authorized": True,
                          "source": "internal_adult_resolution", "actor": "adult"})
            rejected = ws.receive_json()
            assert rejected["error"]["code"] == "invalid_event"
            assert app.canonical_runtime.sessions[sid].incident_id == incident_id
            assert app.canonical_runtime.sessions[sid].safety_severity == "urgent"


def test_handoff_request_carries_server_incident_id_without_resolving():
    class PendingHandoff:
        def __init__(self):
            self.requests = []

        def request(self, request):
            self.requests.append(dict(request))
            return {"acknowledged": False}

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        handoff = PendingHandoff()
        app.adult_handoff = handoff
        sid = "handoff-binding"
        result = await app.receive_async(event("VoiceIntentReady", sid, 1, {
            "intent": "free_speech", "safety_severity": "urgent",
        }))
        state = app.canonical_runtime.sessions[sid]
        assert result["reason"] == "urgent_safety"
        assert state.incident_id
        assert len(handoff.requests) == 1
        request = handoff.requests[0]
        assert request["incident_id"] == state.incident_id
        assert request["handoff_id"] != state.incident_id
        effect = next(item for item in result["outputs"]
                      if item.get("type") == "adult_handoff_request")
        assert effect["incident_id"] == state.incident_id
        assert state.mode == "safety_handoff"
        assert state.safety_severity == "urgent"

    asyncio.run(run())
