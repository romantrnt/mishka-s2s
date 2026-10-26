from __future__ import annotations
import asyncio

import os
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from collections import deque
from pathlib import Path

from .adapters import (ExhibitionProfileProvider, ProfileProviderError, SQLiteProfileProvider, RecordingAudio, RecordingLED, RuleBasedIntentClassifier)
from .application import ScenarioApplication
from .schedule_weather import DemoScheduleWeatherService, ProductionScheduleWeatherService, YandexWeatherProvider
from .canonical_services import CanonicalContentCatalog
from .config import Settings, settings_with_compat_env
from .events import parse_event
from .observability import Metrics, StructuredEventPublisher
from .repositories import HistoryProviderError, InMemoryHistoryRepository, InMemorySessionRepository, SQLiteRepository
from .runtime import S2SRuntimeManager
from .tts_engine import SileroTTSEngine
from .lifecycle import StartupLifecycle
from .s2s import (BoundedConversationEngine, CachedTTSProvider, EnergyVADAdapter,
                  FasterWhisperRecognizer, LocalS2SPipeline,
                  OllamaLLMProvider, SileroTTSProvider,
                  PassthroughVoiceProvider, RVCProvider, RVCV2Provider, S2SConfig,
                  ResilientVoiceProvider,
                  SileroVADAdapter, SoundDevicePlaybackController,
                  LLMProvider, PlaybackController, TTSProvider,
                  SpeechRecognizer, VADAdapter, VLLMLLMProvider, RetryingLLMProvider,
                  VoiceConversionProvider)

PROJECT_ROOT = Path(os.environ.get("MISHKA_ROOT", Path(__file__).resolve().parents[2])).resolve()


def _project_path(value: str, *, keep_bare_command: bool = False) -> str:
    path = Path(value)
    if path.is_absolute() or (keep_bare_command and path.parent == Path(".")):
        return str(path)
    return str((PROJECT_ROOT / path).resolve())


def _normalize_paths(settings: Settings) -> Settings:
    values = asdict(settings)
    for field in ("content_root", "profile_db", "silero_cache_path", "rvc_project_root", "rvc_model", "rvc_index", "rvc_tmp_dir", "warmup_pcm_path"):
        if values.get(field):
            values[field] = _project_path(str(values[field]))
    if values.get("rvc_python"):
        python_path = Path(str(values["rvc_python"]))
        if not python_path.is_absolute():
            python_path = PROJECT_ROOT / python_path
        values["rvc_python"] = str(python_path)
    return Settings(**values)


def _prepare_runtime_dirs(settings: Settings) -> None:
    """Create only bounded runtime state directories owned by the app."""
    paths = {
        PROJECT_ROOT / "runtime",
        PROJECT_ROOT / "runtime" / "rvc",
        PROJECT_ROOT / "logs",
        PROJECT_ROOT / "data",
        Path(settings.rvc_tmp_dir),
        Path(settings.silero_cache_path).parent,
    }
    if settings.runtime_mode != "exhibition":
        paths.add(Path(settings.profile_db).parent)
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def _silero_loader(settings: Settings):
    import torch
    from silero_vad import load_silero_vad
    device = settings.vad_device
    if device not in {"cpu", "cuda"}:
        raise RuntimeError(f"unsupported MISHKA_VAD_DEVICE: {device}")
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("MISHKA_VAD_DEVICE=cuda but CUDA is unavailable")
    model=load_silero_vad()
    if device == "cuda" and hasattr(model, "to"):
        model=model.to(device)
    def probability(samples, sample_rate):
        tensor=torch.tensor(samples,dtype=torch.float32,device=device)
        return float(model(tensor,sample_rate).item())
    return probability


def _whisper_loader(settings: Settings):
    from faster_whisper import WhisperModel
    device="cuda" if settings.profile in {"local_gpu","production_candidate","exhibition_candidate","production_qwen_rvc"} else "cpu"
    compute="float16" if device=="cuda" else "int8"
    return WhisperModel(settings.stt_model,device=device,compute_type=compute)


