"""CPU integration boundaries only; no trained inference, GPU or browser claim."""
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.foundation import api as api_module
from app.foundation import experimental_voice
from app.foundation.config import Settings, SettingsError
from app.foundation.runtime import S2SRuntimeManager
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.s2s import (
    AudioChunk, AudioSynthesisQueue, CancellationController, MockPlaybackController,
    MockTTSProvider, PipelineMetrics, RVCV2Provider, S2SConfig,
)
from scripts import rvc_trained_worker as trained
from scripts.run_remote_stand import RemoteLaunchError, _remote_session, parser, remote_session


class UnavailableVoice:
    async def convert(self, audio, cancelled):
        raise RuntimeError("fixture_conversion_unavailable")


@pytest.mark.asyncio
async def test_warmup_rejects_conversion_failure_before_full_probe():
    class WarmComponent:
        async def warmup(self): return {"ok": True}
    settings = Settings(profile="local_cpu", rvc_fallback_mode="fail_closed")
    tts = MockTTSProvider()
    engine = SimpleNamespace(metrics=PipelineMetrics(settings=settings), tts=tts)
    manager = S2SRuntimeManager(settings, WarmComponent(), tts, UnavailableVoice(), engine,
                                vad=WarmComponent(), stt=WarmComponent(), playback=MockPlaybackController())
    async def forbidden_probe():
        raise AssertionError("failed converter reached the full readiness probe")
    manager.e2e_probe = forbidden_probe
    result = await manager._warmup()
    assert result["ok"] is False
    assert result["reason"] == "fixture_conversion_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,played", [("fail_closed", 0), ("passthrough", 1)])
async def test_converter_failure_obeys_selected_policy_without_hidden_playback(policy, played):
    cancel = CancellationController()
    token = cancel.start("trained-boundary")
    playback = MockPlaybackController()
    queue = AudioSynthesisQueue(MockTTSProvider(), UnavailableVoice(), playback,
                                cancel, S2SConfig(), PipelineMetrics(), fallback_mode=policy)
    await queue.start("trained-boundary", token)
    await queue.submit("Проверка голосовой цепочки.")
    if policy == "fail_closed":
        with pytest.raises(RuntimeError, match="fixture_conversion_unavailable"):
            await queue.finish()
    else:
        await queue.finish()
    assert len(playback.played) == played
    resources = queue.resource_snapshot()
    assert resources["queued_chunks"] == resources["active_audio_bytes"] == 0
    assert queue.worker.done()


@pytest.mark.parametrize("settings,language,rate", [
    (Settings(profile="local_cpu", silero_language="en", silero_sample_rate=24000), "en", 24000),
    (Settings(profile="local_gpu", tts_provider="voxcpm765"), "ru", 48000),
    (Settings(profile="mock"), "mock", 24000),
])
def test_provider_metadata_preserves_baseline_and_identifies_trained_route(settings, language, rate):
    pipeline = PipelineMetrics(settings=settings).pipeline
    assert pipeline["tts_language"] == language
    assert pipeline["tts_sample_rate"] == rate
    assert pipeline["tts_provider"] == ("mock" if settings.profile == "mock" else settings.tts_provider)


@pytest.mark.parametrize("provider,conversion", [("voxcpm765", "rvc_v2"), ("voxcpm765", "off"), ("silero", "off"), ("silero", "rvc_v2"), ("mock", "rvc_v2")])
def test_public_panel_preserves_safe_provider_identity_without_private_fields(provider, conversion):
    projected = HardwareIntegrationStand._panel_metrics({"pipeline": {
        "tts_provider": provider, "voice_conversion": conversion,
        "tts_model": "/private/model/path", "arbitrary_private_field": "PRIVATE_SENTINEL",
    }})
    assert projected["pipeline"]["tts_provider"] == provider
    assert projected["pipeline"]["voice_conversion"] == conversion
    assert "/private/model/path" not in json.dumps(projected)
    assert "PRIVATE_SENTINEL" not in json.dumps(projected)


