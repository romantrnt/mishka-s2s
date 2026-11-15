"""Local safety effects and real application cleanup, with synthetic device I/O."""
import asyncio

import pytest
import pytest_asyncio

from app.foundation.api import build_application
from app.foundation.canonical_services import LED_PRESETS
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import AudioIngressPipeline, SoundDevicePlaybackController


@pytest_asyncio.fixture
async def harness():
    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    sid, sequence = "local-safety", 0
    played, frames = [], []
    original_play, original_frame = app.audio.play_owned, app.led.set_elements
    def play(handle, asset, **kwargs):
        played.append(handle.response_id)
        return original_play(handle, asset, **kwargs)
    def frame(commands):
        commands = tuple(commands)
        frames.append({item.element_id: item.state for item in commands})
        return original_frame(commands)
    app.audio.play_owned, app.led.set_elements = play, frame
    async def send(kind, payload=None, text=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind, sid, sequence, 1., "local-test",
            payload or {}, f"local:{sequence}"), text)
    await send("HandsetLifted")
    played.clear()
    try:
        yield app, sid, send, played, frames
    finally:
        await app.shutdown_identity()
        await app._cleanup_session_resources(sid)


def help_payload(severity="concern"):
    return {"intent": "help_request", "safety_severity": severity}


def block_cleanup(app):
    entered, release, cancelled, cleaned = (asyncio.Event() for _ in range(4))
    original = app.bounded_s2s.cancel_session
    async def cancel(sid):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        await original(sid)
        cleaned.set()
    app.bounded_s2s.cancel_session = cancel
    return entered, release, cancelled, cleaned