def _build_s2s(settings: Settings, root: Path, playback_override: PlaybackController | None = None):
    settings = settings_with_compat_env(settings, ("MISHKA_VOICE_CONVERSION",))
    config=S2SConfig.from_settings(settings)
    playback: PlaybackController; llm: LLMProvider; tts: TTSProvider; voice: VoiceConversionProvider
    vad: VADAdapter; stt: SpeechRecognizer
    if settings.profile=="mock":
        from .s2s import MockLLMProvider, MockPlaybackController, MockSpeechRecognizer, MockTTSProvider
        from .adapters import MockProfileProvider
        playback=playback_override or MockPlaybackController()
        llm=MockLLMProvider(); tts=MockTTSProvider(); voice=RVCProvider()
        engine=BoundedConversationEngine(llm,CachedTTSProvider(tts),voice,playback,config,allow_quick_replies=True,profile=settings.profile,settings=settings)
        vad=EnergyVADAdapter(); stt=MockSpeechRecognizer()
        audio=LocalS2SPipeline(vad,stt,engine,config)
        return engine,audio,S2SRuntimeManager(settings,llm,tts,voice,engine,vad=vad,stt=stt,playback=playback)
    # Complete PyTorch's import on the startup thread before VAD warmup or
    # model workers can import it from other threads.
    import torch
    if settings.llm_provider=="ollama":
        llm=OllamaLLMProvider(settings.ollama_base_url,settings.llm_model,config.llm_timeout_s,quantization=settings.llm_quantization,num_ctx=settings.llm_num_ctx,temperature=settings.llm_temperature,max_tokens=settings.llm_max_tokens,keep_alive=settings.llm_keep_alive,think=settings.llm_think,warmup_timeout_s=settings.llm_warmup_timeout_s)
    elif settings.llm_provider=="vllm":
        llm=VLLMLLMProvider(settings.vllm_base_url,settings.llm_model,config.llm_timeout_s,temperature=settings.llm_temperature,max_tokens=settings.llm_max_tokens)
    else:
        raise RuntimeError(f"unsupported production LLM provider: {settings.llm_provider}")
    if settings.llm_provider in {"ollama", "vllm"}:
        llm=RetryingLLMProvider(llm, retries=1)
    playback=playback_override or SoundDevicePlaybackController(settings.audio_output_device or None)
    def resolved(value: str) -> str:
        return _project_path(value, keep_bare_command=True)
    tts=SileroTTSProvider(SileroTTSEngine(language=settings.silero_language, model_name=settings.silero_model, speaker=settings.silero_speaker, sample_rate=settings.silero_sample_rate, cache_path=settings.silero_cache_path, leading_silence_ms=settings.silero_leading_silence_ms))
    voice_mode = settings.voice_conversion
    if voice_mode in {"off", "disabled"}:
        voice = PassthroughVoiceProvider()
    elif voice_mode == "rvc_v2":
        primary_voice = RVCV2Provider(
            project_root=resolved(settings.rvc_project_root),
            python=resolved(settings.rvc_python),
            model_path=resolved(settings.rvc_model),
            index_path=resolved(settings.rvc_index),
            f0_method=settings.rvc_f0_method,
            filter_radius=settings.rvc_filter_radius,
            f0_up_key=settings.rvc_f0_up_key,
            index_rate=settings.rvc_index_rate,
            protect=settings.rvc_protect,
            tmp_dir=settings.rvc_tmp_dir,
            startup_timeout_s=settings.rvc_startup_timeout_s,
        )
        status = primary_voice.configuration_readiness()
        if not status.get("configuration_ready"):
            raise RuntimeError(
                "RVC v2 requested but its pinned runtime/model/index configuration is unavailable"
            )
        voice = ResilientVoiceProvider(primary_voice)
    else:
        raise ValueError(f"unsupported MISHKA_VOICE_CONVERSION: {voice_mode}")
    engine=BoundedConversationEngine(
        llm,CachedTTSProvider(tts),voice,playback,config,allow_quick_replies=False,profile=settings.profile,settings=settings,
    )
    vad=SileroVADAdapter(lambda: _silero_loader(settings))
    stt=FasterWhisperRecognizer(lambda:_whisper_loader(settings),config,settings.stt_model)
    audio=LocalS2SPipeline(vad,stt,engine,config)
    return engine,audio,S2SRuntimeManager(settings,llm,tts,voice,engine,vad=vad,stt=stt,playback=playback)


