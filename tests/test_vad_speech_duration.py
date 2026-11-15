"""Synthetic VAD duration regressions; no model, provider or audio device."""

import pytest

from app.foundation.s2s import AudioIngressPipeline, EnergyVADAdapter, S2SConfig


FRAME_SAMPLES = 512
QUIET = (0.01,) * FRAME_SAMPLES
VOICE = (0.2,) * FRAME_SAMPLES
SILENCE = (0.0,) * FRAME_SAMPLES


def _collect_frames(ingress, frame, count, token=1):
    for _ in range(count):
        assert ingress.feed(frame, token) == ("collecting", None)


def _finish(ingress, token=1):
    # 6 * 32 ms is below the default 200 ms hangover; 7 * 32 ms ends it.
    _collect_frames(ingress, SILENCE, 6, token)
    return ingress.feed(SILENCE, token)


def test_preroll_does_not_make_one_voiced_frame_long_enough():
    ingress = AudioIngressPipeline(EnergyVADAdapter(), S2SConfig())
    _collect_frames(ingress, SILENCE, 10)
    _collect_frames(ingress, VOICE, 1)
    assert _finish(ingress) == ("too_short", None)


@pytest.mark.parametrize(
    "preroll_ms,quiet_frames",
    [(0, 0), (0, 20), (320, 0), (320, 1), (33, 10), (320, 20)],
)
@pytest.mark.parametrize(
    "voiced_frames,minimum_ms,expected",
    [(9, 300, "too_short"), (10, 300, "speech_end"),
     (9, 288, "speech_end"), (9, 289, "too_short")],
)
def test_minimum_duration_is_independent_of_available_preroll(
    preroll_ms, quiet_frames, voiced_frames, minimum_ms, expected,
):
    config = S2SConfig(vad_preroll_ms=preroll_ms, min_speech_ms=minimum_ms)
    ingress = AudioIngressPipeline(EnergyVADAdapter(), config)
    quiet_pcm = ()
    for index in range(quiet_frames):
        frame = (0.001 * (index + 1),) * FRAME_SAMPLES
        _collect_frames(ingress, frame, 1)
        quiet_pcm += frame
    _collect_frames(ingress, VOICE, voiced_frames, token=7)
    status, chunk = _finish(ingress, token=7)
    assert status == expected
    if expected == "too_short":
        assert chunk is None
    else:
        retained_samples = min(len(quiet_pcm), config.sample_rate * preroll_ms // 1000)
        preroll = quiet_pcm[-retained_samples:] if retained_samples else ()
        assert chunk is not None and chunk.token == 7
        assert chunk.samples == preroll + VOICE * voiced_frames + SILENCE * 7


@pytest.mark.parametrize(
    "first_voice,gap_frames,last_voice,expected",
    [(1, 6, 1, "too_short"), (3, 5, 2, "speech_end")],
)
def test_short_internal_pauses_keep_the_existing_speech_span_policy(
    first_voice, gap_frames, last_voice, expected,
):
    ingress = AudioIngressPipeline(EnergyVADAdapter(), S2SConfig())
    _collect_frames(ingress, QUIET, 10)
    _collect_frames(ingress, VOICE, first_voice)
    _collect_frames(ingress, SILENCE, gap_frames)
    _collect_frames(ingress, VOICE, last_voice)
    status, chunk = _finish(ingress)
    assert status == expected
    if expected == "too_short":
        assert chunk is None
    else:
        assert chunk is not None
        assert chunk.samples == (
            QUIET * 10 + VOICE * first_voice + SILENCE * gap_frames
            + VOICE * last_voice + SILENCE * 7
        )


def test_continuation_threshold_frames_count_after_start_threshold():
    class ProbabilityVAD:
        def speech_probability(self, samples, sample_rate):
            assert len(samples) == FRAME_SAMPLES and sample_rate == 16_000
            return samples[0]

    ingress = AudioIngressPipeline(ProbabilityVAD(), S2SConfig(min_speech_ms=288))
    quiet, start, continued, ending = [
        (value,) * FRAME_SAMPLES for value in (0.34, 0.35, 0.20, 0.19)
    ]
    _collect_frames(ingress, quiet, 10)
    _collect_frames(ingress, start, 1)
    _collect_frames(ingress, continued, 8)
    _collect_frames(ingress, ending, 6)
    status, chunk = ingress.feed(ending, 1)
    assert status == "speech_end" and chunk is not None
    assert chunk.samples == quiet * 10 + start + continued * 8 + ending * 7


@pytest.mark.parametrize("first_result", ["speech_end", "too_short", "explicit_reset"])
def test_new_utterance_does_not_inherit_duration_or_preroll(first_result):
    ingress = AudioIngressPipeline(EnergyVADAdapter(), S2SConfig())
    _collect_frames(ingress, QUIET, 10)
    if first_result == "explicit_reset":
        _collect_frames(ingress, VOICE, 9)
        assert ingress.feed(VOICE[:341], 1) == ("collecting", None)
        ingress.reset()
        ingress.reset()
    else:
        _collect_frames(ingress, VOICE, 10 if first_result == "speech_end" else 1)
        assert _finish(ingress)[0] == first_result

    _collect_frames(ingress, VOICE, 1, token=2)
    assert _finish(ingress, token=2) == ("too_short", None)
    _collect_frames(ingress, QUIET, 10, token=3)
    _collect_frames(ingress, VOICE, 10, token=3)
    status, chunk = _finish(ingress, token=3)
    assert status == "speech_end" and chunk is not None and chunk.token == 3
    assert chunk.samples == QUIET * 10 + VOICE * 10 + SILENCE * 7


@pytest.mark.parametrize("preroll_ms,voiced_frames", [(0, 17), (320, 7)])
def test_max_duration_still_bounds_all_pcm_and_resets(preroll_ms, voiced_frames):
    config = S2SConfig(vad_preroll_ms=preroll_ms, max_speech_ms=544)
    ingress = AudioIngressPipeline(EnergyVADAdapter(), config)
    _collect_frames(ingress, QUIET, 10)
    _collect_frames(ingress, VOICE, voiced_frames - 1)
    status, chunk = ingress.feed(VOICE, 5)
    assert status == "max_duration" and chunk is not None and chunk.token == 5
    assert chunk.duration_ms == 544
    assert chunk.samples == (QUIET * 10 if preroll_ms else ()) + VOICE * voiced_frames
    _collect_frames(ingress, VOICE, 1, token=6)
    assert _finish(ingress, token=6) == ("too_short", None)


@pytest.mark.parametrize("voiced_frames,expected", [(1, "too_short"), (10, "speech_end")])
def test_browser_sized_feeds_preserve_duration_and_pcm(voiced_frames, expected):
    ingress = AudioIngressPipeline(EnergyVADAdapter(), S2SConfig())
    pcm = QUIET * 10 + VOICE * voiced_frames + SILENCE * 7
    for offset in range(0, len(pcm), 341):
        status, chunk = ingress.feed(pcm[offset:offset + 341], 17)
        if offset + 341 < len(pcm):
            assert (status, chunk) == ("collecting", None)
    assert status == expected
    if expected == "too_short":
        assert chunk is None
    else:
        assert chunk is not None and chunk.token == 17
        assert chunk.samples == pcm


@pytest.mark.parametrize("preroll_ms", [1, 33, 64, 320])
@pytest.mark.parametrize("quiet_frames", [1, 2, 3, 10, 20])
def test_exact_24khz_minimum_boundary_with_preroll(preroll_ms, quiet_frames):
    config = S2SConfig(
        sample_rate=24_000, vad_preroll_ms=preroll_ms, min_speech_ms=64, vad_silence_ms=200,
    ).validate()
    ingress = AudioIngressPipeline(EnergyVADAdapter(), config)
    _collect_frames(ingress, QUIET, quiet_frames)
    _collect_frames(ingress, VOICE, 3)  # 1536 PCM samples are exactly 64 ms at 24 kHz.
    _collect_frames(ingress, SILENCE, 9)
    status, chunk = ingress.feed(SILENCE, 23)
    assert status == "speech_end" and chunk is not None and chunk.token == 23
    retained_samples = min(quiet_frames * FRAME_SAMPLES, 24 * preroll_ms)
    assert chunk.samples == QUIET[:1] * retained_samples + VOICE * 3 + SILENCE * 10


@pytest.mark.parametrize(
    "sample_rate,voiced_frames,silence_frames",
    [(8_000, 1, 4), (16_000, 2, 7), (24_000, 3, 10),
     (32_000, 4, 13), (48_000, 6, 19)],
)
@pytest.mark.parametrize("minimum_ms,expected", [(63, "speech_end"), (64, "speech_end"), (65, "too_short")])
@pytest.mark.parametrize("preroll_ms", [0, 1, 320])
def test_exact_minimum_and_neighboring_milliseconds_across_pcm_rates(
    sample_rate, voiced_frames, silence_frames, minimum_ms, expected, preroll_ms,
):
    config = S2SConfig(
        sample_rate=sample_rate, min_speech_ms=minimum_ms, vad_preroll_ms=preroll_ms,
    ).validate()
    ingress = AudioIngressPipeline(EnergyVADAdapter(), config)
    _collect_frames(ingress, QUIET, 20)
    # Each rate/voiced-frame pair above is exactly 64 ms of speech.
    _collect_frames(ingress, VOICE, voiced_frames)
    _collect_frames(ingress, SILENCE, silence_frames - 1)
    status, chunk = ingress.feed(SILENCE, 31)
    assert status == expected
    if expected == "too_short":
        assert chunk is None
    else:
        retained_samples = min(20 * FRAME_SAMPLES, sample_rate * preroll_ms // 1000)
        assert chunk is not None and chunk.token == 31
        assert chunk.samples == (
            QUIET[:1] * retained_samples + VOICE * voiced_frames + SILENCE * silence_frames
        )


@pytest.mark.parametrize(
    "sample_rate,silence_frames,minimum_ms,expected",
    [(22_050, 9, 69, "speech_end"), (22_050, 9, 70, "too_short"),
     (44_100, 18, 34, "speech_end"), (44_100, 18, 35, "too_short")],
)
def test_fractional_millisecond_speech_stays_on_the_correct_side_of_minimum(
    sample_rate, silence_frames, minimum_ms, expected,
):
    config = S2SConfig(
        sample_rate=sample_rate, min_speech_ms=minimum_ms, vad_preroll_ms=1,
    ).validate()
    ingress = AudioIngressPipeline(EnergyVADAdapter(), config)
    _collect_frames(ingress, QUIET, 1)
    _collect_frames(ingress, VOICE, 3)
    _collect_frames(ingress, SILENCE, silence_frames - 1)
    status, chunk = ingress.feed(SILENCE, 37)
    assert status == expected
    if expected == "too_short":
        assert chunk is None
    else:
        assert chunk is not None and chunk.token == 37
        assert chunk.samples == (
            QUIET[:1] * (sample_rate // 1000) + VOICE * 3 + SILENCE * silence_frames
        )
