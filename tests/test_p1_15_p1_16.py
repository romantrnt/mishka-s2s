import json
import os
import subprocess
import sys

import pytest

from app.foundation.config import CANONICAL_POLICY, Settings, SettingsError
from app.foundation.s2s import S2SConfig


def test_canonical_policy_is_versioned_and_runpod_aligned():
    settings = Settings(profile="production_candidate", llm_provider="ollama").validate()
    assert settings.policy_version == "mishka-deployment/v1"
    assert settings.min_available_ram_gb == 12.0
    assert settings.min_available_vram_gb == 8.0
    assert settings.policy_snapshot()["benchmark_status"] == "benchmark_pending"
    assert CANONICAL_POLICY.provenance


def test_settings_and_s2s_config_share_one_boundary(monkeypatch):
    monkeypatch.setenv("MISHKA_PROFILE", "production_candidate")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MIN_AVAILABLE_RAM_GB", "12")
    monkeypatch.setenv("MIN_AVAILABLE_VRAM_GB", "8")
    settings = Settings.from_env()
    config = S2SConfig.from_settings(settings)
    assert config.sample_rate == settings.audio_sample_rate
    assert config.queue_size == settings.tts_queue_size
    assert config.max_history_chars == settings.max_history_chars
    assert config.vad_silence_ms == settings.vad_silence_ms


@pytest.mark.parametrize(("name", "value"), [
    ("MIN_AVAILABLE_RAM_GB", "-1"),
    ("MIN_AVAILABLE_VRAM_GB", "nan"),
    ("VALIDATION_TIMEOUT_MS", "bad"),
    ("MISHKA_LANGGRAPH_STRICT", "maybe"),
])
def test_malformed_or_impossible_values_fail_closed(monkeypatch, name, value):
    monkeypatch.setenv("MISHKA_PROFILE", "production_candidate")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError):
        Settings.from_env()


def test_local_override_is_explicit_and_does_not_change_policy_default(monkeypatch):
    monkeypatch.setenv("MISHKA_PROFILE", "local_cpu")
    monkeypatch.setenv("MIN_AVAILABLE_RAM_GB", "1")
    monkeypatch.setenv("MIN_AVAILABLE_VRAM_GB", "1")
    settings = Settings.from_env()
    assert settings.profile == "local_cpu"
    assert settings.min_available_ram_gb == 1
    assert settings.policy_version == CANONICAL_POLICY.version
    assert Settings(profile="production_candidate", llm_provider="ollama").min_available_ram_gb == 12


def test_redacted_snapshot_never_contains_secret_values():
    settings = Settings(profile="production_candidate", llm_provider="ollama", stand_access_token="TOKEN", profile_db="/secret/db", yandex_weather_api_key="KEY")
    snapshot = settings.redacted_snapshot()
    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert "TOKEN" not in encoded and "KEY" not in encoded and "/secret/db" not in encoded
    json.dumps(snapshot, allow_nan=False)


def test_production_rejects_mock_validation_and_legacy_engine():
    with pytest.raises(SettingsError):
        Settings(profile="production_candidate", llm_provider="ollama", validation_provider="mock").validate()
    with pytest.raises(SettingsError):
        Settings(profile="production_candidate", llm_provider="ollama", scenario_engine="legacy").validate()


def test_repeated_settings_are_deterministic(monkeypatch):
    monkeypatch.setenv("MISHKA_PROFILE", "production_candidate")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    first = Settings.from_env().redacted_snapshot()
    second = Settings.from_env().redacted_snapshot()
    assert first == second


def test_env_manifests_use_same_canonical_thresholds():
    example = open(".env.example", encoding="utf-8").read()
    runpod = open(".env.runpod", encoding="utf-8").read()
    for text in (example, runpod):
        assert "MISHKA_POLICY_VERSION=mishka-deployment/v1" in text
        assert "MIN_AVAILABLE_RAM_GB=12" in text
    # The generic manifest keeps the pre-RVC 8 GB reserve. The canonical pod
    # has a measured 7.43 GB free after Qwen + Whisper + Silero + resident RVC;
    # its explicit deployment reserve is therefore 6 GB with 1.43 GB headroom.
    assert "MIN_AVAILABLE_VRAM_GB=8" in example
    assert "MISHKA_VOICE_CONVERSION=rvc_v2" in runpod
    assert "MIN_AVAILABLE_VRAM_GB=6" in runpod
