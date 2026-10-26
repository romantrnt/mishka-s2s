from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lifecycle import RuntimeState
from app.foundation.stand import build_application, create_stand_app


def _event(event_type, session_id, sequence, source, payload=None):
    return Event(event_type, session_id, sequence, 1.0, source, payload or {}, event_type, 0.0)


def test_fake_pending_playback_has_bounded_canonical_timeout():
    async def run():
        app, _, _, _ = build_application(
            settings=Settings(profile="mock", runtime_mode="exhibition", playback_timeout_s=0.02)
        )
        app.monotonic_now = lambda: 0.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        sid = "timeout-closeout"
        await app.receive_async(_event("HandsetLifted", sid, 1, "probe"))
        app.monotonic_now = lambda: 5.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        await app.flush_welcome_timers()
        assert sid in app._active_audio
        assert app.audio.commands, "fake playback adapter did not accept start"
        await asyncio.sleep(0.08)
        assert app.last_canonical_state["audio_status"] == "failed"
        assert app.last_canonical_state["audio_failure_code"] == "audio_playback_timeout"
        assert sid not in app._active_audio
        # Failed playback now owns an actual stop, so the fake adapter clears
        # its pending command as the physical adapter clears its output.
        assert not app.audio.commands
        assert len([item for item in app.audio_runtime_diagnostics if item["kind"] == "audio_playback_timeout"]) == 1
        state_version = app.last_canonical_state["state_version"]
        await asyncio.sleep(0.05)
        assert app.last_canonical_state["state_version"] == state_version

    asyncio.run(run())


def test_playback_lease_requires_token_lease_and_generation():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        app.monotonic_now = lambda: 0.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        sid = "identity-closeout"
        await app.receive_async(_event("HandsetLifted", sid, 1, "probe"))
        app.monotonic_now = lambda: 5.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        await app.flush_welcome_timers()
        lease = dict(app._active_audio[sid])
        stale = await app.receive_async(_event(
            "AudioCompleted", sid, 1, "browser",
            {"response_id": "PHRASE_001", "token": "wrong",
             "playback_lease": lease["playback_lease"], "generation": lease["generation"]},
        ))
        assert stale["reason"] == "stale_exhibition_audio_completion"
        assert app.canonical_runtime.sessions[sid].mode == "voice_welcome"
        missing_identity = await app.receive_async(_event(
            "AudioFailed", sid, 2, "browser",
            {"response_id": "PHRASE_001", "token": lease["token"]},
        ))
        assert missing_identity["reason"] == "stale_exhibition_audio_completion"
        valid = await app.receive_async(_event(
            "AudioCompleted", sid, 3, "browser",
            {"response_id": "PHRASE_001", "token": lease["token"],
             "playback_lease": lease["playback_lease"], "generation": lease["generation"]},
        ))
        assert valid["reason"] == "exhibition_introduction_started"

    asyncio.run(run())


def test_operator_auth_rejects_audio_mutations_before_route_validation():
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator", stand_access_token="operator-secret", mutation_auth_policy="token_required"))
    api.state.lifecycle = None
    try:
        with TestClient(api) as client:
            payload = {"session_id": "auth-closeout", "response_id": "PHRASE_001", "token": 0}
            assert client.post("/stand/audio/completed", json=payload).status_code == 401
            assert client.post("/stand/audio/failed", json=payload, headers={"x-stand-token": "wrong"}).status_code == 401
            assert client.post("/stand/audio/completed", json=payload, headers={"x-stand-token": "operator-secret"}).status_code == 422
    finally:
        api.state.stand.close()
