"""Physical turn-taking regressions; speaker I/O is deliberately mocked."""
import asyncio
import wave
from dataclasses import replace
from functools import wraps
from types import SimpleNamespace

import pytest

from app.foundation.config import Settings, SettingsError
from app.foundation.domain import Event, PlaybackHandle
from app.foundation.s2s import AudioChunk, AudioIngressPipeline, AudioSynthesisQueue, PCMPlaybackResult, SoundDevicePlaybackController, STTResult
from app.foundation.stand import HardwareIntegrationStand, HEADLESS_OUTPUT_TAIL_S, _HeadlessTurnTakingPlaybackController


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


async def active_stand(monkeypatch, *, enabled=True):
    stand = HardwareIntegrationStand(Settings(profile="mock", headless_turn_taking=enabled), background_scheduler=False)
    app = stand.application
    sid = stand.active_session_id
    await app.receive_async(Event("HandsetLifted", sid, 1, 1., "test", {}, "lift"))
    app._cancel_welcome_timer(sid)
    app.sessions.get(sid).current_node = "bear_dialogue"
    state = app.canonical_runtime.sessions[sid]
    state.mode = "bear_dialogue"
    app._active_audio.clear()
    stand.hardware_mode = "physical"
    calls = asyncio.Queue()

    async def backend(self, audio, owner_current=None, *, start_position_samples=None):
        release = asyncio.Event()
        await calls.put(release)
        await release.wait()
        return PCMPlaybackResult(int(start_position_samples or 0), len(audio.samples), True, True)

    monkeypatch.setattr(SoundDevicePlaybackController, "_play_guarded", backend)
    stand.playback = _HeadlessTurnTakingPlaybackController(None, stand._headless_output_started, stand._headless_output_finished)
    app.bounded_s2s.playback = stand.playback
    stand._headless_audio_queue = asyncio.Queue()
    return stand, calls


async def close(stand):
    stand.hardware_mode = "simulator"
    await stand.aclose()


async def generated_queue(stand):
    engine = stand.application.bounded_s2s
    sid = stand.active_session_id
    queue = AudioSynthesisQueue(engine.tts, engine.voice, stand.playback, engine.cancel, engine.config, engine.metrics)
    await queue.start(sid, engine.cancel.start(sid))
    engine.active_queues[sid] = queue
    return queue


async def root_four_stand(monkeypatch, *, enabled=True):
    stand, calls = await active_stand(monkeypatch, enabled=enabled)
    app, sid = stand.application, stand.active_session_id
    for button in (7, 4):
        await stand.inject("button", {"button_id": button, "state": "pressed"})
    state = app.canonical_runtime.sessions[sid]
    assert state.mode == app.sessions.get(sid).current_node == "support_intake"
    assert state.safety_severity == "none" and state.handset == "up"
    return stand, calls


@run_async
async def test_actual_root_four_output_mutes_capture_and_rejects_pre_output_frames(monkeypatch):
    stand, calls = await root_four_stand(monkeypatch)
    app, sid = stand.application, stand.active_session_id
    ingress = AudioIngressPipeline(app.s2s_audio.vad, app.s2s_audio.config)
    app.s2s_audio.ingress[sid] = ingress
    ingress.feed((.1,) * 600, 0)
    owner = stand._headless_frame_owner()
    stand._enqueue_headless_audio_frame((.3,) * 512, owner)
    operation = asyncio.create_task(stand.playback.play(AudioChunk((.1,) * 512, 16000, 1, sid)))
    try:
        release = await asyncio.wait_for(calls.get(), 2)
        assert not stand._headless_audio_accepting()
        assert not ingress.active and not ingress._vad_pending and not ingress._pre_roll
        denied = stand._headless_frame_owner()
        stand._enqueue_headless_audio_frame((.4,) * 512, denied)
        assert stand._headless_audio_queue.qsize() == 1
        assert owner[1].lifecycle_current() and not owner[1]()
        release.set()
        await operation
        assert not stand._headless_audio_accepting()
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert stand._headless_audio_accepting() and not owner[1]() and not denied[1]()
        stand._enqueue_headless_audio_frame((.1,) * 512)
        stand._headless_audio_queue.put_nowait(None)
        await stand._headless_audio_worker()
        assert ingress.active and len(ingress.buffer._samples) == 512
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await close(stand)


