from __future__ import annotations

import asyncio
import array
import concurrent.futures
import contextlib
import html
import importlib.util
import inspect
import json
import logging
import math
import numpy as np
import os
import re
import queue as thread_queue
import shutil
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


logger = logging.getLogger("uvicorn.error")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path

from app.metrics.latency import VoiceTurnTrace
from app.foundation.voice_pipeline import RVC_SAMPLE_RATE, peak_normalize, prepend_pre_roll, resample_mono


@dataclass
class _NativeTTSJob:
    token: int
    cancelled: Callable[[], bool]
    call: Callable[[], Any]
    future: concurrent.futures.Future[Any]


class _BoundedNativeWorker:
    """One bounded owner for non-interruptible native model work.

    Silero inference cannot be safely interrupted in the middle of a
    native call.  The worker therefore owns at most one active call and one
    waiting job.  Cancelling an asyncio waiter cancels result delivery and
    invalidates the generation; the native call finishes in this single
    resident worker and its result is discarded when stale.
    """

    def __init__(self, name: str, queue_size: int = 1):
        self.name = name
        self._jobs: thread_queue.Queue[_NativeTTSJob | None] = thread_queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.active_jobs = 0
        self.started_jobs = 0
        self.completed_jobs = 0
        self.cancelled_jobs = 0
        self.failed_jobs = 0

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()

    def submit(self, token: int, cancelled: Callable[[], bool], call: Callable[[], Any]) -> concurrent.futures.Future[Any]:
        self._ensure_started()
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        job = _NativeTTSJob(token, cancelled, call, future)
        try:
            self._jobs.put_nowait(job)
        except thread_queue.Full as exc:
            raise RuntimeError("Silero TTS worker queue is full") from exc
        return future

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                with self._lock:
                    self.active_jobs += 1
                    self.started_jobs += 1
                if job.future.cancelled() or job.cancelled():
                    job.future.cancel()
                    with self._lock:
                        self.cancelled_jobs += 1
                    continue
                try:
                    value = job.call()
                except BaseException as exc:
                    if job.cancelled() or job.future.cancelled():
                        job.future.cancel()
                        with self._lock:
                            self.cancelled_jobs += 1
                    else:
                        job.future.set_exception(exc)
                        with self._lock:
                            self.failed_jobs += 1
                else:
                    if job.cancelled() or job.future.cancelled():
                        job.future.cancel()
                        with self._lock:
                            self.cancelled_jobs += 1
                    else:
                        job.future.set_result(value)
                        with self._lock:
                            self.completed_jobs += 1
            finally:
                if job is not None:
                    with self._lock:
                        self.active_jobs = max(0, self.active_jobs - 1)
                self._jobs.task_done()

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            thread = self._thread
            return {
                "active_jobs": self.active_jobs,
                "queued_jobs": self._jobs.qsize(),
                "started_jobs": self.started_jobs,
                "completed_jobs": self.completed_jobs,
                "cancelled_jobs": self.cancelled_jobs,
                "failed_jobs": self.failed_jobs,
                "thread_alive": bool(thread is not None and thread.is_alive()),
                "max_concurrency": 1,
                "max_queued_jobs": self._jobs.maxsize,
            }


@dataclass(frozen=True)
class S2SConfig:
    sample_rate: int = 16_000
    channels: int = 1
    audio_format: str = "float32"
    # Use start/continue hysteresis so quiet word beginnings are not lost.
    vad_threshold: float = 0.35
    vad_continue_threshold: float = 0.20
    vad_preroll_ms: int = 320
    # Production latency calibration: 200 ms is the minimum accepted
    # hangover.  It keeps the real end-of-speech -> first audio budget under
    # one second on the warmed local-GPU path; exhibition tuning may raise it
    # if microphones are unusually noisy.
    vad_silence_ms: int = 200
    min_speech_ms: int = 300
    max_speech_ms: int = 12_000
    max_buffer_seconds: int = 15
    stt_timeout_s: float = 8.0
    llm_timeout_s: float = 10.0
    tts_timeout_s: float = 10.0
    rvc_timeout_s: float = 45.0
    playback_timeout_s: float = 60.0
    max_input_chars: int = 512
    max_response_chars: int = 320
    max_chunk_chars: int = 100
    first_chunk_chars: int = 12
    first_audio_gate_s: float = 1.25
    queue_size: int = 4
    queue_put_timeout_s: float = 2.0
    stt_confidence_threshold: float = 0.45
    stt_beam_size: int = 3

    def validate(self) -> "S2SConfig":
        if not 200 <= self.vad_silence_ms <= 250:
            raise ValueError("VAD_SILENCE_MS must be 200..250 for the initial calibration profile")
        if self.audio_format != "float32" or self.channels != 1:
            raise ValueError("foundation audio contract is mono float32")
        if self.min_speech_ms >= self.max_speech_ms:
            raise ValueError("MIN_SPEECH_MS must be lower than MAX_SPEECH_MS")
        if self.sample_rate <= 0 or self.max_buffer_seconds <= 0:
            raise ValueError("audio limits must be positive")
        if any(value <= 0 for value in (self.stt_timeout_s, self.llm_timeout_s,
                                        self.tts_timeout_s, self.rvc_timeout_s,
                                        self.playback_timeout_s, self.queue_put_timeout_s)):
            raise ValueError("pipeline timeouts must be positive")
        if self.queue_size < 1 or self.max_chunk_chars < 16:
            raise ValueError("queue and chunk limits are invalid")
        if not 0 < self.vad_threshold <= 1 or not 0 < self.vad_continue_threshold <= 1 or self.vad_continue_threshold > self.vad_threshold or not 0 <= self.stt_confidence_threshold <= 1:
            raise ValueError("confidence thresholds must be in 0..1")
        if self.vad_preroll_ms < 0 or self.stt_beam_size < 1:
            raise ValueError("VAD pre-roll and STT beam size must be valid")
        if self.max_turn_seconds <= 0 or self.max_tts_queue_bytes <= 0 or self.max_tts_chunks_per_generation <= 0:
            raise ValueError("turn and TTS queue limits must be positive")
        if self.max_history_entries <= 0 or self.max_history_chars <= 0:
            raise ValueError("history limits must be positive")
        return self

    max_turn_seconds: float = 60.0
    max_tts_queue_bytes: int = 256 * 1024
    max_tts_chunks_per_generation: int = 32
    max_history_entries: int = 20
    max_history_chars: int = 8192

    @classmethod
    def from_settings(cls, settings: Any) -> "S2SConfig":
        return cls(
            sample_rate=settings.audio_sample_rate, channels=settings.audio_channels,
            audio_format=settings.audio_format, vad_threshold=settings.vad_threshold,
            vad_continue_threshold=settings.vad_continue_threshold, vad_preroll_ms=settings.vad_preroll_ms,
            vad_silence_ms=settings.vad_silence_ms, min_speech_ms=settings.min_speech_ms,
            max_speech_ms=settings.max_speech_ms, max_buffer_seconds=settings.max_audio_buffer_seconds,
            stt_timeout_s=settings.stt_timeout_s, llm_timeout_s=settings.llm_timeout_s,
            tts_timeout_s=settings.tts_timeout_s, rvc_timeout_s=settings.rvc_timeout_s,
            playback_timeout_s=settings.playback_timeout_s, max_input_chars=settings.max_input_chars,
            max_response_chars=settings.max_response_chars, max_chunk_chars=settings.max_tts_chunk_chars,
            first_chunk_chars=settings.max_tts_first_chunk_chars, queue_size=settings.tts_queue_size,
            queue_put_timeout_s=settings.tts_queue_put_timeout_s,
            stt_confidence_threshold=settings.stt_confidence_threshold, stt_beam_size=settings.stt_beam_size,
            max_turn_seconds=settings.max_turn_seconds, max_tts_queue_bytes=settings.max_tts_queue_bytes,
            max_tts_chunks_per_generation=settings.max_tts_chunks_per_generation,
            max_history_entries=settings.max_history_entries, max_history_chars=settings.max_history_chars,
        ).validate()

    @classmethod
    def from_env(cls) -> "S2SConfig":
        from .config import Settings
        return cls.from_settings(Settings.from_env())


@dataclass(frozen=True)
class AudioChunk:
    samples: tuple[float, ...]
    sample_rate: int
    token: int
    session_id: str | None = None
    generation_id: int | None = None
    state: str = "queued"

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("audio chunk sample_rate must be positive")
        if self.state not in {"queued", "playing", "cancelled"}:
            raise ValueError(f"invalid audio chunk state: {self.state}")

    @property
    def size_bytes(self) -> int:
        return len(self.samples) * 4

    @property
    def duration_ms(self) -> float:
        return len(self.samples) * 1000.0 / self.sample_rate

    def with_state(self, state: str) -> "AudioChunk":
        return AudioChunk(self.samples, self.sample_rate, self.token, self.session_id, self.generation_id, state)


@dataclass(frozen=True)
class STTResult:
    text: str
    confidence: float
    latency_ms: float
    unknown: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedOutput:
    kind: str
    value: str
    params: dict[str, str] = field(default_factory=dict)


class PipelineMetrics:
    """Technical latency/failure metrics only; values contain no child data."""

    def __init__(self, *, stage_limit: int | None = None, event_limit: int | None = None, settings: Any = None):
        self.stage_limit = int(stage_limit or getattr(settings, "observability_latency_limit", 100))
        self.event_limit = int(event_limit or getattr(settings, "observability_event_limit", 500))
        if min(self.stage_limit, self.event_limit) <= 0:
            raise ValueError("pipeline observability limits must be positive")
        self.latencies_ms: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.stage_limit))
        self.stage_evicted: dict[str, int] = defaultdict(int)
        self.failures: dict[str, int] = defaultdict(int)
        self.events: deque[dict[str, Any]] = deque(maxlen=self.event_limit)
        self.event_evicted = 0
        self.active_voice_turns: dict[str, VoiceTurnTrace] = {}
        self.active_playback: dict[str, float] = {}
        self.completed_voice_turns: deque[dict[str, Any]] = deque(maxlen=100)
        # Bounded redacted lifecycle evidence for every audio chunk.
        self.chunk_trace_limit = max(1, self.event_limit)
        self.chunk_traces: deque[dict[str, Any]] = deque(maxlen=self.chunk_trace_limit)
        self._chunk_trace_index: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.chunk_trace_evicted = 0
        tts_provider = "mock" if getattr(settings, "profile", "mock") == "mock" else "silero"
        voice_conversion = getattr(settings, "voice_conversion", "rvc_v2")
        self.pipeline = {
            "label": "Silero v5.5 + RVC" if tts_provider == "silero" and voice_conversion == "rvc_v2" else f"{tts_provider} + {voice_conversion}",
            "tts_provider": tts_provider,
            "tts_model": getattr(settings, "silero_model", "v5_5_ru") if tts_provider == "silero" else "mock",
            "tts_language": getattr(settings, "silero_language", "ru") if tts_provider == "silero" else "mock",
            "tts_speaker": getattr(settings, "silero_speaker", "aidar") if tts_provider == "silero" else "mock",
            "tts_sample_rate": getattr(settings, "silero_sample_rate", 48000) if tts_provider == "silero" else 24000,
            "voice_conversion": voice_conversion,
            "stream_preroll_ms": getattr(settings, "silero_leading_silence_ms", 150) if tts_provider == "silero" else 0,
        }

    def begin_voice_turn(
        self,
        session_id: str,
        correlation_id: str | None = None,
        *,
        generation_id: object | None = None,
    ) -> VoiceTurnTrace:
        trace = self.active_voice_turns.get(session_id)
        if trace is not None and generation_id is not None:
            if trace.generation_id is None:
                trace.generation_id = generation_id
            elif trace.generation_id != generation_id:
                # A new generation owns a new trace. Archive the old trace as
                # superseded instead of allowing late markers to contaminate
                # the new turn. A genuinely corrupt trace still raises from
                # report(), preserving the diagnostic failure contract.
                trace.outcome = "superseded"
                self.completed_voice_turns.append(trace.report())
                trace = None
        if trace is None:
            trace = VoiceTurnTrace(correlation_id or str(uuid.uuid4()), generation_id=generation_id)
            self.active_voice_turns[session_id] = trace
        return trace

    @staticmethod
    def _owns_generation(trace: VoiceTurnTrace, generation_id: object | None) -> bool:
        return generation_id is None or trace.generation_id is None or trace.generation_id == generation_id

    def mark_voice(self, session_id: str, boundary: str, *, generation_id: object | None = None) -> None:
        trace = self.active_voice_turns.get(session_id)
        if trace is not None and self._owns_generation(trace, generation_id):
            trace.mark(boundary)

    def mark_any_voice(self, boundary: str) -> None:
        if self.active_voice_turns:
            session_id = next(reversed(self.active_voice_turns))
            self.active_voice_turns[session_id].mark(boundary)

    def observe_voice_stage(
        self,
        session_id: str,
        stage: str,
        started_at: float,
        success: bool = True,
        *,
        generation_id: object | None = None,
    ) -> None:
        trace = self.active_voice_turns.get(session_id)
        if trace is not None and self._owns_generation(trace, generation_id):
            trace.observe_stage(stage, started_at, success)

    def mark_playback_start(self, session_id: str) -> None:
        self.active_playback[session_id] = time.monotonic()

    def mark_playback_finish(self, session_id: str) -> None:
        self.active_playback.pop(session_id, None)

    def finish_voice_turn(
        self,
        session_id: str,
        outcome: str,
        *,
        generation_id: object | None = None,
    ) -> dict[str, Any] | None:
        trace = self.active_voice_turns.get(session_id)
        if trace is None or not self._owns_generation(trace, generation_id):
            return None
        self.mark_playback_finish(session_id)
        self.active_voice_turns.pop(session_id, None)
        trace.outcome = outcome
        report = trace.report()
        self.completed_voice_turns.append(report)
        return report

    def event(self, name: str, **fields: Any) -> None:
        """Record a redacted pipeline event and mirror it to the logger.

        Event fields intentionally contain timing/sequence metadata only; no
        child speech or generated text is persisted.
        """
        record = {"event": name, "at": time.time(), **fields}
        if len(self.events) == self.events.maxlen:
            self.event_evicted += 1
        self.events.append(record)
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        if name == "tts_started" and fields.get("provider") == "silero":
            logger.info("Silero synthesis started - chunk %s", fields.get("chunk_index", "?"))
        elif name == "audio_ready" and fields.get("provider") == "silero":
            logger.info("Silero synthesis ready - chunk %s", fields.get("chunk_index", "?"))
        else:
            logger.info("%s%s", name, f" {details}" if details else "")

    def chunk_trace(self, *, session_id: str, turn_id: str, generation_id: object, chunk_index: int, owner: str, event: str, lifecycle: str, terminal_reason: str | None = None, sample_count: int | None = None, byte_count: int | None = None) -> None:
        """Record bounded, redacted lifecycle evidence for one audio chunk."""
        generation = str(generation_id)
        key = (session_id, generation, int(chunk_index))
        trace = self._chunk_trace_index.get(key)
        if trace is None:
            if len(self.chunk_traces) == self.chunk_traces.maxlen:
                evicted = self.chunk_traces.popleft()
                self._chunk_trace_index.pop((str(evicted["session_id"]), str(evicted["generation"]), int(evicted["chunk_index"])), None)
                self.chunk_trace_evicted += 1
            trace = {"session_id": session_id, "turn_id": turn_id, "generation": generation, "chunk_index": int(chunk_index), "owner": owner, "owner_conflict": False, "lifecycle": lifecycle, "timestamps": {}, "event_counts": {}, "sample_count": 0, "byte_count": 0, "terminal_state": None, "terminal_reason": None, "terminal_callbacks": 0, "duplicate_callbacks": 0}
            self.chunk_traces.append(trace)
            self._chunk_trace_index[key] = trace
        if trace["owner"] != owner:
            trace["owner_conflict"] = True
        if event in trace["timestamps"]:
            trace["duplicate_callbacks"] += 1
        else:
            trace["timestamps"][event] = time.time()
        trace["event_counts"][event] = int(trace["event_counts"].get(event, 0)) + 1
        trace["lifecycle"] = lifecycle
        if sample_count is not None:
            trace["sample_count"] = max(trace["sample_count"], int(sample_count))
        if byte_count is not None:
            trace["byte_count"] = max(trace["byte_count"], int(byte_count))
        if lifecycle in {"finished", "cancelled", "failed"}:
            if trace["terminal_state"] is None:
                trace["terminal_state"] = lifecycle
                trace["terminal_reason"] = terminal_reason or lifecycle
                trace["terminal_callbacks"] = 1
            else:
                trace["duplicate_callbacks"] += 1

    def observe(self, stage: str, started_at: float, success: bool = True) -> None:
        values = self.latencies_ms[stage]
        if len(values) == values.maxlen:
            self.stage_evicted[stage] += 1
        values.append((time.perf_counter() - started_at) * 1000)
        if not success:
            self.failures[stage] += 1

    def snapshot(self) -> dict[str, Any]:
        stages: dict[str, Any] = {}
        for stage, values in self.latencies_ms.items():
            stages[stage] = {
                "count": len(values),
                "last_ms": round(values[-1], 2),
                "max_ms": round(max(values), 2),
                "avg_ms": round(sum(values) / len(values), 2),
                "failures": self.failures.get(stage, 0),
                "current_sample_count": len(values),
                "evicted_sample_count": self.stage_evicted.get(stage, 0),
                "limit": self.stage_limit,
            }
        stages["events"] = list(self.events)
        stages["pipeline"] = dict(self.pipeline)
        stages["observability_limits"] = {"stage_samples": self.stage_limit, "events": self.event_limit, "event_evicted_count": self.event_evicted}
        stages["voice_turns"] = list(self.completed_voice_turns)
        stages["active_voice_turns"] = [trace.report() for trace in self.active_voice_turns.values()]
        stages["chunk_traces"] = list(self.chunk_traces)
        stages["chunk_trace_evicted_count"] = self.chunk_trace_evicted
        stages["chunk_trace_limit"] = self.chunk_trace_limit
        return stages

    def clear(self) -> None:
        """Drop per-session observability after an explicit stand reset."""
        self.latencies_ms.clear()
        self.failures.clear()
        self.events.clear()
        self.active_voice_turns.clear()
        self.active_playback.clear()
        self.completed_voice_turns.clear()
        self.chunk_traces.clear()
        self._chunk_trace_index.clear()
        self.chunk_trace_evicted = 0