@pytest.mark.parametrize("pipeline", [None, [], "private", {},
    {"tts_provider": [], "voice_conversion": {}},
    {"tts_provider": "/private/provider", "voice_conversion": "unknown"},
    {"label": "PRIVATE_LABEL", "tts_model": "/private/model"},
])
def test_public_panel_omits_missing_malformed_and_unknown_pipeline_fields(pipeline):
    assert "pipeline" not in HardwareIntegrationStand._panel_metrics({"pipeline": pipeline})


def test_public_panel_preserves_only_individually_valid_pipeline_enums():
    assert HardwareIntegrationStand._panel_metrics({"pipeline": {
        "tts_provider": "voxcpm765", "voice_conversion": "UNKNOWN_PRIVATE",
    }})["pipeline"] == {"tts_provider": "voxcpm765"}
    assert HardwareIntegrationStand._panel_metrics({"pipeline": {
        "tts_provider": "UNKNOWN_PRIVATE", "voice_conversion": "disabled",
    }})["pipeline"] == {"voice_conversion": "disabled"}


@pytest.mark.asyncio
async def test_shutdown_attempts_tts_even_after_session_and_voice_cleanup_fail():
    calls = []
    async def cancel():
        calls.append("cancel")
        raise RuntimeError("fixture_cancel_failure")
    class Component:
        def __init__(self, name, fail=False): self.name, self.fail = name, fail
        async def close(self):
            calls.append(self.name)
            if self.fail: raise RuntimeError("fixture_close_failure")
    manager = S2SRuntimeManager(Settings(), None, Component("tts"), Component("voice", True),
                                SimpleNamespace(cancel_all_sessions=cancel))
    with pytest.raises(BaseExceptionGroup):
        await manager.close()
    assert calls == ["cancel", "voice", "tts"]


def test_trained_builder_keeps_uncached_tts_and_unwrapped_fail_closed_voice(monkeypatch, tmp_path):
    class TTS(MockTTSProvider):
        provider_name = "voxcpm765"
    tts = TTS()
    factory_calls = []
    def factory(route, manifest):
        factory_calls.append((route, manifest))
        return tts
    class Voice:
        def __init__(self, **options): self.options = options
        def configuration_readiness(self): return {"configuration_ready": True}
    def forbidden_wrapper(*args, **kwargs):
        raise AssertionError("trained route acquired a cache or passthrough wrapper")
    monkeypatch.delenv("MISHKA_VOICE_CONVERSION", raising=False)
    monkeypatch.setattr(experimental_voice, "create_experimental_tts_provider", factory)
    monkeypatch.setattr(api_module, "RVCV2Provider", Voice)
    monkeypatch.setattr(api_module, "CachedTTSProvider", forbidden_wrapper)
    monkeypatch.setattr(api_module, "ResilientVoiceProvider", forbidden_wrapper)
    settings = Settings(profile="local_gpu", tts_provider="voxcpm765",
                        experimental_voice_manifest=str(tmp_path / "private-manifest.json"),
                        rvc_fallback_mode="fail_closed", rvc_worker_script=str(Path(trained.__file__)))
    engine, audio, manager = api_module._build_s2s(settings, tmp_path, MockPlaybackController())
    assert factory_calls == [("voxcpm765", settings.experimental_voice_manifest)]
    assert engine.tts is manager.tts is tts
    assert engine.voice is manager.voice
    assert manager.voice.options["worker_script"] == settings.rvc_worker_script


@pytest.mark.asyncio
async def test_trained_launcher_rejects_mock_or_missing_explicit_assets_before_loading(tmp_path, monkeypatch):
    monkeypatch.delenv("MISHKA_VOICE_CONVERSION", raising=False)
    with pytest.raises(RemoteLaunchError, match="remote_trained_voice_requires_explicit_assets"):
        async with _remote_session(max_source=tmp_path, state_dir=tmp_path / "runtime",
                                   profile="mock", voice_route="voxcpm765_rvc100"):
            pytest.fail("trained mock route was admitted")
    with pytest.raises(RemoteLaunchError, match="remote_trained_voice_requires_explicit_assets"):
        async with _remote_session(max_source=tmp_path, state_dir=tmp_path / "runtime",
                                   profile="local_gpu", voice_route="voxcpm765_rvc100"):
            pytest.fail("missing trained assets were admitted")