@run_async
async def test_actual_root_four_queue_holds_mute_across_chunk_gap_and_tail(monkeypatch):
    stand, calls = await root_four_stand(monkeypatch)
    queue = await generated_queue(stand)
    try:
        assert stand._headless_audio_accepting()
        await queue.submit("Облака состоят из маленьких капель.")
        release = await asyncio.wait_for(calls.get(), 2)
        assert not stand._headless_audio_accepting()
        release.set()
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert not stand._headless_audio_accepting() and not queue.worker.done()
        await queue.submit("Капли отражают свет.")
        release = await asyncio.wait_for(calls.get(), 2)
        assert not stand._headless_audio_accepting()
        release.set()
        await queue.finish()
        assert not stand._headless_audio_accepting()
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert stand._headless_audio_accepting() and queue.completed_chunks == 2
    finally:
        await queue.abort(stand.active_session_id, invalidate=False)
        await close(stand)


@pytest.mark.parametrize("boundary", ["flag_off", "browser", "concern", "record_only_mismatch", "recorded_story"])
@run_async
async def test_root_four_policy_does_not_mute_other_admitted_voice_paths(monkeypatch, boundary):
    stand, calls = await root_four_stand(monkeypatch, enabled=boundary != "flag_off")
    app, sid = stand.application, stand.active_session_id
    state = app.canonical_runtime.sessions[sid]
    if boundary == "browser":
        stand.hardware_mode = "simulator"
    elif boundary == "record_only_mismatch":
        state.mode = "bear_dialogue"
        assert app.sessions.get(sid).current_node == "support_intake"
    elif boundary == "recorded_story":
        for sequence, button in ((4, 7), (5, 2)):
            await app.receive_async(Event("ButtonPressed", sid, sequence, float(sequence), "root4-test",
                {"button_id": button}, f"story-{sequence}"))
        await app.receive_async(Event("ButtonReleased", sid, 6, 6., "root4-test", {"button_id": 2}, "story-release"))
        await app.receive_async(Event("ButtonPressed", sid, 7, 7., "root4-test", {"button_id": 1}, "story-select"))
        assert state.mode == app.sessions.get(sid).current_node == "fixed_story_playing"
    operation = asyncio.create_task(stand.playback.play(AudioChunk((.1,) * 512, 16000, 1, sid)))
    try:
        release = await asyncio.wait_for(calls.get(), 2)
        assert stand._headless_output_owners
        if boundary == "concern":
            assert not stand._headless_audio_accepting()
            await app.receive_async(Event("VoiceIntentReady", sid, 4, 4., "root4-test",
                {"intent": "free_speech", "safety_severity": "concern"}, "root4-concern"))
            assert state.mode == app.sessions.get(sid).current_node == "support_intake"
            assert state.safety_severity == "concern" and stand._headless_output_owners
        assert not stand._headless_turn_taking_enabled()
        assert stand._headless_audio_accepting()
        stand._enqueue_headless_audio_frame((.25,) * 512)
        assert stand._headless_audio_queue.qsize() == 1
        if boundary == "concern":
            recognized = []
            class Recognizer:
                async def transcribe(self, audio, cancelled):
                    recognized.append(audio.samples)
                    return STTResult("не хочу жить", .99, .1)
            app.s2s_audio.recognizer = Recognizer()
            for _ in range(14):
                stand._enqueue_headless_audio_frame((.25,) * 512)
            for _ in range(20):
                stand._enqueue_headless_audio_frame((0.,) * 512)
            stand._headless_audio_queue.put_nowait(None)
            await asyncio.wait_for(stand._headless_audio_worker(), 3)
            pending = tuple(app._audio_completions.get(sid, ()))
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 3)
            assert len(recognized) == 1
            assert state.mode == "safety_stop" and state.safety_severity == "urgent"
        release.set()
        await operation
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await close(stand)


