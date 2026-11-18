"""Game resampling must preserve a dispatchable PCM buffer and cue timing."""
import pytest

from app.foundation.game_presentation import GamePresentationMixin
from app.foundation.game_engine import next_deadline
from app.foundation.private_welcome import valid_pcm
from app.foundation.s2s import AudioChunk
from app.foundation.voice_pipeline import resample_mono


@pytest.mark.asyncio
async def test_resampling_overshoot_is_attenuated_once_after_complete_assembly():
    source = (0.,) * 400 + (1.,) * 400 + (0.,) * 400
    first, second = AudioChunk(source, 48000, 7), AudioChunk((.1, -.2), 24000, 7)
    converted = resample_mono(source, 48000, 24000)
    assert valid_pcm(first) and max(map(abs, converted)) > 1.
    audio, cues = await GamePresentationMixin()._assemble_game_audio(
        [(first, (1,)), (second, (2,))], 7, lambda: True)
    assert valid_pcm(audio) and audio.sample_rate == 24000 and audio.token == 7
    raw = converted + (0.,) * 3360 + second.samples + (0.,) * 3360
    assert len(audio.samples) == len(raw)
    assert audio.samples == tuple(value / max(map(abs, converted)) for value in raw)
    assert cues == ((0., (1,)), ((len(converted) + 3360) / 24000, (2,)))


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [24000, 48000])
async def test_normal_game_audio_and_cue_timing_remain_exact(rate):
    samples = (.1, -.2, .3, -.4) * 100
    converted = resample_mono(samples, rate, 24000)
    audio, cues = await GamePresentationMixin()._assemble_game_audio(
        [(AudioChunk(samples, rate, 8), (3,))], 8, lambda: True)
    assert valid_pcm(audio)
    assert audio.samples == converted + (0.,) * 3360
    assert cues == ((0., (3,)),)


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [(), (float("nan"),), (float("inf"),), (float("-inf"),), (1.01,)])
async def test_invalid_game_source_audio_cannot_become_dispatchable_silence(samples):
    assert await GamePresentationMixin()._assemble_game_audio(
        [(AudioChunk(samples, 24000, 1), ())], 1, lambda: True) is None


@pytest.mark.asyncio
async def test_revoked_assembly_owner_cannot_return_pcm():
    assert await GamePresentationMixin()._assemble_game_audio(
        [(AudioChunk((.1,), 24000, 1), ())], 1, lambda: False) is None


@pytest.mark.asyncio
async def test_actual_odd_idle_hint_survives_resampling_and_strict_pcm_dispatch():
    from test_immersive_game_runtime import GameRuntime
    h = GameRuntime("4-5")
    source = (0.,) * 400 + (1.,) * 400 + (0.,) * 400
    class Voice:
        async def synthesize(self, text, token, cancelled):
            return AudioChunk(source, 48000, token)
        async def convert_private(self, audio, cancelled):
            return audio
    original = h.app.audio.play_pcm_owned
    def dispatch(handle, audio):
        assert valid_pcm(audio)  # Same amplitude guard as browser private PCM.
        original(handle, audio)
    h.app.audio.play_pcm_owned = dispatch
    h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = Voice()
    try:
        await h.start(4)
        assert (await h.complete())["reason"] == "game_prompt_finished"
        game = h.state.game_engine_state
        h.clock[0] = next_deadline(game)
        [result] = await h.app.flush_game_timers()
        assert result["reason"] == "game_wait_hint"
        assert h.state.mode == "game_active" and h.state.game_audio_stage == "prompt"
        assert h.pcm[-1][0].response_id == "GAME_PROMPT" and valid_pcm(h.pcm[-1][1])
        assert (await h.complete())["reason"] == "game_prompt_finished"
        assert h.state.mode == "game_active" and h.state.game_audio_stage == "playing"
    finally:
        await h.close()
