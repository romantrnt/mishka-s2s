"""Adversarial ownership tests using synthetic input and local fake devices."""
import asyncio
import base64
import struct
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import pytest_asyncio

from app.foundation.api import build_application, create_app
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.hardware import decode_frame, encode_frame
from app.foundation.input_safety import InputSafetyDecision
from app.foundation.s2s import SoundDeviceCaptureController, STTResult
from app.foundation.stand import HardwareIntegrationStand


def ev(kind, seq, payload=None, source="phone"):
    return Event(kind, "review", seq, 1.0, source, payload or {}, f"{source}:{seq}", 0.0)


@pytest_asyncio.fixture
async def app():
    value, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    yield value
    await value._cleanup_session_resources("review")


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", ["button", "speech"])
async def test_urgent_admitted_before_classifier_survives_newer_same_source(app, newer):
    entered, release = asyncio.Event(), asyncio.Event()
    class Classifier:
        async def classify(self, text):
            if text == "neutral":
                return InputSafetyDecision("none", "test")
            entered.set()
            await release.wait()
            return InputSafetyDecision("urgent", "test")
    app.safety_classifier = Classifier()
    await app.receive_async(ev("HandsetLifted", 1))
    task = asyncio.create_task(app.receive_async(ev("SpeechReceived", 2), "disclosure"))
    await entered.wait()
    if newer == "button":
        await app.receive_async(ev("ButtonPressed", 3, {"button_id": 5}))
    else:
        await app.receive_async(ev("SpeechReceived", 3), "neutral")
    release.set()
    result = await task
    state = app.canonical_runtime.sessions["review"]
    assert result["reason"] == "urgent_safety"
    assert state.mode == "safety_stop" and state.safety_severity == "urgent"
    assert "incident_id" not in state.snapshot()
    assert not any(effect.get("type") == "adult_handoff_request" for effect in result["outputs"])
    assert state.last_sequence_by_source["phone"] == 3
    assert {"phone:2", "phone:3"} <= state.seen_correlations
    assert (await app.receive_async(ev("SpeechReceived", 2), "disclosure"))["reason"] == "idempotent_duplicate"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["HandsetPlaced", "SessionReset", "HardwareDisconnected"])
async def test_classifier_completion_cannot_cross_session_boundary(app, boundary):
    entered, release = asyncio.Event(), asyncio.Event()
    class Classifier:
        async def classify(self, text):
            entered.set()
            await release.wait()
            return InputSafetyDecision("urgent", "test")
    app.safety_classifier = Classifier()
    await app.receive_async(ev("HandsetLifted", 1))
    task = asyncio.create_task(app.receive_async(ev("SpeechReceived", 2), "old disclosure"))
    await entered.wait()
    await app.receive_async(ev(boundary, 3))
    if boundary == "HandsetPlaced":
        await app.receive_async(ev("HandsetLifted", 4))
    release.set()
    assert (await task)["reason"] == "stale_session_generation"
    state = app.canonical_runtime.sessions.get("review")
    assert state is None or state.safety_severity != "urgent"


@pytest.mark.asyncio
async def test_pending_duplicate_is_rejected_before_second_classifier_and_neutral_is_stale(app):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Classifier:
        async def classify(self, text):
            calls.append(text)
            entered.set()
            await release.wait()
            return InputSafetyDecision("none", "test")
    app.safety_classifier = Classifier()
    await app.receive_async(ev("HandsetLifted", 1))
    task = asyncio.create_task(app.receive_async(ev("SpeechReceived", 2), "neutral"))
    await entered.wait()
    assert (await app.receive_async(ev("SpeechReceived", 2), "duplicate"))["reason"] == "idempotent_duplicate"
    await app.receive_async(ev("ButtonPressed", 3, {"button_id": 5}))
    release.set()
    assert (await task)["reason"] == "stale_session_generation"
    assert calls == ["neutral"]
    assert app.canonical_runtime.sessions["review"].last_sequence_by_source["phone"] == 3


@pytest_asyncio.fixture
async def stand():
    value = HardwareIntegrationStand(settings=Settings(profile="mock"))
    value.active_session_id = "review"
    await value.application.receive_async(ev("HandsetLifted", 1))
    value.application.sessions.get("review").current_node = "bear_dialogue"
    yield value
    await value.aclose()