def test_vram_reserve_cli_is_optional_and_float(tmp_path):
    required = ["--max-source", str(tmp_path), "--state-dir", str(tmp_path / "state"),
                "--external-origin", "http://127.0.0.1:8000"]
    assert parser().parse_args(required).minimum_free_vram_gb is None
    assert parser().parse_args(required + ["--minimum-free-vram-gb", "4"]).minimum_free_vram_gb == 4.0


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve", [4.0, 8.0])
async def test_baseline_rejects_explicit_vram_reserve(tmp_path, reserve):
    with pytest.raises(RemoteLaunchError, match="remote_baseline_rejects_vram_override"):
        async with _remote_session(max_source=tmp_path, state_dir=tmp_path / "state",
                                   minimum_free_vram_gb=reserve):
            pytest.fail("baseline accepted a trained-only reserve")


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve", [float("nan"), float("inf"), float("-inf"), 0, -1, 1025])
async def test_trained_reserve_reuses_settings_resource_validation_before_loading(tmp_path, reserve):
    with pytest.raises(SettingsError, match="resource thresholds"):
        async with _remote_session(max_source=tmp_path, state_dir=tmp_path / "state", profile="local_gpu",
                                   voice_route="voxcpm765_rvc100", voice_manifest=tmp_path / "voices.json",
                                   rvc_root=tmp_path, rvc_python=tmp_path / "python", rvc_model=tmp_path / "model",
                                   rvc_index=tmp_path / "index", minimum_free_vram_gb=reserve):
            pytest.fail("invalid resource reserve was admitted")


@pytest.mark.asyncio
@pytest.mark.parametrize("route,reserve,expected", [
    ("baseline_silero", None, 8.0), ("voxcpm765_rvc100", None, 8.0), ("voxcpm765_rvc100", 4.0, 4.0),
])
async def test_launcher_records_effective_reserve_and_passes_it_to_settings(tmp_path, monkeypatch, route, reserve, expected):
    source = os.environ.get("MAX_JSON_TEST_SRC")
    if not source or not (Path(source) / "phone_local_demo.py").is_file():
        pytest.skip("requires explicit unchanged local Max source")
    monkeypatch.delenv("STAND_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MISHKA_VOICE_CONVERSION", raising=False)
    captured = []
    from app.foundation import stand as stand_module
    original = stand_module.create_stand_app
    def mock_models_only(settings, **options):
        # Real Max TLS/SQLite and launcher lifecycle; only model construction is
        # explicitly mock so this CPU test makes no trained GPU acceptance claim.
        captured.append(settings.validate())
        return original(replace(settings, profile="mock", tts_provider="silero", voice_conversion="off"), **options)
    monkeypatch.setattr(stand_module, "create_stand_app", mock_models_only)
    trained_options = {} if route == "baseline_silero" else dict(
        voice_manifest=tmp_path / "voices.json", rvc_root=tmp_path, rvc_python=tmp_path / "python",
        rvc_model=tmp_path / "model", rvc_index=tmp_path / "index")
    async with remote_session(max_source=Path(source), state_dir=tmp_path / "runtime",
                              profile="mock" if route == "baseline_silero" else "local_gpu",
                              voice_route=route, minimum_free_vram_gb=reserve, **trained_options) as runtime:
        assert captured[0].min_available_vram_gb == expected
        assert runtime.manifest["min_available_vram_gb"] == expected
        saved = json.loads((runtime.state_dir / "launch.json").read_text())
        assert saved["min_available_vram_gb"] == expected
        assert runtime.api.state.stand.settings.min_available_vram_gb == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["valid", "missing_model", "wrong_model", "passthrough", "no_generator", "no_query", "index_failure", "boolean_counter"])
