import asyncio
import os
import time
from types import SimpleNamespace

from app.foundation.e2e_probe import ProductionE2EWarmupProbe
from app.foundation.validation import AsyncResponseValidator
from app.foundation.observability import Metrics as ObservabilityMetrics
from app.foundation.s2s import PipelineMetrics


class Trace:
    def __init__(self, correlation_id):
        self.correlation_id = correlation_id

    def report(self):
        return {
            "correlation_id": self.correlation_id,
            "timestamps_monotonic": {"playback_start": 2.0},
            "stage_durations_ms": {
                "stt": 1.0, "llm": 2.0, "validation": 1.0,
                "tts": 3.0, "queue": 4.0, "playback": 5.0,
            },
        }


class Metrics:
    def __init__(self, report=None):
        self.report = report
        self.completed_voice_turns = []

    def begin_voice_turn(self, _session_id, correlation_id):
        return Trace(correlation_id)

    def snapshot(self):
        return {
            "events": [],
            "voice_turns": list(self.completed_voice_turns),
            "active_voice_turns": [],
        }


class Pipeline:
    def __init__(self, result=None, delay=0):
        self.config = SimpleNamespace(sample_rate=16000, vad_silence_ms=200)
        self.result = result
        self.delay = delay
        self.cancelled = []

    async def feed_audio(self, *_args, **_kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.result is not None and self.result.pending is None:
            self.result.pending = asyncio.create_task(asyncio.sleep(0, result=self.result))
        return self.result

    async def cancel_session(self, session_id):
        self.cancelled.append(session_id)


def manager(profile="production_candidate", report=None):
    metrics = Metrics(report)
    engine = SimpleNamespace(metrics=metrics)
    return SimpleNamespace(settings=SimpleNamespace(profile=profile), engine=engine)


def run(coro):
    return asyncio.run(coro)


def test_probe_rejects_mock_without_calling_pipeline():
    pipeline = Pipeline()
    result = run(ProductionE2EWarmupProbe(manager("mock"), pipeline).run())
    assert result["ok"] is False
    assert result["reason"] == "mock_profile_not_allowed"
    assert pipeline.cancelled


def test_probe_reports_real_contract_success(monkeypatch, tmp_path):
    wav = tmp_path / "input.wav"
    import wave
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 1024)
    monkeypatch.setenv("MISHKA_WARMUP_PCM_PATH", str(wav))
    pending = None
    result = SimpleNamespace(
        pending=pending,
        stt=SimpleNamespace(unknown=False),
        turn=SimpleNamespace(fallback_reason=None),
    )
    m = manager()
    m.engine.metrics.completed_voice_turns = [Trace("placeholder").report()]
    m.engine.metrics.completed_voice_turns[0]["correlation_id"] = None
    pipeline = Pipeline(result=result)
    report = run(ProductionE2EWarmupProbe(m, pipeline).run())
    assert report["correlation_id"]
    assert report["ok"] is True
    assert {stage["stage"] for stage in report["stages"]} >= {"pcm_input", "vad", "stt", "llm", "validation", "silero_tts", "bounded_tts_queue", "playback"}
    assert report["metrics_snapshot"]["events"] == []
    import json
    json.dumps(report["metrics_snapshot"], ensure_ascii=False, allow_nan=False)


def test_probe_preserves_metrics_snapshot_on_failure(monkeypatch, tmp_path):
    wav = tmp_path / "input.wav"
    import wave
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 1024)
    monkeypatch.setenv("MISHKA_WARMUP_PCM_PATH", str(wav))

    class NoSpeechPipeline(Pipeline):
        async def feed_audio(self, *_args, **_kwargs):
            return SimpleNamespace(pending=None)

    report = run(ProductionE2EWarmupProbe(manager(), NoSpeechPipeline()).run())
    assert report["ok"] is False
    assert report["reason"] == "vad_no_speech_end"
    assert report["metrics_snapshot"]["events"] == []


def test_observability_snapshot_empty_is_json_serializable():
    snapshot = ObservabilityMetrics().snapshot()
    import json
    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    assert snapshot["counters"] == {}


def test_validator_timeout_preserves_reason_and_elapsed_latency():
    class SlowValidator:
        async def validate(self, _text, _context=()):
            await asyncio.sleep(0.02)
            raise AssertionError("timeout must cancel the provider call")

    validator = AsyncResponseValidator(SlowValidator(), timeout_ms=1, fail_closed=True)
    decision = run(validator.validate("This is a deliberately long ordinary response that requires the safety validator to inspect the complete generated answer.", ()))
    assert decision.blocked is True
    assert decision.reason == "mini_llm_timeout"
    assert decision.source == "validator_timeout"
    assert decision.latency_ms >= 0


def test_observability_bounds_memory_schema_and_eviction():
    metrics = ObservabilityMetrics(latency_limit=2, event_limit=2, watchdog_limit=2)
    for value in (1, 2, 3): metrics.observe(value)
    snapshot = metrics.snapshot()
    assert snapshot["latency_history"] == {"limit": 2, "current_sample_count": 2, "evicted_sample_count": 1}
    assert "rss_current_bytes" in snapshot["memory"]
    assert "rss_peak_bytes" in snapshot["memory"]
    import json
    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)


def test_pipeline_metrics_history_is_bounded_and_serializable():
    metrics = PipelineMetrics(stage_limit=2, event_limit=2)
    metrics.observe("llm", time.perf_counter() - 0.001)
    metrics.observe("llm", time.perf_counter() - 0.001)
    metrics.observe("llm", time.perf_counter() - 0.001)
    metrics.event("one")
    metrics.event("two")
    metrics.event("three")
    snapshot = metrics.snapshot()
    assert snapshot["llm"]["current_sample_count"] == 2
    assert snapshot["llm"]["evicted_sample_count"] == 1
    assert snapshot["llm"]["limit"] == 2
    assert snapshot["observability_limits"] == {"stage_samples": 2, "events": 2, "event_evicted_count": 1}
    import json
    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