class CountingPipeline:
    def __init__(self):
        self.received = []
        self.cancelled = []
    async def feed_audio(self, sid, samples, context, **kwargs):
        self.received.append((sid, samples))
        return SimpleNamespace(stt=None, turn=None, pending=None, barge_in=False, ingress_status="collecting")
    async def cancel_session(self, sid):
        self.cancelled.append(sid)


async def replace_session(stand):
    await stand.application.receive_async(ev("HandsetPlaced", 20))
    await stand.application.receive_async(ev("HandsetLifted", 21))
    stand.application.sessions.get("review").current_node = "bear_dialogue"


def pcm_frame(sequence=10):
    return decode_frame(encode_frame("audio_pcm", sequence, {"encoding": "f32le", "channels": 1,
        "sample_rate": 16000, "data_b64": base64.b64encode(struct.pack("<ff", .25, .5)).decode()}))


@pytest.mark.asyncio
async def test_pcm_waiting_for_uart_lock_is_not_reassigned(stand):
    pipeline = CountingPipeline()
    stand.application.s2s_audio = pipeline
    await stand._transport_lock.acquire()
    task = asyncio.create_task(stand.inject_pcm((.25, .5), 16000, "review", flush_silence=False))
    await asyncio.sleep(0)
    epoch = stand.application._session_epochs.get("review", 0)
    replacement = asyncio.create_task(replace_session(stand))
    # Hangup itself publishes through UART. Observe its synchronous fence,
    # then release transport so both real operations can finish.
    while stand.application._session_epochs.get("review", 0) == epoch:
        await asyncio.sleep(0)
    stand._transport_lock.release()
    assert (await task)["reason"] == "stale_session_generation"
    await replacement
    assert pipeline.received == []


@pytest.mark.asyncio
async def test_pcm_waiting_for_ingress_lock_is_not_reassigned(stand):
    pipeline = CountingPipeline()
    stand.application.s2s_audio = pipeline
    lock = stand._audio_ingress_locks.setdefault("review", asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(stand._dispatch_pcm(pcm_frame()))
    await asyncio.sleep(0)
    await replace_session(stand)
    lock.release()
    assert (await task)["reason"] == "stale_session_generation"
    assert pipeline.received == []


@pytest.mark.asyncio
async def test_pcm_waiting_for_resample_is_not_reassigned(stand):
    pipeline = CountingPipeline()
    stand.application.s2s_audio = pipeline
    entered, release = asyncio.Event(), asyncio.Event()
    async def resample(*args):
        entered.set()
        await release.wait()
        return (.25, .5)
    stand._offload_audio = resample
    task = asyncio.create_task(stand.inject_pcm((.25, .5), 48000, "review", flush_silence=False))
    await entered.wait()
    await replace_session(stand)
    release.set()
    assert (await task)["reason"] == "stale_session_generation"
    assert pipeline.received == []


@pytest.mark.asyncio
async def test_capture_callback_retains_owner_before_event_loop_and_copies_buffer(stand):
    pipeline = CountingPipeline()
    stand.application.s2s_audio = pipeline
    stand._headless_audio_queue = asyncio.Queue(64)
    callbacks = []
    class Loop:
        def is_closed(self): return False
        def call_soon_threadsafe(self, callback, *args): callbacks.append((callback, args))
    capture = SoundDeviceCaptureController()
    capture._loop = Loop()
    capture._on_frame = stand._enqueue_headless_audio_frame
    capture._frame_context = stand._headless_frame_owner
    data = np.array([[.25], [.5]], dtype=np.float32)
    capture._callback(data, 2, None, None)
    data[:] = -.9
    assert tuple(callbacks[0][1][0]) == (.25, .5)
    await replace_session(stand)
    callback, args = callbacks.pop()
    callback(*args)
    assert stand._headless_audio_queue.empty()
    assert pipeline.received == []


@pytest.mark.asyncio
async def test_hid_loss_stops_capture_cancels_pipeline_and_fences_pending_frames(stand):
    pipeline = CountingPipeline()
    stand.application.s2s_audio = pipeline
    stopped = []
    stand.audio_input = SimpleNamespace(stop=lambda: stopped.append(True))
    stand._physical_loop = asyncio.get_running_loop()
    stand._hid_generation = 1
    stand._headless_audio_queue = asyncio.Queue(64)
    stand._headless_audio_task = asyncio.create_task(stand._headless_audio_worker())
    owner = stand._headless_frame_owner()
    stand._enqueue_headless_audio_frame((.25, .5), owner)
    stand._on_hid_error({"kind": "device_disconnected"}, 1)
    assert not stand._headless_audio_accepting()
    await asyncio.wrap_future(stand._hid_loss_future)
    assert stopped and "review" in pipeline.cancelled
    assert stand._headless_audio_queue is None and stand._headless_audio_task is None
    assert not owner[1]() and pipeline.received == []
    assert stand.application.canonical_runtime.sessions["review"].mode == "safety_stop"
    stand._capture_hid_fault = None
    stand._hid_generation = 2
    stand._on_hid_error({"kind": "device_disconnected"}, 1)
    assert stand._capture_hid_fault is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, text, expected", [
    ("fixed_story_playing", "пауза", "story_pause_announcement"),
    ("story_paused", "продолжить", "story_resume_announcement"),
    ("fixed_story_playing", "не хочу жить", "safety_stop"),
    ("fixed_story_playing", "случайные слова", "story_speech_analysis")])
