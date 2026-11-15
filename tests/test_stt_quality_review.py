"""Synthetic STT metadata probes, not acoustic quality or calibration evidence."""
import asyncio
import math
from types import SimpleNamespace

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import AudioChunk, FasterWhisperRecognizer, S2SConfig


class FakeWhisper:
    def __init__(self, segments):
        self.segments = segments

    def transcribe(self, samples, **kwargs):
        assert samples.dtype.name == "float32"
        assert kwargs["vad_filter"] is False
        assert kwargs["condition_on_previous_text"] is False
        return self.segments, None


def recognizer(segments):
    model = FakeWhisper(segments)
    instance = FasterWhisperRecognizer(lambda: model, S2SConfig(), "synthetic")
    instance._model = model
    return instance


def test_bad_decode_is_unknown_despite_strong_speech_presence():
    instance = recognizer([SimpleNamespace(text="обычная речь", no_speech_prob=.01, avg_logprob=-10.)])
    result = asyncio.run(instance.transcribe(AudioChunk((.1,) * 1600, 16000, 1), lambda: False))
    assert result.text == "обычная речь"  # safety-only inspection still possible
    assert result.confidence == pytest.approx(math.exp(-10.))
    assert result.unknown


@pytest.mark.parametrize("segments,expected,unknown", [
    ([SimpleNamespace(text="привет", no_speech_prob=.05, avg_logprob=-.01)], .95, False),
    ([SimpleNamespace(text="привет", no_speech_prob=.9, avg_logprob=-.01)], .1, True),
    ([SimpleNamespace(text="привет", no_speech_prob=.01, avg_logprob=-.5)], math.exp(-.5), False),
    ([], 0., True),
    ([SimpleNamespace(text=" ", no_speech_prob=.01, avg_logprob=-.01)], .99, True),
])
def test_existing_presence_gate_and_empty_input_remain(segments, expected, unknown):
    result = asyncio.run(recognizer(segments).transcribe(AudioChunk((.1,) * 1600, 16000, 1), lambda: False))
    assert result.confidence == pytest.approx(expected)
    assert result.unknown is unknown


@pytest.mark.parametrize("field", ["avg_logprob", "no_speech_prob"])
@pytest.mark.parametrize("bad", [None, True, "0.0", float("nan"), float("inf"), float("-inf")])
def test_invalid_metadata_fails_closed_without_losing_text(field, bad):
    values = {"text": "обычная речь", "no_speech_prob": .01, "avg_logprob": -.1}
    values[field] = bad
    result = asyncio.run(recognizer([SimpleNamespace(**values)]).transcribe(AudioChunk((.1,) * 1600, 16000, 1), lambda: False))
    assert result.confidence == 0. and result.unknown
    assert result.text == "обычная речь"


@pytest.mark.parametrize("values", [
    {"text": "обычная речь", "no_speech_prob": .01},
    {"text": "обычная речь", "avg_logprob": -.1},
    {"text": "обычная речь", "no_speech_prob": -.1, "avg_logprob": -.1},
    {"text": "обычная речь", "no_speech_prob": 1.1, "avg_logprob": -.1},
    {"text": "обычная речь", "no_speech_prob": .01, "avg_logprob": .1},
])
def test_missing_or_out_of_range_metadata_fails_closed(values):
    result = asyncio.run(recognizer([SimpleNamespace(**values)]).transcribe(AudioChunk((.1,) * 1600, 16000, 1), lambda: False))
    assert result.confidence == 0. and result.unknown
    assert result.text == "обычная речь"


@pytest.mark.parametrize("wait", [True, False])
@pytest.mark.parametrize("text,urgent", [("Меня зовут Тест", False), ("хочу умереть", True)])
def test_actual_recognizer_low_decode_is_safety_only_in_both_paths(wait, text, urgent):
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        updates = []
        async def updated(_sid, result):
            updates.append(result)
        app.audio_update_hook = updated
        try:
            event = Event("HandsetLifted", "audit", 1, 1., "audit", {}, "1", 0.)
            await app.receive_async(event)
            state = app.canonical_runtime.sessions["audit"]
            state.mode = "exhibition_name_capture"
            app.s2s_audio.recognizer = recognizer([SimpleNamespace(text=text, no_speech_prob=.01, avg_logprob=-10.)])
            await app.receive_audio_async("audit", (.2,) * 8192, 2, wait_for_turn=wait)
            result = await app.receive_audio_async("audit", (0.,) * 4096, 3, wait_for_turn=wait)
            pending = tuple(app._audio_completions.get("audit", ()))
            if pending:
                await asyncio.gather(*pending)
                result = updates[-1]
            assert result["reason"] == ("urgent_safety" if urgent else "audio_retry")
            if urgent:
                assert state.mode == "safety_stop" and state.safety_severity == "urgent"
                assert "incident_id" not in state.snapshot()
                assert any(item.get("phrase_id") == "PHRASE_091" for item in result["outputs"])
                assert not any(item.get("type") == "adult_handoff_request" for item in result["outputs"])
            state = app.canonical_runtime.sessions["audit"]
            assert state.display_name is None and not app.conversation_contexts
            assert text not in repr(state.dialogue_memory.bounded_turns)
        finally:
            await app._cleanup_session_resources("audit")
    asyncio.run(run())
