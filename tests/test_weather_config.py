import os
from pathlib import Path

import pytest

from app.foundation.config import Settings, SettingsError


WEATHER_NAMES = (
    "MISHKA_PROFILE",
    "MISHKA_WEATHER_PROVIDER", "YANDEX_WEATHER_URL", "YANDEX_WEATHER_API_KEY",
    "YANDEX_WEATHER_LATITUDE", "YANDEX_WEATHER_LONGITUDE", "YANDEX_WEATHER_TIMEOUT_S",
    "WEATHER_PROVIDER", "WEATHER_URL", "WEATHER_API_KEY", "WEATHER_LATITUDE",
    "WEATHER_LONGITUDE", "WEATHER_TIMEOUT_S",
)


def clear_weather_env(monkeypatch):
    for name in WEATHER_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
def test_weather_config_defaults_to_profile_appropriate_provider(monkeypatch):
    clear_weather_env(monkeypatch)
    mock = Settings.from_env(validate=False)
    assert mock.weather_configuration().provider == "demo"
    production = Settings(profile="production_candidate")
    assert production.weather_configuration().provider == "yandex"
    assert production.weather_configuration().available is False


@pytest.mark.unit
def test_canonical_weather_names_win_and_sources_are_recorded(monkeypatch):
    clear_weather_env(monkeypatch)
    monkeypatch.setenv("MISHKA_WEATHER_PROVIDER", "yandex")
    monkeypatch.setenv("YANDEX_WEATHER_URL", "https://weather.example.test/forecast")
    monkeypatch.setenv("YANDEX_WEATHER_API_KEY", "canonical-test-key")
    monkeypatch.setenv("YANDEX_WEATHER_LATITUDE", "55.75")
    monkeypatch.setenv("YANDEX_WEATHER_LONGITUDE", "37.61")
    monkeypatch.setenv("YANDEX_WEATHER_TIMEOUT_S", "1.5")
    settings = Settings.from_env(validate=False)
    config = settings.weather_configuration()
    assert config.provider == "yandex"
    assert config.endpoint.endswith("/forecast")
    assert config.latitude == 55.75
    assert config.timeout_s == 1.5
    assert set(config.sources.values()) == {
        "MISHKA_WEATHER_PROVIDER", "YANDEX_WEATHER_URL", "YANDEX_WEATHER_API_KEY",
        "YANDEX_WEATHER_LATITUDE", "YANDEX_WEATHER_LONGITUDE", "YANDEX_WEATHER_TIMEOUT_S",
    }
    assert config.conflicts == ()


@pytest.mark.unit
def test_legacy_weather_names_are_compatibility_only(monkeypatch):
    clear_weather_env(monkeypatch)
    monkeypatch.setenv("WEATHER_PROVIDER", "yandex")
    monkeypatch.setenv("WEATHER_URL", "https://legacy.example.test/weather")
    monkeypatch.setenv("WEATHER_API_KEY", "legacy-test-key")
    monkeypatch.setenv("WEATHER_LATITUDE", "1")
    monkeypatch.setenv("WEATHER_LONGITUDE", "2")
    monkeypatch.setenv("WEATHER_TIMEOUT_S", "3")
    config = Settings.from_env(validate=False).weather_configuration()
    assert config.provider == "yandex"
    assert config.endpoint.endswith("/weather")
    assert config.latitude == 1.0 and config.longitude == 2.0
    assert config.timeout_s == 3.0
    assert set(config.sources.values()) == {
        "WEATHER_PROVIDER", "WEATHER_URL", "WEATHER_API_KEY", "WEATHER_LATITUDE",
        "WEATHER_LONGITUDE", "WEATHER_TIMEOUT_S",
    }
    assert config.conflicts == ()


@pytest.mark.unit
def test_canonical_values_have_precedence_and_conflicts_are_visible(monkeypatch):
    clear_weather_env(monkeypatch)
    monkeypatch.setenv("MISHKA_WEATHER_PROVIDER", "yandex")
    monkeypatch.setenv("WEATHER_PROVIDER", "disabled")
    monkeypatch.setenv("YANDEX_WEATHER_TIMEOUT_S", "2")
    monkeypatch.setenv("WEATHER_TIMEOUT_S", "9")
    config = Settings.from_env(validate=False).weather_configuration()
    assert config.provider == "yandex"
    assert config.timeout_s == 2.0
    assert set(config.conflicts) == {"provider", "timeout_s"}


@pytest.mark.unit
def test_invalid_weather_value_fails_without_echoing_value(monkeypatch):
    clear_weather_env(monkeypatch)
    monkeypatch.setenv("YANDEX_WEATHER_TIMEOUT_S", "not-a-real-secret-value")
    with pytest.raises(SettingsError, match="YANDEX_WEATHER_TIMEOUT_S") as exc_info:
        Settings.from_env(validate=False)
    assert "not-a-real-secret-value" not in str(exc_info.value)


@pytest.mark.unit
def test_disabled_provider_is_explicitly_unavailable(monkeypatch):
    clear_weather_env(monkeypatch)
    monkeypatch.setenv("MISHKA_PROFILE", "production_candidate")
    monkeypatch.setenv("MISHKA_WEATHER_PROVIDER", "disabled")
    config = Settings.from_env(validate=False).weather_configuration()
    assert config.enabled is False
    assert config.available is False


@pytest.mark.unit
def test_removed_duplicate_weather_adapter_has_no_env_reader():
    assert not Path("app/foundation/experience.py").exists()
    source = Path("app/foundation/schedule_weather.py").read_text()
    assert "open-meteo" not in source.lower()