@run_async
async def test_recorded_intro_mutes_echo_and_fences_pre_output_frames(monkeypatch, tmp_path):
    stand, calls = await active_stand(monkeypatch)
    app, sid = stand.application, stand.active_session_id
    handle = PlaybackHandle(sid, app._session_epochs.get(sid, 0), "intro-play", "intro-lease", "PHRASE_094", 12)
    state = app.canonical_runtime.sessions[sid]
    state.active_audio_id, state.playback_lease = handle.response_id, handle.playback_lease
    app._active_audio[sid] = dict(content_id=handle.response_id, token=handle.token, generation=handle.generation,
                                  playback_id=handle.playback_id, playback_lease=handle.playback_lease)
    path = tmp_path / "intro.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b"\0\0" * 512)
    ingress = AudioIngressPipeline(app.s2s_audio.vad, app.s2s_audio.config)
    app.s2s_audio.ingress[sid] = ingress
    ingress.feed((.1,) * 600, 0)
    assert ingress.active and ingress._vad_pending
    old_owner = stand._headless_frame_owner()
    stand._enqueue_headless_audio_frame((.3,) * 512, old_owner)
    operation = asyncio.create_task(stand._play_asset_audio(sid, handle.response_id, str(path), handle.token, handle=handle))
    try:
        release = await asyncio.wait_for(calls.get(), 2)
        assert not stand._headless_audio_accepting()
        assert not ingress.active and not ingress._vad_pending and not ingress._pre_roll
        denied_owner = stand._headless_frame_owner()
        stand._enqueue_headless_audio_frame((.4,) * 512, denied_owner)
        assert stand._headless_audio_queue.qsize() == 1
        assert old_owner[1].lifecycle_current() and not old_owner[1]()
        release.set()
        await operation
        assert not stand._headless_audio_accepting()
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert stand._headless_audio_accepting()
        assert not old_owner[1]() and not denied_owner[1]()
        # The real dispatcher drops the stale queued frame, then admits new speech.
        stand._enqueue_headless_audio_frame((.1,) * 512)
        stand._headless_audio_queue.put_nowait(None)
        await stand._headless_audio_worker()
        assert ingress.active and len(ingress.buffer._samples) == 512
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await close(stand)


@run_async
async def test_generated_reply_stays_muted_across_long_chunk_gap_and_reopens(monkeypatch):
    stand, calls = await active_stand(monkeypatch)
    queue = await generated_queue(stand)
    ingress = AudioIngressPipeline(stand.application.s2s_audio.vad, stand.application.s2s_audio.config)
    stand.application.s2s_audio.ingress[stand.active_session_id] = ingress
    ingress.feed((.1,) * 600, 0)
    try:
        assert stand._headless_audio_accepting()  # No synthesis-only mute.
        await queue.submit("Привет.")
        release = await asyncio.wait_for(calls.get(), 2)
        assert not stand._headless_audio_accepting()
        # Real TTS chunks omit session_id; physical ownership must still bind
        # their queue and clear the active microphone utterance.
        assert not ingress.active and not ingress._vad_pending
        denied_owner = stand._headless_frame_owner()
        release.set()
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert not stand._headless_audio_accepting()  # Reply still owns its queue.
        await queue.submit("Давай поговорим.")
        release = await asyncio.wait_for(calls.get(), 2)
        stand._enqueue_headless_audio_frame((.8,) * 512)
        assert stand._headless_audio_queue.empty()
        release.set()
        await queue.finish()
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert stand._headless_audio_accepting() and not denied_owner[1]()
        assert queue.completed_chunks == 2
    finally:
        await queue.abort(stand.active_session_id, invalidate=False)
        await close(stand)


