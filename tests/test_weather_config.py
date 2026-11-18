import os
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time
import traceback

import httpx
import pytest

from app.foundation.config import Settings, SettingsError
from app.foundation.schedule_weather import ProductionScheduleWeatherService, ScheduleWeatherProviderError, ScheduleWeatherSchemaError, YandexWeatherProvider


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


@pytest.mark.parametrize("legacy", [False, True])
def test_remote_launcher_binds_only_weather_configuration(monkeypatch, legacy):
    from scripts.run_remote_stand import _weather_settings
    clear_weather_env(monkeypatch)
    names = (("WEATHER_PROVIDER", "WEATHER_URL", "WEATHER_API_KEY", "WEATHER_LATITUDE", "WEATHER_LONGITUDE", "WEATHER_TIMEOUT_S")
             if legacy else ("MISHKA_WEATHER_PROVIDER", "YANDEX_WEATHER_URL", "YANDEX_WEATHER_API_KEY", "YANDEX_WEATHER_LATITUDE", "YANDEX_WEATHER_LONGITUDE", "YANDEX_WEATHER_TIMEOUT_S"))
    for name, value in zip(names, ("yandex", "https://weather.example.test/forecast", "synthetic-weather-key", "55.75", "37.61", "1.5")):
        monkeypatch.setenv(name, value)
    # Explicit launcher voice/private settings must not be replaced by broad env loading.
    monkeypatch.setenv("MISHKA_VOICE_CONVERSION", "invalid-unrelated-setting")
    settings = Settings(profile="local_gpu", voice_conversion="off", **_weather_settings("local_gpu")).validate()
    config = settings.weather_configuration()
    assert config.available and config.provider == "yandex"
    assert config.endpoint == "https://weather.example.test/forecast"
    assert (config.latitude, config.longitude, config.timeout_s) == (55.75, 37.61, 1.5)
    assert config.api_key == "synthetic-weather-key"
    assert settings.voice_conversion == "off"
    assert set(config.sources.values()) == set(names)
    assert "synthetic-weather-key" not in str(settings.redacted_snapshot())


@pytest.mark.parametrize("profile,provider", [("mock", "demo"), ("local_gpu", "yandex")])
def test_remote_launcher_weather_defaults_and_missing_credentials(monkeypatch, profile, provider):
    from scripts.run_remote_stand import _weather_settings
    clear_weather_env(monkeypatch)
    config = Settings(profile=profile, **_weather_settings(profile)).validate().weather_configuration()
    assert config.provider == provider
    assert config.available is False


def test_remote_launcher_weather_conflicts_and_invalid_values_are_safe(monkeypatch):
    from scripts.run_remote_stand import _weather_settings
    clear_weather_env(monkeypatch)
    monkeypatch.setenv("YANDEX_WEATHER_API_KEY", "synthetic-canonical-key")
    monkeypatch.setenv("WEATHER_API_KEY", "synthetic-legacy-key")
    config = Settings(profile="local_gpu", **_weather_settings("local_gpu")).validate().weather_configuration()
    assert config.api_key == "synthetic-canonical-key"
    assert config.conflicts == ("api_key",)
    assert not config.available
    monkeypatch.setenv("YANDEX_WEATHER_LATITUDE", "synthetic-secret-invalid-number")
    with pytest.raises(SettingsError) as caught:
        _weather_settings("local_gpu")
    assert "YANDEX_WEATHER_LATITUDE" in str(caught.value)
    assert "synthetic-secret" not in str(caught.value)


def weather_payload(*, observation=None):
    return {"now_dt": datetime.now(timezone.utc).isoformat(), "info": {}, "forecasts": [],
            "fact": {"condition": "rain", "temp": 3, "obs_time": time.time() if observation is None else observation}}


def weather_http(monkeypatch, handler):
    client = httpx.AsyncClient
    def streamed(request):
        response = handler(request)
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=httpx.ByteStream(response.content))
    monkeypatch.setattr("app.foundation.schedule_weather.httpx.AsyncClient",
                        lambda **options: client(transport=httpx.MockTransport(streamed), **options))


def test_yandex_http_current_observation_and_closed_data_envelope(monkeypatch):
    observation = int(time.time()) - 300
    payload = weather_payload(observation=observation)
    payload["private_untrusted_extra"] = "must-not-pass"
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.path == "/v2/forecast"
        assert request.url.params["hours"] == "false"
        assert request.url.params["lang"] == "ru_RU"
        assert request.headers["X-Yandex-Weather-Key"] == "synthetic-weather-key"
        return httpx.Response(200, json=payload)
    weather_http(monkeypatch, handle)
    value = asyncio.run(YandexWeatherProvider("synthetic-weather-key", 55., 37.).current({}, ""))
    assert len(calls) == 1
    assert value == {"summary": "дождь, +3 °C", "temperature_c": 3, "condition": "rain",
                     "source": "yandex_weather", "observed_at": datetime.fromtimestamp(observation, timezone.utc).isoformat()}
    assert "synthetic-weather-key" not in str(value)


