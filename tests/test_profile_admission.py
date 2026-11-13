from __future__ import annotations

import asyncio
import pytest

from app.foundation.api import build_application, create_app
from app.foundation.config import Settings, SettingsError
from app.foundation.domain import Event
from app.foundation.lifecycle import RuntimeState
from app.foundation.repositories import InMemorySessionRepository
from app.foundation.stand import HardwareIntegrationStand
from test_profile_software_chain import PublicPhoneClientDouble, attach_client


def test_json_profile_uses_current_exhibition_session_policy():
    with pytest.raises(SettingsError, match="exhibition session policy"):
        Settings(profile="mock", runtime_mode="clinical", identity_mode="max_json").validate()


def test_json_client_unavailable_does_not_block_guest_admission():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition",
            hardware_mode="fake", identity_mode="max_json"), background_scheduler=False)
        assert isinstance(app.sessions, InMemorySessionRepository)
        client, boundary = attach_client(app, session_id="guest")
        def unavailable_poll():
            raise RuntimeError("client unavailable")
        client.poll = unavailable_poll
        assert (await boundary.step())["reason"] == "client_unavailable"
        async def ready():
            return {"ready": True}
        app.runtime_manager.readiness = ready
        api = create_app(application=app)
        api.state.lifecycle._state = RuntimeState.READY
        endpoint = next(route.endpoint for route in api.routes if route.path == "/ready")
        status = await endpoint()
        assert status["guest_ready"] and status["ready"]
        assert not status["identity_ready"] and not status["reader_ready"] and not status["production_ready"]
        result = await app.receive_async(Event("HandsetLifted", "guest", 1, 1.0, "test", {}, "guest-lift", 0.0))
        assert result["reason"] == "exhibition_handset_lifted"
        assert result["canonical_state"]["profile_status"] == "anonymous"
        await app._cleanup_session_resources("guest")
        await app.shutdown_identity()
    asyncio.run(run())


def test_panel_roundtrip_preserves_audio_handle_but_not_private_fields():
    stand = HardwareIntegrationStand(settings=Settings(profile="mock", hardware_mode="simulator"), background_scheduler=False)
    try:
        stand.commands.append({"type": "audio_play", "sequence": 5, "payload": {
            "response_id": "PHRASE_001", "session_id": "stand", "playback_id": "play-1",
            "playback_lease": "lease-1", "generation": 2, "token": 3,
            "offset_ms": 420, "duration_ms": 5000, "tag_id": "PRIVATE_SYNTHETIC_UID",
            "display_name": "PRIVATE_SYNTHETIC_NAME"}})
        payload = stand.panel_snapshot()["commands"][-1]["payload"]
        assert {key: payload[key] for key in ("playback_id", "playback_lease", "generation", "token", "offset_ms", "duration_ms")} == {
            "playback_id": "play-1", "playback_lease": "lease-1", "generation": 2,
            "token": 3, "offset_ms": 420, "duration_ms": 5000}
        assert "tag_id" not in payload and "display_name" not in payload
    finally:
        stand.close()


def test_asgi_transport_keeps_auth_and_boot_admission_before_handset():
    import httpx
    from app.foundation.stand import create_stand_app

    async def run():
        api = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
            runtime_mode="exhibition", identity_mode="max_json",
            stand_access_token="synthetic-operator-token"), phone_client=PublicPhoneClientDouble())
        stand = api.state.stand
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://synthetic.invalid") as client:
                denied = await client.post("/stand/handset", json={"state": "up"})
                assert denied.status_code == 401
                unavailable = await client.post("/stand/handset", json={"state": "up"},
                    headers={"x-stand-token": "synthetic-operator-token"})
                assert unavailable.status_code == 503
                assert unavailable.json()["error"]["code"] == "runtime_unavailable"
            assert not stand.application.canonical_runtime.sessions
            assert set(stand.lighting_output.snapshot().values()) == {0}
        finally:
            stand.close()
    asyncio.run(run())
