"""Synthetic private markers at operational logging boundaries; no real providers."""
import logging
from types import SimpleNamespace

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import AudioChunk, FasterWhisperRecognizer, S2SConfig
from scripts.run_stand import StandAccessLogRedactionFilter


PRIVATE = "SYNTHETIC_NAME SYNTHETIC_TRANSCRIPT SYNTHETIC_HEALTH SYNTHETIC_SECRET"


def formatted(record):
    assert StandAccessLogRedactionFilter().filter(record)
    return logging.Formatter().format(record)


@pytest.mark.parametrize("target", [
    "/stand/state?token=SYNTHETIC_SECRET&note=SYNTHETIC_HEALTH",
    "/sessions/SYNTHETIC_NAME/events?text=SYNTHETIC_TRANSCRIPT",
    "https://example.invalid/SYNTHETIC_NAME?health=SYNTHETIC_HEALTH",
    "/stand/%53YNTHETIC_NAME?text=%53YNTHETIC_TRANSCRIPT",
])
def test_http_access_keeps_method_status_without_any_request_target(target):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("client", "POST", target, "1.1", 422), None)
    text = formatted(record)
    assert "POST" in text and "422" in text and "<redacted>" in text
    assert "SYNTHETIC_" not in text and "%53YNTHETIC_" not in text


@pytest.mark.parametrize("message,args", [
    ('%s - "WebSocket %s" [accepted]', ("client", "/stand/ws?name=SYNTHETIC_NAME")),
    ('GET /stand/SYNTHETIC_NAME?text=SYNTHETIC_TRANSCRIPT HTTP/1.1 200', ()),
    ('WebSocket /stand/ws?health=SYNTHETIC_HEALTH [accepted]', ()),
    ('GET /stand/state?note="SYNTHETIC_HEALTH" HTTP/1.1 200', ()),
])
def test_websocket_and_preformatted_request_lines_are_redacted(message, args):
    text = formatted(logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, message, args, None))
    assert "<redacted>" in text and "SYNTHETIC_" not in text


def test_uvicorn_access_formatter_and_repeated_filter_preserve_metadata():
    from uvicorn.logging import AccessFormatter
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1234", "POST", "/stand/SYNTHETIC_NAME", "1.1", 422), None)
    formatter = AccessFormatter('%(request_line)s %(status_code)s', use_colors=False)
    redactor = StandAccessLogRedactionFilter()
    assert redactor.filter(record)
    first = formatter.format(record)
    assert redactor.filter(record) and formatter.format(record) == first
    assert first == "POST <redacted> HTTP/1.1 422 Unprocessable Entity"
    event = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
        "tts_started provider=%s chunk_index=%d", ("vox555", 2), None)
    assert formatted(event) == "tts_started provider=vox555 chunk_index=2"


def test_error_handler_keeps_exception_class_without_body_or_cached_traceback():
    try:
        raise ValueError(PRIVATE)
    except ValueError:
        import sys
        record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1,
            "ASGI operation failed", (), sys.exc_info())
    record.exc_text = "cached traceback: " + PRIVATE
    record.stack_info = PRIVATE
    text = formatted(record)
    assert "ASGI operation failed" in text and "ValueError" in text
    assert "SYNTHETIC_" not in text and record.exc_info is None


@pytest.mark.asyncio
async def test_actual_recognizer_failure_keeps_type_and_timing_without_provider_body(caplog):
    class FailingProvider:
        def transcribe(self, *args, **kwargs):
            raise ValueError(PRIVATE)
    model = FailingProvider()
    recognizer = FasterWhisperRecognizer(lambda: model, S2SConfig(), "synthetic")
    recognizer._model = model
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        with pytest.raises(RuntimeError, match="faster-whisper transcription failed"):
            await recognizer.transcribe(AudioChunk((.1,) * 1600, 16000, 1), lambda: False)
    text = caplog.text
    assert "faster_whisper_transcribe_failed" in text and "ValueError" in text
    assert "samples=1600" in text and "duration_ms=100.0" in text
    assert "SYNTHETIC_" not in text


