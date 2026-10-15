from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import os
from typing import Any


class SettingsError(ValueError):
    """Safe, field-scoped configuration error with no secret values."""


@dataclass(frozen=True)
class WeatherConfiguration:
    provider: str
    endpoint: str
    api_key: str
    latitude: float | None
    longitude: float | None
    timeout_s: float
    enabled: bool
    sources: dict[str, str] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.enabled and self.provider == "yandex" and bool(
            self.api_key and self.latitude is not None and self.longitude is not None
        )


@dataclass(frozen=True)
class DeploymentPolicy:
    """Versioned canonical deployment policy for the production profile."""

    version: str = "mishka-deployment/v1"
    profile: str = "production_candidate"
    min_available_ram_gb: float = 12.0
    min_available_vram_gb: float = 8.0
    provenance: str = "canonical project-relative runtime configuration"
    benchmark_status: str = "benchmark_pending"


CANONICAL_POLICY = DeploymentPolicy()
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _raw(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _text(name: str, default: str) -> str:
    value = _raw(name, default)
    return value if value else default


def _bool(name: str, default: bool) -> bool:
    value = _raw(name, "true" if default else "false").lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise SettingsError(f"{name} must be a boolean")


def _int(name: str, default: int) -> int:
    value = _raw(name, str(default))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be an integer") from exc


def _compat_text(canonical: str, legacy: str, default: str) -> str:
    canonical_value = _raw(canonical)
    if canonical_value:
        return canonical_value
    legacy_value = _raw(legacy)
    return legacy_value if legacy_value else default


def _compat_source(canonical: str, legacy: str) -> str:
    if _raw(canonical):
        return canonical
    if _raw(legacy):
        return legacy
    return "default"


def _compat_conflict(canonical: str, legacy: str) -> bool:
    canonical_value = _raw(canonical)
    legacy_value = _raw(legacy)
    return bool(canonical_value and legacy_value and canonical_value != legacy_value)


def _compat_float(canonical: str, legacy: str, default: float) -> float:
    raw = _compat_text(canonical, legacy, str(default))
    try:
        result = float(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{canonical} must be a number") from exc
    if not math.isfinite(result):
        raise SettingsError(f"{canonical} must be finite")
    return result


def _compat_optional_float(canonical: str, legacy: str) -> float | None:
    raw = _compat_text(canonical, legacy, "")
    return None if not raw else _compat_float(canonical, legacy, 0.0)


def _float(name: str, default: float) -> float:
    value = _raw(name, str(default))
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise SettingsError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class Settings:
    """Single typed boundary for canonical and explicit local/mock profiles."""

    profile: str = "mock"
    runtime_mode: str = "exhibition"
    host: str = "0.0.0.0"
    port: int = 8000
    policy_version: str = CANONICAL_POLICY.version
    no_voice_recording: bool = True
    content_root: str = "content"
    profile_db: str = "runtime/data/profiles.sqlite3"
    stand_access_token: str = ""
    allow_query_token: bool = False
    mutation_auth_policy: str = "token_required"
    audio_output_device: str = ""
    audio_input_device: str = ""
    vad_device: str = "cpu"
    hardware_mode: str = "fake"
    # Explicit exhibition operational policies. These are configuration
    # contracts, not alternate runtimes.
    audio_policy: str = "recorded_catalog"
    hid_policy: str = "space_lifecycle"
    lighting_policy: str = "canonical_effects"
    webpanel_enabled: bool = True
    diagnostics_level: str = "standard"
    allow_degraded_hardware: bool = True
    lighting_device: str = ""
    lighting_baudrate: int = 115200
    lighting_timeout_s: float = 1.0
    hid_device: str = ""
    max_input_chars: int = 512
    session_timeout_s: int = 1200
    session_idle_timeout_s: int = 300
    confidence_threshold: float = 0.6

    llm_provider: str = "ollama"
    llm_model: str = "qwen3:14b"
    llm_quantization: str = "Q4_K_M"
    llm_context_length: int = 32_768
    llm_num_ctx: int = 4_096
    llm_temperature: float = 0.2
    llm_max_tokens: int = 96
    llm_keep_alive: str = "-1"
    llm_think: bool = False
    llm_warmup_timeout_s: float = 120.0
    ollama_base_url: str = "http://127.0.0.1:11434"
    vllm_base_url: str = "http://127.0.0.1:8001"

    stt_model: str = "large-v3-turbo"
    silero_model: str = "v5_5_ru"
    silero_language: str = "ru"
    silero_speaker: str = "aidar"
    silero_sample_rate: int = 48_000
    silero_cache_path: str = "runtime/cache/silero/v5_5_ru.pt"
    silero_leading_silence_ms: int = 150
    voice_conversion: str = "rvc_v2"
    fast_audio_ack: bool = False
    fast_audio_ack_cooldown_s: float = 4.0

    rvc_project_root: str = "vendor/rvc"
    rvc_python: str = ".venv/bin/python"
    rvc_model: str = "models/rvc/mishka-rvc.pth"
    rvc_index: str = "models/rvc/mishka-rvc.index"
    rvc_f0_method: str = "rmvpe"
    rvc_filter_radius: float = 3.0
    rvc_index_rate: float = 0.0
    rvc_protect: float = 0.33
    rvc_tmp_dir: str = "runtime/cache/rvc-tmp"
    rvc_f0_up_key: int = 0
    rvc_fallback_mode: str = "passthrough"

    min_available_ram_gb: float = CANONICAL_POLICY.min_available_ram_gb
    min_available_vram_gb: float = CANONICAL_POLICY.min_available_vram_gb

    audio_sample_rate: int = 16_000
    audio_channels: int = 1
    audio_format: str = "float32"
    vad_threshold: float = 0.35
    vad_continue_threshold: float = 0.20
    vad_preroll_ms: int = 320
    vad_silence_ms: int = 200
    min_speech_ms: int = 300
    max_speech_ms: int = 12_000
    max_audio_buffer_seconds: int = 15
    stt_timeout_s: float = 8.0
    llm_timeout_s: float = 10.0
    tts_timeout_s: float = 10.0
    rvc_timeout_s: float = 45.0
    rvc_startup_timeout_s: float = 60.0
    playback_timeout_s: float = 60.0
    max_response_chars: int = 320
    # Semantic TTS/RVC block, not token streaming.  Keep one to three
    # sentences together so Silero and RVC see a stable prosodic context.
    max_tts_chunk_chars: int = 240
    max_tts_first_chunk_chars: int = 240
    tts_queue_size: int = 4
    tts_queue_put_timeout_s: float = 2.0
    stt_confidence_threshold: float = 0.45
    stt_beam_size: int = 3
    max_turn_seconds: float = 60.0
    max_tts_queue_bytes: int = 256 * 1024
    max_tts_chunks_per_generation: int = 32
    max_history_entries: int = 20
    max_history_chars: int = 8192

    validation_provider: str | None = None
    validation_model: str = "qwen3:0.6b"
    validation_timeout_ms: int = 10_000
    validation_keep_alive: str = "-1"
    validation_num_ctx: int = 1024
    validation_fail_closed: bool = True

    watchdog_interval_s: float = 5.0
    watchdog_stale_after_s: float = 30.0
    watchdog_cooldown_s: float = 30.0
    audio_cleanup_timeout_s: float = 1.0
    observability_latency_limit: int = 500
    observability_event_limit: int = 500
    observability_watchdog_limit: int = 200
    # Cold GPU/model initialization can be much slower than steady-state;
    # keep startup fail-closed but leave enough room for the first load.
    e2e_warmup_timeout_s: float = 180.0
    e2e_stage_timeout_s: float = 120.0
    warmup_pcm_path: str = "assets/reference/mishka_24k_mono.wav"

    # Product identity and execution backend are separate contracts.
    scenario_id: str = "canonical_product"
    graph_backend: str = "langgraph"
    # Temporary compatibility alias; never use it to select the product flow.
    scenario_engine: str | None = None
    langgraph_strict: bool | None = None
    weather_provider: str | None = None
    weather_endpoint: str = "https://api.weather.yandex.ru/v2/forecast"
    yandex_weather_api_key: str = ""
    yandex_weather_latitude: float | None = None
    yandex_weather_longitude: float | None = None
    yandex_weather_timeout_s: float = 2.0

    def __post_init__(self) -> None:
        profile = str(self.profile).strip().lower()
        if self.validation_provider is None:
            object.__setattr__(self, "validation_provider", "mock" if profile == "mock" else "ollama")
        object.__setattr__(self, "scenario_id", str(self.scenario_id).strip().lower())
        object.__setattr__(self, "graph_backend", str(self.graph_backend).strip().lower())
        # Compatibility projection for legacy callers. "langgraph" here is
        # accepted only as an old alias for the LangGraph backend.
        if self.scenario_engine != "canonical":
            object.__setattr__(self, "scenario_engine", "canonical")
        if self.langgraph_strict is not True:
            object.__setattr__(self, "langgraph_strict", True)
        if self.weather_provider is None:
            object.__setattr__(self, "weather_provider", "demo" if profile == "mock" else "yandex")

    def validate(self) -> "Settings":
        profiles = {"mock", "local_cpu", "local_gpu", "production", "production_candidate", "exhibition_candidate", "production_qwen_rvc"}
        if self.profile not in profiles:
            raise SettingsError("MISHKA_PROFILE is unsupported")
        if self.runtime_mode not in {"exhibition", "clinical"}:
            raise SettingsError("MISHKA_RUNTIME_MODE must be exhibition or clinical")
        if self.audio_policy not in {"recorded_catalog"}:
            raise SettingsError("MISHKA_AUDIO_POLICY is unsupported")
        if self.hid_policy not in {"space_lifecycle"}:
            raise SettingsError("MISHKA_HID_POLICY is unsupported")
        if self.lighting_policy not in {"canonical_effects"}:
            raise SettingsError("MISHKA_LIGHTING_POLICY is unsupported")
        if self.diagnostics_level not in {"quiet", "standard", "verbose"}:
            raise SettingsError("MISHKA_DIAGNOSTICS_LEVEL is unsupported")
        if self.mutation_auth_policy != "token_required":
            raise SettingsError("MISHKA_MUTATION_AUTH_POLICY is unsupported")
        if self.policy_version != CANONICAL_POLICY.version:
            raise SettingsError("MISHKA_POLICY_VERSION is unsupported")
        if not self.no_voice_recording:
            raise SettingsError("NO_VOICE_RECORDING must remain true")
        if self.port != 8000 and self.profile not in {"mock", "local_cpu", "local_gpu"}:
            raise SettingsError("production profile requires canonical port 8000")
        if self.llm_provider not in {"mock", "ollama", "vllm"}:
            raise SettingsError("LLM_PROVIDER is unsupported")
        if self.validation_provider not in {"mock", "ollama"}:
            raise SettingsError("VALIDATION_PROVIDER is unsupported")
        if self.weather_provider not in {"demo", "yandex", "disabled"}:
            raise SettingsError("MISHKA_WEATHER_PROVIDER is unsupported")
        if not self.weather_endpoint.strip():
            raise SettingsError("YANDEX_WEATHER_URL must not be empty")
        if self.profile in {"production", "production_candidate", "exhibition_candidate", "production_qwen_rvc"} and self.weather_provider == "demo":
            raise SettingsError("production profile cannot use demo weather")
        if self.voice_conversion not in {"rvc_v2", "off", "disabled"}:
            raise SettingsError("unsupported MISHKA_VOICE_CONVERSION")
        if self.rvc_fallback_mode not in {"passthrough", "controlled_passthrough"}:
            raise SettingsError("RVC_FALLBACK_MODE must be passthrough")
        if not -24 <= self.rvc_f0_up_key <= 24:
            raise SettingsError("RVC_F0_UP_KEY is out of range")
        if self.rvc_f0_method != "rmvpe":
            raise SettingsError("RVC_F0_METHOD must match the accepted rmvpe release")
        if self.rvc_filter_radius < 0:
            raise SettingsError("RVC_FILTER_RADIUS must be non-negative")
        if not 0 <= self.rvc_index_rate <= 1 or not 0 <= self.rvc_protect <= 0.5:
            raise SettingsError("RVC index/protect settings are invalid")
        if self.voice_conversion == "rvc_v2" and not all((self.rvc_project_root.strip(), self.rvc_python.strip(), self.rvc_model.strip(), self.rvc_index.strip(), self.rvc_tmp_dir.strip())):
            raise SettingsError("RVC v2 requires project, python, model, index and tmp paths")
        production = self.profile in {"production", "production_candidate", "exhibition_candidate", "production_qwen_rvc"}
        if production and self.allow_query_token:
            raise SettingsError("MISHKA_ALLOW_QUERY_TOKEN is forbidden for production profiles")
        if production and self.llm_provider == "mock":
            raise SettingsError("production profile cannot use mock LLM")
        if production and self.validation_provider != "ollama":
            raise SettingsError("production profile requires Ollama validation")
        if self.scenario_id != "canonical_product":
            raise SettingsError("MISHKA_SCENARIO_ID must be canonical_product")
        if self.graph_backend != "langgraph":
            raise SettingsError("MISHKA_GRAPH_BACKEND must be langgraph")
        if self.scenario_engine != "canonical":
            raise SettingsError("MISHKA_SCENARIO_ENGINE compatibility alias is invalid")
        if not self.langgraph_strict:
            raise SettingsError("MISHKA_LANGGRAPH_STRICT must remain true")
        if not self.llm_model.strip():
            raise SettingsError("LLM_MODEL must not be empty")
        if not 0 <= self.llm_temperature <= 2:
            raise SettingsError("LLM_TEMPERATURE must be 0..2")
        if self.llm_num_ctx <= 0 or self.llm_context_length <= 0 or self.llm_num_ctx > self.llm_context_length:
            raise SettingsError("LLM_NUM_CTX must be positive and <= LLM_CONTEXT_LENGTH")
        if self.llm_max_tokens <= 0 or self.llm_warmup_timeout_s <= 0:
            raise SettingsError("LLM limits must be positive")
        if self.session_timeout_s <= 0 or self.session_idle_timeout_s <= 0 or self.session_idle_timeout_s > self.session_timeout_s:
            raise SettingsError("session timeouts are invalid")
        if not 0 < self.confidence_threshold <= 1:
            raise SettingsError("INTENT_CONFIDENCE_THRESHOLD must be 0..1")
        if not 0 < self.min_available_ram_gb <= 1024 or not 0 < self.min_available_vram_gb <= 1024:
            raise SettingsError("resource thresholds must be positive and bounded")
        if self.audio_format != "float32" or self.audio_channels != 1 or self.audio_sample_rate <= 0:
            raise SettingsError("audio contract must be mono float32 with positive sample rate")
        if not 0 < self.vad_continue_threshold <= self.vad_threshold <= 1:
            raise SettingsError("VAD thresholds are invalid")
        if not 200 <= self.vad_silence_ms <= 250 or self.min_speech_ms >= self.max_speech_ms:
            raise SettingsError("speech timing limits are invalid")
        numeric = (self.stt_timeout_s, self.llm_timeout_s, self.tts_timeout_s, self.rvc_timeout_s, self.rvc_startup_timeout_s,
                   self.playback_timeout_s, self.tts_queue_put_timeout_s, self.max_turn_seconds,
                   self.audio_cleanup_timeout_s, self.watchdog_interval_s, self.watchdog_stale_after_s,
                   self.watchdog_cooldown_s, self.e2e_warmup_timeout_s, self.e2e_stage_timeout_s,
                   self.yandex_weather_timeout_s)
        if any(value <= 0 for value in numeric):
            raise SettingsError("duration settings must be positive")
        integers = (self.max_input_chars, self.max_response_chars, self.max_tts_chunk_chars,
                    self.max_tts_first_chunk_chars, self.tts_queue_size, self.max_tts_queue_bytes,
                    self.max_tts_chunks_per_generation, self.max_history_entries, self.max_history_chars,
                    self.validation_timeout_ms, self.validation_num_ctx, self.stt_beam_size,
                    self.observability_latency_limit, self.observability_event_limit, self.observability_watchdog_limit)
        if any(value <= 0 for value in integers):
            raise SettingsError("queue/history/observability limits must be positive")
        if not 0 <= self.stt_confidence_threshold <= 1:
            raise SettingsError("STT threshold is invalid")
        for name, value, lower, upper in (("YANDEX_WEATHER_LATITUDE", self.yandex_weather_latitude, -90, 90), ("YANDEX_WEATHER_LONGITUDE", self.yandex_weather_longitude, -180, 180)):
            if value is not None and not lower <= value <= upper:
                raise SettingsError(f"{name} is out of range")
        return self

    def weather_configuration(self) -> WeatherConfiguration:
        pairs = (("provider", "MISHKA_WEATHER_PROVIDER", "WEATHER_PROVIDER"),
                 ("endpoint", "YANDEX_WEATHER_URL", "WEATHER_URL"),
                 ("api_key", "YANDEX_WEATHER_API_KEY", "WEATHER_API_KEY"),
                 ("latitude", "YANDEX_WEATHER_LATITUDE", "WEATHER_LATITUDE"),
                 ("longitude", "YANDEX_WEATHER_LONGITUDE", "WEATHER_LONGITUDE"),
                 ("timeout_s", "YANDEX_WEATHER_TIMEOUT_S", "WEATHER_TIMEOUT_S"))
        sources = {name: _compat_source(canonical, legacy) for name, canonical, legacy in pairs}
        conflicts = tuple(name for name, canonical, legacy in pairs if _compat_conflict(canonical, legacy))
        return WeatherConfiguration(
            provider=self.weather_provider or ("demo" if self.profile == "mock" else "yandex"),
            endpoint=self.weather_endpoint, api_key=self.yandex_weather_api_key,
            latitude=self.yandex_weather_latitude, longitude=self.yandex_weather_longitude,
            timeout_s=self.yandex_weather_timeout_s,
            enabled=(self.weather_provider not in {"disabled", "off"}),
            sources=sources, conflicts=conflicts,
        )

    def redacted_snapshot(self) -> dict[str, Any]:
        secret = {"stand_access_token", "profile_db", "yandex_weather_api_key"}
        data = asdict(self)
        return {key: value for key, value in data.items() if key not in secret}

    @property
    def operator_token_present(self) -> bool:
        return bool(self.stand_access_token.strip())

    @property
    def mutation_policy_ready(self) -> bool:
        return self.mutation_auth_policy == "token_required" and self.operator_token_present

    def policy_snapshot(self) -> dict[str, Any]:
        return {"version": self.policy_version, "profile": self.profile,
                "min_available_ram_gb": self.min_available_ram_gb,
                "min_available_vram_gb": self.min_available_vram_gb,
                "provenance": CANONICAL_POLICY.provenance,
                "benchmark_status": CANONICAL_POLICY.benchmark_status}

    @classmethod
    def from_env(cls, *, validate: bool = True) -> "Settings":
        profile = _text("MISHKA_PROFILE", "mock").lower()
        validation_default = "mock" if profile == "mock" else "ollama"
        legacy_scenario_engine = _text("MISHKA_SCENARIO_ENGINE", "").lower()
        if legacy_scenario_engine and legacy_scenario_engine not in {"canonical", "langgraph"}:
            raise SettingsError("MISHKA_SCENARIO_ENGINE compatibility alias must be canonical or langgraph")
        scenario_id = _text("MISHKA_SCENARIO_ID", "canonical_product").lower()
        graph_backend = _text("MISHKA_GRAPH_BACKEND", "langgraph").lower()
        settings = cls(
            profile=profile, runtime_mode=_text("MISHKA_RUNTIME_MODE", "exhibition").lower(), host=_text("MISHKA_HOST", "0.0.0.0"), port=_int("MISHKA_PORT", 8000),
            policy_version=_text("MISHKA_POLICY_VERSION", CANONICAL_POLICY.version),
            no_voice_recording=_bool("NO_VOICE_RECORDING", True), content_root=_text("MISHKA_CONTENT_ROOT", "content"),
            profile_db=_text("MISHKA_PROFILE_DB", "runtime/data/profiles.sqlite3"), stand_access_token=_text("STAND_ACCESS_TOKEN", ""), allow_query_token=_bool("MISHKA_ALLOW_QUERY_TOKEN", False), mutation_auth_policy=_text("MISHKA_MUTATION_AUTH_POLICY", "token_required").lower(),
            audio_output_device=_text("AUDIO_OUTPUT_DEVICE", ""), audio_input_device=_text("AUDIO_INPUT_DEVICE", ""), vad_device=_text("MISHKA_VAD_DEVICE", "cpu").lower(),
            max_input_chars=_int("MAX_INPUT_CHARS", 512), session_timeout_s=_int("MISHKA_MAX_SESSION_SECONDS", 1200),
            session_idle_timeout_s=_int("MISHKA_SESSION_IDLE_TTL_SECONDS", 300), confidence_threshold=_float("INTENT_CONFIDENCE_THRESHOLD", 0.6),
            llm_provider=_text("LLM_PROVIDER", "ollama"), llm_model=_text("LLM_MODEL", "qwen3:14b"),
            llm_quantization=_text("LLM_QUANTIZATION", "Q4_K_M"), llm_context_length=_int("LLM_CONTEXT_LENGTH", 32768),
            llm_num_ctx=_int("LLM_NUM_CTX", 4096), llm_temperature=_float("LLM_TEMPERATURE", 0.2),
            llm_max_tokens=_int("LLM_MAX_TOKENS", 96), llm_keep_alive=_text("LLM_KEEP_ALIVE", "-1"),
            llm_think=_bool("LLM_THINK", False), llm_warmup_timeout_s=_float("LLM_WARMUP_TIMEOUT_SECONDS", 120),
            ollama_base_url=_text("OLLAMA_BASE_URL", "http://127.0.0.1:11434"), vllm_base_url=_text("VLLM_BASE_URL", "http://127.0.0.1:8001"),
            stt_model=_text("WHISPER_MODEL", "large-v3-turbo"),
            silero_model=_text("SILERO_MODEL", "v5_5_ru"), silero_language=_text("SILERO_LANGUAGE", "ru"), silero_speaker=_text("SILERO_SPEAKER", "aidar").lower(), silero_sample_rate=_int("SILERO_SAMPLE_RATE", 48000), silero_cache_path=_text("SILERO_CACHE_PATH", "runtime/cache/silero/v5_5_ru.pt"), silero_leading_silence_ms=_int("SILERO_LEADING_SILENCE_MS", 150),
            voice_conversion=_text("MISHKA_VOICE_CONVERSION", "rvc_v2").lower(), fast_audio_ack=_bool("FAST_AUDIO_ACK", False), fast_audio_ack_cooldown_s=_float("FAST_AUDIO_ACK_COOLDOWN_S", 4),
            rvc_project_root=_text("RVC_PROJECT_ROOT", "vendor/rvc"), rvc_python=_text("RVC_PYTHON", ".venv/bin/python"), rvc_model=_text("RVC_MODEL_PATH", "models/rvc/mishka-rvc.pth"), rvc_index=_text("RVC_INDEX_PATH", "models/rvc/mishka-rvc.index"), rvc_f0_method=_text("RVC_F0_METHOD", "rmvpe"), rvc_filter_radius=_float("RVC_FILTER_RADIUS", 3), rvc_f0_up_key=_int("RVC_F0_UP_KEY", 0), rvc_index_rate=_float("RVC_INDEX_RATE", 0.0), rvc_protect=_float("RVC_PROTECT", .33), rvc_tmp_dir=_text("RVC_TMP_DIR", "runtime/cache/rvc-tmp"), rvc_startup_timeout_s=_float("RVC_STARTUP_TIMEOUT_SECONDS", 60), rvc_fallback_mode=_text("RVC_FALLBACK_MODE", "passthrough").lower(),
            min_available_ram_gb=_float("MIN_AVAILABLE_RAM_GB", CANONICAL_POLICY.min_available_ram_gb), min_available_vram_gb=_float("MIN_AVAILABLE_VRAM_GB", CANONICAL_POLICY.min_available_vram_gb),
            audio_sample_rate=_int("AUDIO_SAMPLE_RATE", 16000), audio_channels=_int("AUDIO_CHANNELS", 1), audio_format=_text("AUDIO_FORMAT", "float32"), vad_threshold=_float("VAD_THRESHOLD", .35), vad_continue_threshold=_float("VAD_CONTINUE_THRESHOLD", .20), vad_preroll_ms=_int("VAD_PREROLL_MS", 320), vad_silence_ms=_int("VAD_SILENCE_MS", 200), min_speech_ms=_int("MIN_SPEECH_MS", 300), max_speech_ms=_int("MAX_SPEECH_MS", 12000), max_audio_buffer_seconds=_int("MAX_AUDIO_BUFFER_SECONDS", 15), stt_timeout_s=_float("STT_TIMEOUT_SECONDS", 8), llm_timeout_s=_float("LLM_TIMEOUT_SECONDS", 10), tts_timeout_s=_float("TTS_TIMEOUT_SECONDS", 10), rvc_timeout_s=_float("RVC_TIMEOUT_SECONDS", 45), playback_timeout_s=_float("PLAYBACK_TIMEOUT_SECONDS", 60), max_response_chars=_int("MAX_RESPONSE_CHARS", 320), max_tts_chunk_chars=_int("MAX_TTS_CHUNK_CHARS", 240), max_tts_first_chunk_chars=_int("MAX_TTS_FIRST_CHUNK_CHARS", 240), tts_queue_size=_int("TTS_QUEUE_SIZE", 4), tts_queue_put_timeout_s=_float("TTS_QUEUE_PUT_TIMEOUT_SECONDS", 2), stt_confidence_threshold=_float("STT_CONFIDENCE_THRESHOLD", .45), stt_beam_size=_int("STT_BEAM_SIZE", 3), max_turn_seconds=_float("MISHKA_MAX_TURN_SECONDS", 60), max_tts_queue_bytes=_int("MAX_TTS_QUEUE_BYTES", 256*1024), max_tts_chunks_per_generation=_int("MAX_TTS_CHUNKS_PER_GENERATION", 32), max_history_entries=_int("MAX_HISTORY_ENTRIES", 20), max_history_chars=_int("MAX_HISTORY_CHARS", 8192),
            validation_provider=_text("VALIDATION_PROVIDER", validation_default).lower(), validation_model=_text("VALIDATION_MODEL", "qwen3:0.6b"), validation_timeout_ms=_int("VALIDATION_TIMEOUT_MS", 120 if profile == "mock" else 10000), validation_keep_alive=_text("VALIDATION_KEEP_ALIVE", "-1"), validation_num_ctx=_int("VALIDATION_NUM_CTX", 1024), validation_fail_closed=_bool("VALIDATION_FAIL_CLOSED", True),
            watchdog_interval_s=_float("MISHKA_WATCHDOG_INTERVAL_SECONDS", 5), watchdog_stale_after_s=_float("MISHKA_WATCHDOG_STALE_AFTER_SECONDS", 30), watchdog_cooldown_s=_float("MISHKA_WATCHDOG_COOLDOWN_SECONDS", 30), audio_cleanup_timeout_s=_float("MISHKA_AUDIO_CLEANUP_TIMEOUT_SECONDS", 1), observability_latency_limit=_int("OBSERVABILITY_LATENCY_LIMIT", 500), observability_event_limit=_int("OBSERVABILITY_EVENT_LIMIT", 500), observability_watchdog_limit=_int("OBSERVABILITY_WATCHDOG_LIMIT", 200), e2e_warmup_timeout_s=_float("MISHKA_WARMUP_E2E_TIMEOUT_S", 180), e2e_stage_timeout_s=_float("MISHKA_WARMUP_E2E_STAGE_TIMEOUT_S", 120), warmup_pcm_path=_text("MISHKA_WARMUP_PCM_PATH", "assets/reference/mishka_24k_mono.wav"), scenario_id=scenario_id, graph_backend=graph_backend, scenario_engine="canonical", langgraph_strict=_bool("MISHKA_LANGGRAPH_STRICT", True),
            weather_provider=_compat_text("MISHKA_WEATHER_PROVIDER", "WEATHER_PROVIDER", "demo" if profile == "mock" else "yandex").lower(),
            weather_endpoint=_compat_text("YANDEX_WEATHER_URL", "WEATHER_URL", "https://api.weather.yandex.ru/v2/forecast"),
            yandex_weather_api_key=_compat_text("YANDEX_WEATHER_API_KEY", "WEATHER_API_KEY", ""),
            yandex_weather_latitude=_compat_optional_float("YANDEX_WEATHER_LATITUDE", "WEATHER_LATITUDE"),
            yandex_weather_longitude=_compat_optional_float("YANDEX_WEATHER_LONGITUDE", "WEATHER_LONGITUDE"),
            yandex_weather_timeout_s=_compat_float("YANDEX_WEATHER_TIMEOUT_S", "WEATHER_TIMEOUT_S", 2),
        )
        object.__setattr__(settings, "hardware_mode", _text("MISHKA_HARDWARE_MODE", "fake" if profile in {"mock", "local_cpu", "local_gpu"} else "physical").lower())
        object.__setattr__(settings, "audio_policy", _text("MISHKA_AUDIO_POLICY", "recorded_catalog").lower())
        object.__setattr__(settings, "hid_policy", _text("MISHKA_HID_POLICY", "space_lifecycle").lower())
        object.__setattr__(settings, "lighting_policy", _text("MISHKA_LIGHTING_POLICY", "canonical_effects").lower())
        object.__setattr__(settings, "webpanel_enabled", _bool("MISHKA_WEBPANEL_ENABLED", True))
        object.__setattr__(settings, "diagnostics_level", _text("MISHKA_DIAGNOSTICS_LEVEL", "standard").lower())
        object.__setattr__(settings, "allow_degraded_hardware", _bool("MISHKA_ALLOW_DEGRADED_HARDWARE", True))
        object.__setattr__(settings, "lighting_device", _text("MISHKA_LIGHTING_DEVICE", ""))
        object.__setattr__(settings, "lighting_baudrate", _int("MISHKA_LIGHTING_BAUDRATE", 115200))
        object.__setattr__(settings, "lighting_timeout_s", _float("MISHKA_LIGHTING_TIMEOUT_S", 1.0))
        object.__setattr__(settings, "hid_device", _text("MISHKA_HID_DEVICE", ""))
        return settings.validate() if validate else settings

    def operational_snapshot(self) -> dict[str, Any]:
        """Return the non-secret configuration contract for health/diagnostics."""
        if os.getenv("MISHKA_CONFIG_SOURCE"):
            config_source = os.getenv("MISHKA_CONFIG_SOURCE")
        elif os.getenv("MISHKA_ENV_FILE"):
            config_source = "declared-env-file"
        else:
            config_source = "process-environment"
        return {
            "mode": self.runtime_mode,
            "scenario_id": self.scenario_id,
            "graph_backend": self.graph_backend,
            # Deprecated projection retained for old health consumers.
            "graph": self.graph_backend,
            "scenario_engine": self.scenario_engine,
            "content_manifest": "content/audio/voice_manifest.json",
            "audio_policy": self.audio_policy,
            "audio_output_device": self.audio_output_device,
            "audio_input_device": self.audio_input_device,
            "hid_policy": self.hid_policy,
            "lighting_policy": self.lighting_policy,
            "hardware_mode": self.hardware_mode,
            "webpanel_enabled": self.webpanel_enabled,
            "diagnostics_level": self.diagnostics_level,
            "allow_degraded_hardware": self.allow_degraded_hardware,
            "mutation_auth_policy": self.mutation_auth_policy,
            "operator_token_present": self.operator_token_present,
            "mutation_policy_ready": self.mutation_policy_ready,
            "no_voice_recording": self.no_voice_recording,
            "provenance": {
                "config_source": config_source,
                "entrypoint": os.path.basename(__import__("sys").argv[0]) or None,
                "python": ".".join(str(part) for part in __import__("sys").version_info[:3]),
            },
        }


# Explicit compatibility boundary for older direct-construction call sites.
# Production code still receives one validated Settings object; these helpers
# keep legacy tests/demo adapters from reimplementing env parsing themselves.
_COMPAT_ENV_FIELDS = {
    "MISHKA_PROFILE": "profile",
    "MISHKA_PROFILE_DB": "profile_db",
    "STAND_ACCESS_TOKEN": "stand_access_token",
    "MISHKA_VOICE_CONVERSION": "voice_conversion",
    "MISHKA_SCENARIO_ID": "scenario_id",
    "MISHKA_GRAPH_BACKEND": "graph_backend",
    "MISHKA_SCENARIO_ENGINE": "scenario_engine",
    "MISHKA_LANGGRAPH_STRICT": "langgraph_strict",
    "MISHKA_WARMUP_PCM_PATH": "warmup_pcm_path",
    "MISHKA_WARMUP_E2E_TIMEOUT_S": "e2e_warmup_timeout_s",
    "MISHKA_WARMUP_E2E_STAGE_TIMEOUT_S": "e2e_stage_timeout_s",
    "MISHKA_CONTENT_ROOT": "content_root",
    "MISHKA_AUDIO_OUTPUT_DEVICE": "audio_output_device",
    "MISHKA_AUDIO_INPUT_DEVICE": "audio_input_device",
}


def settings_with_compat_env(settings: Settings, env_names: tuple[str, ...] | list[str]) -> Settings:
    """Apply only explicitly requested legacy env overrides via the parser."""
    names = tuple(name for name in env_names if name in os.environ and name in _COMPAT_ENV_FIELDS)
    if not names:
        return settings
    parsed = Settings.from_env()
    values = asdict(settings)
    for env_name in names:
        values[_COMPAT_ENV_FIELDS[env_name]] = getattr(parsed, _COMPAT_ENV_FIELDS[env_name])
    return Settings(**values).validate()


def settings_from_compat_env(default_profile: str = "mock") -> Settings:
    """Use env only when a legacy caller explicitly supplied a config key."""
    if any(name in os.environ for name in _COMPAT_ENV_FIELDS):
        try:
            return Settings.from_env()
        except SettingsError as exc:
            # Direct ScenarioApplication callers historically exercised the
            # strict transition rejection path with an intentionally disabled
            # graph. Canonical production construction still uses validated
            # Settings.from_env() and fails closed before serving traffic.
            if "LangGraph scenario engine" in str(exc) and "MISHKA_SCENARIO_ENGINE" in os.environ:
                return Settings.from_env(validate=False)
            raise
    return Settings(profile=default_profile).validate()


def compatibility_env_value(name: str, default: str) -> str:
    """Read a legacy value only through the canonical config boundary."""
    if name not in _COMPAT_ENV_FIELDS:
        raise SettingsError(f"unsupported compatibility environment key: {name}")
    return _text(name, default)