@pytest.mark.parametrize("age", [7201, -301])
def test_yandex_http_rejects_stale_or_future_measurement_even_with_current_server_time(monkeypatch, age):
    weather_http(monkeypatch, lambda request: httpx.Response(200, json=weather_payload(observation=time.time() - age)))
    with pytest.raises(ScheduleWeatherProviderError, match="yandex_weather_observation_not_current"):
        asyncio.run(YandexWeatherProvider("synthetic-key", 55., 37.).current({}, ""))


@pytest.mark.parametrize("field,value", [("obs_time", None), ("obs_time", True), ("obs_time", "invalid"),
                                         ("obs_time", float("inf")), ("temp", True), ("condition", [])])
def test_yandex_http_rejects_malformed_fact_without_echoing_body(monkeypatch, field, value):
    payload = weather_payload()
    payload["fact"][field] = value
    payload["private_untrusted_extra"] = "synthetic-private-data"
    # Non-finite numbers are also invalid upstream JSON, but exercise normalization directly.
    if field == "obs_time" and value == float("inf"):
        with pytest.raises(ScheduleWeatherProviderError):
            YandexWeatherProvider._normalize(payload)
        return
    weather_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ScheduleWeatherProviderError) as caught:
        asyncio.run(YandexWeatherProvider("synthetic-secret-key", 55., 37.).current({}, ""))
    assert "synthetic-private-data" not in str(caught.value)
    assert "synthetic-secret-key" not in str(caught.value)


@pytest.mark.parametrize("failure", [302, 403, 500, "timeout", "bad_json"])
def test_yandex_http_failures_are_bounded_and_redacted(monkeypatch, failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-secret-key", request=request)
        if failure == "bad_json":
            return httpx.Response(200, content=b"synthetic-private-data")
        return httpx.Response(failure, text="synthetic-private-data")
    weather_http(monkeypatch, handle)
    provider = YandexWeatherProvider("synthetic-secret-key", 55., 37.)
    with pytest.raises(ScheduleWeatherProviderError) as caught:
        asyncio.run(provider.current({}, ""))
    assert str(caught.value) in {"weather_provider_timeout", "yandex_weather_network_failure"}
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "synthetic-secret-key" not in diagnostic
    assert "synthetic-private-data" not in diagnostic


@pytest.mark.parametrize("name,value", [("YANDEX_WEATHER_LATITUDE", "91"), ("YANDEX_WEATHER_TIMEOUT_S", "0"),
                                        ("YANDEX_WEATHER_TIMEOUT_S", "nan"), ("MISHKA_WEATHER_PROVIDER", "unknown-provider")])
def test_remote_launcher_rejects_invalid_weather_settings(monkeypatch, name, value):
    from scripts.run_remote_stand import _weather_settings
    clear_weather_env(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError):
        Settings(profile="local_gpu", **_weather_settings("local_gpu")).validate()


@pytest.mark.parametrize("key,latitude,longitude", [("", 55., 37.), ("synthetic-key", None, 37.), ("synthetic-key", 55., None)])
def test_yandex_missing_configuration_makes_no_http_request(monkeypatch, key, latitude, longitude):
    def unexpected(request):
        pytest.fail("unconfigured weather attempted network I/O")
    weather_http(monkeypatch, unexpected)
    with pytest.raises(ScheduleWeatherProviderError, match="not_configured"):
        asyncio.run(YandexWeatherProvider(key, latitude, longitude).current({}, ""))


@pytest.mark.parametrize("age,accepted", [(7200, True), (7200.01, False), (-300, True), (-300.01, False)])
def test_yandex_observation_policy_boundaries(monkeypatch, age, accepted):
    now = 1_800_000_000
    monkeypatch.setattr("app.foundation.schedule_weather.time.time", lambda: now)
    payload = weather_payload(observation=now - age)
    if accepted:
        assert YandexWeatherProvider._normalize(payload)["source"] == "yandex_weather"
    else:
        with pytest.raises(ScheduleWeatherProviderError, match="not_current"):
            YandexWeatherProvider._normalize(payload)


@pytest.mark.parametrize("temperature,accepted", [(-100, True), (-99.99, True), (0, True), (69.99, True), (70, True),
                                                (-100.01, False), (70.01, False), (-1e100, False), (1e100, False)])
def test_yandex_temperature_plausibility_before_typed_rendering(monkeypatch, temperature, accepted):
    from app.foundation.canonical_services import TypedInfoRenderer
    payload = weather_payload()
    payload["fact"]["temp"] = temperature
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=payload)
    weather_http(monkeypatch, handle)
    renderer = TypedInfoRenderer(ProductionScheduleWeatherService(YandexWeatherProvider("synthetic-key", 55., 37.)), {}, None)
    if accepted:
        rendered = asyncio.run(renderer.render("weather"))
        assert rendered == {"kind": "weather", "text": f"дождь, {temperature:+g} °C", "source": "yandex_weather"}
    else:
        with pytest.raises(ScheduleWeatherSchemaError, match="^malformed_yandex_weather_response$"):
            asyncio.run(renderer.render("weather"))
    assert len(calls) == 1