@dataclass(frozen=True)
class ProfileContext:
    """Bounded, age-aware profile context allowed to reach the model."""
    display_name: str | None = None
    age: int | None = None
    age_band: str = "3-5"
    age_source: str = "anonymous_default"
    language: str = "ru-RU"
    accessibility: tuple[str, ...] = ()

    @classmethod
    def from_profile(cls, profile: dict[str, Any] | None) -> "ProfileContext":
        profile = profile or {}
        from .age_policy import age_details
        details = age_details(profile)
        age = profile.get("age")
        flags = tuple(str(item)[:40] for key in ("motor_flags", "sensory_flags", "communication_flags") for item in (profile.get(key) or [])[:4])
        return cls(display_name=str(profile["display_name"])[:64] if profile.get("display_name") else None, age=int(age) if isinstance(age, int) and 0 <= age <= 18 else details.get("age_years"), age_band=str(details.get("age_band") or "3-5"), age_source=str(details.get("age_band_source") or "anonymous_default"), language=str(profile.get("language") or "ru-RU"), accessibility=flags)

    def prompt_fields(self) -> dict[str, Any]:
        return {key: value for key, value in (("display_name", self.display_name), ("age", self.age), ("age_band", self.age_band), ("age_source", self.age_source), ("language", self.language), ("accessibility", self.accessibility)) if value is not None}

@dataclass(frozen=True)
class ScenarioContext:
    scenario_id: str
    node_id: str
    allowed_topics: tuple[str, ...]
    forbidden_topics: tuple[str, ...]


@dataclass
class ConversationContext:
    session_id: str
    scenario_id: str
    node_id: str
    allowed_topics: tuple[str, ...]
    forbidden_topics: tuple[str, ...]
    max_turns: int
    max_duration_seconds: int
    context_window: int
    profile: ProfileContext = field(default_factory=ProfileContext)
    system_prompt: str = ""
    history: list[tuple[str, str]] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    turns: int = 0


class VADAdapter(Protocol):
    def speech_probability(self, samples: tuple[float, ...], sample_rate: int) -> float: ...

class SpeechRecognizer(Protocol):
    async def transcribe(self, audio: AudioChunk, cancelled: Callable[[], bool]) -> STTResult: ...

class LLMProvider(Protocol):
    # This is an async generator, not a coroutine returning an async iterator.
    # Keeping the protocol shape exact prevents false-positive type errors in
    # the streaming consumer and documents the provider contract correctly.
    def stream(self, messages: list[dict[str, str]], request_id: str, cancelled: Callable[[], bool]) -> AsyncIterator[str]: ...

class TTSProvider(Protocol):
    async def synthesize(self, text: str, token: int, cancelled: Callable[[], bool]) -> AudioChunk: ...

class VoiceConversionProvider(Protocol):
    async def convert(self, audio: AudioChunk, cancelled: Callable[[], bool]) -> AudioChunk: ...

class PlaybackController(Protocol):
    async def play(self, audio: AudioChunk) -> None: ...
    async def stop(self) -> None: ...

class AudioAssetProvider(Protocol):
    async def get(self, asset_id: str) -> AudioChunk | None: ...


class AudioBuffer:
    """Bounded RAM-only PCM buffer. No filesystem methods exist by design."""
    def __init__(self, config: S2SConfig):
        self.config = config; self._samples: list[float] = []
        self.limit = config.sample_rate * config.max_buffer_seconds
    def append(self, samples: tuple[float, ...]) -> None:
        if len(self._samples) + len(samples) > self.limit:
            raise BufferError("audio buffer limit exceeded")
        self._samples.extend(float(max(-1.0, min(1.0, x))) for x in samples)
    def clear(self) -> None: self._samples.clear()
    def chunk(self, token: int) -> AudioChunk: return AudioChunk(tuple(self._samples), self.config.sample_rate, token)
    @property
    def duration_ms(self) -> float: return len(self._samples) * 1000 / self.config.sample_rate


class EnergyVADAdapter:
    """Dependency-free test/CPU fallback; Silero adapter is preferred when available."""
    def speech_probability(self, samples, sample_rate):
        if not samples: return 0.0
        rms = math.sqrt(sum(x*x for x in samples) / len(samples))
        return min(1.0, rms * 12.0)


