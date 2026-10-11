import asyncio

import pytest

from app.foundation.s2s import (
    AudioSynthesisQueue,
    CancellationController,
    MockPlaybackController,
    MockTTSProvider,
    PipelineMetrics,
    RVCProvider,
    S2SConfig,
)


def _queue(tts, playback, config=None):
    config = config or S2SConfig(queue_size=2)
    cancel = CancellationController()
    token = cancel.start("p1-6")
    queue = AudioSynthesisQueue(
        tts,
        RVCProvider(),
        playback,
        cancel,
        config,
        PipelineMetrics(),
    )
    return queue, cancel, token


def test_p1_6_normal_playback_owns_pcm_until_finished():
    started = asyncio.Event()
    release = asyncio.Event()

    class GatedPlayback(MockPlaybackController):
        async def play(self, audio):
            started.set()
            await release.wait()
            await super().play(audio)

    playback = GatedPlayback()

    async def flow():
        queue, _cancel, token = _queue(MockTTSProvider(), playback)
        await queue.start("p1-6", token)
        await queue.submit("owned audio")
        await started.wait()
        active = queue.resource_snapshot()
        assert active["queued_bytes"] == 0
        assert active["active_audio_bytes"] > 0
        assert active["pcm_bytes"] == active["active_audio_bytes"]
        release.set()
        await queue.finish()
        finished = queue.resource_snapshot()
        assert finished["queued_bytes"] == 0
        assert finished["active_audio_bytes"] == 0
        assert finished["cancelled_chunks"] == 0
        assert finished["failed_chunks"] == 0
        assert finished["completed_chunks"] == 1
        assert finished["ownership_states"] == {"finished": 1}

    asyncio.run(asyncio.wait_for(flow(), 1))


def test_p1_6_drain_and_repeated_abort_are_idempotent():
    started = asyncio.Event()

    class HangingTTS:
        async def synthesize(self, _text, _token, _cancelled):
            started.set()
            await asyncio.Event().wait()

    async def flow():
        queue, _cancel, token = _queue(HangingTTS(), MockPlaybackController())
        await queue.start("p1-6", token)
        await queue.submit("in flight")
        await queue.submit("queued")
        await started.wait()
        await queue.abort("p1-6", invalidate=False)
        first = queue.resource_snapshot()
        await queue.abort("p1-6", invalidate=False)
        second = queue.resource_snapshot()
        assert first == second
        assert second["queued_chunks"] == 0
        assert second["queued_bytes"] == 0
        assert second["active_audio_bytes"] == 0
        assert second["pcm_bytes"] == 0
        assert second["cancelled_chunks"] == 2
        assert second["completed_chunks"] == 0
        assert second["failed_chunks"] == 0

    asyncio.run(asyncio.wait_for(flow(), 1))


def test_p1_6_provider_failure_marks_work_and_drains_without_negative_counters():
    async def flow():
        queue, _cancel, token = _queue(MockTTSProvider(fail=True), MockPlaybackController())
        await queue.start("p1-6", token)
        await queue.submit("first")
        await queue.submit("second")
        await queue.failure_event.wait()
        snapshot = queue.resource_snapshot()
        assert snapshot["queued_chunks"] == 0
        assert snapshot["queued_bytes"] == 0
        assert snapshot["active_audio_bytes"] == 0
        assert snapshot["cancelled_chunks"] == 0
        assert snapshot["failed_chunks"] == 2
        assert all(value >= 0 for key, value in snapshot.items() if key.endswith("bytes") or key.endswith("chunks"))
        with pytest.raises(RuntimeError):
            await queue.finish()

    asyncio.run(asyncio.wait_for(flow(), 1))


def test_p1_6_repeated_generations_do_not_drift_ownership_counters():
    async def flow():
        queue, _cancel, _token = _queue(MockTTSProvider(), MockPlaybackController())
        for generation in range(20):
            token = queue.cancel.start("p1-6")
            await queue.start("p1-6", token)
            await queue.submit(f"turn-{generation}")
            await queue.finish()
            snapshot = queue.resource_snapshot()
            assert snapshot["queued_bytes"] == 0
            assert snapshot["active_audio_bytes"] == 0
            assert snapshot["cancelled_chunks"] == 0
            assert snapshot["failed_chunks"] == 0
            assert snapshot["completed_chunks"] == 1
            assert snapshot["ownership_size"] == 1

    asyncio.run(asyncio.wait_for(flow(), 3))