async def test_headless_story_pcm_reaches_real_vad_stt_and_canonical_transition(stand, mode, text, expected):
    app = stand.application
    state = app.canonical_runtime.sessions["review"]
    state.mode, state.active_story_id = mode, "fixed_aibolit_v1"
    state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": 700}
    if mode == "story_paused":
        checkpoint = await app.receive_async(ev("StoryPositionUpdated", 2,
            {"story_id": state.active_story_id, "offset_ms": 700}, source="audio_pause"))
        assert checkpoint["reason"] == "story_checkpoint_updated" and state.story_checkpoint_confirmed
    app.sessions.get("review").current_node = state.mode
    transcripts = []
    class STT:
        async def transcribe(self, audio, cancelled):
            transcripts.append(len(audio.samples))
            return STTResult(text, .99, .1)
    app.s2s_audio.recognizer = STT()
    stand._headless_audio_queue = asyncio.Queue(64)
    # Enough speech and silence for the actual configured VAD endpoint.
    for _ in range(10): stand._enqueue_headless_audio_frame((.25,) * 512)
    for _ in range(20): stand._enqueue_headless_audio_frame((0.,) * 512)
    stand._headless_audio_queue.put_nowait(None)
    await stand._headless_audio_worker()
    pending = list(app._audio_completions.get("review", ()))
    if pending: await asyncio.gather(*pending)
    assert transcripts
    assert app.canonical_runtime.sessions["review"].mode == expected
    if text == "пауза":
        assert app.canonical_runtime.sessions["review"].dialogue_memory.story_checkpoint["story_id"] == "fixed_aibolit_v1"
    if text == "продолжить":
        assert app.canonical_runtime.sessions["review"].dialogue_memory.story_checkpoint["offset_ms"] == 700


@pytest.mark.asyncio
async def test_cold_readiness_is_off_loop_single_flight_and_survives_request_cancellation(app):
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    calls = []
    def report():
        calls.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release content verifier"
        return {"ready": True, "voice": {}}
    app.scenarios = SimpleNamespace(report=report)
    api = create_app(app)
    ready = next(route.endpoint for route in api.routes if getattr(route, "path", None) == "/ready")
    first = asyncio.create_task(ready())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not first.done()  # loop is responsive while verification is blocked
        assert not api.state.lifecycle.admits_users()
        second = asyncio.create_task(ready())
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        await asyncio.sleep(0)
        release.set()
        await second
        await ready()
        assert len(calls) == 1 and calls[0] != threading.get_ident()
    finally:
        release.set()
        if not first.done(): await first


@pytest.mark.asyncio
async def test_speech_started_before_lift_cannot_become_new_child_disclosure(app):
    entered, release = asyncio.Event(), asyncio.Event()
    class Classifier:
        async def classify(self, text):
            entered.set()
            await release.wait()
            return InputSafetyDecision("urgent", "test")
    app.safety_classifier = Classifier()
    task = asyncio.create_task(app.receive_async(ev("SpeechReceived", 1), "old input"))
    await entered.wait()
    await app.receive_async(ev("HandsetLifted", 2))
    release.set()
    assert (await task)["reason"] == "stale_session_generation"
    assert app.canonical_runtime.sessions["review"].safety_severity == "none"


def test_memory_probe_does_not_import_torch(monkeypatch):
    import builtins
    import sys
    from app.foundation.observability import Metrics
    monkeypatch.setitem(sys.modules, "torch", None)
    original = builtins.__import__
    def checked(name, *args, **kwargs):
        assert name != "torch", "readiness must not initialize torch"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", checked)
    memory = Metrics().memory_snapshot()
    assert memory["gpu"]["allocated_bytes"] == {"value": None, "reason": "torch_not_loaded"}


