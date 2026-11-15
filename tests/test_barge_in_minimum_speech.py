"""Synthetic barge-in contracts; no real recognizer, model, TTS or device."""

import asyncio

from app.foundation.s2s import (
    ConversationContext, EnergyVADAdapter, LocalS2SPipeline, PipelineMetrics,
    S2SConfig, STTResult, TurnResult,
)


VOICE = (0.2,) * 512
QUIET = (0.01,) * 512
SILENCE = (0.0,) * 512


class Conversation:
    def __init__(self, active=True, config=None):
        self.config = config or S2SConfig()
        self.metrics = PipelineMetrics()
        self.active = active
        self.cancellations = []
        self.turns = []

    def is_active(self, session_id):
        return self.active

    async def cancel_session(self, session_id):
        self.cancellations.append(session_id)
        # Keep reporting active to detect duplicate cancellations for one utterance.

    async def run_text_turn(self, text, context, **kwargs):
        self.turns.append(text)
        return TurnResult((), (), False)


class Recognizer:
    def __init__(self, block_first=False):
        self.audio = []
        self.block_first = block_first
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, audio, cancelled):
        self.audio.append(audio)
        self.started.set()
        if self.block_first and len(self.audio) == 1:
            await self.release.wait()
        if cancelled():
            raise asyncio.CancelledError
        return STTResult("fixture", .95, .1)


def setup(active=True, block_first=False, config=None):
    conversation = Conversation(active, config)
    recognizer = Recognizer(block_first)
    pipeline = LocalS2SPipeline(EnergyVADAdapter(), recognizer, conversation)
    context = ConversationContext("s", "fixture", "fixture", (), (), 2, 120, 2)
    return pipeline, conversation, recognizer, context


async def feed_frames(pipeline, context, frame, count):
    results = []
    for _ in range(count):
        results.append(await pipeline.feed_audio("s", frame, context, wait_for_turn=False))
    return results


async def end_speech(pipeline, context):
    await feed_frames(pipeline, context, SILENCE, 6)
    return await pipeline.feed_audio("s", SILENCE, context, wait_for_turn=False)


def test_short_spike_does_not_cancel_active_response():
    async def flow():
        pipeline, conversation, recognizer, context = setup()
        await feed_frames(pipeline, context, QUIET, 10)
        result = (await feed_frames(pipeline, context, VOICE, 1))[0]
        assert not result.barge_in and conversation.cancellations == []
        result = await end_speech(pipeline, context)
        assert result.ingress_status == "too_short" and not result.barge_in
        assert conversation.cancellations == [] and recognizer.audio == []
        assert not pipeline.ingress["s"].active
    asyncio.run(flow())


def test_confirmed_speech_cancels_once_before_stt_and_preserves_pcm():
    async def flow():
        pipeline, conversation, recognizer, context = setup()
        await feed_frames(pipeline, context, QUIET, 10)
        early = await feed_frames(pipeline, context, VOICE, 9)
        assert all(not result.barge_in for result in early)
        assert conversation.cancellations == []
        confirmed = (await feed_frames(pipeline, context, VOICE, 1))[0]
        assert confirmed.barge_in and confirmed.ingress_status == "collecting"
        assert conversation.cancellations == ["s"] and recognizer.audio == []
        later = await feed_frames(pipeline, context, VOICE, 2)
        later += await feed_frames(pipeline, context, SILENCE, 3)
        later += await feed_frames(pipeline, context, VOICE, 1)
        assert all(not result.barge_in for result in later)
        result = await end_speech(pipeline, context)
        assert result.pending is not None
        completed = await result.pending
        assert completed.stt is not None and completed.stt.text == "fixture"
        assert conversation.cancellations == ["s"] and conversation.turns == ["fixture"]
        assert recognizer.audio[0].samples == (
            QUIET * 10 + VOICE * 12 + SILENCE * 3 + VOICE + SILENCE * 7
        )
        assert recognizer.audio[0].token == pipeline.generations["s"] == 1
        assert sum(event["event"] == "barge_in" for event in conversation.metrics.events) == 1
        next_utterance = await feed_frames(pipeline, context, VOICE, 10)
        assert sum(result.barge_in for result in next_utterance) == 1
        assert conversation.cancellations == ["s", "s"]
    asyncio.run(flow())


