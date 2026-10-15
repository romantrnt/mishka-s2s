"""Silero TTS v5.5 engine used by the canonical voice pipeline.

The engine owns model loading and the non-interruptible inference boundary.
It deliberately has no legacy or synthetic-audio fallback: if Silero cannot
load or synthesize, the caller receives a bounded, diagnosable failure.
"""
from __future__ import annotations

import threading
from pathlib import Path
from app.foundation.voice_pipeline import plan_prosody, prepend_pre_roll, validate_audio

SILERO_MODEL_URL = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

class SileroTTSLoadError(RuntimeError):
    """The configured Silero model could not be loaded."""

class SileroTTSEngine:
    """Lazy, process-local Silero v5.5 Russian synthesizer."""
    def __init__(self, *, language="ru", model_name="v5_5_ru", speaker="aidar",
                 sample_rate=48000, cache_path=PROJECT_ROOT / "runtime/cache/silero/v5_5_ru.pt", device=None,
                 leading_silence_ms=150):
        if language != "ru":
            raise ValueError("Silero v5.5 production engine supports only language=ru")
        if model_name != "v5_5_ru":
            raise ValueError("Silero production engine requires model=v5_5_ru")
        if speaker != "aidar":
            raise ValueError("Silero speaker must be aidar")
        if sample_rate not in {8000, 24000, 48000}:
            raise ValueError("Silero sample rate must be 8000, 24000, or 48000")
        if leading_silence_ms < 0:
            raise ValueError("Silero leading silence padding must be non-negative")
        self.language, self.model_name, self.speaker = language, model_name, speaker
        self.sample_rate = sample_rate
        self.leading_silence_ms = int(leading_silence_ms)
        cache = Path(cache_path).expanduser()
        self.cache_path = cache if cache.is_absolute() else PROJECT_ROOT / cache
        self.device_name = device
        self._model = None
        self._device = None
        self._load_source = None
        self._load_error = None
        self._load_lock = threading.Lock()
        self._synthesis_lock = threading.Lock()

    @staticmethod
    def normalize(text):
        return plan_prosody(text)


    def _load_from_hub(self, torch):
        model, _ = torch.hub.load(repo_or_dir="snakers4/silero-models", model="silero_tts",
                                  language=self.language, speaker=self.model_name)
        if model is None or not hasattr(model, "apply_tts"):
            raise RuntimeError("torch.hub returned no v5.5 TTS model")
        return model

    def _load_from_package(self, torch):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.cache_path.is_file():
            torch.hub.download_url_to_file(SILERO_MODEL_URL, str(self.cache_path))
        if not self.cache_path.is_file() or self.cache_path.stat().st_size <= 0:
            raise SileroTTSLoadError("Silero model download produced no file")
        return torch.package.PackageImporter(str(self.cache_path)).load_pickle("tts_models", "model")

    def _ensure_loaded(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                import torch
                device_name = self.device_name or ("cuda" if torch.cuda.is_available() else "cpu")
                if device_name == "cuda" and not torch.cuda.is_available():
                    raise SileroTTSLoadError("Silero requested CUDA but CUDA is unavailable")
                self._device = torch.device(device_name)
                try:
                    model = self._load_from_hub(torch)
                    self._load_source = "torch.hub"
                except Exception as hub_error:
                    try:
                        model = self._load_from_package(torch)
                        self._load_source = "package_cache"
                    except Exception as package_error:
                        raise SileroTTSLoadError("Silero v5.5 hub and package-cache loading failed") from package_error
                    self._load_error = type(hub_error).__name__
                if hasattr(model, "to"):
                    moved = model.to(self._device)
                    if moved is not None:
                        model = moved
                if hasattr(model, "eval"):
                    evaluated = model.eval()
                    if evaluated is not None:
                        model = evaluated
                self._model = model
                return model
            except SileroTTSLoadError:
                raise
            except Exception as exc:
                self._load_error = type(exc).__name__
                raise SileroTTSLoadError("Silero v5.5 initialization failed") from exc

    def readiness(self):
        return {"ready": self._model is not None, "provider": "silero", "model": self.model_name,
                "language": self.language, "speaker": self.speaker, "sample_rate": self.sample_rate,
                "put_accent": True, "put_yo": True,
                "leading_silence_ms": self.leading_silence_ms,
                "device": str(self._device) if self._device is not None else None,
                "load_source": self._load_source, "cache_present": self.cache_path.is_file(),
                "reason": None if self._model is not None else "model_not_loaded", "load_error": self._load_error}

    def synthesize_blocking(self, text, *, leading_silence_ms=0):
        import torch
        model = self._ensure_loaded()
        with self._synthesis_lock, torch.inference_mode():
            audio = model.apply_tts(text=self.normalize(text), speaker=self.speaker,
                                     sample_rate=self.sample_rate, put_accent=True, put_yo=True)
            # Keep Silero dynamics intact.  Independent peak normalization at
            # this stage makes every short block equally loud and gives RVC a
            # synthetic input envelope.  The only loudness normalization is
            # performed once after the complete RVC block.
            values = validate_audio(audio.detach().to("cpu").float().reshape(-1).tolist(), name="Silero audio")
        if not values:
            raise RuntimeError("Silero produced no audio")
        leading_ms = int(leading_silence_ms)
        if leading_ms < 0:
            raise ValueError("Silero leading silence padding must be non-negative")
        return prepend_pre_roll(values, self.sample_rate, leading_ms), self.sample_rate

    def synthesize_streaming_blocking(self, text, *, first_chunk):
        """Keep stream padding positional: only chunk zero gets the pre-roll."""
        leading_ms = self.leading_silence_ms if first_chunk else 0
        return self.synthesize_blocking(text, leading_silence_ms=leading_ms)