class SileroVADAdapter:
    """Lazy adapter around an injected Silero callable; importing never loads torch."""
    def __init__(self, model_loader: Callable[[], Callable[[tuple[float, ...], int], float]] | None = None): self.model_loader=model_loader; self._model: Callable[[tuple[float, ...], int], float] | None=None
    def speech_probability(self, samples, sample_rate):
        if self._model is None:
            if self.model_loader is None: raise RuntimeError("Silero model is not configured")
            self._model=self.model_loader()
        model=self._model
        if model is None: raise RuntimeError("Silero model failed to initialize")
        return float(model(samples, sample_rate))

    def readiness(self) -> dict[str, Any]:
        package = importlib.util.find_spec("silero_vad") is not None
        torch = importlib.util.find_spec("torch") is not None
        return {"ready": package and torch and self.model_loader is not None,
                "provider": "silero_vad", "package": package, "torch": torch,
                "model_loader": self.model_loader is not None}

    async def warmup(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if self._model is None:
                if self.model_loader is None:
                    raise RuntimeError("Silero model is not configured")
                self._model = await asyncio.to_thread(self.model_loader)
            return {"ok": True, "latency_ms": round((time.perf_counter()-started)*1000, 2)}
        except Exception as exc:
            return {"ok": False, "latency_ms": round((time.perf_counter()-started)*1000, 2),
                    "reason": f"{type(exc).__name__}: {str(exc)[:180]}"}


class AudioIngressPipeline:
    def __init__(self, vad: VADAdapter, config: S2SConfig):
        self.vad, self.config, self.buffer = vad, config, AudioBuffer(config); self.active=False; self.silence_ms=0.0
        # Browser AudioContext frames are often 341 samples after 48 kHz ->
        # 16 kHz resampling. Silero's TorchScript model rejects windows below
        # 512 samples, so collect a small RAM-only VAD window first.
        self._vad_pending: list[float] = []
        self._pre_roll: deque[float] = deque(maxlen=max(0, int(config.sample_rate * config.vad_preroll_ms / 1000)))
    def feed(self, samples: tuple[float, ...], token: int) -> tuple[str, AudioChunk | None]:
        self._vad_pending.extend(samples)
        if len(self._vad_pending) < 512:
            return "collecting", None
        result: tuple[str, AudioChunk | None] = ("collecting", None)
        while len(self._vad_pending) >= 512:
            # Silero VAD accepts exactly 512 samples at 16 kHz (not an
            # arbitrary larger window). Keep any remainder for the next call.
            frame = tuple(self._vad_pending[:512])
            del self._vad_pending[:512]
            result = self._feed_frame(frame, token)
            if result[0] != "collecting":
                return result
        return result

    def _feed_frame(self, samples: tuple[float, ...], token: int) -> tuple[str, AudioChunk | None]:
        frame_ms=len(samples)*1000/self.config.sample_rate; probability=self.vad.speech_probability(samples, self.config.sample_rate)
        if not self.active:
            if probability < self.config.vad_threshold:
                if self._pre_roll.maxlen:
                    self._pre_roll.extend(samples)
                return "collecting", None
            self.active=True; self.buffer.clear(); self.silence_ms=0
            if self._pre_roll:
                self.buffer.append(tuple(self._pre_roll)); self._pre_roll.clear()
            self.buffer.append(samples)
        elif probability >= self.config.vad_continue_threshold:
            self.silence_ms=0; self.buffer.append(samples)
        elif self.active:
            self.silence_ms += frame_ms; self.buffer.append(samples)
        if self.buffer.duration_ms >= self.config.max_speech_ms:
            chunk=self.buffer.chunk(token); self.reset(); return "max_duration", chunk
        # ``silence_ms`` already includes the current silent frame above.
        # Comparing it again with ``+ frame_ms`` would end speech one VAD
        # window early and truncate the final syllable.
        if self.active and self.silence_ms >= self.config.vad_silence_ms:
            chunk=self.buffer.chunk(token); duration=self.buffer.duration_ms-self.silence_ms; self.reset()
            if duration < self.config.min_speech_ms: return "too_short", None
            return "speech_end", chunk
        return "collecting", None
    def reset(self): self.active=False; self.silence_ms=0; self.buffer.clear(); self._vad_pending.clear(); self._pre_roll.clear()


class MockSpeechRecognizer:
    def __init__(self, text="mock recognized speech", confidence=.95): self.text=text; self.confidence=confidence
    async def transcribe(self, audio, cancelled):
        started=time.perf_counter(); await asyncio.sleep(0)
        if cancelled(): raise asyncio.CancelledError
        return STTResult(self.text, self.confidence, (time.perf_counter()-started)*1000, self.confidence < .5, {"samples":len(audio.samples)})


class FasterWhisperRecognizer:
    def __init__(self, model_loader: Callable[[], Any], config:S2SConfig, model_name: str = ""):
        self.loader=model_loader; self.config=config; self.model_name=model_name; self._model: Any=None

    def readiness(self) -> dict[str, Any]:
        package = importlib.util.find_spec("faster_whisper") is not None
        path = Path(self.model_name)
        local_reference = path.exists() if (path.is_absolute() or "/" in self.model_name) else None
        return {"ready": package and local_reference is not False, "provider": "faster_whisper",
                "package": package, "model": self.model_name,
                "local_model_present": local_reference,
                "model_load_requires_warmup": self._model is None}

    async def warmup(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if self._model is None:
                self._model = await asyncio.to_thread(self.loader)
            return {"ok": True, "latency_ms": round((time.perf_counter()-started)*1000, 2)}
        except Exception as exc:
            return {"ok": False, "latency_ms": round((time.perf_counter()-started)*1000, 2),
                    "reason": f"{type(exc).__name__}: {str(exc)[:180]}"}
    async def transcribe(self, audio, cancelled):
        if cancelled(): raise asyncio.CancelledError
        try:
            if self._model is None: self._model=await asyncio.to_thread(self.loader)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError("faster-whisper model unavailable") from exc
        started=time.perf_counter()
        def run():
            # faster-whisper treats a Python list as a filename/file-like
            # input and sends it through decode_audio().  The browser/STT
            # boundary is an in-memory float32 PCM stream, so make that
            # contract explicit with a numpy waveform before calling the
            # provider.  Without this conversion short live turns fail with
            # an opaque ValueError even though the model itself is healthy.
            try:
                import numpy as np
            except ImportError as exc:
                raise RuntimeError("numpy is required for faster-whisper PCM input") from exc
            # Very short turns are valid at the product boundary (for
            # example, a child saying one short word), but some
            # faster-whisper/CTranslate2 combinations are brittle when the
            # feature extractor receives only a few hundred milliseconds.
            # Keep the original samples in RAM and pad only the local
            # inference buffer; this does not change the recorded input.
            samples=np.asarray(audio.samples, dtype=np.float32)
            minimum_samples=max(1600, int(audio.sample_rate * 0.75))
            if samples.size < minimum_samples:
                samples=np.pad(samples, (0, minimum_samples - samples.size))
            # VAD already ran at the ingress boundary. A second Whisper VAD
            # drops short Russian utterances. no_speech_prob is more useful
            # than treating avg_logprob as calibrated confidence.
            segments,_=self._model.transcribe(
                samples, language="ru", beam_size=self.config.stt_beam_size,
                vad_filter=False, condition_on_previous_text=False,
            )
            values=list(segments); text=" ".join(x.text.strip() for x in values).strip()
            if values:
                no_speech=sum(float(getattr(x,"no_speech_prob",0.0)) for x in values)/len(values)
                confidence=max(0.0,min(1.0,1.0-no_speech))
                if confidence == 1.0 and all(not hasattr(x,"no_speech_prob") for x in values):
                    confidence=math.exp(sum(x.avg_logprob for x in values)/len(values))
            else: confidence=0.0
            return text, confidence
        try:
            text,confidence=await asyncio.wait_for(asyncio.to_thread(run), self.config.stt_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "faster_whisper_transcribe_failed type=%s error=%s samples=%d duration_ms=%.1f",
                type(exc).__name__, str(exc)[:240], len(audio.samples), len(audio.samples) * 1000 / audio.sample_rate,
            )
            raise RuntimeError("faster-whisper transcription failed") from exc
        if cancelled(): raise asyncio.CancelledError
        return STTResult(text, confidence, (time.perf_counter()-started)*1000, not text or confidence < .45)


class InputNormalizer:
    def normalize(self, text): return re.sub(r"\s+", " ", html.unescape(text).strip())

class InputSafetyFilter:
    BLOCK=re.compile(r"покажи.*(промпт|инструкц)|игнорируй.*инструкц|show.*system.*prompt|ignore.*instructions|диагноз|diagnos|назнач.*лекар|medicine|treatment|как.*навредить|how.*harm|убить|ударить", re.I)
    SIGNAL=re.compile(r"мне страшно|мне больно|меня обижают|мне плохо|i am scared|i.?m hurt", re.I)
    def check(self, text):
        if self.BLOCK.search(text): return "block"
        if self.SIGNAL.search(text): return "signal"
        return "allow"


class TopicPolicy:
    """Conservative lexical gate before the LLM; unknown topics return to scenario."""

    TOPICS = {
        "greetings": re.compile(r"привет|здравствуй|добрый\s+(день|вечер|утр)|хелло|hello|hi", re.I),
        "clinic": re.compile(r"поликлиник|врач|кабинет", re.I),
        "weather": re.compile(r"погод|дожд|снег|солнц", re.I),
        "games": re.compile(r"игр|кнопк", re.I),
        "stories": re.compile(r"сказ|истори", re.I),
        "feelings": re.compile(r"чувств|настро|рад|груст|весел", re.I),
        "questions": re.compile(r"почему|зачем|как |что |можно|расскажи|подскажи", re.I),
    }

    def check(self, text: str, allowed: tuple[str, ...], forbidden: tuple[str, ...]) -> bool:
        lowered = text.casefold()
        if any(topic.casefold() in lowered for topic in forbidden):
            return False
        if "__all__" in allowed:
            return True
        detected = {topic for topic, pattern in self.TOPICS.items() if pattern.search(text)}
        return bool(detected.intersection(allowed))


class ResponseTagParser:
    TAG=re.compile(r"<say>(.*?)</say>|<action\s+([^>]*)/>", re.S)
    ATTR=re.compile(r'(\w+)="([\w.-]+)"')
    def parse(self, value:str) -> list[ParsedOutput]:
        if "<" not in value: return [ParsedOutput("say", value.strip())] if value.strip() else []
        outputs=[]; consumed=[]
        for match in self.TAG.finditer(value):
            consumed.append(match.span())
            if match.group(1) is not None: outputs.append(ParsedOutput("say", match.group(1).strip()))
            else:
                attrs=dict(self.ATTR.findall(match.group(2))); action=attrs.pop("type", "")
                if action == "led" and attrs.get("pattern") in {"calm","attention","off"}: outputs.append(ParsedOutput("action", "led", attrs))
                elif action == "return_to_scenario" and re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", attrs.get("target", "")): outputs.append(ParsedOutput("action", "return_to_scenario", attrs))
                else: raise ValueError("unknown or invalid model action")
        residue=value
        for start,end in reversed(consumed): residue=residue[:start]+residue[end:]
        if "<" in residue or ">" in residue: raise ValueError("unknown or unclosed tag")
        if residue.strip(): outputs.append(ParsedOutput("say", residue.strip()))
        return outputs

class StreamingTokenConsumer:
    def __init__(self, max_chars=2048): self.max_chars=max_chars
    async def consume(self, provider, messages, request_id, cancelled):
        parts=[]; size=0
        async for token in provider.stream(messages,request_id,cancelled):
            size += len(token)
            if size > self.max_chars: raise ValueError("LLM stream exceeded limit")
            parts.append(token)
        return "".join(parts)

    async def consume_parsed(self, provider, messages, request_id, cancelled, parser, on_output, on_token=None):
        """Parse allowlisted output and emit ``<say>`` text incrementally."""
        buffer = ""
        say_text = ""
        in_say = False
        total = 0

        def safe_prefix(value: str, marker: str) -> tuple[str, str]:
            """Keep a possible split closing tag in the remainder."""
            longest = 0
            for size in range(1, min(len(value), len(marker) - 1) + 1):
                if value.endswith(marker[:size]):
                    longest = size
            return value[:-longest] if longest else value, value[-longest:] if longest else ""

        async def emit_say_fragment(fragment: str) -> None:
            if fragment:
                await on_output(ParsedOutput("say", fragment))

        async for token in provider.stream(messages, request_id, cancelled):
            if cancelled():
                raise asyncio.CancelledError
            total += len(token)
            if total > self.max_chars:
                raise ValueError("LLM stream exceeded limit")
            if on_token is not None:
                on_token(token)
            buffer += token
            while True:
                if in_say:
                    end = buffer.find("</say>")
                    if end >= 0:
                        say_text += buffer[:end]
                        buffer = buffer[end + len("</say>") :]
                        await emit_say_fragment(say_text)
                        say_text = ""
                        in_say = False
                        continue
                    ready, buffer = safe_prefix(buffer, "</say>")
                    say_text += ready
                    # Keep streaming latency, but never publish a partial
                    # sentence: an EOF/error can still invalidate the open
                    # <say> block.  Only a completed sentence is provisional.
                    if re.search(r'[.!?…](?:["»)]*)?\s*$', say_text):
                        await emit_say_fragment(say_text)
                        say_text = ""
                    break

                buffer = buffer.lstrip()
                if buffer.startswith("<say>"):
                    buffer = buffer[len("<say>") :]
                    in_say = True
                    continue
                if buffer.startswith("<action"):
                    end = buffer.find("/>")
                    if end < 0:
                        break
                    segment, buffer = buffer[: end + 2], buffer[end + 2 :]
                    for item in parser.parse(segment):
                        await on_output(item)
                    continue
                if buffer.startswith("<"):
                    if any(prefix.startswith(buffer) for prefix in ("<say>", "<action ")):
                        break
                    raise ValueError("unknown model tag")
                # Plain-text fallback cannot safely be distinguished from a
                # split tag until EOF, so it remains buffered.
                break

        if in_say:
            raise ValueError("unclosed <say> tag")
        if buffer.strip():
            for item in parser.parse(buffer):
                await on_output(item)


class SentenceChunker:
    """Emit complete spoken units without cutting through a word.

    The old implementation fell back to ``cut = limit`` when there was no
    preceding space.  That is a real audio bug for streaming TTS: a short
    first chunk could contain ``При`` and the next chunk ``вет``.  RVC then
    converted those fragments independently and the first consonants were
    especially easy to lose.  Prefer a punctuation/whitespace boundary and
    wait for the next boundary when a single word is longer than the target.
    """

    def __init__(self, max_chars=100, first_max_chars=None):
        self.max_chars=max_chars
        self.first_max_chars=first_max_chars or max_chars
        if self.max_chars < 1 or self.first_max_chars < 1:
            raise ValueError("chunk limits must be positive")
        self.first=True
        self.buffer=""

    @property
    def first_min_chars(self):
        # RVC needs more than a greeting plus punctuation to infer stable
        # phonemes.  Keep a short first sentence buffered until either this
        # context is available or the producer flushes the completed reply.
        return min(self.first_max_chars, 16)

    def feed(self, token):
        self.buffer += token
        result=[]
        while True:
            match=re.search(r"^(.+?[.!?,;:])(?:\s|$)", self.buffer)
            if match:
                spoken_prefix=match.group(1).strip()
                if self.first and len(spoken_prefix) < self.first_min_chars:
                    # Do not feed a 5–10 character fragment such as
                    # ``Привет,`` to an independent RVC conversion job.
                    # Continue accumulating until the first context target,
                    # then the normal whitespace boundary logic below takes
                    # over.  ``flush`` preserves genuinely short replies.
                    pass
                else:
                    end=match.end()
                    result.append(self.buffer[:end].strip())
                    self.buffer=self.buffer[end:].lstrip()
                    self.first=False
                    continue
            limit=self.first_max_chars if self.first else self.max_chars
            if len(self.buffer) < limit:
                break
            # Never split a word just because the latency target was reached.
            # Include the boundary whitespace in the consumed prefix, then
            # strip it from the next buffer so chunks remain natural speech.
            cut=self.buffer.rfind(" ",0,limit + 1)
            if cut > 0:
                result.append(self.buffer[:cut].strip())
                self.buffer=self.buffer[cut:].lstrip()
                self.first=False
                continue
            # A long first/next word is kept intact until its terminating
            # whitespace arrives.  The bounded response/queue limits still
            # cap memory; semantic integrity is more important than a hard
            # character cap at this boundary.
            next_space=re.search(r"\s", self.buffer[limit:])
            if next_space is None:
                break
            cut=limit + next_space.start()
            if cut <= 0:
                break
            result.append(self.buffer[:cut].strip())
            self.buffer=self.buffer[cut:].lstrip()
            self.first=False
        return [x for x in result if x]

    def flush(self):
        value=self.buffer.strip()
        self.buffer=""
        return [value] if value else []

class OutputSafetyValidator:
    BLOCK=re.compile(r"диагноз|diagnos|дозировк|dosage|прими таблет|take .*medicine|прекрати леч|stop treatment|системн.*промпт|system prompt|как навредить|how to harm", re.I)
    def validate(self, text, max_chars=320):
        clean=InputNormalizer().normalize(text)
        if len(clean)>max_chars or self.BLOCK.search(clean): raise ValueError("unsafe or oversized model output")
        return clean


class MockLLMProvider:
    def __init__(self, tokens=None, fail=False): self.tokens=tokens or ["<say>Тестовая ", "реплика.</say>"]; self.fail=fail
    async def stream(self, messages, request_id, cancelled):
        if self.fail: raise RuntimeError("mock LLM unavailable")
        for token in self.tokens:
            await asyncio.sleep(0)
            if cancelled(): raise asyncio.CancelledError
            yield token

class OllamaLLMProvider:
    """Ollama `/api/chat` NDJSON adapter with exact model readiness checks."""

    def __init__(self, base_url: str, model: str, timeout_s: float, *, quantization: str = "", num_ctx: int = 8192, temperature: float = 0.2, max_tokens: int = 160, keep_alive: str = "-1", think: bool = False, warmup_timeout_s: float = 120.0):
        self.base_url=base_url.rstrip("/"); self.model=model; self.timeout_s=timeout_s
        self.quantization=quantization; self.num_ctx=num_ctx; self.temperature=temperature
        self.max_tokens=max_tokens; self.keep_alive=keep_alive; self.think=think; self.warmup_timeout_s=warmup_timeout_s; self.last_stats: dict[str, Any]={}

    def _payload(self, messages: list[dict[str, str]], stream: bool, max_tokens: int | None = None) -> dict[str, Any]:
        keep_alive: Any = self.keep_alive
        if isinstance(keep_alive, str) and keep_alive.strip().lstrip("-").isdigit():
            keep_alive = int(keep_alive.strip())
        return {"model":self.model,"messages":messages,"stream":stream,"think":self.think,"keep_alive":keep_alive,"options":{"temperature":self.temperature,"top_p":0.85,"top_k":40,"repeat_penalty":1.05,"num_predict":max_tokens or self.max_tokens,"num_ctx":self.num_ctx}}

    async def stream(self,messages,request_id,cancelled):
        try:
            import httpx
            timeout=httpx.Timeout(self.timeout_s)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST",f"{self.base_url}/api/chat",json=self._payload(messages,True),headers={"x-request-id":request_id}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if cancelled(): raise asyncio.CancelledError
                        if not line: continue
                        data=json.loads(line)
                        if data.get("done"):
                            self.last_stats={key:data.get(key) for key in ("total_duration","load_duration","prompt_eval_count","prompt_eval_duration","eval_count","eval_duration")}
                            break
                        token=str(data.get("message",{}).get("content",""))
                        if token: yield token
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError("local Ollama provider unavailable") from exc

    async def readiness(self) -> dict[str, Any]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response=await client.get(f"{self.base_url}/api/tags"); response.raise_for_status()
            models=response.json().get("models",[])
            match=next((item for item in models if item.get("name")==self.model or item.get("model")==self.model),None)
            actual=(match or {}).get("details",{}).get("quantization_level")
            quantization_ok=not self.quantization or actual==self.quantization
            return {"ready":match is not None and quantization_ok,"provider":"ollama","model":self.model,"model_present":match is not None,"expected_quantization":self.quantization or None,"actual_quantization":actual,"num_ctx":self.num_ctx}
        except Exception as exc:
            return {"ready":False,"provider":"ollama","model":self.model,"reason":type(exc).__name__}

    async def warmup(self) -> dict[str, Any]:
        started=time.perf_counter()
        try:
            import httpx
            messages=[{"role":"system","content":"Return only <say>ok</say>."},{"role":"user","content":"warmup"}]
            async with httpx.AsyncClient(timeout=self.warmup_timeout_s) as client:
                response=await client.post(f"{self.base_url}/api/chat",json=self._payload(messages,False,8)); response.raise_for_status()
            return {"ok":True,"latency_ms":round((time.perf_counter()-started)*1000,2)}
        except Exception as exc:
            return {"ok":False,"latency_ms":round((time.perf_counter()-started)*1000,2),"reason":type(exc).__name__}


class RetryingLLMProvider:
    """Retry a transient provider failure only before the first token.

    Retrying after partial output would duplicate speech and violate the
    streaming contract, so once a token has been yielded the original error
    is propagated to the bounded safety fallback.
    """

    def __init__(self, delegate: LLMProvider, retries: int = 1, delay_s: float = 0.05):
        self.delegate = delegate
        self.retries = max(0, int(retries))
        self.delay_s = max(0.0, float(delay_s))

    async def stream(self, messages, request_id, cancelled):
        for attempt in range(self.retries + 1):
            yielded = False
            try:
                async for token in self.delegate.stream(messages, request_id, cancelled):
                    yielded = True
                    yield token
                return
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                if yielded or attempt >= self.retries or cancelled():
                    raise
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)

    async def readiness(self) -> dict[str, Any]:
        method = getattr(self.delegate, "readiness", None)
        if method is None:
            return {"ready": False, "provider": "delegate_without_readiness", "reason": "readiness_unavailable"}
        value = method()
        return await value if inspect.isawaitable(value) else value

    async def warmup(self) -> dict[str, Any]:
        method = getattr(self.delegate, "warmup", None)
        if method is None:
            return {"ok": False, "provider": "delegate_without_warmup", "reason": "warmup_unavailable"}
        value = method()
        return await value if inspect.isawaitable(value) else value


class VLLMLLMProvider:
    """OpenAI-compatible vLLM streaming adapter; server owns quantization/loading."""

    def __init__(self, base_url: str, model: str, timeout_s: float, *, temperature: float = 0.2, max_tokens: int = 160):
        self.base_url=base_url.rstrip("/"); self.model=model; self.timeout_s=timeout_s; self.temperature=temperature; self.max_tokens=max_tokens

    def _payload(self,messages,stream,max_tokens=None): return {"model":self.model,"messages":messages,"stream":stream,"temperature":self.temperature,"max_tokens":max_tokens or self.max_tokens,"tools":[]}

    async def stream(self,messages,request_id,cancelled):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream("POST",f"{self.base_url}/v1/chat/completions",json=self._payload(messages,True),headers={"x-request-id":request_id}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if cancelled(): raise asyncio.CancelledError
                        if not line.startswith("data:"): continue
                        value=line[5:].strip()
                        if value=="[DONE]": break
                        data=json.loads(value); token=str(data.get("choices",[{}])[0].get("delta",{}).get("content") or "")
                        if token: yield token
        except asyncio.CancelledError: raise
        except Exception as exc: raise RuntimeError("local vLLM provider unavailable") from exc

    async def readiness(self) -> dict[str, Any]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response=await client.get(f"{self.base_url}/v1/models"); response.raise_for_status()
            ids={item.get("id") for item in response.json().get("data",[])}
            return {"ready":self.model in ids,"provider":"vllm","model":self.model,"model_present":self.model in ids}
        except Exception as exc: return {"ready":False,"provider":"vllm","model":self.model,"reason":type(exc).__name__}

    async def warmup(self) -> dict[str, Any]:
        started=time.perf_counter()
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response=await client.post(f"{self.base_url}/v1/chat/completions",json=self._payload([{"role":"user","content":"Return ok."}],False,2)); response.raise_for_status()
            return {"ok":True,"latency_ms":round((time.perf_counter()-started)*1000,2)}
        except Exception as exc: return {"ok":False,"latency_ms":round((time.perf_counter()-started)*1000,2),"reason":type(exc).__name__}

class MockTTSProvider:
    """Deterministic audible stand-in for TTS used by the mock/dev profile.

    A mock response must still have real PCM duration.  Returning one sample per
    character makes the browser play a sub-millisecond click and immediately
    enter the waiting state, which hides sequencing bugs in the audio path.
    This waveform is intentionally synthetic (production uses Silero); it is
    only a stable, audible development timing signal.
    """

    SAMPLE_RATE = 24_000

    def __init__(self, fail: bool = False, delay_s: float = 0.0):
        self.fail = fail
        self.delay_s = delay_s

    async def synthesize(self, text, token, cancelled):
        await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("mock TTS unavailable")
        if cancelled():
            raise asyncio.CancelledError

        # Approximate speech timing: 55 ms per character plus short margins,
        # bounded so stress tests cannot allocate an unbounded audio buffer.
        duration_s = min(6.0, max(0.12, 0.055 * len(text) + 0.08))
        count = max(1, int(round(self.SAMPLE_RATE * duration_s)))
        samples: list[float] = []
        for index in range(count):
            if cancelled():
                raise asyncio.CancelledError
            t = index / self.SAMPLE_RATE
            progress = index / max(1, count - 1)
            attack = min(1.0, progress / 0.025)
            release = min(1.0, (1.0 - progress) / 0.08)
            envelope = 0.16 * attack * release
            # Two harmonics make the mock clearly audible without pretending
            # to be a real voice recording.
            pitch = 185.0 + 24.0 * math.sin(2.0 * math.pi * 2.1 * t)
            value = envelope * (math.sin(2.0 * math.pi * pitch * t) + 0.28 * math.sin(2.0 * math.pi * 2.0 * pitch * t))
            samples.append(float(value))
        return AudioChunk(tuple(samples), self.SAMPLE_RATE, token)


class SileroTTSProvider:
    """Silero v5.5 adapter with one bounded non-interruptible worker."""

    provider_name = "silero"

    def __init__(self, engine):
        self.engine = engine
        self._worker = _BoundedNativeWorker("mishka-silero-tts", queue_size=1)

    def readiness(self):
        return {**self.engine.readiness(), "in_process": True}

    def native_worker_snapshot(self):
        return self._worker.snapshot()

    def _synthesize_with_preroll(self, text):
        method = self.engine.synthesize_blocking
        leading_ms = int(getattr(self.engine, "leading_silence_ms", 150))
        try:
            return method(text, leading_silence_ms=leading_ms)
        except TypeError as exc:
            if "leading_silence_ms" not in str(exc):
                raise
            samples, sample_rate = method(text)
            return prepend_pre_roll(samples, sample_rate, leading_ms), sample_rate

    async def synthesize(self, text, token, cancelled):
        if cancelled():
            raise asyncio.CancelledError
        native_future = self._worker.submit(
            token, cancelled, lambda: self._synthesize_with_preroll(text)
        )
        return await self._await_native(native_future, token, cancelled)

    async def synthesize_stream_chunk(self, text, token, cancelled, *, first_chunk):
        """Synthesize stream audio with pre-roll only on chunk zero."""
        if cancelled():
            raise asyncio.CancelledError
        stream_method = getattr(self.engine, "synthesize_streaming_blocking", None)
        if stream_method is not None:
            # The engine owns the one-time 150 ms pre-roll.  Do not prepend it
            # again here: doing so would alter the clean Silero PCM contract.
            native_future = self._worker.submit(
                token, cancelled, lambda: stream_method(text, first_chunk=first_chunk)
            )
            return await self._await_native(native_future, token, cancelled)
        native_future = self._worker.submit(
            token, cancelled, lambda: self._synthesize_with_preroll(text)
        )
        audio = await self._await_native(native_future, token, cancelled)
        if first_chunk:
            leading_ms = int(getattr(self.engine, "leading_silence_ms", 150))
            leading = (0.0,) * round(audio.sample_rate * leading_ms / 1000)
            audio = AudioChunk(leading + audio.samples, audio.sample_rate, audio.token)
        return audio

    async def _await_native(self, native_future, token, cancelled):
        try:
            samples, sample_rate = await asyncio.wrap_future(native_future)
        except asyncio.CancelledError:
            native_future.cancel()
            raise
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "worker queue is full" in str(exc):
                raise
            raise RuntimeError("Silero TTS synthesis failed") from exc
        if cancelled():
            raise asyncio.CancelledError
        return AudioChunk(samples, sample_rate, token)


class CachedTTSProvider:
    """Bounded in-memory cache for generated bear speech; no child input is stored."""

    def __init__(self, delegate: TTSProvider, max_items: int = 64):
        self.delegate=delegate; self.max_items=max_items; self.cache: dict[str, tuple[tuple[float,...],int]]={}

    @property
    def provider_name(self):
        return getattr(self.delegate, "provider_name", type(self.delegate).__name__.lower())

    async def synthesize(self,text,token,cancelled):
        cached=self.cache.get(text)
        if cached is not None: return AudioChunk(cached[0],cached[1],token)
        audio=await self.delegate.synthesize(text,token,cancelled)
        if len(self.cache)>=self.max_items: self.cache.pop(next(iter(self.cache)))
        self.cache[text]=(audio.samples,audio.sample_rate)
        return audio

    async def synthesize_stream_chunk(self, text, token, cancelled, *, first_chunk):
        stream_method = getattr(self.delegate, "synthesize_stream_chunk", None)
        if stream_method is None:
            return await self.delegate.synthesize(text, token, cancelled)
        # Stream padding is position-dependent; never cache padded chunks.
        return await stream_method(text, token, cancelled, first_chunk=first_chunk)
class DictAudioAssetProvider:
    def __init__(self, assets=None): self.assets=assets or {}
    async def get(self,asset_id): return self.assets.get(asset_id)


class RVCV2Provider:
    """Persistent, process-isolated RVC v2 adapter.

    Exactly one Applio worker owns the CUDA models and handles one conversion
    at a time. A cancelled or timed-out conversion terminates that worker so
    stale PCM cannot escape; the next request starts a clean worker. The
    surrounding audio queue remains the bounded owner of pending chunks.
    """

    def __init__(self, converter=None, *, project_root: str = "", python: str = "python3", model_path: str = "", index_path: str = "", f0_method: str = "rmvpe", filter_radius: float = 3.0, f0_up_key: int = 0, index_rate: float = 0.0, protect: float = 0.33, tmp_dir: str = "runtime/cache/rvc-tmp", startup_timeout_s: float = 60.0, worker_script: str = ""):
        self.converter=converter; self.project_root=_project_path(project_root) if project_root else None
        python_path = _project_path(python) if not Path(python).is_absolute() and _project_path(python).is_file() else Path(python)
        self.python=str(python_path); self.model_path=_project_path(model_path) if model_path else None; self.index_path=_project_path(index_path) if index_path else None
        self.f0_method=f0_method; self.filter_radius=filter_radius; self.f0_up_key=f0_up_key; self.index_rate=index_rate; self.protect=protect; self.startup_timeout_s=float(startup_timeout_s); self.tmp_dir=_project_path(tmp_dir)
        default_worker=PROJECT_ROOT/"scripts"/"rvc_worker.py"
        self.worker_script=_project_path(worker_script) if worker_script else default_worker
        self._process: asyncio.subprocess.Process | None=None
        self._start_lock=asyncio.Lock(); self._job_lock=asyncio.Lock()
        self._stderr_task: asyncio.Task | None=None; self._stderr_tail: deque[str]=deque(maxlen=20)
        self._worker_info: dict[str,Any]={}; self._active=False
        self.jobs_submitted=0; self.jobs_completed=0; self.jobs_cancelled=0; self.jobs_failed=0; self.worker_restarts=0
        self.last_latency_ms: float | None=None

    def _configuration_readiness(self) -> dict[str, Any]:
        if self.converter is not None:
            return {"ready": True, "configuration_ready": True, "provider": "rvc_v2", "mode": "in_memory_callable", "worker_alive": True, "worker_pid": None}
        executable=shutil.which(self.python) if not Path(self.python).is_absolute() else (self.python if Path(self.python).is_file() else None)
        root=bool(self.project_root and (self.project_root/"rvc/infer/infer.py").is_file())
        model=bool(self.model_path and self.model_path.is_file()); index=bool(self.index_path and self.index_path.is_file())
        worker=self.worker_script.is_file()
        candidate=self.tmp_dir if self.tmp_dir.exists() else self.tmp_dir.parent
        ram_tmp=candidate.exists() and os.access(candidate,os.W_OK)
        process_alive=bool(self._process and self._process.returncode is None)
        configuration_ready=bool(executable and root and model and index and worker and ram_tmp)
        return {"ready":configuration_ready, "configuration_ready":configuration_ready,
                "reason":None if configuration_ready else "rvc_configuration_unavailable",
                "provider":"rvc_v2","mode":"persistent_worker","python":bool(executable),"project":root,"model":model,"index":index,"worker":worker,
                "worker_alive":process_alive,"worker_pid":self._process.pid if process_alive else None,"worker_info":dict(self._worker_info),"ram_tmp":ram_tmp,
                "f0_method":self.f0_method,"filter_radius":self.filter_radius,"f0_up_key":self.f0_up_key,"index_rate":self.index_rate,"protect":self.protect,"active":self._active,
                "jobs_submitted":self.jobs_submitted,"jobs_completed":self.jobs_completed,"jobs_cancelled":self.jobs_cancelled,"jobs_failed":self.jobs_failed,
                "worker_restarts":self.worker_restarts,"last_latency_ms":self.last_latency_ms}

    def configuration_readiness(self) -> dict[str, Any]:
        return self._configuration_readiness()

    def readiness(self) -> dict[str, Any]:
        status=self._configuration_readiness()
        if self.converter is not None:
            return status
        live=bool(status.get("configuration_ready") and status.get("worker_alive"))
        status["ready"] = live
        status["reason"] = None if live else ("worker_not_alive" if status.get("configuration_ready") else "rvc_configuration_unavailable")
        return status

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            line=await stream.readline()
            if not line: return
            text=line.decode("utf-8","replace").strip()
            if text:
                self._stderr_tail.append(text[:500])


    async def _ensure_worker(self) -> None:
        if self._process is not None and self._process.returncode is None: return
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None: return
            status=self.configuration_readiness()
            if not status.get("configuration_ready"): raise RuntimeError("RVC v2 runtime/model/index unavailable")
            assert self.project_root is not None and self.model_path is not None and self.index_path is not None
            self.tmp_dir.mkdir(parents=True,exist_ok=True)
            env=os.environ.copy(); env["PYTHONUNBUFFERED"]="1"; env.setdefault("TOKENIZERS_PARALLELISM","false")
            self._process=await asyncio.create_subprocess_exec(
                self.python,str(self.worker_script),"--project-root",str(self.project_root),
                "--model",str(self.model_path),"--index",str(self.index_path),
                "--f0-method",self.f0_method,"--filter-radius",str(self.filter_radius),"--f0-up-key",str(self.f0_up_key),"--index-rate",str(self.index_rate),"--protect",str(self.protect),
                cwd=self.project_root,env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,start_new_session=True,
            )
            assert self._process.stdout is not None and self._process.stderr is not None
            self._stderr_task=asyncio.create_task(self._drain_stderr(self._process.stderr))
            try:
                raw=await asyncio.wait_for(self._process.stdout.readline(),self.startup_timeout_s)
                reply=json.loads(raw) if raw else {}
                if reply.get("event")!="ready": raise RuntimeError("RVC worker did not become ready")
                self._worker_info=reply; self.worker_restarts+=1
            except BaseException:
                await self._stop_worker(force=True)
                raise

    async def _stop_worker(self, *, force: bool=False) -> None:
        process=self._process; self._process=None; self._worker_info={}
        if process is not None and process.returncode is None:
            if not force and process.stdin is not None:
                try:
                    process.stdin.write(b'{"op":"shutdown","id":"shutdown"}\n'); await process.stdin.drain()
                    await asyncio.wait_for(process.wait(),2.0)
                except (BrokenPipeError,ConnectionError,TimeoutError):
                    pass
            if process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(),3.0)
                except TimeoutError:
                    process.kill(); await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self._stderr_task
            self._stderr_task=None

    async def close(self) -> None:
        async with self._job_lock: await self._stop_worker()

    async def _read_reply(self, request_id: str, cancelled) -> dict[str,Any]:
        assert self._process is not None and self._process.stdout is not None
        read_task=asyncio.create_task(self._process.stdout.readline())
        try:
            while True:
                done,_=await asyncio.wait({read_task},timeout=.05)
                if read_task in done:
                    raw=read_task.result()
                    if not raw: raise RuntimeError("RVC worker exited: "+" | ".join(self._stderr_tail)[-1000:])
                    reply=json.loads(raw)
                    if str(reply.get("id"))!=request_id: raise RuntimeError("RVC worker response id mismatch")
                    return reply
                if cancelled(): raise asyncio.CancelledError
        finally:
            if not read_task.done(): read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await read_task

    async def convert(self,audio,cancelled):
        if cancelled(): raise asyncio.CancelledError
        if self.converter is not None:
            converted_input = np.asarray(audio.samples, dtype=np.float32)
            if audio.sample_rate != RVC_SAMPLE_RATE:
                converted_input = np.asarray(
                    resample_mono(audio.samples, audio.sample_rate, RVC_SAMPLE_RATE),
                    dtype=np.float32,
                )
            if not np.all(np.isfinite(converted_input)) or np.max(np.abs(converted_input), initial=0.0) > 1.0:
                raise ValueError("RVC input must be finite mono float32 in [-1, 1]")
            samples = self.converter(converted_input, RVC_SAMPLE_RATE)
            if cancelled(): raise asyncio.CancelledError
            return AudioChunk(peak_normalize(samples), RVC_SAMPLE_RATE, audio.token)
        async with self._job_lock:
            if cancelled(): raise asyncio.CancelledError
            await self._ensure_worker()
            assert self._process is not None and self._process.stdin is not None
            self.tmp_dir.mkdir(parents=True,exist_ok=True); self.jobs_submitted+=1; self._active=True
            request_id=uuid.uuid4().hex; started=time.perf_counter()
            try:
                with tempfile.TemporaryDirectory(prefix="turn-",dir=self.tmp_dir) as folder:
                    input_path=Path(folder)/"silero.wav"; output_path=Path(folder)/"rvc.wav"
                    rvc_input = audio
                    if audio.sample_rate != RVC_SAMPLE_RATE:
                        rvc_input = AudioChunk(
                            resample_mono(audio.samples, audio.sample_rate, RVC_SAMPLE_RATE),
                            RVC_SAMPLE_RATE,
                            audio.token,
                        )
                    self._write_wav(input_path,rvc_input)
                    request={"op":"convert","id":request_id,"input":str(input_path),"output":str(output_path)}
                    self._process.stdin.write((json.dumps(request,separators=(",",":"))+"\n").encode())
                    await self._process.stdin.drain()
                    reply=await self._read_reply(request_id,cancelled)
                    if reply.get("event")!="converted": raise RuntimeError("RVC v2 conversion failed: "+str(reply.get("message") or reply.get("error") or "worker_error")[:500])
                    if cancelled(): raise asyncio.CancelledError
                    samples,rate=self._read_wav(output_path)
                if cancelled(): raise asyncio.CancelledError
                if rate != RVC_SAMPLE_RATE:
                    samples = resample_mono(samples, rate, RVC_SAMPLE_RATE)
                    rate = RVC_SAMPLE_RATE
                samples = peak_normalize(samples)
                self.jobs_completed+=1; self.last_latency_ms=round((time.perf_counter()-started)*1000,2)
                return AudioChunk(samples,rate,audio.token)
            except asyncio.CancelledError:
                self.jobs_cancelled+=1; await self._stop_worker(force=True); raise
            except Exception:
                self.jobs_failed+=1; await self._stop_worker(force=True); raise
            finally:
                self._active=False

    @staticmethod
    def _write_wav(path: Path, audio: AudioChunk) -> None:
        samples = np.asarray(audio.samples, dtype=np.float32)
        if samples.ndim != 1 or not np.all(np.isfinite(samples)):
            raise ValueError("RVC input must be finite mono float32")
        if np.max(np.abs(samples), initial=0.0) > 1.0:
            raise ValueError("RVC input amplitude exceeds [-1, 1]")
        # PCM16 is only the transport container for the file-based vendor API;
        # no limiter, compressor, equalizer, or np.clip is applied here.
        pcm = np.rint(samples * 32767.0).astype(np.int16)
        if sys.byteorder != "little":
            pcm.byteswap()
        with wave.open(str(path),"wb") as target:
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(audio.sample_rate); target.writeframes(pcm.tobytes())

    @staticmethod
    def _read_wav(path: Path) -> tuple[tuple[float,...],int]:
        with wave.open(str(path),"rb") as source:
            if source.getnchannels()!=1 or source.getsampwidth()!=2: raise RuntimeError("RVC output must be mono PCM16")
            rate=source.getframerate(); raw=source.readframes(source.getnframes())
        pcm=array.array("h"); pcm.frombytes(raw)
        if sys.byteorder!="little": pcm.byteswap()
        return tuple(value/32768.0 for value in pcm),rate


class RVCProvider:
    """Mock/pass-through converter retained for GPU-free tests only."""

    async def convert(self, audio, cancelled):
        if cancelled():
            raise asyncio.CancelledError
        return audio


class PassthroughVoiceProvider:
    """Explicit non-production bypass for profiles that disable conversion."""

    def readiness(self):
        return {"ready": True, "provider": "passthrough", "bypassed": True}

    async def convert(self, audio, cancelled):
        if cancelled():
            raise asyncio.CancelledError
        return audio


class ResilientVoiceProvider:
    """Make optional RVC degradation local to one audio chunk.

    RVC is a quality enhancement, not a prerequisite for a usable call.  The
    wrapper exposes a healthy user-facing provider while retaining the primary
    adapter diagnostics for /health and emitting a warning on each transition
    to passthrough.
    """

    def __init__(self, primary: RVCV2Provider):
        self.primary = primary
        self.last_fallback_reason: str | None = None

    def configuration_readiness(self) -> dict[str, Any]:
        return self.primary.configuration_readiness()

    def readiness(self) -> dict[str, Any]:
        primary = self.primary.readiness()
        return {
            "ready": True,
            "provider": "rvc_v2+passthrough",
            "degraded": not bool(primary.get("ready")),
            "fallback": "passthrough",
            "primary": primary,
        }

    async def convert(self, audio, cancelled):
        if cancelled():
            raise asyncio.CancelledError
        try:
            result = await self.primary.convert(audio, cancelled)
            self.last_fallback_reason = getattr(self.primary, "last_fallback_reason", None)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_fallback_reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.warning("RVC conversion unavailable; returning clean Silero audio: %s", self.last_fallback_reason)
            return audio

    async def close(self) -> None:
        await self.primary.close()


class MockPlaybackController:
    def __init__(self): self.played=[]; self.stops=0; self.active_token=0
    async def play(self,audio):
        if audio.token==self.active_token: self.played.append(audio)
    async def stop(self): self.stops+=1; self.played.clear()


class SoundDevicePlaybackController:
    """Lazy, cancellable local speaker adapter without a daemon worker thread."""

    def __init__(self, device: str | int | None = None):
        self.device=device; self.active_token=0; self._stream=None; self._generation=0

    def _device_arg(self):
        value = self.device
        if value is None or value == "":
            return None
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    def readiness(self) -> dict[str, Any]:
        package = importlib.util.find_spec("sounddevice") is not None
        if not package:
            return {"ready": False, "provider": "sounddevice", "package": False,
                    "device": self.device}
        try:
            import sounddevice as sd
            info = sd.query_devices(self._device_arg(), "output")
            channels = int(info.get("max_output_channels", 0))
            return {"ready": channels > 0, "provider": "sounddevice", "package": True,
                    "device": self.device, "max_output_channels": channels}
        except Exception as exc:
            return {"ready": False, "provider": "sounddevice", "package": True,
                    "device": self.device, "reason": f"{type(exc).__name__}: {str(exc)[:180]}"}

    async def play(self,audio):
        generation=self._generation
        try:
            import numpy as np
            import sounddevice as sd
            stream=sd.OutputStream(samplerate=audio.sample_rate,channels=1,dtype="float32",device=self._device_arg())
            self._stream=stream
            await asyncio.to_thread(stream.start)
            if generation!=self._generation or audio.token!=self.active_token: return
            await asyncio.to_thread(stream.write,np.asarray(audio.samples,dtype=np.float32).reshape(-1,1))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError("local playback unavailable") from exc
        finally:
            stream=self._stream
            if stream is not None:
                with contextlib.suppress(Exception): await asyncio.to_thread(stream.abort)
                with contextlib.suppress(Exception): await asyncio.to_thread(stream.close)
                if self._stream is stream: self._stream=None

    async def stop(self):
        self._generation+=1
        stream=self._stream
        if stream is not None:
            with contextlib.suppress(Exception): await asyncio.to_thread(stream.abort)
            with contextlib.suppress(Exception): await asyncio.to_thread(stream.close)
            if self._stream is stream: self._stream=None

class SoundDeviceCaptureController:
    """Cancellable local microphone adapter for the physical headless stand.

    PortAudio callbacks only copy bounded PCM frames and hand them to the
    asyncio loop; inference stays outside the real-time callback.
    """

    def __init__(self, device: str | int | None = None, *, sample_rate: int = 16_000,
                 channels: int = 1, blocksize: int = 512):
        self.device = device
        self.sample_rate = int(sample_rate)
        self.channels = max(1, int(channels))
        self.blocksize = max(64, int(blocksize))
        self._stream = None
        self._loop = None
        self._on_frame = None
        self._frames = 0
        self._last_status = None
        self._last_error = None

    def _device_arg(self):
        value = self.device
        if value is None or value == "":
            return None
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    def readiness(self) -> dict[str, Any]:
        package = importlib.util.find_spec("sounddevice") is not None
        if not package:
            return {"ready": False, "provider": "sounddevice", "direction": "input",
                    "package": False, "device": self.device}
        try:
            import sounddevice as sd
            info = sd.query_devices(self._device_arg(), "input")
            available = int(info.get("max_input_channels", 0))
            return {"ready": available >= self.channels, "provider": "sounddevice",
                    "direction": "input", "package": True, "device": self.device,
                    "max_input_channels": available, "sample_rate": self.sample_rate,
                    "channels": self.channels}
        except Exception as exc:
            return {"ready": False, "provider": "sounddevice", "direction": "input",
                    "package": True, "device": self.device,
                    "reason": f"{type(exc).__name__}: {str(exc)[:180]}"}

    def is_ready(self) -> bool:
        return self._stream is not None

    def start(self, loop: asyncio.AbstractEventLoop, on_frame: Callable[[Any], None]) -> None:
        if self.is_ready():
            return
        import sounddevice as sd
        self._loop = loop
        self._on_frame = on_frame
        self._last_error = None
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate, channels=self.channels,
                dtype="float32", blocksize=self.blocksize, device=self._device_arg(),
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            stream, self._stream = self._stream, None
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            self._last_error = {"exception_type": type(exc).__name__, "detail": str(exc)[:180]}
            raise RuntimeError("local microphone capture unavailable") from exc

    def _callback(self, indata, _frames, _time_info, status) -> None:
        if status:
            self._last_status = str(status)
        loop = self._loop
        on_frame = self._on_frame
        if loop is None or on_frame is None or loop.is_closed():
            return
        try:
            values = np.asarray(indata, dtype=np.float32)
            values = values.mean(axis=1, dtype=np.float32) if values.ndim == 2 and values.shape[1] > 1 else values.reshape(-1)
            loop.call_soon_threadsafe(on_frame, np.ascontiguousarray(values, dtype=np.float32))
            self._frames += 1
        except Exception as exc:
            self._last_error = {"exception_type": type(exc).__name__, "detail": str(exc)[:180]}

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        self._on_frame = None
        self._loop = None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.abort()
            with contextlib.suppress(Exception):
                stream.close()

    def diagnostics(self) -> dict[str, Any]:
        result = {"ready": self.is_ready(), "provider": "sounddevice",
                  "backend": "portaudio", "direction": "input", "device": self.device,
                  "sample_rate": self.sample_rate, "channels": self.channels,
                  "blocksize": self.blocksize, "frames": self._frames,
                  "last_status": self._last_status, "last_error": self._last_error}
        if not self.is_ready():
            result.update({key: value for key, value in self.readiness().items() if key not in result})
        return result


class CancellationController:
    def __init__(self): self.tokens={}; self.events={}
    def start(self,session_id):
        token=self.tokens.get(session_id,0)+1; self.tokens[session_id]=token; self.events[session_id]=asyncio.Event(); return token
    def cancelled(self,session_id,token): return self.tokens.get(session_id)!=token or self.events.get(session_id,asyncio.Event()).is_set()
    def cancel(self,session_id):
        self.tokens[session_id]=self.tokens.get(session_id,0)+1
        if session_id in self.events: self.events[session_id].set()

class TTSQueueOverflowError(RuntimeError):
    """The bounded TTS queue could not accept a chunk within its budget."""

class ResponseValidationError(RuntimeError):
    """A streamed response was rejected before it could be spoken."""

    def __init__(
        self,
        reason: str,
        source: str = "response_validation",
        latency_ms: float = 0.0,
        timeout_ms: float | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.source = source
        self.latency_ms = float(latency_ms)
        self.timeout_ms = timeout_ms

@dataclass
class _AudioOwnership:
    chunk_index: int
    queued_bytes: int
    state: str = "queued"
    active_audio_bytes: int = 0


class AudioSynthesisQueue:
    _TERMINAL_STATES = frozenset({"finished", "cancelled", "failed"})

    def __init__(self,tts,voice,playback,cancel,config,metrics,trace_generation_id=None,voice_mode="rvc_v2",fallback_mode="passthrough"):
        self.tts,self.voice,self.playback,self.cancel,self.config=tts,voice,playback,cancel,config
        self.metrics=metrics
        self.trace_generation_id=trace_generation_id
        # Keep the conversion decision explicit on the queue.  The queue is
        # the actual user-turn boundary; relying on an absent ``settings``
        # attribute here silently defaulted every production turn to the legacy voice path.
        self.voice_mode=(voice_mode or "rvc_v2").strip().lower()
        self.fallback_mode=(fallback_mode or "passthrough").strip().lower()
        self.last_fallback_reason=None
        self.queue: asyncio.Queue[tuple[int, str] | None]=asyncio.Queue(config.queue_size)
        self.worker: asyncio.Task[None] | None=None
        self.error: BaseException | None = None
        self.next_chunk_index=0
        self.first_audio_ready: asyncio.Event | None = None
        self.failure_event: asyncio.Event | None = None
        self.session_id: str | None = None
        self.token: int | None = None
        self.closed=False
        self.queued_bytes=0
        self.submitted_chunks=0
        self.cancelled_chunks=0
        self.completed_chunks=0
        self.failed_chunks=0
        self.active_audio_bytes=0
        self._ownership: dict[int, _AudioOwnership] = {}
        self.owner = ""
        self.turn_id = ""

    async def start(self,session_id,token):
        if self.worker is not None and not self.worker.done():
            raise RuntimeError("audio synthesis queue already running")
        self.queue=asyncio.Queue(self.config.queue_size)
        self.error=None
        self.next_chunk_index=0
        self.session_id=session_id; self.token=token; self.closed=False
        self.owner=f"s2s-tts:{session_id}:{token}"
        self.turn_id=f"{session_id}:turn:{token}"
        self.queued_bytes=0; self.submitted_chunks=0; self.cancelled_chunks=0
        self.completed_chunks=0; self.failed_chunks=0; self.active_audio_bytes=0
        self._ownership={}
        self.first_audio_ready=asyncio.Event(); self.failure_event=asyncio.Event()
        self.worker=asyncio.create_task(self._run(session_id,token), name=f"s2s-tts-{session_id}-{token}")

    def _transition(self, chunk_index: int, state: str, *, audio_bytes: int = 0) -> bool:
        """Apply one idempotent ownership transition on the event-loop owner."""
        ownership=self._ownership.get(chunk_index)
        if ownership is None or ownership.state in self._TERMINAL_STATES:
            return False
        current=ownership.state
        allowed={
            "processing": {"queued"},
            "playing": {"processing"},
            "finished": {"playing"},
            "cancelled": {"queued", "processing", "playing"},
            "failed": {"queued", "processing", "playing"},
        }
        if state not in allowed or current not in allowed[state]:
            return False
        if current == "queued":
            self.queued_bytes=max(0, self.queued_bytes-ownership.queued_bytes)
        if current == "playing":
            self.active_audio_bytes=max(0, self.active_audio_bytes-ownership.active_audio_bytes)
            ownership.active_audio_bytes=0
        if state == "playing":
            ownership.active_audio_bytes=max(0, int(audio_bytes))
            self.active_audio_bytes += ownership.active_audio_bytes
        ownership.state=state
        if state == "finished":
            self.completed_chunks += 1
        elif state == "cancelled":
            self.cancelled_chunks += 1
        elif state == "failed":
            self.failed_chunks += 1
        if hasattr(self.metrics, "chunk_trace"):
            event_name = {"processing": "start", "playing": "play_start", "finished": "play_finish", "cancelled": "cancel", "failed": "fail"}.get(state, state)
            self.metrics.chunk_trace(
                session_id=self.session_id or "", turn_id=self.turn_id,
                generation_id=self.trace_generation_id if self.trace_generation_id is not None else (self.token or 0),
                chunk_index=chunk_index, owner=self.owner, event=event_name,
                lifecycle=state, terminal_reason=state if state in self._TERMINAL_STATES else None,
                byte_count=audio_bytes if state == "playing" else None,
            )
        return True

    def _cancel_open_ownership(self, state: str = "cancelled") -> None:
        for chunk_index, ownership in tuple(self._ownership.items()):
            if ownership.state not in self._TERMINAL_STATES:
                self._transition(chunk_index, state)
    async def wait_first_audio(self, timeout_s: float) -> bool:
        event=self.first_audio_ready
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.01, timeout_s))
            return True
        except asyncio.TimeoutError:
            return False

    async def submit(self,text):
        if self.error is not None: raise self.error
        if self.closed or self.worker is None:
            raise RuntimeError("audio synthesis queue is closed")
        item_bytes=max(1,len(text.encode("utf-8"))*4)
        if (self.submitted_chunks >= self.config.max_tts_chunks_per_generation or
                self.queued_bytes + item_bytes > self.config.max_tts_queue_bytes):
            self.error=TTSQueueOverflowError("TTS queue resource limit exceeded")
            self.closed=True
            self.cancel(self.session_id)
            self._drain("cancelled")
            raise self.error
        chunk_index=self.next_chunk_index; self.next_chunk_index+=1
        item=(chunk_index,text)
        self._ownership[chunk_index]=_AudioOwnership(chunk_index,item_bytes)
        self.queued_bytes+=item_bytes; self.submitted_chunks+=1
        if hasattr(self.metrics, "chunk_trace"):
            self.metrics.chunk_trace(
                session_id=self.session_id or "", turn_id=self.turn_id,
                generation_id=self.trace_generation_id if self.trace_generation_id is not None else (self.token or 0),
                chunk_index=chunk_index, owner=self.owner, event="enqueue",
                lifecycle="queued", byte_count=item_bytes,
            )
        deadline=asyncio.get_running_loop().time()+self.config.queue_put_timeout_s
        while True:
            if self.error is not None:
                self._transition(chunk_index,"failed")
                raise self.error
            if self.closed:
                self._transition(chunk_index,"cancelled")
                raise RuntimeError("audio synthesis queue is closed")
            if self.cancel.cancelled(self.session_id or "", self.token or 0):
                self._transition(chunk_index,"cancelled")
                raise asyncio.CancelledError
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining <= 0:
                overflow=TTSQueueOverflowError("bounded TTS queue backpressure budget exceeded")
                self._transition(chunk_index,"cancelled")
                self.error=overflow; self.closed=True; self.cancel.cancel(self.session_id or ""); self._drain("cancelled")
                if self.failure_event is not None: self.failure_event.set()
                await self.abort(self.session_id or "", invalidate=False)
                raise overflow
            try:
                await asyncio.wait_for(self.queue.put(item), min(0.05, remaining))
                return
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                self._transition(chunk_index,"cancelled")
                raise
    def resource_snapshot(self) -> dict[str, Any]:
        states=defaultdict(int)
        for ownership in self._ownership.values():
            states[ownership.state]+=1
        return {
            "queued_chunks": self.queue.qsize(),
            "queued_bytes": max(0,self.queued_bytes),
            "active_audio_bytes": max(0,self.active_audio_bytes),
            "pcm_bytes": max(0,self.active_audio_bytes),
            "cancelled_chunks": max(0,self.cancelled_chunks),
            "completed_chunks": max(0,self.completed_chunks),
            "failed_chunks": max(0,self.failed_chunks),
            "submitted_chunks": self.submitted_chunks,
            "ownership_states": dict(states),
            "ownership_size": len(self._ownership),
        }

    async def finish(self):
        if self.worker is None:
            raise RuntimeError("audio synthesis queue was not started")
        if self.error is None and not self.closed:
            await self.queue.put(None)
        if self.worker is not None and not self.worker.done():
            await self.worker
        if self.error is not None: raise self.error
    async def abort(self,session_id, *, invalidate=True):
        self.metrics.event("audio_cancelled", reason="barge_in_or_reset")
        if invalidate: self.cancel.cancel(session_id)
        self.closed=True
        self._drain("cancelled")
        if self.worker and not self.worker.done():
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.worker
        self._drain("cancelled")
        self._cancel_open_ownership("cancelled")
        await self.playback.stop()
    def _drain(self, terminal_state: str = "cancelled") -> int:
        drained=0
        while True:
            try:
                item=self.queue.get_nowait()
                if item is not None:
                    self._transition(item[0], terminal_state)
                    drained+=1
                self.queue.task_done()
            except asyncio.QueueEmpty:
                return drained
    async def _run(self,session_id,token):
        while True:
            item=await self.queue.get()
            stage="queue"; started=time.perf_counter(); playback_started=False
            chunk_index: int | None = None
            try:
                if item is None: return
                chunk_index,text=item
                ownership=self._ownership.get(chunk_index)
                if ownership is None or ownership.state != "queued":
                    continue
                self._transition(chunk_index,"processing")
                if self.error is not None:
                    self._transition(chunk_index,"failed")
                    continue
                cancelled=lambda:self.cancel.cancelled(session_id,token)
                stage="silero_tts"; started=time.perf_counter()
                provider = getattr(self.tts, "provider_name", type(self.tts).__name__.lower())
                self.metrics.event("tts_started", provider=provider, chunk_index=chunk_index, token=token,
                                   stream_preroll_ms=150 if provider == "silero" and chunk_index == 0 else 0)
                self.metrics.mark_voice(session_id, "tts_request", generation_id=self.trace_generation_id)
                stream_method = getattr(self.tts, "synthesize_stream_chunk", None)
                synthesis = (stream_method(text, token, cancelled, first_chunk=(chunk_index == 0))
                             if stream_method is not None else self.tts.synthesize(text, token, cancelled))
                audio=await asyncio.wait_for(synthesis,self.config.tts_timeout_s)
                self.metrics.observe(stage,started)
                self.metrics.observe_voice_stage(session_id, "tts", started, generation_id=self.trace_generation_id)
                if self.voice_mode in {"off", "disabled"}:
                    self.metrics.event("voice_conversion_bypassed", provider="passthrough", chunk_index=chunk_index, token=token)
                else:
                    stage="rvc_v2"; started=time.perf_counter(); original_audio=audio
                    try:
                        audio=await asyncio.wait_for(self.voice.convert(audio,cancelled), self.config.rvc_timeout_s)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_fallback_reason=f"{type(exc).__name__}: {str(exc)[:160]}"
                        audio=original_audio
                        logger.warning("RVC chunk failed; returning clean Silero audio: %s", self.last_fallback_reason)
                        self.metrics.event("rvc_v2_fallback", reason=self.last_fallback_reason, provider="passthrough", policy="passthrough")
                    self.metrics.observe(stage,started)
                    fallback_reason=getattr(self.voice,"last_fallback_reason",None)
                    if fallback_reason: self.metrics.event("rvc_v2_fallback",reason=fallback_reason,provider="passthrough")
                # RVC PCM is delivered unchanged; there is no post-RVC audio processing.
                self.metrics.event("audio_ready", provider=provider, chunk_index=chunk_index, token=token, sample_rate=audio.sample_rate, samples=len(audio.samples))
                if hasattr(self.metrics, "chunk_trace"):
                    self.metrics.chunk_trace(
                        session_id=session_id, turn_id=self.turn_id,
                        generation_id=self.trace_generation_id if self.trace_generation_id is not None else token,
                        chunk_index=chunk_index, owner=self.owner, event="audio_ready",
                        lifecycle="processing", sample_count=len(audio.samples), byte_count=len(audio.samples) * 4,
                    )
                self.metrics.mark_voice(session_id, "first_audio_chunk", generation_id=self.trace_generation_id)
                if chunk_index == 0 and self.first_audio_ready is not None:
                    self.first_audio_ready.set()
                if audio.token != token:
                    self._transition(chunk_index,"cancelled")
                    self.metrics.event("audio_dropped", reason="invalid_generation_tag", chunk_index=chunk_index, token=audio.token)
                    continue
                if not cancelled():
                    self._transition(chunk_index,"playing",audio_bytes=len(audio.samples)*4)
                    stage="playback"; started=time.perf_counter(); self.playback.active_token=token
                    self.metrics.event("playback_started", chunk_index=chunk_index, token=token)
                    self.metrics.mark_voice(session_id, "playback_start", generation_id=self.trace_generation_id)
                    if hasattr(self.metrics, "mark_playback_start"):
                        self.metrics.mark_playback_start(session_id)
                    playback_started=True
                    await asyncio.wait_for(self.playback.play(audio),self.config.playback_timeout_s); self.metrics.observe(stage,started); self.metrics.observe_voice_stage(session_id, "playback", started, generation_id=self.trace_generation_id)
                    if cancelled():
                        self._transition(chunk_index,"cancelled")
                        self.metrics.event("playback_cancelled", chunk_index=chunk_index, token=token)
                    else:
                        self._transition(chunk_index,"finished")
                        self.metrics.event("playback_finished", chunk_index=chunk_index, token=token)
                else:
                    self._transition(chunk_index,"cancelled")
            except asyncio.CancelledError:
                if chunk_index is not None:
                    self._transition(chunk_index,"cancelled")
                current=asyncio.current_task()
                if current is not None and getattr(current,"cancelling",lambda: 1)():
                    raise
                self.error=TimeoutError("audio generation cancelled before output")
                self.closed=True
                if self.failure_event is not None: self.failure_event.set()
                self.metrics.observe(stage,started,False)
                self.cancel.cancel(session_id)
                self._drain("cancelled")
                return
            except Exception as exc:
                if chunk_index is not None:
                    self._transition(chunk_index,"failed")
                self.error = exc
                self.closed = True
                if self.failure_event is not None: self.failure_event.set()
                self.metrics.observe(stage,started,False)
                self.cancel.cancel(session_id)
                self._drain("failed")
                return
            finally:
                if chunk_index is not None:
                    ownership=self._ownership.get(chunk_index)
                    if ownership is not None and ownership.state not in self._TERMINAL_STATES:
                        self._transition(chunk_index,"cancelled")
                if playback_started and hasattr(self.metrics, "mark_playback_finish"):
                    self.metrics.mark_playback_finish(session_id)
                self.queue.task_done()