def test_pending_recognition_survives_spike_and_cancels_only_at_minimum():
    async def flow():
        pipeline, conversation, recognizer, context = setup(active=False, block_first=True)
        await feed_frames(pipeline, context, VOICE, 10)
        first = await end_speech(pipeline, context)
        assert first.pending is not None
        await recognizer.started.wait()
        short = (await feed_frames(pipeline, context, VOICE, 1))[0]
        assert not short.barge_in and not first.pending.done()
        rejected = await end_speech(pipeline, context)
        assert rejected.ingress_status == "too_short" and not first.pending.done()
        early = await feed_frames(pipeline, context, VOICE, 9)
        assert all(not result.barge_in for result in early)
        assert not first.pending.done() and conversation.cancellations == []
        confirmed = (await feed_frames(pipeline, context, VOICE, 1))[0]
        assert confirmed.barge_in and first.pending.cancelled()
        assert conversation.cancellations == ["s"]
        second = await end_speech(pipeline, context)
        assert second.pending is not None
        completed = await second.pending
        assert completed.stt is not None and conversation.turns == ["fixture"]
        assert recognizer.audio[1].samples == VOICE * 10 + SILENCE * 7
        assert pipeline.pending_tasks == {} and pipeline.pending_started_at == {}
    asyncio.run(flow())


def test_terminal_reset_cancels_pending_work_below_speech_minimum():
    async def flow():
        pipeline, conversation, recognizer, context = setup(active=False, block_first=True)
        await feed_frames(pipeline, context, VOICE, 10)
        first = await end_speech(pipeline, context)
        assert first.pending is not None
        await recognizer.started.wait()
        conversation.active = True
        result = (await feed_frames(pipeline, context, VOICE, 1))[0]
        assert not result.barge_in and not first.pending.done()
        await asyncio.wait_for(pipeline.cancel_session("s"), .5)
        assert first.pending.cancelled() and conversation.cancellations == ["s"]
        assert pipeline.ingress == {} and pipeline.generations == {}
        assert pipeline.pending_tasks == {} and pipeline.pending_started_at == {}
        fresh = (await feed_frames(pipeline, context, VOICE, 1))[0]
        assert not fresh.barge_in and conversation.cancellations == ["s"]
    asyncio.run(flow())


def test_batched_confirmation_survives_utterance_reset_before_recognition():
    async def flow():
        pipeline, conversation, recognizer, context = setup()
        pcm = QUIET * 10 + VOICE * 10 + SILENCE * 7
        result = await pipeline.feed_audio("s", pcm, context)
        assert result.ingress_status == "speech_end" and result.barge_in
        assert conversation.cancellations == ["s"] and conversation.turns == ["fixture"]
        assert recognizer.audio[0].samples == pcm
        assert recognizer.audio[0].token == pipeline.generations["s"] == 1
    asyncio.run(flow())


def test_trailing_silence_does_not_confirm_short_speech():
    async def flow():
        pipeline, conversation, recognizer, context = setup(config=S2SConfig(min_speech_ms=100))
        results = await feed_frames(pipeline, context, VOICE, 1)
        results += await feed_frames(pipeline, context, SILENCE, 7)
        assert all(not result.barge_in for result in results)
        assert results[-1].ingress_status == "too_short"
        assert conversation.cancellations == [] and recognizer.audio == []
    asyncio.run(flow())


def test_confirmation_keeps_exact_integer_boundary_at_24khz():
    async def flow():
        config = S2SConfig(sample_rate=24_000, min_speech_ms=64, vad_preroll_ms=1)
        pipeline, conversation, recognizer, context = setup(config=config)
        await feed_frames(pipeline, context, SILENCE, 1)
        early = await feed_frames(pipeline, context, VOICE, 2)
        assert all(not result.barge_in for result in early)
        confirmed = (await feed_frames(pipeline, context, VOICE, 1))[0]
        assert confirmed.barge_in and conversation.cancellations == ["s"]
        results = await feed_frames(pipeline, context, SILENCE, 10)
        assert results[-1].pending is not None
        completed = await results[-1].pending
        assert completed.stt is not None
        assert recognizer.audio[0].samples == SILENCE[:24] + VOICE * 3 + SILENCE * 10
    asyncio.run(flow())
