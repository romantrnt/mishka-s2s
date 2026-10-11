from __future__ import annotations

import asyncio
import json
import struct
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from .s2s import ConversationContext, LocalS2SPipeline
from .config import compatibility_env_value


class ProductionE2EWarmupProbe:
    """Real PCM -> VAD -> STT -> LLM -> validation -> TTS -> playback probe."""

    def __init__(self, manager: Any, audio_pipeline: LocalS2SPipeline):
        self.manager = manager
        self.audio_pipeline = audio_pipeline

    @staticmethod
    def _resample(samples: tuple[float, ...], source_rate: int, target_rate: int) -> tuple[float, ...]:
        if source_rate == target_rate or not samples:
            return samples
        length = max(1, round(len(samples) * target_rate / source_rate))
        result = []
        for index in range(length):
            position = index * source_rate / target_rate
            left = min(int(position), len(samples) - 1)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            result.append(samples[left] * (1 - fraction) + samples[right] * fraction)
        return tuple(result)

    @classmethod
    def _read_pcm(cls, path: Path, target_rate: int) -> tuple[float, ...]:
        if not path.is_file():
            raise FileNotFoundError(f"warmup PCM not found: {path}")
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.getnframes()
            if channels not in {1, 2} or width != 2 or rate <= 0:
                raise ValueError("warmup PCM must be mono/stereo signed PCM16 WAV")
            if frames <= 0 or frames > rate * 15:
                raise ValueError("warmup PCM must be 0 < duration <= 15 seconds")
            data = source.readframes(frames)
        values = tuple(value / 32768.0 for value in struct.unpack("<" + "h" * (len(data) // 2), data))
        if channels == 2:
            values = tuple((values[i] + values[i + 1]) / 2 for i in range(0, len(values), 2))
        return cls._resample(values, rate, target_rate)

    @staticmethod
    def _stage(name: str, duration_ms: float | None, success: bool, reason: str | None = None) -> dict[str, Any]:
        item = {"stage": name, "duration_ms": None if duration_ms is None else round(max(0.0, duration_ms), 2), "success": bool(success)}
        if reason:
            item["reason"] = reason
        return item

    def _metric_snapshot(self) -> dict[str, Any]:
        """Return the existing pipeline snapshot without leaking raw content."""
        metrics = getattr(self.manager.engine, "metrics", None)
        snapshot = getattr(metrics, "snapshot", None)
        if not callable(snapshot):
            return {"available": False, "reason": "metrics_snapshot_unavailable"}
        try:
            value = snapshot()
            if not isinstance(value, dict):
                return {"available": False, "reason": "metrics_snapshot_invalid"}
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return {"available": False, "reason": "metrics_snapshot_not_serializable"}
        return value

    async def run(self) -> dict[str, Any]:
        correlation_id = str(uuid.uuid4())
        started = time.perf_counter()
        stages: list[dict[str, Any]] = []
        session_id = f"warmup-{correlation_id}"
        settings = self.manager.settings
        total_timeout = float(getattr(settings, "e2e_warmup_timeout_s", 90.0))
        stage_timeout = float(getattr(settings, "e2e_stage_timeout_s", 20.0))
        project_root = Path(__file__).resolve().parents[2]
        pcm_path = Path(getattr(settings, "warmup_pcm_path", compatibility_env_value("MISHKA_WARMUP_PCM_PATH", "assets/reference/mishka_24k_mono.wav"))).expanduser()
        if not pcm_path.is_absolute():
            pcm_path = project_root / pcm_path

        async def run_inner() -> dict[str, Any]:
            if self.manager.settings.profile == "mock":
                return {"ok": False, "correlation_id": correlation_id, "stages": [self._stage("pcm_input", 0, False, "mock_profile_not_allowed")], "reason": "mock_profile_not_allowed", "metrics_snapshot": self._metric_snapshot()}
            try:
                pcm_started = time.perf_counter()
                samples = await asyncio.wait_for(asyncio.to_thread(self._read_pcm, pcm_path, self.audio_pipeline.config.sample_rate), stage_timeout)
                stages.append(self._stage("pcm_input", (time.perf_counter() - pcm_started) * 1000, bool(samples)))
                if not samples:
                    raise ValueError("warmup PCM is empty")
            except Exception as exc:
                stages.append(self._stage("pcm_input", (time.perf_counter() - started) * 1000, False, f"{type(exc).__name__}: {str(exc)[:160]}"))
                return {"ok": False, "correlation_id": correlation_id, "stages": stages, "reason": "pcm_input_failed", "metrics_snapshot": self._metric_snapshot()}
            trace = self.manager.engine.metrics.begin_voice_turn(session_id, correlation_id)
            context = ConversationContext(session_id, "dev_core", "dev_dialogue", ("__all__",), (), 1, 120, 4)
            vad_started = time.perf_counter()
            result = None
            try:
                for offset in range(0, len(samples), 1024):
                    result = await asyncio.wait_for(self.audio_pipeline.feed_audio(session_id, samples[offset:offset + 1024], context, wait_for_turn=False), stage_timeout)
                    if result.pending is not None:
                        break
                if result is None or result.pending is None:
                    silence = (0.0,) * max(1024, round(self.audio_pipeline.config.sample_rate * (self.audio_pipeline.config.vad_silence_ms + 64) / 1000))
                    result = await asyncio.wait_for(self.audio_pipeline.feed_audio(session_id, silence, context, wait_for_turn=False), stage_timeout)
                stages.append(self._stage("vad", (time.perf_counter() - vad_started) * 1000, result is not None and result.pending is not None, None if result and result.pending is not None else "no_speech_end"))
                if result is None or result.pending is None:
                    return {"ok": False, "correlation_id": correlation_id, "stages": stages, "reason": "vad_no_speech_end", "metrics_snapshot": self._metric_snapshot()}
                completed = await asyncio.wait_for(asyncio.shield(result.pending), stage_timeout)
                if completed is None or not hasattr(completed, "stt") or not hasattr(completed, "turn"):
                    raise RuntimeError("pipeline_returned_invalid_turn_result")
                result = completed
            except asyncio.TimeoutError:
                stages.append(self._stage("pipeline", (time.perf_counter() - vad_started) * 1000, False, "stage_timeout"))
                return {"ok": False, "correlation_id": correlation_id, "stages": stages, "reason": "e2e_stage_timeout", "metrics_snapshot": self._metric_snapshot()}
            except Exception as exc:
                stages.append(self._stage("pipeline", (time.perf_counter() - vad_started) * 1000, False, f"{type(exc).__name__}: {str(exc)[:160]}"))
                return {"ok": False, "correlation_id": correlation_id, "stages": stages, "reason": "e2e_pipeline_failure", "metrics_snapshot": self._metric_snapshot()}

            report = next((item for item in reversed(self.manager.engine.metrics.completed_voice_turns) if item.get("correlation_id") == correlation_id), trace.report())
            metric_snapshot = self._metric_snapshot()
            report["events"] = metric_snapshot.get("events", [])[-40:]
            report["cleanup"] = {"session_id": session_id, "cancel_requested": True}
            durations = report.get("stage_durations_ms", {})
            specs = (
                ("stt", result.stt is not None and not result.stt.unknown, "stt_failure"),
                ("llm", result.turn is not None and result.turn.fallback_reason is None, "llm_or_response_failure"),
                ("validation", result.turn is not None and result.turn.fallback_reason is None, "response_validation_failure"),
                ("silero_tts", "tts" in durations, "tts_not_observed"),
                ("bounded_tts_queue", "queue" in durations, "queue_not_observed"),
                ("playback", "playback" in durations or "playback_start" in report.get("timestamps_monotonic", {}), "playback_not_observed"),
            )
            for name, success, failure_reason in specs:
                stages.append(self._stage(name, durations.get({"silero_tts": "tts", "bounded_tts_queue": "queue"}.get(name, name)), success, None if success else failure_reason))
            failed = next((item for item in stages if not item["success"]), None)
            validation_failure = next((event for event in reversed(report.get("events", []))
                                       if event.get("event") in {"validation_failure", "validation_block"}), None)
            if failed is not None and validation_failure is not None:
                failed["reason"] = validation_failure.get("reason", failed.get("reason", "stage_failed"))
                failed["diagnostic"] = validation_failure
            return {"ok": failed is None, "correlation_id": correlation_id, "stages": stages, "trace": report,
                    "metrics_snapshot": metric_snapshot,
                    "reason": None if failed is None else failed.get("reason", "stage_failed"),
                    "diagnostic": validation_failure}

        try:
            return await asyncio.wait_for(run_inner(), total_timeout)
        except asyncio.TimeoutError:
            stages.append(self._stage("e2e", (time.perf_counter() - started) * 1000, False, "overall_timeout"))
            return {"ok": False, "correlation_id": correlation_id, "stages": stages, "reason": "e2e_overall_timeout", "metrics_snapshot": self._metric_snapshot()}
        finally:
            await self.audio_pipeline.cancel_session(session_id)