@run_async
async def test_old_cancelled_playback_cannot_reopen_new_output(monkeypatch):
    stand, calls = await active_stand(monkeypatch)
    audio = AudioChunk((.1,) * 512, 16000, 1, stand.active_session_id)
    first = asyncio.create_task(stand.playback.play(audio))
    second = None
    try:
        await asyncio.wait_for(calls.get(), 2)
        second = asyncio.create_task(stand.playback.play(audio))
        release = await asyncio.wait_for(calls.get(), 2)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert not stand._headless_audio_accepting()
        release.set()
        await second
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert stand._headless_audio_accepting()
    finally:
        first.cancel()
        if second is not None: second.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        await close(stand)


@run_async
async def test_button_stop_and_handset_still_cancel_muted_generated_reply(monkeypatch):
    for kind, payload in (("button", {"button_id": 7, "state": "pressed"}), ("handset", {"state": "down"})):
        stand, calls = await active_stand(monkeypatch)
        queue = await generated_queue(stand)
        try:
            await queue.submit("Привет.")
            await asyncio.wait_for(calls.get(), 2)
            assert not stand._headless_audio_accepting()
            result = await stand.inject(kind, payload)
            assert "error" not in result
            assert queue.worker.done() and queue.closed, result
            if kind == "handset":
                record = stand.application.sessions.get(stand.active_session_id)
                assert record is None or not record.handset_up
                assert not stand._headless_audio_accepting()
            else:
                assert stand.application.sessions.get(stand.active_session_id).current_node != "bear_dialogue"
        finally:
            await queue.abort(stand.active_session_id, invalidate=False)
            await close(stand)


@run_async
async def test_toggle_defaults_scoping_and_direct_browser_ingress(monkeypatch):
    monkeypatch.delenv("MISHKA_HEADLESS_TURN_TAKING", raising=False)
    assert not Settings.from_env().headless_turn_taking
    monkeypatch.setenv("MISHKA_HEADLESS_TURN_TAKING", "true")
    assert Settings.from_env().headless_turn_taking
    monkeypatch.setenv("MISHKA_HEADLESS_TURN_TAKING", "invalid")
    with pytest.raises(SettingsError, match="MISHKA_HEADLESS_TURN_TAKING"):
        Settings.from_env()
    monkeypatch.delenv("MISHKA_HEADLESS_TURN_TAKING")
    stand, calls = await active_stand(monkeypatch, enabled=False)
    audio = AudioChunk((.1,) * 512, 16000, 1, stand.active_session_id)
    owner = stand._headless_frame_owner()
    operation = asyncio.create_task(stand.playback.play(audio))
    try:
        release = await asyncio.wait_for(calls.get(), 2)
        assert stand._headless_audio_accepting() and owner[1]()
        stand.settings = replace(stand.settings, headless_turn_taking=True)
        assert not stand._headless_audio_accepting()
        record = stand.application.sessions.get(stand.active_session_id)
        record.current_node = "support_intake"
        assert stand._headless_audio_accepting()
        record.current_node = "bear_dialogue"
        stand.hardware_mode = "simulator"
        assert stand._headless_audio_accepting()
        stand.hardware_mode = "physical"
        received = []
        async def feed(sid, samples, context, **kwargs):
            received.append(samples)
            return SimpleNamespace(stt=None, turn=None, pending=None, barge_in=False, ingress_status="collecting")
        monkeypatch.setattr(stand.application.s2s_audio, "feed_audio", feed)
        result = await stand.inject_pcm((.2,) * 512, 16000, flush_silence=False, wait_for_turn=False)
        assert received and result.get("reason") != "stale_session_generation"
        release.set()
        await operation
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await close(stand)