async def test_trained_reply_requires_exact_identity_and_actual_conversion_evidence(tmp_path, monkeypatch, mutation):
    identity = {"model_sha256": trained.MODEL_SHA256, "index_sha256": trained.INDEX_SHA256,
                "source_revision": trained.SOURCE_REVISION}
    reply = {"event": "converted", "actual_route": "rvc100", "fallback_used": False,
             "passthrough_used": False, **identity,
             "route_evidence": {"model_calls": 1, "index_queries": 1, "index_failures": 0, "full_probe_retries": 0}}
    provider = RVCV2Provider(worker_script=str(Path(trained.__file__)), tmp_dir=str(tmp_path))
    provider._worker_info = dict(identity)
    if mutation == "missing_model": reply.pop("model_sha256")
    if mutation == "wrong_model": reply["model_sha256"] = "0" * 64
    if mutation == "passthrough": reply["passthrough_used"] = True
    if mutation == "no_generator": reply["route_evidence"]["model_calls"] = 0
    if mutation == "no_query": reply["route_evidence"]["index_queries"] = 0
    if mutation == "index_failure": reply["route_evidence"]["index_failures"] = 1
    if mutation == "boolean_counter": reply["route_evidence"]["model_calls"] = True
    class Sink:
        def write(self, value): pass
        async def drain(self): pass
    provider._process = SimpleNamespace(stdin=Sink())
    stopped = []
    async def ensure(): pass
    async def read(request_id, cancelled): return reply
    async def stop(*args, **kwargs): stopped.append(True); provider._process = None
    monkeypatch.setattr(provider, "_ensure_worker", ensure)
    monkeypatch.setattr(provider, "_read_reply", read)
    monkeypatch.setattr(provider, "_stop_worker_drained", stop)
    monkeypatch.setattr(provider, "_read_wav", lambda path: ((.1, -.1), 40000))
    audio = AudioChunk((.1, -.1), 40000, 7)
    if mutation == "valid":
        converted = await provider.convert(audio, lambda: False)
        assert converted.token == 7 and converted.sample_rate == 40000
        assert provider._last_conversion_evidence["actual_route"] == "rvc100"
        assert not stopped
    else:
        with pytest.raises(RuntimeError, match="trained_rvc_conversion_unproven"):
            await provider.convert(audio, lambda: False)
        assert stopped and provider.jobs_completed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_model", "wrong_route", "passthrough", "invalid_revision"])
async def test_trained_ready_handshake_rejects_missing_or_unproven_identity(tmp_path, monkeypatch, mutation):
    reply = {"event": "ready", "actual_route": "rvc100", "fallback_used": False,
             "passthrough_used": False, "model_sha256": trained.MODEL_SHA256,
             "index_sha256": trained.INDEX_SHA256, "source_revision": trained.SOURCE_REVISION}
    if mutation == "missing_model": reply.pop("model_sha256")
    if mutation == "wrong_route": reply["actual_route"] = "passthrough"
    if mutation == "passthrough": reply["passthrough_used"] = True
    if mutation == "invalid_revision": reply["source_revision"] = "unproven"
    provider = RVCV2Provider(project_root=str(tmp_path), model_path=str(tmp_path / "model.pth"),
                             index_path=str(tmp_path / "model.index"), worker_script=str(Path(trained.__file__)),
                             tmp_dir=str(tmp_path / "runtime"))
    monkeypatch.setattr(provider, "configuration_readiness", lambda: {"configuration_ready": True})
    class Stream:
        async def readline(self): return json.dumps(reply).encode() + b"\n"
    async def create(*args, **kwargs):
        return SimpleNamespace(returncode=None, pid=123, stdout=Stream())
    stopped = []
    async def stop(*args, **kwargs): stopped.append(True); provider._process = None
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(provider, "_stop_worker_drained", stop)
    with pytest.raises(RuntimeError, match="trained_rvc_identity_unproven"):
        await provider._ensure_worker()
    assert stopped and not provider._worker_info and provider.jobs_completed == 0
