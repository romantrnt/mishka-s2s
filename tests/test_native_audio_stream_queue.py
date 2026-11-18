"""Native audio ownership/overlap tests; synthetic PCM, no model claims."""
import asyncio

import pytest

from app.foundation.s2s import (
    AudioChunk, AudioSynthesisQueue, CachedTTSProvider, CancellationController,
    BoundedConversationEngine, ConversationContext, MockLLMProvider,
    MockPlaybackController, MockTTSProvider, PipelineMetrics, ResilientVoiceProvider, S2SConfig,
)


class NativeTTS:
    provider_name = "voxcpm765"

    def __init__(self, count=3):
        self.count = count
        self.closed = 0
        self.generated = 0
        self.finished = asyncio.Event()

    async def synthesize(self, *_args):
        raise AssertionError("native stream must not call full inference")

    async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk):
        try:
            for index in range(self.count):
                if cancelled():
                    raise asyncio.CancelledError
                self.generated += 1
                yield AudioChunk((.1 * (index + 1),) * 8, 8000, token)
            self.finished.set()
        finally:
            self.closed += 1


class Voice:
    def __init__(self):
        self.received = []

    async def convert(self, audio, cancelled):
        assert not cancelled()
        self.received.append(audio.samples[0])
        return AudioChunk(tuple(value / 2 for value in audio.samples), 8000, audio.token)


class Playback(MockPlaybackController):
    def __init__(self):
        super().__init__()
        self.first = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.records = []

    async def play(self, audio):
        self.records.append(audio)
        self.first.set()
        await self.release.wait()
        await super().play(audio)


async def start(tts=None, voice=None, playback=None, **config):
    tts, voice, playback = tts or NativeTTS(), voice or Voice(), playback or Playback()
    cancel = CancellationController()
    token = cancel.start("native-test")
    metrics = PipelineMetrics()
    queue = AudioSynthesisQueue(tts, voice, playback, cancel, S2SConfig(**config), metrics,
                                fallback_mode="passthrough")
    await queue.start("native-test", token)
    await queue.submit("Публичная тестовая фраза.")
    return queue, cancel, token, metrics, playback


@pytest.mark.asyncio
async def test_native_play_starts_before_source_end_and_converts_every_piece_in_order():
    tts, voice, playback = NativeTTS(5), Voice(), Playback()
    playback.release.clear()
    queue, _, token, metrics, _ = await start(CachedTTSProvider(tts), voice, playback)
    await asyncio.wait_for(playback.first.wait(), .5)
    assert not tts.finished.is_set()
    assert queue._ownership[0].state == "playing" and queue.completed_chunks == 0
    assert tts.generated <= 3  # one current + at most two buffered/in-flight
    assert await queue.wait_first_audio(.1)
    playback.release.set()
    await asyncio.wait_for(queue.finish(), .5)
    assert voice.received == pytest.approx([.1, .2, .3, .4, .5])
    assert [item.samples[0] for item in playback.records] == pytest.approx([.05, .1, .15, .2, .25])
    assert all(item.token == token for item in playback.records)
    assert queue.completed_chunks == 1 and queue._ownership[0].state == "finished"
    assert tts.closed == 1 and not queue._native_producer
    trace = next(iter(metrics.chunk_traces))
    assert trace["terminal_reason"] == "finished"
    assert queue.resource_snapshot()["pcm_bytes"] == 0


@pytest.mark.asyncio
async def test_native_rvc_failure_is_closed_even_with_legacy_passthrough_policy():
    class BrokenVoice(Voice):
        async def convert(self, *_args):
            raise RuntimeError("synthetic conversion failure")
    tts = NativeTTS()
    queue, _, _, _, playback = await start(tts, ResilientVoiceProvider(BrokenVoice()))
    with pytest.raises(RuntimeError, match="synthetic conversion failure"):
        await asyncio.wait_for(queue.finish(), .5)
    assert not playback.records and not queue.first_audio_ready.is_set()
    assert queue.failed_chunks == 1 and tts.closed == 1
    assert not queue._native_producer and queue.resource_snapshot()["pcm_bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["token", "session", "empty", "nan", "oversized", "count"])
async def test_corrupt_or_unbounded_native_output_never_reports_ready(fault):
    class Corrupt(NativeTTS):
        async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk):
            try:
                if fault == "count":
                    for _ in range(3):
                        yield AudioChunk((.1,), 8000, token)
                    return
                samples = {"empty": (), "nan": (float("nan"),), "oversized": (.1,) * 8001}.get(fault, (.1,))
                yield AudioChunk(samples, 8000, token + (fault == "token"),
                                 "other" if fault == "session" else None)
            finally:
                self.closed += 1
    tts = Corrupt()
    queue, _, _, _, playback = await start(tts, max_turn_seconds=1, max_tts_chunks_per_generation=2)
    with pytest.raises(ValueError):
        await asyncio.wait_for(queue.finish(), .5)
    if fault != "count":
        assert not queue.first_audio_ready.is_set() and not playback.records
    assert tts.closed == 1 and queue.failed_chunks == 1 and not queue._native_producer


