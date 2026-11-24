"""Held native-call probes only; no Whisper package, model, GPU or child audio."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.foundation.s2s import AudioChunk, FasterWhisperRecognizer, S2SConfig


def audio(token=1):
    return AudioChunk((.1,) * 1600, 16000, token)


class HeldModel:
    def __init__(self):
        self.release = threading.Event()
        self.two_started = threading.Event()
        self.lock = threading.Lock()
        self.calls = self.active = self.peak = 0
        self.options = []

    def transcribe(self, samples, **options):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.options.append(options)
            if self.calls >= 2:
                self.two_started.set()
        try:
            assert self.release.wait(3), 'test must release the held native provider'
            return ([SimpleNamespace(text='Тест', no_speech_prob=0., avg_logprob=0.)], None)
        finally:
            with self.lock:
                self.active -= 1


async def drain(recognizer, tasks, release):
    release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(*recognizer._native_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['timeout', 'cancel'])
async def test_abandoned_native_calls_keep_slots_and_new_requests_cannot_grow_threads(ending):
    model = HeldModel()
    recognizer = FasterWhisperRecognizer(lambda: model, S2SConfig(stt_timeout_s=.04 if ending == 'timeout' else 2))
    recognizer._model = model
    tasks = [asyncio.create_task(recognizer.transcribe(audio(i), lambda: False)) for i in (1, 2)]
    try:
        assert await asyncio.to_thread(model.two_started.wait, 1)
        if ending == 'timeout':
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(result, RuntimeError) for result in results)
        else:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert recognizer.native_worker_snapshot()['active_jobs'] == 2
        extra = [asyncio.create_task(recognizer.transcribe(audio(i), lambda: False)) for i in range(3, 9)]
        tasks.extend(extra)
        await asyncio.sleep(.03)  # Give actual to_thread submissions a scheduling opportunity.
        assert model.calls == model.peak == 2, 'timed-out/cancelled waiters must not free native slots'
        assert recognizer.native_worker_snapshot()['active_jobs'] == 2
        assert recognizer.native_worker_snapshot()['queued_jobs'] == 0
        assert all(task.done() and isinstance(task.exception(), RuntimeError) for task in extra)
        model.release.set()
        await asyncio.gather(*recognizer._native_tasks, return_exceptions=True)
        assert recognizer.native_worker_snapshot()['active_jobs'] == 0
        fresh = await recognizer.transcribe(audio(9), lambda: False)
        assert fresh.text == 'Тест' and not fresh.unknown and model.calls == 3
    finally:
        await drain(recognizer, tasks, model.release)


@pytest.mark.asyncio
async def test_two_ordinary_recognitions_keep_parallel_single_call_fast_path():
    model = HeldModel()
    recognizer = FasterWhisperRecognizer(lambda: model, S2SConfig())
    recognizer._model = model
    tasks = [asyncio.create_task(recognizer.transcribe(audio(i), lambda: False)) for i in (1, 2)]
    try:
        assert await asyncio.to_thread(model.two_started.wait, 1)
        assert model.calls == model.peak == 2
        model.release.set()
        results = await asyncio.gather(*tasks)
        assert [(value.text, value.confidence, value.unknown) for value in results] == [('Тест', 1., False)] * 2
        assert model.options == [{'language': 'ru', 'beam_size': 3, 'vad_filter': False,
                                  'condition_on_previous_text': False}] * 2
        assert recognizer.native_worker_snapshot()['active_jobs'] == 0
    finally:
        await drain(recognizer, tasks, model.release)


@pytest.mark.asyncio
async def test_cold_warmup_and_recognition_share_one_native_model_load():
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    loads, instances = [], []

    class Model:
        def transcribe(self, *_args, **_kwargs):
            return ([SimpleNamespace(text='Тест', no_speech_prob=0., avg_logprob=0.)], None)

    def loader():
        with lock:
            loads.append(True)
            entered.set()
        assert release.wait(3)
        value = Model()
        instances.append(value)
        return value

    recognizer = FasterWhisperRecognizer(loader, S2SConfig())
    tasks = [asyncio.create_task(recognizer.warmup()),
             asyncio.create_task(recognizer.transcribe(audio(), lambda: False))]
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(.03)
        assert len(loads) == 1, 'cold concurrent entry must not allocate two model owners'
        assert recognizer.native_worker_snapshot()['active_jobs'] == 1
        release.set()
        warmed, result = await asyncio.gather(*tasks)
        assert warmed['ok'] and result.text == 'Тест'
        assert len(instances) == 1 and recognizer._model is instances[0]
    finally:
        await drain(recognizer, tasks, release)


@pytest.mark.asyncio
async def test_cancelled_cold_waiter_does_not_release_or_duplicate_model_load():
    entered, release = threading.Event(), threading.Event()
    loads = []
    model = SimpleNamespace(transcribe=lambda *_args, **_kwargs:
        ([SimpleNamespace(text='Тест', no_speech_prob=0., avg_logprob=0.)], None))
    def loader():
        loads.append(True)
        entered.set()
        assert release.wait(3)
        return model
    recognizer = FasterWhisperRecognizer(loader, S2SConfig())
    first = asyncio.create_task(recognizer.warmup())
    tasks = [first]
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(recognizer.transcribe(audio(), lambda: False))
        tasks.append(second)
        await asyncio.sleep(.03)
        assert len(loads) == 1 and recognizer.native_worker_snapshot()['active_jobs'] == 1
        release.set()
        assert (await second).text == 'Тест' and recognizer._model is model
    finally:
        await drain(recognizer, tasks, release)


@pytest.mark.asyncio
async def test_failed_shared_load_can_be_retried_without_a_resident_failed_future():
    calls = []
    model = SimpleNamespace(transcribe=lambda *_args, **_kwargs:
        ([SimpleNamespace(text='Тест', no_speech_prob=0., avg_logprob=0.)], None))
    def loader():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('synthetic load failure')
        return model
    recognizer = FasterWhisperRecognizer(loader, S2SConfig())
    assert (await recognizer.warmup())['ok'] is False
    assert (await recognizer.warmup())['ok'] is True
    assert len(calls) == 2 and recognizer._model is model
    assert (await recognizer.transcribe(audio(), lambda: False)).text == 'Тест'
    assert recognizer.native_worker_snapshot()['active_jobs'] == 0
