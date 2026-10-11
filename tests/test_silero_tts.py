import asyncio

from app.foundation.s2s import RVCV2Provider, SileroTTSProvider
from app.foundation.tts_engine import SileroTTSEngine


def test_silero_normalization_and_contract():
    assert SileroTTSEngine.normalize("  Привет\n   мир  ") == "Привет мир"
    try:
        SileroTTSEngine.normalize(" \t ")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty text must be rejected")


def test_silero_package_fallback_keeps_model_when_to_returns_none(tmp_path):
    class FakeModel:
        def to(self, device):
            self.device = str(device)
            return None

        def eval(self):
            return None

        def apply_tts(self, **kwargs):
            import torch
            assert kwargs["speaker"] == "kseniya"
            return torch.tensor([0.1, -0.1], dtype=torch.float32)

    engine = SileroTTSEngine(cache_path=str(tmp_path / "silero.pt"), device="cpu")
    engine._load_from_hub = lambda torch: (_ for _ in ()).throw(RuntimeError("stale hub"))
    engine._load_from_package = lambda torch: FakeModel()
    samples, rate = engine.synthesize_blocking("Привет")
    assert rate == 48000
    assert len(samples) == 2
    assert samples == (0.10000000149011612, -0.10000000149011612)
    assert engine.readiness()["load_source"] == "package_cache"


def test_silero_provider_has_bounded_worker_and_cancellation_contract():
    class FakeEngine:
        def readiness(self):
            return {"ready": True, "provider": "silero"}

        def synthesize_blocking(self, text):
            return (0.1, -0.1), 48000

    provider = SileroTTSProvider(FakeEngine())
    audio = asyncio.run(provider.synthesize("Привет", 1, lambda: False))
    assert audio.sample_rate == 48000
    assert len(audio.samples) == 2
    snapshot = provider.native_worker_snapshot()
    assert snapshot["active_jobs"] == 0
    assert snapshot["queued_jobs"] == 0


def test_rvc_production_pitch_shift_is_explicit():
    provider = RVCV2Provider(converter=lambda samples, rate: samples, f0_up_key=0,
                             filter_radius=3, index_rate=.70, protect=.33)
    assert provider.f0_up_key == 0
    assert provider.filter_radius == 3
    assert provider.index_rate == .70
    assert provider.protect == .33


def test_silero_stream_preroll_is_only_on_first_chunk():
    class FakeEngine:
        def synthesize_streaming_blocking(self, text, *, first_chunk):
            return ((0.0,) * 7200 + (0.25, -0.25) if first_chunk else (0.25, -0.25)), 48000

    provider = SileroTTSProvider(FakeEngine())

    async def run_chunks():
        first = await provider.synthesize_stream_chunk("Привет", 1, lambda: False, first_chunk=True)
        second = await provider.synthesize_stream_chunk("мир", 1, lambda: False, first_chunk=False)
        return first, second

    first, second = asyncio.run(run_chunks())
    assert len(first.samples) == 7202
    assert first.samples[:7200] == (0.0,) * 7200
    assert first.samples[-2:] == (0.25, -0.25)
    assert second.samples == (0.25, -0.25)