@dataclass(frozen=True)
class TurnResult:
    chunks: tuple[str,...]
    actions: tuple[ParsedOutput,...]
    return_to_scenario: bool
    fallback_reason: str|None=None


@dataclass(frozen=True)
class AudioTurnResult:
    ingress_status: str
    stt: STTResult | None = None
    turn: TurnResult | None = None
    barge_in: bool = False
    # When browser PCM is handled in non-blocking mode, recognition and the
    # bounded response continue in this task.  The task never carries audio
    # off-process; it is only a RAM-local handoff between ingress and the
    # application completion callback.
    pending: asyncio.Task["AudioTurnResult"] | None = None


class LocalS2SPipeline:
    """Session-scoped audio ingress -> STT -> bounded conversation orchestrator."""

    def __init__(self, vad: VADAdapter, recognizer: SpeechRecognizer, conversation: "BoundedConversationEngine", config: S2SConfig | None = None):
        self.config = (config or conversation.config).validate()
        self.vad = vad
        self.recognizer = recognizer
        self.conversation = conversation
        self.ingress: dict[str, AudioIngressPipeline] = {}
        self.generations: dict[str, int] = {}
        self.pending_tasks: dict[str, asyncio.Task[AudioTurnResult]] = {}
        self.pending_started_at: dict[str, float] = {}

    async def _recognize_turn(self, session_id: str, audio: AudioChunk, context: ConversationContext, generation: int, status: str, barge_in: bool, turn_handler=None) -> AudioTurnResult:
        """Run the expensive part after VAD has released the ingress loop."""
        try:
            started=time.perf_counter()
            result = await asyncio.wait_for(
                self.recognizer.transcribe(audio, lambda: self.generations.get(session_id, 0) != generation),
                self.config.stt_timeout_s,
            )
            self.conversation.metrics.observe("stt",started)
            self.conversation.metrics.observe_voice_stage(session_id, "stt", started, generation_id=generation)
            self.conversation.metrics.mark_voice(session_id, "stt_result", generation_id=generation)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, RuntimeError, ValueError, OSError, IndexError):
            self.conversation.metrics.observe("stt",started,False)
            self.conversation.metrics.observe_voice_stage(session_id, "stt", started, False, generation_id=generation)
            self.conversation.metrics.finish_voice_turn(session_id, "stt_failure", generation_id=generation)
            return AudioTurnResult("stt_failure", turn=TurnResult((), (), True, "stt_failure"), barge_in=barge_in)
        if result.unknown or result.confidence < self.config.stt_confidence_threshold or not result.text.strip():
            self.conversation.metrics.finish_voice_turn(session_id, "stt_low_confidence", generation_id=generation)
            return AudioTurnResult("stt_unknown", stt=result, turn=TurnResult((), (), True, "stt_low_confidence"), barge_in=barge_in)
        self.conversation.metrics.event("input_completed", source="audio", stt_ms=round(result.latency_ms, 2))
        if turn_handler is None:
            turn = await self.conversation.run_text_turn(result.text, context, trace_generation_id=generation)
        else:
            turn = await turn_handler(result.text, generation)
        return AudioTurnResult(status, stt=result, turn=turn, barge_in=barge_in)

    def _remember_pending(self, session_id: str, task: asyncio.Task[AudioTurnResult]) -> None:
        self.pending_tasks[session_id] = task
        self.pending_started_at[session_id] = time.monotonic()

        def forget(done: asyncio.Task[AudioTurnResult]) -> None:
            if self.pending_tasks.get(session_id) is done:
                self.pending_tasks.pop(session_id, None)
                self.pending_started_at.pop(session_id, None)

        task.add_done_callback(forget)

    async def feed_audio(self, session_id: str, samples: tuple[float, ...], context: ConversationContext, *, wait_for_turn: bool = True, turn_handler=None) -> AudioTurnResult:
        generation = self.generations.get(session_id, 0)
        self.conversation.metrics.begin_voice_turn(session_id, generation_id=generation)
        self.conversation.metrics.mark_voice(session_id, "ingress", generation_id=generation)
        pipeline = self.ingress.setdefault(session_id, AudioIngressPipeline(self.vad, self.config))
        was_active = pipeline.active
        vad_started = time.monotonic()
        try:
            status, audio = pipeline.feed(samples, generation)
            self.conversation.metrics.observe_voice_stage(session_id, "vad", vad_started, generation_id=generation)
        except (RuntimeError, ValueError):
            # A provider-specific VAD error must not tear down the browser
            # WebSocket or leak a TorchScript traceback into the console.
            pipeline.reset()
            self.conversation.metrics.observe_voice_stage(session_id, "vad", vad_started, False, generation_id=generation)
            self.conversation.metrics.event("vad_failure", session_id=session_id)
            self.conversation.metrics.finish_voice_turn(session_id, "vad_failure", generation_id=generation)
            return AudioTurnResult("vad_failure", turn=TurnResult((), (), True, "vad_failure"))
        pending = self.pending_tasks.get(session_id)
        barge_in=not was_active and pipeline.active and (self.conversation.is_active(session_id) or bool(pending and not pending.done()))
        if barge_in:
            # New speech while output is active is a barge-in. The old task is
            # cancelled before any newly recognized turn can be dispatched.
            self.conversation.metrics.event("barge_in", session_id=session_id)
            await self.cancel_output(session_id)
            generation = self.generations.get(session_id, 0)
            self.conversation.metrics.begin_voice_turn(session_id, generation_id=generation)
            self.conversation.metrics.mark_voice(session_id, "ingress", generation_id=generation)
        if status in {"collecting", "too_short"}:
            return AudioTurnResult(status,barge_in=barge_in)
        if status == "max_duration" or audio is None:
            self.conversation.metrics.mark_voice(session_id, "vad_decision", generation_id=generation)
            self.conversation.metrics.finish_voice_turn(session_id, "audio_too_long", generation_id=generation)
            return AudioTurnResult(status, turn=TurnResult((), (), True, "audio_too_long"),barge_in=barge_in)
        # This is the user-facing latency boundary: VAD has observed the
        # configured silence window and released the utterance.  Recording it
        # before STT prevents TTFA from being understated by measuring from
        # ``input_completed`` (which is emitted only after recognition).
        self.conversation.metrics.event("speech_end", source="audio", samples=len(audio.samples))
        self.conversation.metrics.mark_voice(session_id, "vad_decision", generation_id=generation)
        task = asyncio.create_task(self._recognize_turn(session_id, audio, context, generation, status, barge_in, turn_handler), name=f"s2s-turn-{session_id}")
        self._remember_pending(session_id, task)
        if not wait_for_turn:
            return AudioTurnResult(status, barge_in=barge_in, pending=task)
        try:
            return await task
        finally:
            if self.pending_tasks.get(session_id) is task:
                self.pending_tasks.pop(session_id, None)
                self.pending_started_at.pop(session_id, None)

    async def cancel_output(self, session_id: str) -> None:
        """Cancel response work without discarding the currently collecting utterance."""
        self.generations[session_id] = self.generations.get(session_id, 0) + 1
        pending = self.pending_tasks.pop(session_id, None)
        self.pending_started_at.pop(session_id, None)
        if pending is not None and not pending.done() and pending is not asyncio.current_task():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        await self.conversation.cancel_session(session_id)

    async def cancel_session(self, session_id: str) -> None:
        self.generations[session_id] = self.generations.get(session_id, 0) + 1
        pipeline = self.ingress.pop(session_id, None)
        if pipeline is not None:
            pipeline.reset()
        pending = self.pending_tasks.pop(session_id, None)
        self.pending_started_at.pop(session_id, None)
        if pending is not None and not pending.done() and pending is not asyncio.current_task():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        await self.conversation.cancel_session(session_id)
        # A terminal session must not retain a generation tombstone.  New
        # input starts a fresh generation; stale completions are already
        # cancelled/awaited above.
        self.generations.pop(session_id, None)