@pytest.mark.asyncio
async def test_hangup_closes_iterator_joins_child_and_allows_repeated_start():
    tts, playback = NativeTTS(20), Playback()
    playback.release.clear()
    queue, cancel, _, _, _ = await start(tts, playback=playback)
    await asyncio.wait_for(playback.first.wait(), .5)
    await asyncio.wait_for(queue.abort("native-test"), .5)
    assert tts.closed == 1 and queue.worker.done()
    assert not queue._native_producer and queue.resource_snapshot()["pcm_bytes"] == 0
    assert queue.cancelled_chunks == 1 and playback.stops == 1
    generated = tts.generated
    await asyncio.sleep(0)
    assert tts.generated == generated
    playback.release.set()
    token = cancel.start("native-test")
    tts.count = 2
    await queue.start("native-test", token)
    await queue.submit("Новая тестовая фраза.")
    await asyncio.wait_for(queue.finish(), .5)
    assert queue.completed_chunks == 1 and tts.closed == 2
    assert [audio.token for audio in playback.played] == [token, token]
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("s2s-native-")]


@pytest.mark.asyncio
async def test_native_source_timeout_closes_iterator_without_audio():
    class Stalled(NativeTTS):
        async def synthesize_audio_stream(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
                yield  # pragma: no cover
            finally:
                self.closed += 1
    tts = Stalled()
    queue, _, _, _, playback = await start(tts, tts_timeout_s=.02)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(queue.finish(), .5)
    assert tts.closed == 1 and not playback.records and not queue.first_audio_ready.is_set()
    assert not queue._native_producer


@pytest.mark.asyncio
async def test_replacing_generation_during_conversion_cannot_set_first_audio_ready():
    entered, release = asyncio.Event(), asyncio.Event()
    class LateVoice(Voice):
        async def convert(self, audio, cancelled):
            entered.set()
            await release.wait()
            return audio
    queue, cancel, _, _, playback = await start(voice=LateVoice())
    await asyncio.wait_for(entered.wait(), .5)
    fresh = cancel.start("native-test")
    release.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(queue.finish(), .5)
    assert not queue.first_audio_ready.is_set() and not playback.records
    assert cancel.tokens["native-test"] == fresh
    assert not queue._native_producer


@pytest.mark.asyncio
async def test_native_raw_float_pcm_reaches_converter_unchanged():
    class Raw(NativeTTS):
        async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk):
            yield AudioChunk((1.2, -.4), 8000, token)
    voice = Voice()
    queue, _, _, _, playback = await start(Raw(), voice)
    await asyncio.wait_for(queue.finish(), .5)
    assert voice.received == [1.2]
    assert playback.records[0].samples == (.6, -.2)


