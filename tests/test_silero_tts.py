import asyncio
from xml.etree import ElementTree
import pytest

from app.foundation.s2s import RVCV2Provider, SileroTTSProvider
from app.foundation.tts_engine import SileroTTSEngine


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [
    (.1, -.1, .05, 0.),
    (1., -1., .5, 0.),
    (1.0080755949020386, -1.0080755949020386, .5040377974510193, 0.),
])
async def test_silero_owned_synthesis_attenuates_only_finite_overload(samples):
    import torch
    tensor = torch.tensor(samples, dtype=torch.float32)
    class Model:
        def apply_tts(self, **kwargs):
            assert "Голубая ленточка." in kwargs["text"]
            return tensor
    engine = SileroTTSEngine()
    engine._model = Model()
    provider = SileroTTSProvider(engine)
    audio = await provider.synthesize("Голубая ленточка. Найди такой же цвет.", 7, lambda: False)
    original = tuple(tensor.tolist())
    peak = max(map(abs, original))
    expected = tuple(value / peak for value in original) if peak > 1. else original
    assert audio.token == 7 and audio.sample_rate == 48000
    assert audio.samples[:7200] == (0.,) * 7200
    assert audio.samples[7200:] == expected
    assert max(map(abs, audio.samples)) <= 1.


@pytest.mark.asyncio
@pytest.mark.parametrize("samples,reason", [
    ((), "Silero audio is empty"),
    ((float("nan"), .1), "Silero audio contains non-finite samples"),
    ((.1, float("inf")), "Silero audio contains non-finite samples"),
    ((float("-inf"), 1.01), "Silero audio contains non-finite samples"),
])
async def test_silero_owned_synthesis_still_rejects_invalid_model_audio(samples, reason):
    import torch
    class Model:
        def apply_tts(self, **kwargs):
            return torch.tensor(samples, dtype=torch.float32)
    engine = SileroTTSEngine()
    engine._model = Model()
    with pytest.raises(RuntimeError, match="Silero TTS synthesis failed") as failure:
        await SileroTTSProvider(engine).synthesize("Публичная игровая реплика.", 7, lambda: False)
    assert isinstance(failure.value.__cause__, ValueError)
    assert str(failure.value.__cause__) == reason


def test_silero_normalization_and_contract():
    speech = ElementTree.fromstring(SileroTTSEngine.normalize("  Привет\n   мир  "))
    assert speech.tag == "speak" and speech.find("s") is not None
    assert "".join(speech.itertext()) == "Привет мир"
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
            assert kwargs["speaker"] == "aidar"
            return torch.tensor([0.1, -0.1], dtype=torch.float32)

    engine = SileroTTSEngine(cache_path=str(tmp_path / "silero.pt"), device="cpu")
    engine._load_from_hub = lambda torch: (_ for _ in ()).throw(RuntimeError("stale hub"))
    engine._load_from_package = lambda torch: FakeModel()
    samples, rate = engine.synthesize_blocking("Привет")
    assert rate == 48000
    assert len(samples) == 2
    assert samples == pytest.approx((0.1, -0.1))  # Silero dynamics remain unchanged.
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
    assert len(audio.samples) == 7202
    assert audio.samples[:7200] == (0.0,) * 7200
    provider._worker._jobs.join()  # Delivery precedes the worker's final accounting.
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
