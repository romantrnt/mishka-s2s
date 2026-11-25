"""Bounded native admission with synthetic held threads; no Whisper/model import."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

import app.foundation.s2s as s2s
from app.foundation.canonical_contract import normalize_exhibition_age


def audio(token=0):
    return s2s.AudioChunk((.1,) * 1600, 16000, token)


class HeldDecoder:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def transcribe(self, samples, **_options):
        with self.lock:
            self.calls += 1
            if self.calls == 2:
                self.entered.set()
        assert self.release.wait(3), 'test must release held native work'
        return ([SimpleNamespace(text='Мне шесть годиков', no_speech_prob=0., avg_logprob=0.)], None)


def pipeline(recognizer, config):
    metrics = s2s.PipelineMetrics()
    conversation = SimpleNamespace(metrics=metrics, config=config)
    return s2s.LocalS2SPipeline(s2s.EnergyVADAdapter(), recognizer, conversation, config)


async def drain(recognizer, tasks, release):
    release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(*recognizer._native_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('cold', [False, True])
async def test_native_busy_keeps_typed_identity_through_load_and_decode_wrappers(cold):
    config = s2s.S2SConfig(stt_timeout_s=1)
    release = threading.Event()
    model = HeldDecoder()
    model.release.set()
    recognizer = s2s.FasterWhisperRecognizer(lambda: model, config)
    if not cold:
        recognizer._model = model
    entered = [threading.Event(), threading.Event()]
    def hold(index):
        entered[index].set()
        assert release.wait(3)
    tasks = [asyncio.create_task(recognizer._run_native(lambda index=index: hold(index))) for index in (0, 1)]
    try:
        assert await asyncio.to_thread(entered[0].wait, 1)
        assert await asyncio.to_thread(entered[1].wait, 1)
        with pytest.raises(RuntimeError) as rejected:
            await recognizer.transcribe(audio(), lambda: False)
        assert type(rejected.value).__name__ == 'SpeechRecognizerBusyError'
        assert recognizer.native_worker_snapshot() == {'active_jobs': 2, 'queued_jobs': 0}
    finally:
        await drain(recognizer, tasks, release)
    assert (await recognizer.transcribe(audio(), lambda: False)).text == 'Мне шесть годиков'
    assert recognizer.native_worker_snapshot() == {'active_jobs': 0, 'queued_jobs': 0}


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['timeout', 'cancel'])
async def test_pipeline_busy_is_not_misrecognition_and_release_accepts_one_current_turn(ending):
    config = s2s.S2SConfig(stt_timeout_s=.03 if ending == 'timeout' else 1)
    model = HeldDecoder()
    recognizer = s2s.FasterWhisperRecognizer(lambda: model, config)
    recognizer._model = model
    old = [asyncio.create_task(recognizer.transcribe(audio(), lambda: False)) for _ in range(2)]
    local = pipeline(recognizer, config)
    accepted = []
    async def handler(text, generation):
        accepted.append((text, generation, normalize_exhibition_age(text)))
        return s2s.TurnResult((), (), False)
    try:
        assert await asyncio.to_thread(model.entered.wait, 1)
        if ending == 'cancel':
            for task in old:
                task.cancel()
        await asyncio.gather(*old, return_exceptions=True)
        blocked = await local._recognize_turn('current', audio(), None, 0, 'speech_end', False, handler)
        assert blocked.ingress_status == 'stt_busy'
        assert blocked.stt is None and blocked.turn.fallback_reason == 'stt_busy'
        assert not accepted and model.calls == 2
        assert recognizer.native_worker_snapshot() == {'active_jobs': 2, 'queued_jobs': 0}
        model.release.set()
        await asyncio.gather(*recognizer._native_tasks)
        result = await local._recognize_turn('current', audio(), None, 0, 'speech_end', False, handler)
        assert result.ingress_status == 'speech_end' and result.stt.text == 'Мне шесть годиков'
        assert accepted == [('Мне шесть годиков', 0, 6)]
        assert model.calls == 3 and recognizer.native_worker_snapshot() == {'active_jobs': 0, 'queued_jobs': 0}
    finally:
        await drain(recognizer, old, model.release)


@pytest.mark.asyncio
async def test_released_stale_native_result_never_enters_parser_or_current_turn():
    config = s2s.S2SConfig(stt_timeout_s=1)
    model = HeldDecoder()
    recognizer = s2s.FasterWhisperRecognizer(lambda: model, config)
    recognizer._model = model
    local = pipeline(recognizer, config)
    accepted = []
    async def handler(text, generation):
        accepted.append((text, generation))
        return s2s.TurnResult((), (), False)
    old = [asyncio.create_task(local._recognize_turn('old'+str(i), audio(), None, 0, 'speech_end', False, handler)) for i in (0, 1)]
    try:
        assert await asyncio.to_thread(model.entered.wait, 1)
        local.generations.update(old0=1, old1=1)
        model.release.set()
        stale = await asyncio.gather(*old, return_exceptions=True)
        assert all(isinstance(value, asyncio.CancelledError) for value in stale)
        assert not accepted
        result = await local._recognize_turn('current', audio(1), None, 0, 'speech_end', False, handler)
        assert result.stt.text == 'Мне шесть годиков'
        assert accepted == [('Мне шесть годиков', 0)]
    finally:
        await drain(recognizer, old, model.release)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['runtime_error', 'timeout', 'unknown'])
async def test_nonbusy_failure_and_unknown_are_not_reclassified_by_error_text(kind):
    config = s2s.S2SConfig(stt_timeout_s=.01)
    class Recognizer:
        async def transcribe(self, *_args):
            if kind == 'runtime_error':
                raise RuntimeError('faster-whisper native workers are busy')
            if kind == 'timeout':
                await asyncio.sleep(1)
            return s2s.STTResult('Тест', .1, 0., True)
    local = pipeline(Recognizer(), config)
    async def forbidden(*_args):
        raise AssertionError('non-admitted recognition must not enter a turn')
    result = await local._recognize_turn('current', audio(), None, 0, 'speech_end', False, forbidden)
    assert result.ingress_status == ('stt_unknown' if kind == 'unknown' else 'stt_failure')
