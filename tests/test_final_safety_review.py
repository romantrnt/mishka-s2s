"""Final safety regressions. Synthetic adapters, no clinical transport."""
import asyncio

import pytest

from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event


def event(kind, seq, payload=None):
    return Event(kind, "final-review", seq, 1.0, "phone", payload or {}, str(seq), 0.0)


@pytest.mark.parametrize("kind,payload,mode", [
    ("VoiceIntentReady", {"intent": "free_speech", "safety_severity": "urgent"}, "fixed_story_playing"),
    ("VoiceIntentReady", {"intent": "request_adult", "safety_severity": "concern"}, "fixed_story_playing"),
    ("ButtonPressed", {"button_id": 7}, "wellbeing_check"),
])
def test_request_adult_revokes_current_audio_before_delivery(kind, payload, mode):
    runtime = CanonicalRuntime()
    state = CanonicalState("final-review", mode=mode, handset="up",
                           active_audio_id="STORY_001", playback_lease="old-story", audio_status="playing")
    runtime.sessions[state.session_id] = state
    result = runtime.handle(CanonicalEvent(kind, state.session_id, 1, 0, "phone", payload))
    types = [effect["type"] for effect in result["effects"]]
    assert types.index("audio_stop") < types.index("adult_handoff_request")
    assert types.index("lighting_preset") < types.index("adult_handoff_request")
    assert state.active_audio_id is None
    assert state.playback_lease is None


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["ButtonPressed", "HandsetPlaced"])
async def test_urgent_dispatch_survives_interruption_during_audio_cleanup(interrupt):
    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []

    class Delivery:
        async def request(self, payload):
            requests.append(payload)
            delivered.set()
            return {"acknowledged": False}

    original = app._cancel_audio_owner
    blocked_once = False

    async def blocked(owner, sid, **kwargs):
        nonlocal blocked_once
        if kwargs.get("operation") == "audio_stop" and not blocked_once:
            blocked_once = True
            entered.set()
            await release.wait()
            return {}
        return await original(owner, sid, **kwargs)

    try:
        await app.receive_async(event("HandsetLifted", 1))
        app.adult_handoff = Delivery()
        app._cancel_audio_owner = blocked
        task = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "не хочу жить"))
        await asyncio.wait_for(entered.wait(), 5)
        await app.receive_async(event(interrupt, 3, {"button_id": 1} if interrupt == "ButtonPressed" else {}))
        release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0)
        assert len(requests) == 1
        assert delivered.is_set()
        assert app.canonical_runtime.sessions["final-review"].incident_id == requests[0]["incident_id"]
    finally:
        release.set()
        app._cancel_audio_owner = original
        await app._cleanup_session_resources("final-review")


@pytest.mark.asyncio
async def test_pending_concern_stops_audio_and_late_ack_cannot_reach_next_child():
    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    entered, release = asyncio.Event(), asyncio.Event()
    requests, cancelled, played = [], [], []

    class Delivery:
        async def request(self, payload):
            requests.append(payload)
            entered.set()
            await release.wait()
            return {"acknowledged": True}

    original_cancel = app.audio.cancel
    original_play = app.audio.play_owned
    app.audio.cancel = lambda token: (cancelled.append(token), original_cancel(token))[-1]
    app.audio.play_owned = lambda handle, asset, **kw: (played.append(handle.response_id), original_play(handle, asset, **kw))[-1]
    task = None
    try:
        await app.receive_async(event("HandsetLifted", 1))
        cancelled.clear()
        app.adult_handoff = Delivery()
        task = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "мне страшно"))
        await asyncio.wait_for(entered.wait(), 5)
        assert cancelled, "pending transport must not leave old playback running"
        await app.receive_async(event("HandsetPlaced", 3))
        await app.receive_async(event("HandsetLifted", 4))
        played.clear()
        release.set()
        await asyncio.wait_for(task, 5)
        assert "PHRASE_089" not in played
        assert len(requests) == 1
        assert app.canonical_runtime.sessions["final-review"].incident_id is None
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await app._cleanup_session_resources("final-review")


@pytest.mark.asyncio
async def test_incident_transport_is_owned_and_drained_on_application_shutdown():
    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Delivery:
        async def request(self, payload):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    task = None
    try:
        await app.receive_async(event("HandsetLifted", 1))
        app.adult_handoff = Delivery()
        task = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "не хочу жить"))
        await asyncio.wait_for(entered.wait(), 5)
        assert app._handoff_delivery_tasks
        # Losing the initiating request is not equivalent to incident resolution.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not cancelled.is_set()
        shutdown = await app.shutdown_identity()
        assert cancelled.is_set()
        assert shutdown["remaining"] == 0
        assert not app._handoff_delivery_tasks
    finally:
        await app.shutdown_identity()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await app._cleanup_session_resources("final-review")
