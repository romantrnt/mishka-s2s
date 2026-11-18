"""LLM generation deadlines must not consume validation or audio budgets."""
import asyncio
from types import SimpleNamespace

import pytest

from app.foundation.config import Settings
from app.foundation.s2s import (
    BoundedConversationEngine, ConversationContext, MockLLMProvider,
    MockPlaybackController, MockTTSProvider, RVCProvider, S2SConfig,
)
from app.foundation.observability import Metrics


def context():
    return ConversationContext("budget-session", "canonical_product", "bear_dialogue",
                               ("__all__",), ("diagnosis", "medicine"), 2, 120, 2)


@pytest.mark.asyncio
async def test_completed_llm_does_not_timeout_during_validation_and_first_audio():
    class SlowTTS(MockTTSProvider):
        async def synthesize(self, text, token, cancelled):
            await asyncio.sleep(.06)
            return await super().synthesize(text, token, cancelled)

    class SlowValidation:
        timeout_s = 1.0
        async def validate(self, *_args, **_kwargs):
            await asyncio.sleep(.04)
            return SimpleNamespace(blocked=False, source="synthetic", latency_ms=40)

    playback = MockPlaybackController()
    engine = BoundedConversationEngine(
        MockLLMProvider(tokens=["<say>Тестовая фраза.</say>"]), SlowTTS(), RVCProvider(), playback,
        S2SConfig(llm_timeout_s=.025, first_audio_gate_s=.2, max_turn_seconds=1),
        settings=Settings(profile="mock"))
    engine.response_validator = SlowValidation()
    result = await engine.run_text_turn("fixture", context())
    assert result.fallback_reason is None
    assert result.chunks == ("Тестовая фраза.",) and playback.played
    assert not engine.active_tasks and not engine.active_queues


@pytest.mark.asyncio
async def test_slow_provider_keeps_a_cumulative_deadline_and_does_not_cancel_caller():
    closed = asyncio.Event()
    class SlowLLM:
        async def stream(self, messages, request_id, cancelled):
            try:
                for token in ("<say>", "Тестовая ", "фраза.</say>"):
                    await asyncio.sleep(.02)
                    yield token
            finally:
                closed.set()

    playback = MockPlaybackController()
    engine = BoundedConversationEngine(SlowLLM(), MockTTSProvider(), RVCProvider(), playback,
        S2SConfig(llm_timeout_s=.03, max_turn_seconds=1), settings=Settings(profile="mock"))
    result = await engine.run_text_turn("fixture", context())
    assert result.fallback_reason == "voice_generation_timeout"
    assert closed.is_set() and not playback.played
    assert not asyncio.current_task().cancelling()
    assert not engine.active_tasks and not engine.active_queues


@pytest.mark.asyncio
async def test_total_turn_deadline_still_bounds_downstream_validation():
    class SlowValidation:
        timeout_s = 1.0
        async def validate(self, *_args, **_kwargs):
            await asyncio.sleep(1)
            return SimpleNamespace(blocked=False, source="synthetic", latency_ms=1000)
    playback = MockPlaybackController()
    engine = BoundedConversationEngine(
        MockLLMProvider(tokens=["<say>Тестовая фраза.</say>"]), MockTTSProvider(), RVCProvider(), playback,
        S2SConfig(llm_timeout_s=1, max_turn_seconds=.03), settings=Settings(profile="mock"))
    engine.response_validator = SlowValidation()
    result = await asyncio.wait_for(engine.run_text_turn("fixture", context()), .5)
    assert result.fallback_reason == "turn_timeout" and not playback.played
    assert not engine.active_tasks and not engine.active_queues


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_path", ["session", "queue"])
async def test_cancelled_queue_retires_before_native_drain_without_watchdog_mismatch(cancel_path):
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class DrainingTTS:
        async def synthesize(self, text, token, cancelled):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                draining.set()
                await release.wait()
                raise
    playback = MockPlaybackController()
    engine = BoundedConversationEngine(
        MockLLMProvider(tokens=["<say>Тестовая фраза.</say>"]), DrainingTTS(), RVCProvider(), playback,
        S2SConfig(first_audio_gate_s=1), settings=Settings(profile="mock"))
    turn = asyncio.create_task(engine.run_text_turn("fixture", context()))
    await asyncio.wait_for(entered.wait(), .5)
    queue = engine.active_queues["budget-session"]
    cleanup = asyncio.create_task(engine.cancel_session("budget-session") if cancel_path == "session"
                                  else queue.abort("budget-session"))
    try:
        await asyncio.wait_for(draining.wait(), .5)
        assert not cleanup.done() and not queue.worker.done()
        assert "budget-session" not in engine.active_queues
        generations = {sid: (item.token, engine.cancel.tokens.get(sid))
                       for sid, item in engine.active_queues.items()}
        check = Metrics().watchdog_check(0, queue_generations=generations)
        assert check["stale_queue_generations"] == []
    finally:
        release.set()
        await asyncio.wait_for(cleanup, .5)
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
    assert not engine.active_tasks and not engine.active_queues
    engine.tts = MockTTSProvider()
    fresh = await engine.run_text_turn("fixture", context())
    assert fresh.fallback_reason is None and len(playback.played) == 1