@pytest.mark.asyncio
async def test_playback_error_closes_native_iterator_and_joins_producer():
    class BrokenPlayback(Playback):
        async def play(self, audio):
            raise RuntimeError("synthetic playback failure")
    tts = NativeTTS(20)
    queue, _, _, _, _ = await start(tts, playback=BrokenPlayback())
    with pytest.raises(RuntimeError, match="synthetic playback failure"):
        await asyncio.wait_for(queue.finish(), .5)
    assert tts.closed == 1 and not queue._native_producer
    assert queue.resource_snapshot()["pcm_bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_real_browser_transport_binds_current_session_and_trace_generation(native):
    from app.foundation.stand import BrowserPlaybackController
    class Hub:
        def __init__(self):
            self.sent = []
        async def publish(self, payload):
            self.sent.append(payload)
    class Metrics(PipelineMetrics):
        def __init__(self):
            super().__init__()
            self.proxy_tags = []
        def mark_voice(self, session_id, boundary, *, generation_id=None):
            if boundary == "proxy_send":
                self.proxy_tags.append((session_id, generation_id))
            super().mark_voice(session_id, boundary, generation_id=generation_id)
    hub, metrics, cancel = Hub(), Metrics(), CancellationController()
    playback = BrowserPlaybackController(hub)
    playback.metrics = metrics
    token = cancel.start("stand")
    queue = AudioSynthesisQueue(NativeTTS(2) if native else MockTTSProvider(), Voice(),
        playback, cancel, S2SConfig(), metrics, trace_generation_id=41)
    await queue.start("stand", token)
    await queue.submit("Тестовая фраза.")
    await asyncio.wait_for(queue.finish(), .5)
    assert hub.sent and all(packet["type"] == "audio_pcm_output" for packet in hub.sent)
    assert all(packet["session_id"] == "stand" and packet["generation"] == 0
               and packet["token"] == token for packet in hub.sent)
    assert metrics.proxy_tags and set(metrics.proxy_tags) == {("stand", 41)}


class RecordingNativeTTS(NativeTTS):
    def __init__(self):
        super().__init__(1)
        self.texts = []

    async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk):
        self.texts.append(text)
        async for audio in super().synthesize_audio_stream(text, token, cancelled, first_chunk=first_chunk):
            yield audio


def conversation():
    return ConversationContext("native-test", "canonical_product", "bear_dialogue",
                               ("__all__",), ("diagnosis", "medicine"), 2, 120, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_second", [False, True])
async def test_native_closed_first_sentence_plays_before_second_tag_and_later_gate_stays_closed(unsafe_second):
    from app.foundation.config import Settings
    from app.foundation.validation import AsyncResponseValidator
    release = asyncio.Event()
    class StagedLLM:
        async def stream(self, messages, request_id, cancelled):
            self.messages = messages
            yield "<say>Привет, друг.</say>"
            await release.wait()
            yield "<say>" + ("Не рассказывай взрослым." if unsafe_second else "Давай поиграем.") + "</say>"
    class Gate(AsyncResponseValidator):
        def __init__(self):
            super().__init__()
            self.candidates = []
        async def validate(self, text, **kwargs):
            self.candidates.append(text)
            return await super().validate(text, **kwargs)
    tts, playback, llm, gate = RecordingNativeTTS(), Playback(), StagedLLM(), Gate()
    engine = BoundedConversationEngine(llm, CachedTTSProvider(tts), Voice(), playback,
        S2SConfig(first_audio_gate_s=.2), settings=Settings(profile="mock"))
    engine.response_validator = gate
    task = asyncio.create_task(engine.run_text_turn("Привет", conversation()))
    try:
        await asyncio.wait_for(playback.first.wait(), .5)
        assert not release.is_set() and not task.done()
        assert tts.texts == ["Привет, друг."]
        assert "emit only complete <say>...</say> blocks" in llm.messages[0]["content"]
        release.set()
        result = await asyncio.wait_for(task, .5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    second = "Не рассказывай взрослым." if unsafe_second else "Давай поиграем."
    assert gate.candidates == ["Привет, друг.", "Привет, друг. " + second]
    if unsafe_second:
        assert result.fallback_reason == "provider_or_validation_failure"
        assert tts.texts == ["Привет, друг."] and len(playback.records) == 1
    else:
        assert result.fallback_reason is None
        assert tts.texts == ["Привет, друг.", second] and len(playback.records) == 2
    assert not engine.active_queues and not engine.active_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_only_native_route_splits_sentences_inside_a_completed_say_block(native):
    from app.foundation.config import Settings
    class LLM(MockLLMProvider):
        async def stream(self, messages, request_id, cancelled):
            self.messages = messages
            async for value in super().stream(messages, request_id, cancelled):
                yield value
    llm = LLM(tokens=["<say>Привет, друг. Давай поиграем.</say>"])
    tts = RecordingNativeTTS() if native else MockTTSProvider()
    engine = BoundedConversationEngine(llm, tts, Voice(), Playback(), settings=Settings(profile="mock"))
    result = await engine.run_text_turn("Привет", conversation())
    assert result.fallback_reason is None
    assert result.chunks == (("Привет, друг.", "Давай поиграем.") if native
                             else ("Привет, друг. Давай поиграем.",))
    assert "one short complete sentence per <say>...</say> block" not in llm.messages[0]["content"]
