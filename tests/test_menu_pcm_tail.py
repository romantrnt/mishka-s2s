"""Actual menu/native protocol/playback chain, with synthetic worker and device.

The approved PHRASE_019 WAV is real. Generated PCM is a protocol fixture,
not speech or a listening/voice-quality result.
"""
import asyncio
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from app.foundation.s2s import PassthroughVoiceProvider, SoundDevicePlaybackController
from app.foundation.s2s import AudioChunk
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput
from app.foundation.config import Settings
from test_experimental_voice import configured
from test_scene_menu_runtime import Harness


@pytest.mark.asyncio
@pytest.mark.parametrize('drain_failure', [False, True])
async def test_actual_menu_keeps_native_final_frame_and_waits_for_device_drain(configured, monkeypatch, drain_failure):
    provider, _, _ = configured('vox555', inference_timeout_s=3.)
    h = Harness()
    stand = HardwareIntegrationStand(settings=Settings(profile='mock'), background_scheduler=False)
    unused_app = stand.application
    stand.application = h.app
    stand.active_session_id = h.sid
    stand.playback = SoundDevicePlaybackController()
    h.app.bounded_s2s.tts = provider
    h.app.bounded_s2s.voice = PassthroughVoiceProvider()
    h.app.settings = replace(h.app.settings, voice_conversion='off')
    h.app.audio = HeadlessAudioOutput()
    h.app.audio.on_asset_play = stand._headless_asset_play
    h.app.audio.on_pcm_stream_play = stand._headless_pcm_stream_play
    h.app.audio.on_cancel = lambda _token: stand._cancel_response_audio()
    streams = []
    drain_entered, release_drain = threading.Event(), threading.Event()

    class Stream:
        def __init__(self, **kwargs):
            self.rate = kwargs['samplerate']
            self.values = []
            self.drained = False
            self.aborted = False
            streams.append(self)

        def start(self):
            pass

        def write(self, values):
            self.values.extend(np.asarray(values).ravel().tolist())

        def stop(self):
            # Stream0 is the recorded invitation. Stream1 is native MENU_GAME.
            if len(streams) == 2:
                drain_entered.set()
                assert release_drain.wait(3)
                if drain_failure:
                    raise RuntimeError('synthetic device drain failure')
            self.drained = True

        def abort(self):
            self.aborted = True

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream))
    monkeypatch.setattr('app.foundation.s2s._ensure_sounddevice_ready', lambda *args: None)
    try:
        await h.button(1)
        await h.release(1)
        assert await asyncio.to_thread(drain_entered.wait, 2), stand.errors
        assert h.state.mode == 'game_menu'
        assert h.state.scene_menu_phase == 'narrating'
        assert h.state.active_audio_id == 'MENU_GAME'
        assert len(streams) == 2 and streams[0].drained
        generated = streams[1].values
        expected = np.repeat(np.arange(1, 13, dtype=np.float32) / 16, 12000)
        # The real playback controller has a 3ms leading fade and appended fade.
        assert len(generated) == len(expected) + 144
        assert generated[144:len(expected)] == expected[144:].tolist()
        assert generated[len(expected)-1] == .75  # Last native frame survives.
        assert not streams[1].drained and not streams[1].aborted
        release_drain.set()
        expected_phase = 'ready' if drain_failure else 'repeat_prompt'
        for _ in range(200):
            if h.state.scene_menu_phase == expected_phase:
                break
            await asyncio.sleep(.005)
        assert h.state.scene_menu_phase == expected_phase, stand.errors
        assert streams[1].drained is not drain_failure
        assert not streams[1].aborted
        if drain_failure:
            assert h.state.scene_menu_answers['game_menu'] == 'failed'
            assert not stand.playback.last_playback_completed
        assert provider.jobs_completed == 1 and provider.worker_starts == 1
    finally:
        release_drain.set()
        await provider.close()
        await stand.aclose()
        await unused_app.shutdown_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['drain', 'close'])
async def test_cancellation_during_native_drain_still_closes_before_reuse(monkeypatch, boundary):
    entered, release = threading.Event(), threading.Event()
    closed = []

    class Stream:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def write(self, values):
            pass
        def stop(self):
            if boundary == 'drain':
                entered.set()
                assert release.wait(3)
        def abort(self):
            pass
        def close(self):
            if boundary == 'close':
                entered.set()
                assert release.wait(3)
            closed.append(True)

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream))
    monkeypatch.setattr('app.foundation.s2s._ensure_sounddevice_ready', lambda *args: None)
    playback = SoundDevicePlaybackController()
    playback.active_token = 42

    async def chunks():
        yield AudioChunk((.1,) * 800, 8000, 42)

    task = asyncio.create_task(playback.play_stream(chunks()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done(), 'native drain must retain its owner until actual release'
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and closed == [True]
        assert playback._stream is None
        assert not playback.last_playback_completed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
