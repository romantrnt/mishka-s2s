"""Actual bounded engine, synthetic speech only, no live provider."""
import json
import pytest
from app.foundation.config import Settings
from app.foundation.s2s import (BoundedConversationEngine, CachedTTSProvider,
    MockLLMProvider, MockTTSProvider, MockPlaybackController, RVCProvider, S2SConfig)
from test_s2s import context


@pytest.mark.asyncio
async def test_guest_dialogue_leaves_shared_tts_caches_empty():
    raw = MockTTSProvider()
    inner, outer = CachedTTSProvider(raw), None
    outer = CachedTTSProvider(inner)
    engine = BoundedConversationEngine(MockLLMProvider(), outer, RVCProvider(),
        MockPlaybackController(), S2SConfig(), settings=Settings(profile='mock', voice_conversion='off'))
    result = await engine.run_text_turn('Привет', context())
    assert result.chunks and not result.fallback_reason
    assert outer.cache == inner.cache == {}
    assert engine.tts is outer


@pytest.mark.asyncio
async def test_provider_valueerror_body_is_not_retained_in_metrics_or_logs(caplog):
    class Broken(MockLLMProvider):
        async def stream(self, *args, **kwargs):
            raise ValueError('SYNTHETIC_PRIVATE_PROVIDER_BODY')
            yield ''
    engine = BoundedConversationEngine(Broken(), CachedTTSProvider(MockTTSProvider()), RVCProvider(),
        MockPlaybackController(), S2SConfig(), settings=Settings(profile='mock', voice_conversion='off'))
    result = await engine.run_text_turn('Привет', context())
    assert result.fallback_reason == 'provider_or_validation_failure'
    assert 'SYNTHETIC_PRIVATE_PROVIDER_BODY' not in json.dumps(list(engine.metrics.events))
    assert 'SYNTHETIC_PRIVATE_PROVIDER_BODY' not in caplog.text
    assert any(e.get('reason') == 'validation_exception' for e in engine.metrics.events)


@pytest.mark.asyncio
async def test_legacy_voice_passthrough_failure_keeps_body_out_of_logs_and_metrics(caplog):
    from app.foundation.s2s import ResilientVoiceProvider, AudioChunk, AudioSynthesisQueue, CancellationController, PipelineMetrics
    class Broken:
        async def convert(self, *args):
            raise RuntimeError('SYNTHETIC_PRIVATE_RVC_BODY')
    audio = AudioChunk((.1,) * 32, 24000, 1)
    wrapper = ResilientVoiceProvider(Broken())
    assert await wrapper.convert(audio, lambda:False) is audio
    assert wrapper.last_fallback_reason == 'RuntimeError'
    cancel = CancellationController()
    token = cancel.start('synthetic')
    metrics = PipelineMetrics()
    queue = AudioSynthesisQueue(MockTTSProvider(), Broken(), MockPlaybackController(),
        cancel, S2SConfig(), metrics, voice_mode='rvc_v2', fallback_mode='passthrough')
    await queue.start('synthetic', token)
    await queue.submit('Синтетическая реплика о деревьях.')
    await queue.finish()
    assert queue.last_fallback_reason == 'RuntimeError'
    assert 'SYNTHETIC_PRIVATE_RVC_BODY' not in caplog.text
    assert 'SYNTHETIC_PRIVATE_RVC_BODY' not in json.dumps(list(metrics.events))
