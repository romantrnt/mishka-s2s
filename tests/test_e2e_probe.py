import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from app.foundation.e2e_probe import ProductionE2EWarmupProbe
from app.foundation.validation import AsyncResponseValidator
from app.foundation.observability import Metrics as ObservabilityMetrics
from app.foundation.s2s import AudioTurnResult, BoundedConversationEngine, PipelineMetrics, TurnResult


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


class ProbeEngine:
    playback = BoundedConversationEngine.playback
    scoped_playback = BoundedConversationEngine.scoped_playback

    def __init__(self, metrics):
        self.metrics = metrics
        self.playback = None


def manager(profile="production_candidate", report=None):
    metrics = Metrics(report)
    engine = ProbeEngine(metrics)
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


def metric_probe(monkeypatch, metrics, *, outcome="stt_failure", events=(), noise=0):
    class DiagnosticPipeline(Pipeline):
        async def feed_audio(self, session_id, *_args, **_kwargs):
            correlation_id = metrics.active_voice_turns[session_id].correlation_id
            for name, owner in events:
                metrics.event(name, correlation_id=correlation_id if owner == "current" else owner,
                              reason=owner + "_validation", source="fixture_validator")
            for index in range(noise):
                metrics.event("unrelated", correlation_id="foreign", sequence=index)
            for stage in ("stt", "llm", "validation", "tts", "queue", "playback"):
                metrics.observe_voice_stage(session_id, stage, time.monotonic())
            metrics.finish_voice_turn(session_id, outcome)
            completed = AudioTurnResult(
                outcome,
                stt=None if outcome == "stt_failure" else SimpleNamespace(unknown=False),
                turn=TurnResult((), (), outcome != "completed", None if outcome == "completed" else outcome),
            )
            return AudioTurnResult("processing", pending=asyncio.create_task(asyncio.sleep(0, result=completed)))

    pipeline = DiagnosticPipeline()
    playback = object()
    m = manager()
    m.engine.metrics, m.engine.playback = metrics, playback
    probe = ProductionE2EWarmupProbe(m, pipeline)
    monkeypatch.setattr(probe, "_read_pcm", lambda *_args: (.1,) * 1024)
    report = run(probe.run())
    assert pipeline.cancelled == ["warmup-" + report["correlation_id"]]
    assert m.engine.playback is playback
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


@pytest.mark.parametrize("owner", ["foreign", "current"])
def test_probe_keeps_stt_failure_without_validation_diagnostic(monkeypatch, owner):
    metrics = PipelineMetrics()
    metrics.event("validation_failure", correlation_id="old", reason="old_timeout")
    report = metric_probe(monkeypatch, metrics, events=(("validation_block", owner),))
    failed = next(stage for stage in report["stages"] if not stage["success"])
    assert failed["stage"] == "stt" and failed["reason"] == "stt_failure"
    assert report["reason"] == "stt_failure" and report["diagnostic"] is None
    assert "diagnostic" not in failed
    assert all(event["correlation_id"] == report["correlation_id"] for event in report["trace"]["events"])
    assert report["metrics_snapshot"]["events"][0]["reason"] == "old_timeout"


def test_successful_probe_has_no_validation_diagnostic(monkeypatch):
    metrics = PipelineMetrics()
    metrics.event("validation_failure", correlation_id="old", reason="old_timeout")
    report = metric_probe(monkeypatch, metrics, outcome="completed",
                          events=(("validation_failure", "current"), ("validation_block", "foreign")))
    assert report["ok"] and report["reason"] is None and report["diagnostic"] is None


def test_probe_llm_failure_ignores_fresh_foreign_validation(monkeypatch):
    metrics = PipelineMetrics()
    metrics.event("validation_failure", correlation_id="old", reason="old_timeout")
    report = metric_probe(monkeypatch, metrics, outcome="provider_or_validation_failure",
                          events=(("validation_block", "foreign"),))
    assert report["reason"] == "llm_or_response_failure" and report["diagnostic"] is None
    assert report["trace"]["events"] == []