class BoundedConversationEngine:
    def __init__(self,llm,tts,voice,playback,config=None,metrics=None,*,allow_quick_replies=None,profile=None,settings=None):
        if settings is None:
            from .config import Settings, SettingsError
            try:
                settings = Settings.from_env()
            except SettingsError as exc:
                if "VALIDATION_PROVIDER" in str(exc) or "Ollama validation" in str(exc):
                    raise RuntimeError("production profiles require VALIDATION_PROVIDER=ollama") from exc
                raise
        self.config=(config or S2SConfig()).validate(); self.llm=llm; self.tts=tts; self.voice=voice; self.playback=playback; self.settings=settings; self.cancel=CancellationController(); self.normalizer=InputNormalizer(); self.input_safety=InputSafetyFilter(); self.topic_policy=TopicPolicy(); self.output_safety=OutputSafetyValidator();
        profile=(profile or settings.profile).strip().lower()
        self.allow_quick_replies = (profile == "mock") if allow_quick_replies is None else bool(allow_quick_replies)
        from .validation import AsyncResponseValidator, MockMiniLLMValidator, OllamaMiniLLMValidator
        validation_provider=settings.validation_provider
        if validation_provider not in {"mock", "ollama"}:
            raise RuntimeError(f"unsupported response validation provider: {validation_provider}")
        if profile != "mock" and validation_provider != "ollama":
            raise RuntimeError("production profiles require VALIDATION_PROVIDER=ollama")
        validation_model=settings.validation_model
        validation_timeout_ms=settings.validation_timeout_ms
        validation_num_ctx=settings.validation_num_ctx
        validation_fail_closed=settings.validation_fail_closed
        validation_mini=(OllamaMiniLLMValidator(base_url=getattr(settings, "ollama_base_url", "http://127.0.0.1:11434"), model=validation_model, timeout_s=validation_timeout_ms/1000.0, fail_closed=validation_fail_closed, num_ctx=validation_num_ctx, keep_alive=getattr(settings, "validation_keep_alive", "-1"))
                         if validation_provider == "ollama" else MockMiniLLMValidator())
        self.response_validator=AsyncResponseValidator(mini=validation_mini, timeout_ms=validation_timeout_ms, fail_closed=validation_fail_closed)
        self.parser=ResponseTagParser(); self.consumer=StreamingTokenConsumer(self.config.max_response_chars*4)
        self.metrics=metrics or PipelineMetrics(settings=settings)
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.task_started_at: dict[str, float] = {}
        self.active_queues: dict[str, AudioSynthesisQueue] = {}

    def is_active(self, session_id: str) -> bool:
        task = self.active_tasks.get(session_id)
        return bool(task and not task.done())

    async def speak_text(self, session_id: str, text: str) -> TurnResult:
        """Synthesize trusted application text through the normal audio path."""
        text = self.normalizer.normalize(text)
        if not text or len(text) > self.config.max_response_chars:
            return TurnResult((), (), True, "provider_or_validation_failure")
        await self.cancel_session(session_id)
        token = self.cancel.start(session_id)
        queue = AudioSynthesisQueue(
            self.tts, self.voice,
            self.playback, self.cancel, self.config, self.metrics,
            trace_generation_id=token,
            voice_mode=getattr(self.settings, "voice_conversion", "rvc_v2"),
            fallback_mode=getattr(self.settings, "rvc_fallback_mode", "passthrough"),
        )
        await queue.start(session_id, token)
        self.active_queues[session_id] = queue
        try:
            decision = await self.response_validator.validate_trusted(text, generation_id=token)
            if decision.blocked:
                await queue.abort(session_id, invalidate=False)
                return TurnResult((), (), True, "provider_or_validation_failure")
            await queue.submit(text)
            await queue.wait_first_audio(self.config.first_audio_gate_s)
            await queue.finish()
            return TurnResult((text,), (), False)
        except asyncio.CancelledError:
            await queue.abort(session_id, invalidate=False)
            raise
        except Exception:
            await queue.abort(session_id, invalidate=False)
            return TurnResult((), (), True, "provider_or_validation_failure")
        finally:
            if self.active_queues.get(session_id) is queue:
                self.active_queues.pop(session_id, None)

    def _bound_history(self, history, *, protect_last: bool = False):
        items = list(history)
        limit = max(1, self.config.max_history_chars)
        protected = 1 if protect_last and items else 0
        while len(items) > protected and (len(items) > max(1, self.config.max_history_entries) or sum(len(item[0]) + len(item[1]) for item in items) > limit):
            items.pop(0)
            self.history_evicted_entries = getattr(self, "history_evicted_entries", 0) + 1
        if items and sum(len(item[0]) + len(item[1]) for item in items) > limit:
            text, response = items[-1]
            keep = max(0, limit - len(text))
            items[-1] = (text[-limit:] if len(text) > limit else text, response[:keep])
            self.history_evicted_entries = getattr(self, "history_evicted_entries", 0) + 1
        self.history_chars = sum(len(item[0]) + len(item[1]) for item in items)
        return items[-getattr(self.config, "context_window", len(items)): ]

    async def run_text_turn(self,text,context,*,trace_generation_id=None):
        try:
            async with asyncio.timeout(self.config.max_turn_seconds):
                return await self._run_text_turn(text, context, trace_generation_id=trace_generation_id)
        except TimeoutError:
            await self.cancel_session(context.session_id)
            return TurnResult((), (), True, "turn_timeout")

    async def _run_text_turn(self,text,context,*,trace_generation_id=None):
        text=self.normalizer.normalize(text)
        if len(text)>self.config.max_input_chars: return TurnResult((),(),True,"oversized_input")
        safety=self.input_safety.check(text)
        if safety!="allow": return TurnResult((),(),True,"safety_signal" if safety=="signal" else "blocked_input")
        if not self.topic_policy.check(text, context.allowed_topics, context.forbidden_topics): return TurnResult((),(),True,"unknown_topic")
        if context.turns>=context.max_turns or time.monotonic()-context.started_at>=context.max_duration_seconds: return TurnResult((),(),True,"bounded_limit")
        self.metrics.event("turn_started", source="text")
        # There is one conversational generation per installation session.
        # A new text/voice turn must invalidate and await the previous one
        # before replacing its queue, otherwise two LLM/TTS workers can race
        # and a late old response can reach the speaker after barge-in.
        current=asyncio.current_task()
        previous=self.active_tasks.get(context.session_id)
        if previous is not None and previous is not current and not previous.done():
            await self.cancel_session(context.session_id)
        token=self.cancel.start(context.session_id)
        if trace_generation_id is None:
            trace_generation_id = token
        self.metrics.begin_voice_turn(context.session_id, generation_id=trace_generation_id)
        self.metrics.mark_voice(context.session_id, "llm_request", generation_id=trace_generation_id)
        queue=AudioSynthesisQueue(self.tts,self.voice,self.playback,self.cancel,self.config,self.metrics,trace_generation_id=trace_generation_id,voice_mode=getattr(self.settings, "voice_conversion", "rvc_v2"), fallback_mode=getattr(self.settings, "rvc_fallback_mode", "passthrough"))
        queue_started=time.perf_counter(); await queue.start(context.session_id,token)
        task = current
        if task is not None:
            self.active_tasks[context.session_id] = task
            self.task_started_at[context.session_id] = time.monotonic()
        self.active_queues[context.session_id] = queue
        topic_instruction = ("Open developer dialogue: accept any topic unless it violates the safety rules. "
                             if "__all__" in context.allowed_topics else
                             "Allowed topics: "+", ".join(context.allowed_topics)+". ")
        policy = ((context.system_prompt + ' ') if context.system_prompt else '') + topic_instruction + 'Forbidden: ' + ', '.join(context.forbidden_topics) + '. Start the answer immediately. Output only complete <say>...</say> tags or allowlisted actions; every <say> must have a closing </say>. Never output analysis, markdown, or plain text outside these tags. Use one or two short spoken sentences, at most 45 words total, and stop.'
        profile_context=context.profile.prompt_fields()
        history=self._bound_history(context.history)
        messages=[{"role":"system","content":policy+f" Scenario={context.scenario_id}/{context.node_id}. Profile={profile_context}. History={history}"},{"role":"user","content":text}]
        llm_started=time.perf_counter(); llm_completed=False
        # Canonical runtime owns intent routing; no demo quick-reply
        # table is allowed to bypass the bounded provider contract.
        provider = self.llm
        try:
            chunks=[]; actions=[]; spoken_chars=0; raw_spoken=""
            chunker=SentenceChunker(self.config.max_chunk_chars, self.config.first_chunk_chars)
            token_index=0

            def token_received(value):
                nonlocal token_index
                token_index += 1
                self.metrics.mark_voice(context.session_id, "llm_first_token", generation_id=trace_generation_id)
                self.metrics.event("token_received", token_index=token_index, chars=len(value))

            async def close_chunks(values):
                for chunk in values:
                    if self.cancel.cancelled(context.session_id, token):
                        raise asyncio.CancelledError
                    try:
                        validation_started=time.perf_counter(); decision=await self.response_validator.validate(chunk, context=context.history[-4:], generation_id=token)
                        self.metrics.observe_voice_stage(context.session_id, "validation", validation_started, generation_id=trace_generation_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.metrics.event("validation_error", reason=type(exc).__name__, source="response_validation")
                        raise ResponseValidationError("validation_exception") from exc
                    if self.cancel.cancelled(context.session_id, token):
                        raise asyncio.CancelledError
                    if decision.blocked:
                        self.metrics.event(
                            "validation_block",
                            reason=decision.reason,
                            source=decision.source,
                            latency_ms=round(decision.latency_ms, 2),
                            timeout_ms=round(self.response_validator.timeout_s * 1000.0, 2),
                        )
                        raise ResponseValidationError(
                            decision.reason,
                            decision.source,
                            decision.latency_ms,
                            self.response_validator.timeout_s * 1000.0,
                        )
                    chunks.append(chunk)
                    self.metrics.event("chunk_closed", chunk_index=len(chunks)-1, chars=len(chunk), validation=decision.source, validation_ms=round(decision.latency_ms, 2))
                    await queue.submit(chunk)
                    if len(chunks) == 1:
                        self.metrics.event("first_audio_gate_entered", timeout_s=self.config.first_audio_gate_s)
                        await queue.wait_first_audio(self.config.first_audio_gate_s)
                        self.metrics.event("first_audio_gate_released")
            async def handle(item):
                nonlocal spoken_chars, raw_spoken
                if item.kind=="say":
                    raw_spoken += item.value
                    # Validate the complete text seen so far as well as the
                    # fragment. This prevents a blocked phrase split across
                    # multiple LLM tokens from reaching TTS.
                    self.output_safety.validate(raw_spoken,self.config.max_response_chars)
                    validated=self.output_safety.validate(item.value,self.config.max_response_chars)
                    safe=validated
                    if item.value[:1].isspace() and spoken_chars and safe and not safe.startswith(" "):
                        safe=" "+safe
                    if item.value[-1:].isspace() and safe and not safe.endswith(" "):
                        safe += " "
                    spoken_chars += len(safe)
                    if spoken_chars > self.config.max_response_chars: raise ValueError("total model speech exceeded limit")
                    await close_chunks(chunker.feed(safe))
                else: actions.append(item)
            async with asyncio.timeout(self.config.llm_timeout_s):
                consume_task=asyncio.create_task(self.consumer.consume_parsed(provider,messages,str(uuid.uuid4()),lambda:self.cancel.cancelled(context.session_id,token),self.parser,handle,token_received), name=f"s2s-llm-{context.session_id}-{token}")
                failure_task=asyncio.create_task(queue.failure_event.wait(), name=f"s2s-queue-failure-{{context.session_id}}-{{token}}") if queue.failure_event is not None else None
                try:
                    if failure_task is None:
                        await consume_task
                    else:
                        done,_=await asyncio.wait((consume_task,failure_task),return_when=asyncio.FIRST_COMPLETED)
                        if failure_task in done and queue.error is not None:
                            consume_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await consume_task
                            raise queue.error
                        await consume_task
                finally:
                    if failure_task is not None and not failure_task.done():
                        failure_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await failure_task
                await close_chunks(chunker.flush())
                self.metrics.observe("llm",llm_started)
                self.metrics.observe_voice_stage(context.session_id, "llm", llm_started, generation_id=trace_generation_id)
                llm_completed=True
            await queue.finish(); self.metrics.observe_voice_stage(context.session_id, "queue", queue_started, generation_id=trace_generation_id); context.turns+=1; context.history=self._bound_history(context.history+[(text," ".join(chunks))], protect_last=True)
            self.metrics.mark_voice(context.session_id, "turn_complete", generation_id=trace_generation_id)
            self.metrics.finish_voice_turn(context.session_id, "completed", generation_id=trace_generation_id)
            return TurnResult(tuple(chunks),tuple(actions),any(x.value=="return_to_scenario" for x in actions))
        except TTSQueueOverflowError:
            await queue.abort(context.session_id, invalidate=False)
            self.metrics.finish_voice_turn(context.session_id, "tts_queue_overflow", generation_id=trace_generation_id)
            return TurnResult((),(),True,"tts_queue_overflow")
        except ResponseValidationError as exc:
            self.metrics.event(
                "validation_failure",
                reason=exc.reason,
                source=exc.source,
                latency_ms=round(exc.latency_ms, 2),
                timeout_ms=round(exc.timeout_ms, 2) if exc.timeout_ms is not None else None,
            )
            await queue.abort(context.session_id, invalidate=False)
            self.metrics.finish_voice_turn(context.session_id, "provider_or_validation_failure", generation_id=trace_generation_id)
            return TurnResult((),(),True,"provider_or_validation_failure")
        except ValueError as exc:
            self.metrics.event("validation_failure", reason=str(exc)[:120], source="stream_parser")
            await queue.abort(context.session_id, invalidate=False)
            self.metrics.finish_voice_turn(context.session_id, "provider_or_validation_failure", generation_id=trace_generation_id)
            return TurnResult((),(),True,"provider_or_validation_failure")
        except (TimeoutError,RuntimeError) as exc:
            if self.cancel.cancelled(context.session_id, token) and queue.error is None:
                await queue.abort(context.session_id, invalidate=False)
                raise asyncio.CancelledError
            if not llm_completed: self.metrics.observe("llm",llm_started,False)
            failure_reason = "voice_generation_timeout" if isinstance(exc, TimeoutError) else "provider_or_validation_failure"
            await queue.abort(context.session_id); self.metrics.finish_voice_turn(context.session_id, failure_reason, generation_id=trace_generation_id); return TurnResult((),(),True,failure_reason)
        except asyncio.CancelledError:
            await queue.abort(context.session_id, invalidate=False); raise
        finally:
            if self.active_tasks.get(context.session_id) is task:
                self.active_tasks.pop(context.session_id, None)
                self.task_started_at.pop(context.session_id, None)
            if self.active_queues.get(context.session_id) is queue: self.active_queues.pop(context.session_id, None)
            if context.session_id in self.metrics.active_voice_turns:
                outcome = "cancelled" if self.cancel.cancelled(context.session_id, token) else "failed"
                self.metrics.finish_voice_turn(context.session_id, outcome, generation_id=trace_generation_id)

    def resource_snapshot(self) -> dict[str, int]:
        queues=tuple(self.active_queues.values())
        return {
            "active_sessions": len(queues),
            "active_tasks": sum(1 for task in self.active_tasks.values() if not task.done()),
            "queued_chunks": sum(queue.queue.qsize() for queue in queues),
            "queued_bytes": sum(getattr(queue, "queued_bytes", 0) for queue in queues),
            "active_audio_bytes": sum(getattr(queue, "active_audio_bytes", 0) for queue in queues),
            "pcm_bytes": sum(getattr(queue, "active_audio_bytes", 0) for queue in queues),
            "cancelled_chunks": sum(getattr(queue, "cancelled_chunks", 0) for queue in queues),
            "completed_chunks": sum(getattr(queue, "completed_chunks", 0) for queue in queues),
            "failed_chunks": sum(getattr(queue, "failed_chunks", 0) for queue in queues),
            "registry_size": len(self.active_tasks) + len(self.active_queues),
            "history_chars": int(getattr(self, "history_chars", 0)),
            "history_evicted_entries": int(getattr(self, "history_evicted_entries", 0)),
        }

    async def cancel_all_sessions(self) -> dict[str, int]:
        """Cancel every owned session and drain its generation during recovery."""
        sessions = set(self.active_tasks) | set(self.active_queues)
        for session_id in tuple(sessions):
            await self.cancel_session(session_id)
        await self.playback.stop()
        return self.resource_snapshot()

    async def cancel_session(self,session_id):
        self.cancel.cancel(session_id)
        task = self.active_tasks.get(session_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await task
        if task is None or task.done():
            self.task_started_at.pop(session_id, None)
        queue = self.active_queues.get(session_id)
        if queue is not None:
            await queue.abort(session_id, invalidate=True)
            if self.active_queues.get(session_id) is queue:
                self.active_queues.pop(session_id, None)
        await self.playback.stop()