def test_optional_process_probe_retains_failure_type_without_exception_body(monkeypatch, caplog):
    from app.foundation.observability import Metrics
    def failed(*args):
        raise RuntimeError(PRIVATE)
    monkeypatch.setitem(__import__('sys').modules, "psutil", SimpleNamespace(process_iter=failed))
    metrics = Metrics()
    with caplog.at_level(logging.WARNING, logger="mishka.observability"):
        result = metrics.memory_snapshot()
    assert result["ollama"]["rss_bytes"]["reason"] == "ollama_unavailable"
    assert metrics.observation_errors[-1] == {"source": "ollama_rss", "error": "RuntimeError"}
    assert "RuntimeError" in caplog.text and "SYNTHETIC_" not in caplog.text


@pytest.mark.asyncio
async def test_optional_voice_warmup_keeps_fallback_without_exception_body(caplog):
    from app.foundation.runtime import S2SRuntimeManager
    class Component:
        async def warmup(self):
            return {"ok": True}
        async def synthesize(self, *args):
            return AudioChunk((.1,) * 240, 24000, 1)
        async def convert(self, *args):
            raise RuntimeError(PRIVATE)
    component = Component()
    engine = SimpleNamespace(metrics=SimpleNamespace(observe=lambda *args, **kwargs: None))
    manager = S2SRuntimeManager(Settings(profile="local_cpu", voice_conversion="rvc_v2",
        rvc_fallback_mode="passthrough"), component, component, component, engine,
        vad=component, stt=component, playback=SimpleNamespace(readiness=lambda: {"ready": True}))
    async def probe():
        return {"ok": False, "reason": "synthetic_probe_not_production_evidence"}
    manager.e2e_probe = probe
    with caplog.at_level(logging.WARNING, logger="mishka.runtime"):
        result = await manager._warmup()
    assert not result["ok"] and result["reason"] == "synthetic_probe_not_production_evidence"
    assert result["stages"]["rvc_v2"]["fallback"] == "passthrough"
    assert result["stages"]["rvc_v2"]["primary_error"] == "RuntimeError"
    assert "RuntimeError" in caplog.text and "SYNTHETIC_" not in caplog.text


@pytest.mark.asyncio
async def test_actual_watchdog_failure_logs_only_type_and_remains_noncritical(caplog):
    app, *_ = build_application(settings=Settings(profile="mock", voice_conversion="off"), background_scheduler=False)
    def failed(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    app.metrics.watchdog_check = failed
    try:
        with caplog.at_level(logging.WARNING, logger="mishka.application"):
            result = await app.watchdog_check_once()
        assert result["error"] == "RuntimeError" and result["stale_tasks"] == []
        assert "RuntimeError" in caplog.text and "SYNTHETIC_" not in caplog.text
        assert "SYNTHETIC_" not in str(app.runtime_status["last_watchdog_event"])
    finally:
        await app.shutdown_identity()


@pytest.mark.asyncio
async def test_actual_state_hook_failure_keeps_warning_without_exception_body(caplog):
    app, *_ = build_application(settings=Settings(profile="mock", voice_conversion="off"), background_scheduler=False)
    async def failed(*args, **kwargs):
        raise ValueError(PRIVATE)
    app.state_update_hook = failed
    try:
        with caplog.at_level(logging.WARNING, logger="mishka.application"):
            result = await app.receive_async(Event("HandsetLifted", "synthetic", 1, 1.0, "test"))
        assert result["canonical_state"]["handset"] == "up"
        assert "canonical state update hook failed" in caplog.text and "ValueError" in caplog.text
        assert "SYNTHETIC_" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records if record.name == "mishka.application")
    finally:
        await app.shutdown_identity()