def build_application(content_root: str | Path | None = None, settings: Settings | None = None,
                      *, audio_output=None, led_controller=None,
                      playback_override: PlaybackController | None = None, display_controller=None,
                      monotonic_now=None, background_scheduler: bool = True):
    settings=settings or Settings.from_env()
    settings = settings_with_compat_env(settings, ("MISHKA_PROFILE_DB", "MISHKA_CONTENT_ROOT"))
    settings = _normalize_paths(settings).validate()
    _prepare_runtime_dirs(settings)
    root = Path(content_root).resolve() if content_root is not None else Path(settings.content_root).resolve()
    scenarios = CanonicalContentCatalog(str(root), allow_development_assets=(settings.profile in {"mock", "local_cpu", "local_gpu"} or str(settings.hardware_mode).lower() != "physical")); metrics = Metrics(latency_limit=settings.observability_latency_limit, event_limit=settings.observability_event_limit, watchdog_limit=settings.observability_watchdog_limit); publisher = StructuredEventPublisher()
    s2s,audio_pipeline,runtime_manager=_build_s2s(settings,root,playback_override)
    profile_db = settings.profile_db
    if settings.runtime_mode == "exhibition":
        # Exhibition is deliberately self-contained: no RFID/profile provider,
        # SQLite session persistence, or clinical identity gate is constructed.
        sessions = InMemorySessionRepository(); history = InMemoryHistoryRepository()
        profiles = ExhibitionProfileProvider()
    elif settings.profile == "mock":
        from .adapters import MockProfileProvider
        sessions = InMemorySessionRepository(); history = InMemoryHistoryRepository()
        profiles = MockProfileProvider()
    else:
        if not profile_db:
            raise RuntimeError("MISHKA_PROFILE_DB is required for production profile integration")
        sessions = SQLiteRepository(profile_db); history = sessions
        profiles = SQLiteProfileProvider(profile_db)
    weather_config = settings.weather_configuration()
    if weather_config.provider == "demo":
        weather = DemoScheduleWeatherService()
    elif weather_config.provider == "yandex":
        weather = ProductionScheduleWeatherService(YandexWeatherProvider.from_settings(settings), weather_config.timeout_s)
    else:
        weather = ProductionScheduleWeatherService(None, weather_config.timeout_s)
    app = ScenarioApplication(sessions, scenarios, profiles, RuleBasedIntentClassifier(), audio_output or RecordingAudio(), led_controller or RecordingLED(), publisher, now=time.time, threshold=settings.confidence_threshold, bounded_s2s=s2s, history=history, max_session_seconds=settings.session_timeout_s, idle_timeout_seconds=settings.session_idle_timeout_s, display=display_controller, schedule_weather=weather, allow_quick_replies=settings.profile == "mock", settings=settings, monotonic_now=monotonic_now or time.monotonic, background_scheduler=background_scheduler)
    app.s2s_audio=audio_pipeline; app.runtime_manager=runtime_manager; runtime_manager.audio_pipeline=audio_pipeline; app.runtime_status=runtime_manager.configuration(); app.settings=settings; app.metrics=metrics
    # The bounded S2S engine owns the authoritative backend lifecycle. Let it
    # drive the same canonical LED engine used by the physical stand and UI.
    s2s.lifecycle_hook = app.dialogue_lifecycle
    if settings.profile != "mock":
        from .e2e_probe import ProductionE2EWarmupProbe
        runtime_manager.e2e_probe = ProductionE2EWarmupProbe(runtime_manager, audio_pipeline).run
    app.history_repository=history
    return app, scenarios, metrics, publisher