@pytest.mark.asyncio
async def test_commits_stop_capture_and_safe_frame_before_cleanup_wait(harness):
    app, sid, send, played, frames = harness
    pipeline = AudioIngressPipeline(app.s2s_audio.vad, app.s2s_audio.config)
    app.s2s_audio.ingress[sid] = pipeline
    entered, release, _, _ = block_cleanup(app)
    request = asyncio.create_task(send("VoiceIntentReady", help_payload()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert app.canonical_runtime.sessions[sid].mode == "safety_stop"
        assert sid not in app.s2s_audio.ingress
        assert app.s2s_audio.generations[sid] > 0
        assert all(frames[-1][element] == value for element, value in LED_PRESETS["SAFE_STOP"])
        assert played == []
        release.set()
        result = await asyncio.wait_for(request, 1)
        assert result["reason"] == "help_requested"
        assert played == ["PHRASE_091"]
        assert not hasattr(app, "adult_handoff")
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("drain", [False, True])
async def test_waits_actual_cancellation_resistant_stop_before_guidance(harness, drain):
    app, sid, send, played, _ = harness
    app.audio_cleanup_timeout_seconds = .04
    entered, release, cancelled, cleaned = block_cleanup(app)
    stop_saw_guidance = []
    original_stop = app.bounded_s2s.playback.stop
    async def stop():
        stop_saw_guidance.extend(played)
        await original_stop()
    app.bounded_s2s.playback.stop = stop
    request = asyncio.create_task(send("VoiceIntentReady", help_payload("urgent")))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert played == []
        if drain:
            release.set()
        result = await asyncio.wait_for(request, 1)
        assert app.canonical_runtime.sessions[sid].mode == "safety_stop"
        assert played == (["PHRASE_091"] if drain else [])
        if not drain:
            assert any(item.get("resolution") == "local_safety_audio_cleanup_pending" for item in result["outputs"])
            state = app.canonical_runtime.sessions[sid]
            assert state.audio_status == "failed" and state.active_audio_id is None
            assert state.playback_lease is None
            assert state.audio_failure_code == "local_safety_audio_cleanup_pending"
            assert sid not in app._active_audio
        release.set()
        await asyncio.wait_for(cleaned.wait(), 1)
        await asyncio.sleep(.01)
        assert stop_saw_guidance == [], "no old stop may run after local guidance starts"
    finally:
        release.set()
        app.bounded_s2s.playback.stop = original_stop
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["hangup", "reset", "cancel_ingress"])
async def test_cleanup_cannot_emit_late_guidance_after_boundary(harness, boundary):
    app, sid, send, played, _ = harness
    app.audio_cleanup_timeout_seconds = .02
    entered, release, _, cleaned = block_cleanup(app)
    request = asyncio.create_task(send("VoiceIntentReady", help_payload("urgent")))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if boundary == "hangup":
            result = await send("HandsetPlaced")
            assert result["canonical_state"]["mode"] == "idle"
        elif boundary == "reset":
            result = await app.reset_session(sid)
            assert result["reset"] is True
            assert sid not in app.canonical_runtime.sessions
        else:
            request.cancel()
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.wait_for(cleaned.wait(), 1)
        await asyncio.sleep(.01)
        assert played == []
        if boundary == "cancel_ingress":
            state = app.canonical_runtime.sessions[sid]
            assert state.audio_status == "cancelled" and state.active_audio_id is None
            assert state.playback_lease is None
            assert state.audio_failure_code == "local_safety_guidance_cancelled"
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_button_speech_spam_and_severity_upgrade_keep_initial_voice_owner(harness):
    app, sid, send, played, _ = harness
    entered, release, _, _ = block_cleanup(app)
    request = asyncio.create_task(send("VoiceIntentReady", help_payload()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        revision = app._session_revisions[sid]
        for _ in range(5):
            for kind, payload, text in [
                ("ButtonPressed", {"button_id": 1}, None),
                ("ButtonReleased", {"button_id": 1}, None),
                ("SpeechReceived", {}, "продолжи сказку"),
            ]:
                result = await send(kind, payload, text)
                assert result["outputs"] == []
        upgraded = await send("VoiceIntentReady", help_payload("urgent"))
        assert upgraded["canonical_state"]["safety_severity"] == "urgent"
        assert upgraded["outputs"] == []
        assert app._session_revisions[sid] == revision
        release.set()
        await asyncio.wait_for(request, 1)
        assert played == ["PHRASE_091"]
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["audio", "capture", "lighting"])
async def test_sync_cleanup_fault_does_not_skip_other_local_resources(harness, resource):
    app, sid, send, played, frames = harness
    capture_calls = []
    original_capture = app.s2s_audio.close_capture
    def capture(session_id):
        capture_calls.append(session_id)
        original_capture(session_id)
        if resource == "capture":
            raise OSError("synthetic capture fault")
    app.s2s_audio.close_capture = capture
    original_audio = app.audio.cancel
    original_frame = app.led.set_elements
    if resource == "audio":
        app.audio.cancel = lambda *_: (_ for _ in ()).throw(OSError("synthetic audio fault"))
    if resource == "lighting":
        app.led.set_elements = lambda *_: (_ for _ in ()).throw(OSError("synthetic lighting fault"))
    try:
        result = await send("VoiceIntentReady", help_payload())
        assert app.canonical_runtime.sessions[sid].mode == "safety_stop"
        assert capture_calls
        if resource != "lighting":
            assert any(all(frame[element] == value for element, value in LED_PRESETS["SAFE_STOP"])
                       for frame in frames)
        assert played == []
        assert any(item.get("resource") == resource and item.get("status") == "failed"
                   for item in result["outputs"])
    finally:
        app.audio.cancel, app.led.set_elements = original_audio, original_frame
        app.s2s_audio.close_capture = original_capture


@pytest.mark.asyncio
async def test_concern_uses_support_recording_without_entering_local_stop(harness):
    app, sid, send, played, _ = harness
    result = await send("SpeechReceived", text="мне страшно")
    assert result["canonical_state"]["mode"] == "support_intake"
    assert played == ["PHRASE_071"]
    assert not any(item.get("type") in {"adult_handoff_offer", "adult_handoff_request"} for item in result["outputs"])


@pytest.mark.asyncio
async def test_cancelled_ingress_waiter_does_not_cancel_shared_reset_barrier(harness):
    app, sid, send, played, _ = harness
    entered, release, _, _ = block_cleanup(app)
    reset = asyncio.create_task(app.reset_session(sid))
    follower = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        barrier = app._session_reset_waiters[sid][1]
        follower = asyncio.create_task(send("HandsetLifted"))
        await asyncio.sleep(0)
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)
        assert not barrier.cancelled()
        assert app.last_canonical_state["mode"] == "idle"
        release.set()
        assert (await asyncio.wait_for(reset, 1))["reset"] is True
        assert sid not in app._session_reset_waiters
        assert "PHRASE_091" not in played
    finally:
        release.set()
        await asyncio.gather(reset, *([follower] if follower else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_owner_without_cleanup_port_suppresses_local_guidance(harness):
    app, sid, send, played, _ = harness
    original = app.bounded_s2s.cancel_session
    app.bounded_s2s.cancel_session = None
    try:
        result = await send("VoiceIntentReady", help_payload())
        assert app.canonical_runtime.sessions[sid].mode == "safety_stop"
        assert played == []
        assert any(item.get("resolution") == "local_safety_audio_cleanup_failed" for item in result["outputs"])
    finally:
        app.bounded_s2s.cancel_session = original


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["conversation", "recognition"])
@pytest.mark.parametrize("replacement", ["queue", "recorded_welcome"])
async def test_late_old_cleanup_does_not_stop_new_physical_session(harness, stage, replacement):
    app, sid, send, _, _ = harness
    app.audio_cleanup_timeout_seconds = .005
    release = asyncio.Event()
    async def provider():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
    old = asyncio.create_task(provider())
    engine = app.bounded_s2s
    registry = engine.active_tasks if stage == "conversation" else app.s2s_audio.pending_tasks
    registry[sid] = old
    aborted = []
    class Queue:
        async def abort(self, *_args, **_kwargs):
            aborted.append("new queue")
        def resource_snapshot(self):
            return {}
    try:
        await send("VoiceIntentReady", help_payload("urgent"))
        await send("HandsetPlaced")
        await send("HandsetLifted")
        assert app.canonical_runtime.sessions[sid].mode == "voice_welcome"
        new_lease = dict(app._active_audio[sid])
        if replacement == "queue":
            engine.active_queues[sid] = Queue()
        engine.playback.played.append("NEW_SESSION_AUDIO")
        release.set()
        await asyncio.wait_for(old, 1)
        await asyncio.sleep(.02)
        assert aborted == [], "old cancellation cannot abort a newly registered queue"
        assert engine.playback.played == ["NEW_SESSION_AUDIO"], "old stop cannot silence new output"
        assert app._active_audio[sid] == new_lease
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)
        if registry.get(sid) is old:
            registry.pop(sid, None)


@pytest.mark.asyncio
async def test_native_stop_does_not_close_stream_installed_during_old_io():
    playback = SoundDevicePlaybackController()
    class Stream:
        def __init__(self):
            self.closed = False
        def abort(self):
            self.closed = True
        def close(self):
            self.closed = True
    old, fresh, release = Stream(), Stream(), asyncio.Event()
    playback._stream = old
    playback._io_future = asyncio.create_task(release.wait())
    stop = asyncio.create_task(playback.stop())
    try:
        await asyncio.sleep(0)
        playback._stream = fresh
        release.set()
        await asyncio.wait_for(stop, 1)
        assert playback._stream is fresh and not fresh.closed
    finally:
        release.set()
        await asyncio.gather(stop, return_exceptions=True)
        await playback._close_stream(fresh)


@pytest.mark.asyncio
async def test_late_cleanup_cannot_stop_concern_recording_in_same_revision(harness):
    app, sid, send, played, _ = harness
    app.audio_cleanup_timeout_seconds = .005
    release = asyncio.Event()
    async def provider():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
    old = asyncio.create_task(provider())
    engine = app.bounded_s2s
    engine.active_tasks[sid] = old
    try:
        result = await send("SpeechReceived", text="мне страшно")
        assert result["reason"] == "concern_support" and played == ["PHRASE_071"]
        new_lease = dict(app._active_audio[sid])
        engine.playback.played.append("CONCERN_071")
        release.set()
        await asyncio.wait_for(old, 1)
        await asyncio.sleep(.02)
        assert engine.playback.played == ["CONCERN_071"]
        assert app._active_audio[sid] == new_lease
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)
        if engine.active_tasks.get(sid) is old:
            engine.active_tasks.pop(sid, None)