@run_async
async def test_headless_turn_taking_play_stream_mutes_mic_until_finished(monkeypatch):
    stand, _calls = await active_stand(monkeypatch)
    app, sid = stand.application, stand.active_session_id
    chunk_gate = asyncio.Event()
    ingress = AudioIngressPipeline(app.s2s_audio.vad, app.s2s_audio.config)
    app.s2s_audio.ingress[sid] = ingress
    ingress.feed((.1,) * 600, 0)
    assert ingress.active and ingress._vad_pending

    async def fake_super_play_stream(self, chunk_aiter, *, on_chunk=None, owner_current=None):
        played = 0
        assert not stand._headless_audio_accepting()
        assert not ingress.active and not ingress._vad_pending and not ingress._pre_roll
        async for chunk in chunk_aiter:
            assert not stand._headless_audio_accepting()
            if callable(on_chunk):
                res = on_chunk(played, chunk)
                if asyncio.iscoroutine(res):
                    await res
            played += len(chunk.samples)
            await chunk_gate.wait()
        return PCMPlaybackResult(0, played, True, True)

    monkeypatch.setattr(SoundDevicePlaybackController, "play_stream", fake_super_play_stream)

    async def chunks():
        yield AudioChunk((.1,) * 256, 16000, 1, sid)
        yield AudioChunk((.1,) * 256, 16000, 1, sid)

    assert stand._headless_audio_accepting()
    task = asyncio.create_task(stand.playback.play_stream(chunks()))
    try:
        await asyncio.sleep(0.02)
        assert not stand._headless_audio_accepting()
        chunk_gate.set()
        result = await asyncio.wait_for(task, 2)
        assert result.completed and result.written_samples == 512
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + 0.05)
        assert stand._headless_audio_accepting()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await close(stand)


@run_async
async def test_headless_audio_output_play_pcm_stream_owned_cues_and_completion(monkeypatch):
    stand = HardwareIntegrationStand(
        Settings(profile="mock", hardware_mode="physical", allow_degraded_hardware=True, headless_turn_taking=True),
        background_scheduler=False,
    )
    app, sid = stand.application, stand.active_session_id
    await app.receive_async(Event("HandsetLifted", sid, 1, 1., "test", {}, "lift"))
    app._cancel_welcome_timer(sid)
    state = app.canonical_runtime.sessions[sid]
    state.mode = "game_active"
    state.game_audio_stage = "prompt"
    app.sessions.get(sid).current_node = "bear_dialogue"
    app._active_audio.clear()
    handle = PlaybackHandle(sid, app._session_epochs.get(sid, 0), "GAME_PROMPT", "lease-gp", "GAME_PROMPT", 21)
    state.active_audio_id, state.playback_lease = handle.response_id, handle.playback_lease
    app._active_audio[sid] = dict(content_id=handle.response_id, token=handle.token, generation=handle.generation,
                                  playback_id=handle.playback_id, playback_lease=handle.playback_lease)
    events, game_started, cues = [], [], []
    orig_receive = app.receive_async
    async def capture_receive(ev):
        events.append(ev.event_type)
        return await orig_receive(ev)
    monkeypatch.setattr(app, "receive_async", capture_receive)
    monkeypatch.setattr(app, "_game_audio_started", lambda *a: game_started.append(a))

    async def fake_super_play_stream(self, chunk_aiter, *, on_chunk=None, owner_current=None):
        idx = 0
        total = 0
        async for chunk in chunk_aiter:
            if callable(on_chunk):
                res = on_chunk(idx, chunk)
                if asyncio.iscoroutine(res):
                    await res
            idx += 1
            total += len(chunk.samples)
        return PCMPlaybackResult(0, total, True, True)

    monkeypatch.setattr(SoundDevicePlaybackController, "play_stream", fake_super_play_stream)

    async def stream_items():
        yield AudioChunk((.1,) * 480, 24000, handle.token, sid, handle.generation), (1, 2)
        yield AudioChunk((.1,) * 480, 24000, handle.token, sid, handle.generation), ()

    try:
        assert stand.audio_output.owned_render_timeout_s >= 45.0
        stand.audio_output.play_pcm_stream_owned(handle, stream_items(), on_cue=lambda btns: cues.append(btns))
        for _ in range(50):
            if "AudioCompleted" in events:
                break
            await asyncio.sleep(0.02)
        assert cues == [(1, 2)]
        assert len(game_started) == 1
        assert "AudioCompleted" in events
    finally:
        await close(stand)