@pytest.mark.asyncio
async def test_hid_failure_during_open_cannot_reopen_microphone(stand, monkeypatch):
    from dataclasses import replace
    from app.foundation import stand as stand_module
    capture_starts = []
    class FailingReader:
        def __init__(self, *args): pass
        def open(self): self.on_error({"kind": "device_disconnected"})
        def is_ready(self): return False
        def close(self): pass
    monkeypatch.setattr(stand_module, "KeyboardHIDInputAdapter", FailingReader)
    stand.hardware_mode = "physical"
    stand.settings = replace(stand.settings, hid_device="/dev/input/synthetic", allow_degraded_hardware=True)
    stand.input_adapter = None
    stand._start_headless_audio_capture = lambda: capture_starts.append(True)
    await stand.start_hardware()
    await asyncio.wrap_future(stand._hid_loss_future)
    assert capture_starts == []
    assert stand._capture_hid_fault == stand._hid_generation
    assert stand._startup_state == "degraded"
    assert not stand._headless_audio_accepting()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, delayed", [("pcm", "body"), ("wav", "body"), ("wav", "decoder")])
async def test_upload_ownership_precedes_body_and_wav_decoder(kind, delayed):
    import io
    import wave
    from app.foundation.stand import create_stand_app
    api = create_stand_app(Settings(profile="mock"))
    stand = api.state.stand
    entered, release = asyncio.Event(), asyncio.Event()
    raw = struct.pack("<ff", .25, .5)
    if kind == "wav":
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(struct.pack("<hh", 8192, 16384))
        raw = buffer.getvalue()
    class Request:
        headers = {}
        async def stream(self):
            if delayed == "body":
                entered.set()
                await release.wait()
            yield raw
    original_offload = stand._offload_audio
    async def decode(*args):
        entered.set()
        await release.wait()
        return await original_offload(*args)
    if delayed == "decoder": stand._offload_audio = decode
    endpoint = next(r.endpoint for r in api.routes if getattr(r, "path", None) == f"/stand/audio/{kind}")
    pipeline = CountingPipeline()
    try:
        await stand.application.receive_async(ev("HandsetLifted", 1))
        stand.application.s2s_audio = pipeline
        task = asyncio.create_task(endpoint(Request(), session_id="review"))
        await entered.wait()
        await replace_session(stand)
        release.set()
        assert (await task)["reason"] == "stale_session_generation"
        assert pipeline.received == []
    finally:
        release.set()
        await stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio_pcm", "flush_audio"])
async def test_websocket_stamps_audio_owner_before_detached_task_starts(kind):
    from fastapi import WebSocketDisconnect
    from app.foundation.lifecycle import RuntimeState
    from app.foundation.stand import create_stand_app
    token = "synthetic-ownership-test"
    api = create_stand_app(Settings(profile="mock", stand_access_token=token, mutation_auth_policy="token_required"))
    stand = api.state.stand
    # Synthetic transport admission only; no physical readiness is asserted.
    api.state.lifecycle._state = RuntimeState.READY
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pipeline = CountingPipeline()
    original = stand.inject_pcm
    async def delayed_audio(*args, **kwargs):
        entered.set()
        await release.wait()
        try:
            return await original(*args, **kwargs)
        finally:
            completed.set()
    stand.inject_pcm = delayed_audio
    class Socket:
        headers = {"x-stand-token": token}
        cookies = {}
        sent = False
        async def accept(self): pass
        async def send_json(self, value):
            assert value.get("type") != "error", value
        async def receive_json(self):
            if not self.sent:
                self.sent = True
                return {"type": kind, "session_id": "review", "sample_rate": 16000,
                        "data_b64": base64.b64encode(struct.pack("<ff", .25, .5)).decode()}
            await entered.wait()
            await replace_session(stand)
            release.set()
            await completed.wait()
            raise WebSocketDisconnect()
    endpoint = next(r.endpoint for r in api.routes if getattr(r, "path", None) == "/stand/ws")
    try:
        await stand.application.receive_async(ev("HandsetLifted", 1))
        stand.application.s2s_audio = pipeline
        await asyncio.wait_for(endpoint(Socket()), 10)
        assert pipeline.received == []
    finally:
        release.set()
        await stand.aclose()