@pytest.mark.parametrize("event", ["validation_failure", "validation_block"])
def test_probe_filters_own_validation_before_last_40_events(monkeypatch, event):
    metrics = PipelineMetrics(event_limit=100)
    report = metric_probe(monkeypatch, metrics, outcome="provider_or_validation_failure",
                          events=((event, "current"), ("validation_failure", "foreign")), noise=41)
    assert report["reason"] == "current_validation"
    assert report["diagnostic"]["correlation_id"] == report["correlation_id"]
    assert report["trace"]["events"] == [report["diagnostic"]]
    assert len(report["metrics_snapshot"]["events"]) == 43
    failed = next(stage for stage in report["stages"] if not stage["success"])
    assert failed["stage"] == "llm" and failed["diagnostic"] == report["diagnostic"]


def test_repeated_probe_does_not_reuse_previous_cycle_diagnostic(monkeypatch):
    metrics = PipelineMetrics()
    first = metric_probe(monkeypatch, metrics, outcome="provider_or_validation_failure",
                         events=(("validation_failure", "current"),))
    second = metric_probe(monkeypatch, metrics, outcome="provider_or_validation_failure")
    assert first["diagnostic"]["correlation_id"] == first["correlation_id"]
    assert second["correlation_id"] != first["correlation_id"]
    assert second["reason"] == "llm_or_response_failure" and second["diagnostic"] is None
    assert second["trace"]["events"] == []


def test_probe_does_not_replace_evicted_own_diagnostic_with_foreign_event(monkeypatch):
    metrics = PipelineMetrics(event_limit=2)
    report = metric_probe(monkeypatch, metrics, outcome="provider_or_validation_failure",
                          events=(("validation_failure", "current"), ("validation_block", "foreign")), noise=1)
    assert metrics.event_evicted == 1
    assert report["reason"] == "llm_or_response_failure" and report["diagnostic"] is None
    assert report["trace"]["events"] == []
    assert report["metrics_snapshot"]["events"][0]["correlation_id"] == "foreign"


@pytest.mark.parametrize("failure", ["block", "exception", "parser"])
def test_validation_events_keep_captured_trace_after_generation_changes(failure):
    from app.foundation.config import Settings
    from app.foundation.s2s import (
        BoundedConversationEngine, ConversationContext, MockPlaybackController,
        MockTTSProvider, RVCProvider,
    )
    from app.foundation.validation import ValidationDecision

    metrics = PipelineMetrics()
    metrics.begin_voice_turn("fixture-session", "original-correlation", generation_id=1)

    class Tokens:
        async def stream(self, *_args):
            await asyncio.sleep(0)
            metrics.begin_voice_turn("fixture-session", "replacement-correlation", generation_id=2)
            yield "<say>Незавершённый ответ" if failure == "parser" else "<say>Короткий ответ.</say>"

    class Validator:
        timeout_s = .01

        async def validate(self, *_args, **_kwargs):
            await asyncio.sleep(0)
            if failure == "exception":
                raise RuntimeError("synthetic validator failure")
            return ValidationDecision("block", False, "fixture_block", 0, "fixture_validator")

    playback = MockPlaybackController()
    engine = BoundedConversationEngine(Tokens(), MockTTSProvider(), RVCProvider(), playback,
                                       metrics=metrics, settings=Settings(profile="mock"), allow_quick_replies=False)
    engine.response_validator = Validator()
    context = ConversationContext("fixture-session", "canonical", "warmup_speech", ("__all__",), (), 1, 120, 4)
    result = run(engine.run_text_turn("fixture", context))
    assert result.fallback_reason == "provider_or_validation_failure" and playback.played == []
    events = [event for event in metrics.snapshot()["events"]
              if event["event"] in {"validation_block", "validation_failure"}]
    assert events and {event.get("correlation_id") for event in events} == {"original-correlation"}
    assert metrics.active_voice_turns[context.session_id].correlation_id == "replacement-correlation"
    assert all(set(event) <= {"event", "at", "correlation_id", "reason", "source", "latency_ms", "timeout_ms"}
               for event in events)
    assert not engine.active_tasks and not engine.active_queues
    json.dumps(metrics.snapshot(), ensure_ascii=False, allow_nan=False)
