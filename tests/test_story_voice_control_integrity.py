"""Synthetic PCM/VAD/STT with a real owned asset task; no acoustic echo claim."""
import asyncio
import io
import wave

import pytest

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import EnergyVADAdapter, PCMPlaybackResult, STTResult
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput


@pytest.mark.asyncio
@pytest.mark.parametrize('text,expected', [
    ('бла-бла', 'story_speech_analysis'),
    ('Ух ты, здорово!', 'fixed_story_playing'),
    ('пауза', 'story_pause_announcement'),
    ('останови сказку', 'fixed_story_choice_wave'),
])
async def test_pcm_story_controls_preserve_or_revoke_actual_playback_owner(text, expected):
    stand = HardwareIntegrationStand(Settings(profile='mock', runtime_mode='clinical'),
                                     background_scheduler=False)
    app, sid = stand.application, stand.active_session_id
    started, stopped = asyncio.Event(), asyncio.Event()
    guards, cancellations, transcripts, model_calls = [], [], [], []
    story = 'fixed_aibolit_v1'

    class Playback:
        active_token = 0
        async def play_owned_pcm(self, audio, current, *, start_position_samples=0):
            assert current() and audio.samples and audio.session_id == sid
            guards.append(current)
            started.set()
            await stopped.wait()
            return PCMPlaybackResult(start_position_samples, 4000, False, True)
        async def stop(self):
            stopped.set()

    class Recognizer:
        async def transcribe(self, audio, cancelled):
            assert not cancelled()
            transcripts.append(audio.samples)
            return STTResult(text, .99, .1)

    class NoModel:
        async def stream(self, *args, **kwargs):
            model_calls.append(True)
            raise AssertionError('Story controls must not invoke dialogue generation')
            yield  # Keep the provider's async-iterator interface.

    wav = io.BytesIO()
    with wave.open(wav, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b'\x01\x00' * 16000)
    async def synthetic_asset(*args):
        return wav.getvalue()

    try:
        await app.receive_async(Event('HandsetLifted', sid, 1, 1., 'test', {}, 'lift', 0.))
        app._cancel_welcome_timer(sid)
        state = app.canonical_runtime.sessions[sid]
        verified = app.canonical_runtime.stories.get_verified(story)
        assert verified and story in stand.stories
        # Seed the admitted playing scene; asset ownership/dispatch below are real.
        state.mode, state.substate = 'fixed_story_playing', 'story_playing'
        state.active_story_id = state.active_audio_id = story
        state.playback_lease = 'synthetic-story-lease'
        state.expected_inputs = ['button_6', 'button_7', 'speech', 'handset_down']
        app.sessions.get(sid).current_node = state.mode
        stand.hardware_mode = 'physical'
        stand.playback = Playback()
        stand._offload_audio = synthetic_asset
        output = HeadlessAudioOutput()
        def dispatch(response, asset, token, offset, handle):
            if response == story:
                stand._headless_asset_play(response, asset, token, offset, handle)
        def cancel(token):
            cancellations.append(token)
            stand._cancel_response_audio()
        output.on_asset_play, output.on_pause, output.on_cancel = dispatch, stand._pause_story_playback, cancel
        app.audio = output
        app._play_owned_audio(sid, story, verified['audio_file'], 1,
                              {'playback_lease': state.playback_lease})
        await asyncio.wait_for(started.wait(), 2.)
        owner, task = dict(app._active_audio[sid]), stand._active_story_task
        cancellations.clear()
        app.s2s_audio.vad, app.s2s_audio.recognizer = EnergyVADAdapter(), Recognizer()
        app.bounded_s2s.llm = NoModel()
        stand._headless_audio_queue = asyncio.Queue(64)
        assert stand._headless_audio_accepting() and guards[0]()
        for _ in range(10):
            stand._enqueue_headless_audio_frame((.25,) * 512)
        for _ in range(20):
            stand._enqueue_headless_audio_frame((0.,) * 512)
        stand._headless_audio_queue.put_nowait(None)
        await asyncio.wait_for(stand._headless_audio_worker(), 3.)
        pending = tuple(app._audio_completions.get(sid, ()))
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), 3.)
        assert len(transcripts) == 1 and len(transcripts[0]) >= 5120
        assert not model_calls
        assert state.mode == expected and state.handset == 'up'
        if text in {'бла-бла', 'Ух ты, здорово!'}:
            assert app._active_audio[sid] == owner
            assert state.active_audio_id == story and guards[0]()
            assert stand._active_story_task is task and not task.done()
            assert not cancellations and not stopped.is_set()
        elif text == 'пауза':
            assert task.done() and not guards[0]() and stopped.is_set()
            assert state.story_checkpoint_confirmed
            assert state.dialogue_memory.story_checkpoint == {'story_id': story, 'offset_ms': 250}
            assert state.active_audio_id == 'PHRASE_062'
        else:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2.)
            assert cancellations and not guards[0]()
            assert app._active_audio[sid]['playback_id'] != owner['playback_id']
            assert state.active_audio_id == 'PHRASE_066'
    finally:
        stopped.set()
        stand.hardware_mode = 'simulator'
        await stand.aclose()
