"""Release checks for configured weather/typed schedules; synthetic HTTP only."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
import traceback

import httpx
import pytest

from app.foundation.canonical_services import TypedInfoRenderer
from app.foundation.config import Settings, SettingsError
from app.foundation.schedule_weather import (
    DemoScheduleWeatherService, ProductionScheduleWeatherService,
    ScheduleWeatherProviderError, YandexWeatherProvider,
)
from test_weather_config import weather_http, weather_payload
from test_weather_request_scope import weather_app


def service():
    # Deliberately synthetic configured point, never location inference.
    return ProductionScheduleWeatherService(YandexWeatherProvider("synthetic-release-key", 1.25, -2.5))


@pytest.mark.asyncio
async def test_real_http_adapter_uses_only_config_and_minimizes_response(monkeypatch):
    calls = []
    observed = int(time.time())
    payload = weather_payload(observation=observed)
    payload["info"] = {"url": "https://synthetic.invalid/private-city", "lat": 1.25, "lon": -2.5}
    payload["private_extra"] = "SYNTHETIC_VISITOR_MARKER"
    def handle(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "api.weather.yandex.ru"
        assert request.url.path == "/v2/forecast"
        assert dict(request.url.params) == {"lat": "1.25", "lon": "-2.5", "lang": "ru_RU", "limit": "1", "hours": "false"}
        assert request.headers["X-Yandex-Weather-Key"] == "synthetic-release-key"
        assert request.content == b""
        assert "SYNTHETIC_VISITOR_MARKER" not in str(request.url)
        return httpx.Response(200, json=payload)
    weather_http(monkeypatch, handle)
    profile = {"display_name": "SYNTHETIC_VISITOR_MARKER", "city": "SYNTHETIC_CITY",
               "latitude": 77., "longitude": 88., "schedule": [{"title": "PRIVATE_SCHEDULE"}]}
    result = await service().weather(profile, "PRIVATE_SCHEDULE_REF")
    assert len(calls) == 1
    assert set(result) == {"summary", "temperature_c", "condition", "source", "observed_at"}
    assert result["source"] == "yandex_weather" and result["temperature_c"] == 3
    assert not any(marker in repr(result) for marker in ("SYNTHETIC_", "PRIVATE_", "city", "latitude"))


@pytest.mark.asyncio
async def test_missing_config_fails_before_network_even_if_profile_has_location(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("unconfigured weather opened HTTP")
    monkeypatch.setattr("app.foundation.schedule_weather.httpx.AsyncClient", forbidden)
    for key, lat, lon in (("", 1., 2.), ("synthetic", None, 2.), ("synthetic", 1., None)):
        provider = YandexWeatherProvider(key, lat, lon)
        with pytest.raises(ScheduleWeatherProviderError, match="not_configured"):
            await provider.current({"latitude": 1., "longitude": 2., "city": "synthetic"}, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "server"])
async def test_actual_root_seven_weather_failure_keeps_date_and_guest_private(monkeypatch, failure):
    calls = []
    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC_PRIVATE_ERROR", request=request)
        return httpx.Response(503, text="SYNTHETIC_PRIVATE_ERROR")
    weather_http(monkeypatch, handle)
    h, render = await weather_app("day", service())
    sid = h.state.session_id
    # A stale cached profile must not advertise private appointments to a guest.
    h.app._canonical_profiles[sid] = {"display_name": "SYNTHETIC_VISITOR_MARKER",
        "schedule": [{"time": "09:00", "title": "SYNTHETIC_PRIVATE_APPOINTMENT",
                      "start_at": datetime.now(timezone.utc).isoformat(), "status": "in_progress"}]}
    try:
        assert h.state.profile_status != "identified"
        assert [item["id"] for item in h.state.scene_menu_descriptor["items"]] == ["date", "weather"]
        slot = next(item["slot"] for item in h.state.scene_menu_descriptor["items"] if item["id"] == "weather")
        result, _ = await h.key(slot)
        assert len(calls) == 1 and h.state.active_audio_id == "PHRASE_100"
        assert not h.model.calls and not any("°C" in text for text in render.texts)
        assert not any(marker in json.dumps((result, h.state.snapshot(), h.app.audio.commands), ensure_ascii=False)
                       for marker in ("SYNTHETIC_PRIVATE", "SYNTHETIC_VISITOR"))
        await h.done()
        await h.key(1)
        assert h.state.active_audio_id == "TYPED_DATE" and len(calls) == 1
        assert "Сегодня" in render.texts[-1] and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_weather_and_dated_schedule_remain_independent_when_either_unavailable(monkeypatch):
    now = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
    profile = {"timezone": "UTC", "schedule": [{"time": "23:59", "title": "Синтетическое занятие",
               "start_at": (now + timedelta(hours=1)).isoformat(), "status": "scheduled"}]}
    available = [False]
    weather_http(monkeypatch, lambda request: httpx.Response(200, json=weather_payload()) if available[0] else httpx.Response(503))
    renderer = TypedInfoRenderer(service(), profile, "synthetic", now=lambda: now)
    with pytest.raises(ScheduleWeatherProviderError):
        await renderer.render("weather", weather_scope="configured_point")
    schedule = await renderer.render("what_next")
    assert schedule["text"] == "Сегодня в 11:00 — Синтетическое занятие"
    assert "23:59" not in schedule["text"]
    available[0] = True
    guest = TypedInfoRenderer(service(), None, None, now=lambda: now)
    assert (await guest.render("weather", weather_scope="configured_point"))["source"] == "yandex_weather"
    with pytest.raises(ScheduleWeatherProviderError, match="typed_information_unavailable"):
        await guest.render("where_now")


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Для какого города у тебя погода?", "Какая погода в Синтетикограде?"])
async def test_actual_day_city_queries_never_fetch_or_invent_location(monkeypatch, question):
    calls = []
    weather_http(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json=weather_payload()))
    h, render = await weather_app("day", service())
    try:
        await h.send("SpeechReceived", text=question)
        assert not calls and not h.model.calls and h.state.active_audio_id == "TYPED_WEATHER"
        spoken = render.texts[-1]
        assert ("Название города мне не передали" in spoken if "какого города" in question else "ответить не могу" in spoken)
        assert "°C" not in spoken and "Синтетикоград" not in spoken
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_dated_schedule_never_promotes_yesterday_or_undated_to_current_and_labels_future_day():
    now = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
    rows = [{"time": "09:00", "title": "Вчерашнее синтетическое занятие", "start_at": (now - timedelta(days=1)).isoformat(), "status": "in_progress"},
            {"time": "12:00", "title": "Завтрашнее синтетическое занятие", "start_at": (now + timedelta(days=1)).isoformat(), "status": "scheduled"},
            {"time": "10:00", "title": "Недатированное синтетическое занятие", "status": "in_progress"}]
    renderer = TypedInfoRenderer(ProductionScheduleWeatherService(), {"timezone": "UTC", "schedule": rows}, "synthetic", now=lambda: now)
    with pytest.raises(ScheduleWeatherProviderError, match="schedule_current_item_unavailable"):
        await renderer.render("where_now")
    result = await renderer.render("what_next")
    assert result["text"] == "25.11.2026 в 10:00 — Завтрашнее синтетическое занятие"
    assert "Сегодня" not in result["text"] and "12:00" not in result["text"]


@pytest.mark.asyncio
async def test_demo_source_is_explicit_and_production_cannot_select_it():
    demo = await DemoScheduleWeatherService().weather({}, "synthetic")
    assert demo["source"] == "demo"
    with pytest.raises(SettingsError):
        Settings(profile="production_candidate", weather_provider="demo").validate()
    from app.foundation.schedule_weather import validate_weather
    with pytest.raises(ScheduleWeatherProviderError, match="non_production_weather_source"):
        validate_weather(demo)
    assert validate_weather(demo, allow_demo=True)["source"] == "demo"


@pytest.mark.asyncio
async def test_stale_yandex_observation_is_not_replaced_by_fresh_api_clock(monkeypatch):
    payload = weather_payload(observation=time.time() - 7201)
    payload["private_extra"] = "SYNTHETIC_PRIVATE_BODY"
    weather_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ScheduleWeatherProviderError, match="observation_not_current") as caught:
        await service().weather({}, "synthetic")
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "SYNTHETIC_PRIVATE" not in diagnostic and "synthetic-release-key" not in diagnostic
