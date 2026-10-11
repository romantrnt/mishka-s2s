import asyncio

import pytest

from app.foundation.s2s import MockTTSProvider


def test_mock_tts_returns_audible_duration_not_one_sample_per_character():
    async def run():
        chunk = await MockTTSProvider().synthesize("Тестовая реплика.", 1, lambda: False)
        return chunk

    chunk = asyncio.run(run())
    assert chunk.sample_rate == 24_000
    assert len(chunk.samples) >= 2_000
    assert len(chunk.samples) / chunk.sample_rate >= 0.12
    assert max(abs(sample) for sample in chunk.samples) > 0.05
    assert len(set(chunk.samples)) > 100


def test_mock_tts_cancellation_stops_before_audio_is_emitted():
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await MockTTSProvider().synthesize("длинный ответ", 1, lambda: True)

    asyncio.run(run())
