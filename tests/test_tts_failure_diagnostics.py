"""A queue failure retains its cause without retaining speech or raw errors."""
import asyncio

import pytest

from app.foundation.s2s import (
    AudioChunk, AudioSynthesisQueue, CancellationController, MockPlaybackController,
    PipelineMetrics, RVCProvider, S2SConfig,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,code,cause_class", [
    ("empty", "silero_empty_audio", "ValueError"),
    ("private", "provider_failure", "ValueError"),
    ("custom", "provider_failure", "other"),
])
async def test_second_chunk_failure_records_safe_cause_and_keeps_invalidation(kind, code, cause_class):
    class PrivateError(Exception):
        pass
    cause = (ValueError("Silero audio is empty") if kind == "empty" else
             ValueError("private generated response Bearer secret") if kind == "private" else
             PrivateError("private generated response Bearer secret"))
    failure = RuntimeError("Silero TTS synthesis failed")
    failure.__cause__ = cause
    class TTS:
        calls = 0
        async def synthesize(self, text, token, cancelled):
            self.calls += 1
            if self.calls == 2:
                raise failure
            return AudioChunk((.1, -.2), 24000, token)
    cancel, metrics, playback = CancellationController(), PipelineMetrics(), MockPlaybackController()
    token = cancel.start("safe-failure")
    queue = AudioSynthesisQueue(TTS(), RVCProvider(), playback, cancel, S2SConfig(), metrics, voice_mode="off")
    await queue.start("safe-failure", token)
    try:
        await queue.submit("Первая синтетическая реплика.")
        assert await queue.wait_first_audio(.5)
        await queue.submit("private generated response")
        await asyncio.wait_for(queue.failure_event.wait(), .5)
        await queue.worker
        assert queue.error is failure and queue.closed and queue.queue.empty()
        assert cancel.tokens["safe-failure"] == token + 1
        assert queue.completed_chunks == 1 and queue.failed_chunks == 1
        assert len(playback.played) == 1
        [item] = [item for item in metrics.events if item["event"] == "audio_generation_failed"]
        assert item["stage"] == "silero_tts" and item["chunk_index"] == 1
        assert item["text_chars"] == len("private generated response")
        assert item["exception_class"] == "RuntimeError" and item["cause_class"] == cause_class
        assert item["code"] == code
        assert set(item) == {"event", "at", "stage", "chunk_index", "text_chars",
                             "exception_class", "cause_class", "code"}
        assert "private generated" not in str(item) and "secret" not in str(item)
    finally:
        await queue.abort("safe-failure", invalidate=False)
