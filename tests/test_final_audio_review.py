"""Fresh regressions for audio captured outside an active handset session."""
import asyncio
import struct
from types import SimpleNamespace

import pytest

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.stand import HardwareIntegrationStand, create_stand_app


def event(kind, seq):
    return Event(kind, "review", seq, 1.0, "review", {}, f"review:{seq}")


class Pipeline:
    def __init__(self):
        self.received = []
    async def feed_audio(self, sid, samples, context, **kwargs):
        self.received.append((sid, samples))
        return SimpleNamespace(stt=None, turn=None, pending=None, barge_in=False, ingress_status="collecting")
    async def cancel_session(self, sid):
        pass


@pytest.mark.asyncio
async def test_upload_started_while_handset_down_cannot_enter_next_lift():
    api = create_stand_app(Settings(profile="mock"))
    stand = api.state.stand
    entered, release = asyncio.Event(), asyncio.Event()
    class Request:
        headers = {}
        async def stream(self):
            entered.set()
            await release.wait()
            yield struct.pack("<ff", .25, .5)
    endpoint = next(r.endpoint for r in api.routes if getattr(r, "path", None) == "/stand/audio/pcm")
    pipeline = Pipeline()
    try:
        # A rejected input can establish a down-handset session record.
        await stand.application.receive_async(event("SpeechReceived", 1), "hello")
        assert stand.application.sessions.get("review").handset_up is False
        stand.application.s2s_audio = pipeline
        task = asyncio.create_task(endpoint(Request(), session_id="review", sample_rate=16000))
        await entered.wait()
        await stand.application.receive_async(event("HandsetLifted", 2))
        release.set()
        assert (await task)["reason"] == "stale_session_generation"
        assert pipeline.received == []
    finally:
        release.set()
        await stand.aclose()


@pytest.mark.asyncio
async def test_capture_owner_taken_while_down_never_becomes_valid_on_lift():
    stand = HardwareIntegrationStand(settings=Settings(profile="mock"))
    try:
        stand.active_session_id = "review"
        await stand.application.receive_async(event("SpeechReceived", 1), "hello")
        sid, guard = stand._headless_frame_owner()
        assert sid == "review" and not guard()
        await stand.application.receive_async(event("HandsetLifted", 2))
        assert not guard()
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_direct_audio_waiting_for_expiry_cannot_cross_first_lift():
    stand = HardwareIntegrationStand(settings=Settings(profile="mock"))
    app = stand.application
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.expire_identity_leases
    async def delayed_expiry():
        entered.set()
        await release.wait()
        return await original()
    try:
        await app.receive_async(event("SpeechReceived", 1), "hello")
        app.s2s_audio = pipeline = Pipeline()
        app.expire_identity_leases = delayed_expiry
        task = asyncio.create_task(app.receive_audio_async("review", (.25, .5), 3))
        await entered.wait()
        app.expire_identity_leases = original
        await app.receive_async(event("HandsetLifted", 2))
        release.set()
        assert (await task)["reason"] == "stale_session_generation"
        assert pipeline.received == []
    finally:
        release.set()
        await stand.aclose()