def create_app(application=None, *, include_test_routes: bool = False):
    try:
        from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
        from fastapi.responses import JSONResponse
    except ImportError as exc: raise RuntimeError("Run make install before starting the HTTP API") from exc
    # With postponed annotations FastAPI resolves WebSocket from module globals.
    globals()["WebSocket"] = WebSocket
    globals()["Response"] = Response
    if application is None:
        build_settings = Settings(profile="mock") if include_test_routes else None
        application, scenarios, metrics, publisher = build_application(settings=build_settings)
    else:
        # The hardware stand injects the already-wired application so it can
        # share the PTY-backed audio/LED adapters.  Do not discard its
        # scenario repository here: doing so made /ready report
        # {"external": true} even though the stand had validated content.
        scenarios = getattr(application, "scenarios", None)
        metrics = getattr(application, "metrics", None) or Metrics()
    lifecycle = StartupLifecycle()
    manager = getattr(application, "runtime_manager", None)

    @asynccontextmanager
    async def lifespan(_api):
        # The hardware stand is attached after create_app() returns. Resolve it
        # at lifespan entry so physical HID/lighting startup is not skipped.
        hardware_owner = getattr(_api.state, "stand", None)
        if hardware_owner is None and hasattr(application, "start_hardware"):
            hardware_owner = application
        try:
            if hardware_owner is not None and hasattr(hardware_owner, "start_hardware"):
                await hardware_owner.start_hardware()
                led = getattr(getattr(hardware_owner, "application", hardware_owner), "led", None)
                start_breathing = getattr(led, "start_breathing", None)
                if callable(start_breathing):
                    start_breathing()
            if manager is None:
                lifecycle.dependency_failed("runtime", "manager_unavailable")
            else:
                await lifecycle.start(manager.warmup)
                if hasattr(application, "start_idle_monitor"):
                    await application.start_idle_monitor()
                if hasattr(application, "start_watchdog"):
                    await application.start_watchdog(lifecycle, manager)
            yield
        finally:
            try:
                if hasattr(application, "stop_watchdog"):
                    await application.stop_watchdog()
                if hasattr(application, "stop_idle_monitor"):
                    await application.stop_idle_monitor()
                await lifecycle.stop()
                if manager is not None:
                    await manager.close()
            finally:
                owned_stand = getattr(_api.state, "stand", None)
                hardware_stopped = False
                try:
                    if hardware_owner is not None and hasattr(hardware_owner, "stop_hardware"):
                        await hardware_owner.stop_hardware()
                        hardware_stopped = True
                finally:
                    if owned_stand is not None:
                        await owned_stand.aclose(hardware_stopped=hardware_stopped)

    api = FastAPI(
        title="Мягкий телефон Мишки — production candidate",
        version="0.3.0",
        lifespan=lifespan,
    )
    api.state.application = application
    api.state.lifecycle = lifecycle

    @api.exception_handler(ValueError)
    async def invalid_event(_request, exc): return JSONResponse(status_code=422,content={"error":{"code":"invalid_event","message":str(exc)}})
    @api.exception_handler(ProfileProviderError)
    async def profile_provider_failure(_request, exc): return JSONResponse(status_code=503,content={"error":{"code":"profile_provider_unavailable","message":str(exc)}})
    @api.exception_handler(HistoryProviderError)
    async def history_provider_failure(_request, exc): return JSONResponse(status_code=503, content={"error":{"code":"history_provider_unavailable","message":str(exc)}})
    sequence_counters: dict[str, int] = {}
    request_times: dict[str, deque[float]] = {}
    async def dispatch(session_id, payload, sequence=None):
        now=time.monotonic(); window=request_times.setdefault(session_id,deque())
        while window and now-window[0]>1.0: window.popleft()
        if len(window)>=30:
            raise HTTPException(status_code=429, detail={"code":"rate_limit", "message":"session rate limit exceeded"})
        window.append(now)
        requested = sequence if sequence is not None else payload.get("sequence")
        if requested is None:
            value = sequence_counters.get(session_id, 0) + 1
        else:
            value = int(requested)
        sequence_counters[session_id] = max(sequence_counters.get(session_id, 0), value)
        event = parse_event(payload, session_id, value, "api")
        return await application.receive_async(event, payload.get("text"))
    @api.get("/")
    def root():
        """Lightweight probe for reverse proxies and reverse-proxy HTTP services."""
        return {"service": "mishka", "status": "ok", "health": "/health", "ready": "/ready"}
    def dependency_identity(profile_status, runtime):
        settings = getattr(application, "settings", None)
        components = runtime.get("components", {}) if isinstance(runtime, dict) else {}
        hardware = getattr(application, "dependency_identity", {}).get("hardware_backend")
        if not isinstance(hardware, dict):
            hardware = {"name": "unknown", "state": "unavailable", "reason": "hardware_identity_unavailable"}
        def component(name, fallback):
            value = components.get(name)
            if isinstance(value, dict):
                return dict(value)
            return {"name": fallback, "state": "configured" if fallback != "unknown" else "unknown", **({} if fallback != "unknown" else {"reason": "component_identity_unavailable"})}
        history_repo = getattr(application, "history_repository", None)
        history_status = (history_repo.readiness() if history_repo is not None and hasattr(history_repo, "readiness")
                          else {"ready": False, "provider": type(history_repo).__name__ if history_repo is not None else None,
                                "provider_state": "unavailable", "reason": "history_provider_readiness_unavailable"})
        return {
            "runtime_mode": {"name": getattr(settings, "runtime_mode", "unknown"), "state": "configured"},
            "hardware_backend": hardware,
            "profile_provider": dict(profile_status),
            "llm": component("llm", runtime.get("llm_provider", "unknown")),
            "stt": component("stt", runtime.get("stt_provider", "unknown")),
            "tts": component("silero", runtime.get("tts", "unknown")),
            "voice_conversion": component("rvc_v2", runtime.get("voice_conversion", "unknown")),
            "history": dict(history_status),
        }

    @api.get("/health")
    def health():
        operational_stand = getattr(api.state, "stand", None)
        if operational_stand is not None and hasattr(operational_stand, "health_snapshot"):
            return operational_stand.health_snapshot()
        manager=getattr(application,"runtime_manager",None)
        warmup=getattr(manager,"last_warmup",None) if manager is not None else None
        provider = getattr(application, "profiles", None)
        profile_status = provider.readiness() if provider is not None and hasattr(provider, "readiness") else {"ready": False, "provider": type(provider).__name__ if provider is not None else None, "reason": "profile_provider_readiness_unavailable"}
        engine=getattr(getattr(application, "runtime_manager", None), "engine", None)
        resources=application.resource_snapshot() if hasattr(application, "resource_snapshot") else (engine.resource_snapshot() if engine is not None and hasattr(engine, "resource_snapshot") else {})
        metrics.observe_resources(resources)
        runtime_status = getattr(manager, "configuration", lambda: {})()
        hardware = dependency_identity(profile_status, runtime_status).get("hardware_backend", {})
        simulated_hardware = str(hardware.get("state", "")).lower() in {"simulator", "fake", "degraded"}
        return {"status":"ok", "profile":getattr(application,"runtime_status",{}).get("profile","external"),
                "degraded": simulated_hardware, "software_runtime_usable": True,
                "production_ready": False if simulated_hardware else None,
                "physical_hardware_ready": not simulated_hardware,
                "observability":{"resources":resources,"metrics":metrics.snapshot(),"warmup_history":{"last_successful":getattr(manager,"last_successful_warmup",None),"last_failed":getattr(manager,"last_failed_warmup",None),"active_probe_correlation_id":getattr(manager,"active_probe_correlation_id",None)},"last_recovery":getattr(manager,"last_recovery",None)},
                "lifecycle": lifecycle.snapshot(), "warmup": warmup,
                "models_loaded":bool(warmup and warmup.get("ok")), "no_voice_recording":True,
                "profile_provider":type(provider).__name__, "profile_provider_ready":bool(profile_status.get("ready")),
                "profile_db_configured":bool(getattr(getattr(application, "settings", None), "profile_db", "")),
                "dependencies": dependency_identity(profile_status, runtime_status)}
    @api.get("/live")
    def live(): return {"live":True}
    @api.get("/health/live")
    def health_live():
        return {"live": True, "service": "mishka"}
    @api.get("/health/safe")
    def health_safe():
        handoff = getattr(application, "adult_handoff", None)
        simulated = type(handoff).__name__ == "SimulatedAdultHandoff"
        configured = handoff is not None
        return {"safe": configured and not simulated, "adult_handoff_simulated": simulated,
                "adult_handoff": {"configured": configured, "simulated": simulated},
                "safety_policy": "canonical_fail_closed", "fail_closed": True}
    async def _bounded_runtime_readiness(manager):
        if manager is None:
            return {"ready": False, "profile": "external", "reason": "runtime_manager_unavailable"}
        timeout_s = 3.0
        try:
            timeout_s = min(max(float(getattr(getattr(manager, "settings", None), "readiness_timeout_s", timeout_s)), 0.25), 5.0)
        except (TypeError, ValueError):
            timeout_s = 3.0
        try:
            return await asyncio.wait_for(manager.readiness(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return {"ready": False, "reason": "runtime_readiness_timeout", "timeout_s": timeout_s, "bounded": True}
        except Exception as exc:
            return {"ready": False, "reason": "runtime_readiness_error", "error": type(exc).__name__, "bounded": True}

    @api.get("/ready")
    @api.get("/health/ready")
    async def ready(response: Response = None):
        manager=getattr(application,"runtime_manager",None)
        runtime=await _bounded_runtime_readiness(manager)
        content_ready = scenarios is None or bool(scenarios.report().get("ready"))
        authorizer = getattr(application, "transition_authorizer", None)
        graph_status = (authorizer.status() if authorizer is not None else {
            "provider": "langgraph", "strict": True, "ready": False,
            "reason": "canonical_authorizer_unavailable",
        })
        provider = getattr(application, "profiles", None)
        profile_status = provider.readiness() if provider is not None and hasattr(provider, "readiness") else {"ready": False, "provider": type(provider).__name__ if provider is not None else None, "reason": "profile_provider_readiness_unavailable"}
        history_repo = getattr(application, "history_repository", None)
        history_status = (history_repo.readiness() if history_repo is not None and hasattr(history_repo, "readiness")
                          else {"ready": False, "provider": type(history_repo).__name__ if history_repo is not None else None,
                                "provider_state": "unavailable", "reason": "history_provider_readiness_unavailable"})
        lifecycle_status = lifecycle.snapshot()
        runtime_ready = bool(runtime.get("ready"))
        if lifecycle_status["ready"] and not runtime_ready:
            lifecycle.observe_noncritical("runtime_not_ready", {"source": "readiness", "runtime_reason": runtime.get("reason")})
            lifecycle_status = lifecycle.snapshot()
        hardware = dependency_identity(profile_status, runtime).get("hardware_backend", {})
        simulated_hardware = str(hardware.get("state", "")).lower() in {"simulator", "fake", "degraded"}
        content_report = scenarios.report() if scenarios else {"external": True}
        voice_report = content_report.get("voice", {}) if isinstance(content_report, dict) else {}
        audio_content_ready = bool(voice_report.get("approved_recorded_count", 0) >= voice_report.get("required_recorded_count", 1))
        settings = getattr(application, "settings", None)
        security_ready = bool(getattr(settings, "mutation_policy_ready", False))
        provenance = settings.operational_snapshot().get("provenance", {}) if settings is not None else {}
        launcher_provenance_ready = provenance.get("entrypoint") == "run_stand.py"
        lifecycle_admission = bool(
            lifecycle_status.get("ready") and runtime_ready
        ) or bool(
            simulated_hardware
            and lifecycle_status.get("state") == "DEGRADED"
            and lifecycle_status.get("reason") == "resource_capacity_insufficient"
        )
        software_runtime_usable = bool(
            lifecycle_admission
            and bool(profile_status.get("ready"))
            and bool(history_status.get("ready"))
            and bool(graph_status.get("ready"))
        )
        content_production_ready = bool(
            isinstance(content_report, dict)
            and voice_report.get("physical_production_ready")
            and all(bool(value) for value in content_report.get("fixed_stories", {}).values())
        )
        production_ready = bool(
            not simulated_hardware
            and software_runtime_usable
            and content_ready
            and content_production_ready
            and security_ready
            and launcher_provenance_ready
            and bool(profile_status.get("ready"))
            and bool(history_status.get("ready"))
            and bool(graph_status.get("ready"))
        )
        if response is not None:
            response.status_code = 200 if software_runtime_usable else 503
        return {
            "ready": software_runtime_usable,
            "readiness_contract": "software_admission",
            "degraded": not software_runtime_usable,
            "software_runtime_usable": software_runtime_usable,
            "production_ready": production_ready,
            "headless_ready": software_runtime_usable,
            "webpanel_ready": True,
            "simulated_hardware_ready": simulated_hardware,
            "physical_hardware_ready": not simulated_hardware,
            "audio_content_ready": audio_content_ready,
            "security": {
                "mutation_auth_policy": getattr(settings, "mutation_auth_policy", "unconfigured"),
                "operator_token_present": bool(getattr(settings, "operator_token_present", False)),
                "mutation_policy_ready": security_ready,
                "launcher_provenance_ready": launcher_provenance_ready,
            },
            "production_blockers": [
                code for code, blocked in (
                    ("simulator_hardware", simulated_hardware),
                    ("software_runtime_unusable", not software_runtime_usable),
                    ("content_not_production_ready", not content_production_ready),
                    ("mutation_policy_unconfigured_or_unsafe", not security_ready),
                    ("launcher_provenance_unestablished", not launcher_provenance_ready),
                ) if blocked
            ],
            "lifecycle": lifecycle_status,
            "content": content_report,
            "metrics": metrics.snapshot(),
            "dependencies": {"s2s": runtime, "profile": profile_status, "history": history_status,
                             "state_machine": graph_status, **dependency_identity(profile_status, runtime)},
        }
    @api.post("/internal/s2s/warmup")
    async def warmup():
        manager=getattr(application,"runtime_manager",None)
        if manager is None: raise HTTPException(status_code=503,detail="S2S runtime manager unavailable")
        result=await lifecycle.run_now(manager.warmup)
        if not result.get("ok"): raise HTTPException(status_code=503,detail=result)
        return result
    if include_test_routes:
        @api.post("/session/start")
        def start(payload:dict): return application.start_session(str(payload.get("session_id","mock")),str(payload.get("correlation_id","")) or None)
        @api.post("/mock/rfid")
        async def rfid(payload:dict): return await dispatch(str(payload.get("session_id","mock")), {"type":"rfid","tag_id":payload.get("tag_id")}, payload.get("sequence"))
        @api.post("/mock/handset")
        async def handset(payload:dict): return await dispatch(str(payload.get("session_id","mock")), {"type":"handset_up" if payload.get("state","up")=="up" else "handset_down"}, payload.get("sequence"))
        @api.post("/mock/button")
        async def button(payload:dict): return await dispatch(str(payload.get("session_id","mock")), {"type":"button","button_id":payload.get("button_id")}, payload.get("sequence"))
        @api.post("/mock/text")
        async def text(payload:dict): return await dispatch(str(payload.get("session_id","mock")), {"type":"text","text":payload.get("text","")}, payload.get("sequence"))
        @api.get("/mock/session/{session_id}")
        def snapshot(session_id:str):
            value = application.sessions.get(session_id)
            if not value: raise HTTPException(status_code=404, detail="session not found")
            return {"session_id":value.session_id,"state":value.state.value,"node":value.current_node,"correlation_id":value.correlation_id,"last_sequence":value.last_sequence}
        @api.post("/session/reset")
        async def reset(payload:dict):
            session_id=str(payload.get("session_id","mock")); sequence_counters.pop(session_id, None); request_times.pop(session_id,None)
            return await application.reset_session(session_id)
    @api.websocket("/ws/{session_id}")
    async def ws(websocket:WebSocket, session_id:str):
        settings = getattr(application, "settings", None)
        access_token = str(getattr(settings, "stand_access_token", "") or "")
        headers = getattr(websocket, "headers", {}) or {}
        cookies = getattr(websocket, "cookies", {}) or {}
        supplied = headers.get("x-stand-token") or cookies.get("stand_token") or ""
        if getattr(settings, "mutation_auth_policy", "token_required") != "token_required" or not access_token:
            await websocket.close(code=1013, reason="mutation_auth_unconfigured")
            return
        if not secrets.compare_digest(supplied, access_token):
            await websocket.close(code=1008, reason="auth_failure")
            return
        if not lifecycle.admits_users():
            snapshot = lifecycle.snapshot()
            state = str(snapshot.get("state", "DEGRADED"))
            reason = (
                "booting" if state == "BOOTING" else
                "warming_up" if state == "WARMING_UP" else
                "resource_capacity_insufficient"
                if snapshot.get("reason") == "resource_capacity_insufficient"
                else "degraded"
            )
            await websocket.accept()
            await websocket.send_json({
                "type": "admission_error",
                "error": {
                    "code": "runtime_unavailable",
                    "reason": reason,
                    "state": state,
                    "user_available": False,
                    "retry_after_ms": 5000 if reason in {"booting", "warming_up"} else 30000,
                },
            })
            await websocket.close(code=1013, reason=reason)
            return
        await websocket.accept()
        try:
            while True:
                payload=await websocket.receive_json()
                if payload.get("session_id") not in {None,session_id}:
                    await websocket.send_json({"error":{"code":"session_binding","message":"payload session_id does not match WebSocket path"}}); continue
                try: await websocket.send_json(await dispatch(session_id,payload))
                except (TypeError,ValueError) as exc: await websocket.send_json({"error":{"code":"invalid_event","message":str(exc)}})
        except WebSocketDisconnect: pass
    return api
