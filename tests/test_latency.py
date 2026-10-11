import pytest

from app.metrics.latency import VoiceTurnTrace
from app.foundation.s2s import PipelineMetrics


def test_voice_turn_trace_reports_monotonic_boundaries_and_derived_durations():
    trace = VoiceTurnTrace("turn-1")
    for index, boundary in enumerate((
        "ingress", "vad_decision", "stt_result", "llm_request",
        "llm_first_token", "tts_request", "first_audio_chunk",
        "playback_start", "proxy_send", "turn_complete",
    )):
        trace.mark(boundary, 10.0 + index / 1000)
    trace.outcome = "completed"
    report = trace.report()
    assert report["correlation_id"] == "turn-1"
    assert report["outcome"] == "completed"
    assert report["durations_ms"]["ingress_to_proxy_send_ms"] == 8.0
    assert report["durations_ms"]["tts_to_first_audio_ms"] == 1.0
    trace.observe_stage("vad", 10.0)
    assert report["stage_durations_ms"] == {}
    assert all(value >= 0 for value in report["durations_ms"].values() if value is not None)


def test_voice_turn_trace_keeps_first_timestamp_and_marks_unavailable_stages():
    trace = VoiceTurnTrace("turn-2")
    trace.mark("ingress", 5.0)
    trace.mark("ingress", 1.0)
    report = trace.report()
    assert report["timestamps_monotonic"]["ingress"] == 5.0
    assert report["durations_ms"]["llm_to_first_token_ms"] is None


def test_voice_turn_trace_rejects_negative_interval():
    trace = VoiceTurnTrace("turn-3")
    trace.mark("ingress", 2.0)
    trace.mark("proxy_send", 1.0)
    with pytest.raises(ValueError, match="negative latency interval"):
        trace.report()


def test_pipeline_metrics_keeps_one_report_per_completed_turn():
    metrics = PipelineMetrics()
    metrics.begin_voice_turn("session-1", "corr-1")
    metrics.mark_voice("session-1", "ingress")
    metrics.mark_voice("session-1", "proxy_receive")
    metrics.mark_any_voice("proxy_send")
    report = metrics.finish_voice_turn("session-1", "cancelled")
    assert report["correlation_id"] == "corr-1"
    assert report["outcome"] == "cancelled"
    assert report["timestamps_monotonic"]["proxy_send"] >= report["timestamps_monotonic"]["proxy_receive"]
    assert metrics.snapshot()["active_voice_turns"] == []
    assert metrics.snapshot()["voice_turns"][-1] == report


def test_voice_turn_trace_records_stage_duration_without_wall_clock_regression():
    trace = VoiceTurnTrace("turn-4")
    started = __import__("time").monotonic()
    trace.observe_stage("vad", started)
    report = trace.report()
    assert report["stage_durations_ms"]["vad"] >= 0

def test_pipeline_metrics_drops_late_markers_from_superseded_generation():
    metrics = PipelineMetrics()
    metrics.begin_voice_turn("session-1", "corr-old", generation_id=1)
    metrics.mark_voice("session-1", "llm_request", generation_id=1)

    metrics.begin_voice_turn("session-1", "corr-new", generation_id=2)
    # This marker belongs to the cancelled generation and must not enter the
    # new trace, even though both turns share the same session id.
    metrics.mark_voice("session-1", "tts_request", generation_id=1)
    metrics.mark_voice("session-1", "llm_request", generation_id=2)
    metrics.mark_voice("session-1", "tts_request", generation_id=2)

    snapshot = metrics.snapshot()
    active = snapshot["active_voice_turns"]
    assert len(active) == 1
    assert active[0]["correlation_id"] == "corr-new"
    assert active[0]["timestamps_monotonic"]["llm_request"] <= active[0]["timestamps_monotonic"]["tts_request"]
    superseded = [item for item in snapshot["voice_turns"] if item["correlation_id"] == "corr-old"]
    assert superseded and superseded[-1]["outcome"] == "superseded"

