from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.parse import urlencode, urlsplit
import httpx
from typing import Any


class ScheduleWeatherProviderError(RuntimeError):
    """The configured production schedule/weather provider failed or is absent."""


class ScheduleWeatherSchemaError(ScheduleWeatherProviderError):
    """A provider returned data outside the accepted report contract."""


class YandexWeatherProvider:
    """Current observations from the configured Yandex Weather REST endpoint."""

    _endpoint = "https://api.weather.yandex.ru/v2/forecast"
    _conditions = {
        "clear": "ясно",
        "partly-cloudy": "переменная облачность",
        "cloudy": "облачно",
        "overcast": "пасмурно",
        "drizzle": "морось",
        "light-rain": "небольшой дождь",
        "rain": "дождь",
        "moderate-rain": "умеренный дождь",
        "heavy-rain": "сильный дождь",
        "continuous-heavy-rain": "сильный продолжительный дождь",
        "showers": "ливень",
        "wet-snow": "мокрый снег",
        "light-snow": "небольшой снег",
        "snow": "снег",
        "snow-showers": "снегопад",
        "hail": "град",
        "thunderstorm": "гроза",
        "thunderstorm-with-rain": "гроза с дождём",
        "thunderstorm-with-hail": "гроза с градом",
    }

    def __init__(self, api_key: str | None, latitude: float | None, longitude: float | None,
                 timeout_s: float = 2.0, *, endpoint: str | None = None):
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        self.latitude = latitude
        self.longitude = longitude
        self.timeout_s = timeout_s
        self._http_slots = asyncio.Semaphore(2)
        self.endpoint = (endpoint or self._endpoint).rstrip("?")

    @classmethod
    def from_settings(cls, settings) -> "YandexWeatherProvider":
        config = settings.weather_configuration()
        return cls(config.api_key, config.latitude, config.longitude,
                   config.timeout_s, endpoint=config.endpoint)

    @classmethod
    def from_env(cls) -> "YandexWeatherProvider":
        # Compatibility shim only; production construction receives validated
        # values explicitly from Settings through the canonical API factory.
        from .config import Settings
        settings = Settings.from_env()
        return cls.from_settings(settings)

    async def current(self, profile: dict[str, Any], schedule_ref: str) -> dict[str, Any]:
        if not self.api_key:
            raise ScheduleWeatherProviderError("yandex_weather_api_key_not_configured")
        if self.latitude is None or self.longitude is None:
            raise ScheduleWeatherProviderError("yandex_weather_coordinates_not_configured")
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ScheduleWeatherProviderError("yandex_weather_coordinates_invalid")
        return await self._fetch()

    MAX_RESPONSE_BYTES = 256 * 1024
    # Local current-weather policy, not an upstream freshness guarantee.
    MAX_OBSERVATION_AGE_S = 2 * 60 * 60
    MAX_FUTURE_SKEW_S = 5 * 60

    async def _fetch(self) -> dict[str, Any]:
        params = {"lat": self.latitude, "lon": self.longitude, "lang": "ru_RU"}
        if urlsplit(self.endpoint).path.rstrip("/") != "/v2/informers":
            params.update(limit=1, hours="false")
        query = urlencode(params)
        try:
            async with asyncio.timeout(self.timeout_s):
                async with self._http_slots:
                    async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False) as client:
                        async with client.stream(
                            "GET", f"{self.endpoint}?{query}",
                            headers={"X-Yandex-Weather-Key": self.api_key or "",
                                     "Accept": "application/json", "Accept-Encoding": "identity"},
                        ) as response:
                            response.raise_for_status()
                            if int(response.headers.get("content-length", "0")) > self.MAX_RESPONSE_BYTES:
                                raise ScheduleWeatherProviderError("yandex_weather_response_too_large")
                            body = bytearray()
                            async for chunk in response.aiter_raw():
                                if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                                    raise ScheduleWeatherProviderError("yandex_weather_response_too_large")
                                body.extend(chunk)
            payload = json.loads(body.decode("utf-8"))
        except ScheduleWeatherProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ScheduleWeatherProviderError("weather_provider_timeout") from None
        except Exception:
            raise ScheduleWeatherProviderError("yandex_weather_network_failure") from None
        return self._normalize(payload)

    @classmethod
    def _normalize(cls, payload: Any) -> dict[str, Any]:
        # Current observations use fact; forecast/informers have different
        # prediction envelopes, neither of which supplies current authority.
        if not isinstance(payload, dict) or not isinstance(payload.get("info"), dict):
            raise ScheduleWeatherSchemaError("malformed_yandex_weather_response")
        fact = payload.get("fact")
        if not isinstance(fact, dict):
            raise ScheduleWeatherSchemaError("malformed_yandex_weather_response")
        condition = fact.get("condition")
        temperature = fact.get("temp")
        observed = fact.get("obs_time")
        if (not isinstance(condition, str) or condition not in cls._conditions
                or not isinstance(temperature, (int, float)) or isinstance(temperature, bool)
                or not isinstance(observed, (int, float)) or isinstance(observed, bool)):
            raise ScheduleWeatherSchemaError("malformed_yandex_weather_response")
        try:
            finite = math.isfinite(temperature) and math.isfinite(observed)
        except OverflowError:
            finite = False
        # Our conservative outdoor Celsius plausibility policy, not Yandex bounds.
        if not finite or not -100 <= temperature <= 70:
            raise ScheduleWeatherSchemaError("malformed_yandex_weather_response")
        # now/now_dt describe the API server clock, not the age of the measurement.
        if not -cls.MAX_FUTURE_SKEW_S <= time.time() - observed <= cls.MAX_OBSERVATION_AGE_S:
            raise ScheduleWeatherProviderError("yandex_weather_observation_not_current")
        return {
            "summary": f"{cls._conditions[condition]}, {temperature:+g} °C",
            "temperature_c": temperature,
            "condition": condition,
            "source": "yandex_weather",
            "observed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
        }


def validate_schedule(value: Any) -> list[dict[str, Any]]:
    """Validate and minimize typed schedule facts without dropping navigation fields."""
    if not isinstance(value, list) or not value:
        raise ScheduleWeatherSchemaError("schedule_data_unavailable")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("time"), str) or not isinstance(item.get("title"), str):
            raise ScheduleWeatherSchemaError("malformed_schedule_response")
        if not item["time"].strip() or not item["title"].strip() or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item["time"].strip()):
            raise ScheduleWeatherSchemaError("malformed_schedule_response")
        normalized: dict[str, Any] = {"time": item["time"].strip(), "title": item["title"].strip()}
        for key in ("room", "floor", "start_at", "minutes_until", "status", "priority"):
            value = item.get(key)
            if value is None:
                continue
            if key in {"room", "start_at", "status"} and isinstance(value, str) and value.strip():
                normalized[key] = value.strip()[:80]
            elif key in {"floor", "minutes_until", "priority"} and isinstance(value, int) and not isinstance(value, bool):
                normalized[key] = value
        result.append(normalized)
    return result


def validate_weather(value: Any, *, allow_demo: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ScheduleWeatherSchemaError("malformed_weather_response")
    source = value.get("source")
    if not isinstance(source, str) or not source.strip() or (not allow_demo and source in {"demo", "mock", "weather_fallback"}):
        raise ScheduleWeatherSchemaError("non_production_weather_source")
    temperature = value.get("temperature_c")
    if temperature is not None and not isinstance(temperature, (int, float)):
        raise ScheduleWeatherSchemaError("malformed_weather_response")
    return dict(value)


def validate_report(value: Any, *, allow_demo: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScheduleWeatherSchemaError("malformed_schedule_weather_response")
    schedule = validate_schedule(value.get("schedule"))
    weather = validate_weather(value.get("weather"), allow_demo=allow_demo)
    spoken = value.get("spoken_text")
    if not isinstance(spoken, str) or not spoken.strip():
        raise ScheduleWeatherSchemaError("malformed_schedule_weather_response")
    return {"spoken_text": spoken.strip(), "schedule": schedule, "weather": weather, "source": weather["source"]}


class ProductionScheduleWeatherService:
    """Production schedule/weather service backed by the configured RU adapter."""

    def __init__(self, weather_provider: YandexWeatherProvider | None = None, timeout_s: float = 2.0):
        self.weather_provider = weather_provider
        self.timeout_s = timeout_s

    async def schedule(self, profile: Any, schedule_ref: str) -> list[dict[str, Any]]:
        if not isinstance(profile, dict):
            raise ScheduleWeatherProviderError("profile_data_unavailable")
        return validate_schedule(profile.get("schedule"))

    async def weather(self, profile: Any, schedule_ref: str, *, allow_demo: bool = False) -> dict[str, Any]:
        if self.weather_provider is None:
            raise ScheduleWeatherProviderError("ru_weather_provider_not_configured")
        if not isinstance(profile, dict):
            raise ScheduleWeatherProviderError("profile_data_unavailable")
        try:
            value = await asyncio.wait_for(self.weather_provider.current(profile, schedule_ref), timeout=self.timeout_s)
        except asyncio.TimeoutError as exc:
            raise ScheduleWeatherProviderError("weather_provider_timeout") from exc
        except ScheduleWeatherProviderError:
            raise
        except Exception as exc:
            raise ScheduleWeatherProviderError("weather_provider_network_failure") from exc
        return validate_weather(value, allow_demo=allow_demo)

    async def build(self, profile: Any, schedule_ref: str) -> dict[str, Any]:
        schedule = await self.schedule(profile, schedule_ref)
        weather = await self.weather(profile, schedule_ref)
        items = ", ".join(f"{item['time']} — {item['title']}" for item in schedule[:4])
        return validate_report({
            "spoken_text": f"Расписание: {items}. Погода: {weather['summary']}.",
            "schedule": schedule, "weather": weather, "source": weather["source"],
        })


class DemoScheduleWeatherService:
    """Development-only service. Never use this in a production profile."""

    async def schedule(self, profile: Any, schedule_ref: str) -> list[dict[str, Any]]:
        profile = profile if isinstance(profile, dict) else {}
        return validate_schedule(profile.get("schedule") or [{"time": "09:00", "title": "Занятие"}])

    async def weather(self, profile: Any, schedule_ref: str, *, allow_demo: bool = False) -> dict[str, Any]:
        return validate_weather({"summary": "ясно, без осадков", "temperature_c": 15, "source": "demo"}, allow_demo=True)

    async def build(self, profile: Any, schedule_ref: str) -> dict[str, Any]:
        profile = profile if isinstance(profile, dict) else {}
        schedule = profile.get("schedule") or [{"time": "09:00", "title": "Занятие"}]
        weather = {"summary": "clear, no precipitation", "temperature_c": 15, "source": "demo"}
        return {"spoken_text": "Расписание и погода доступны в демонстрационном режиме.", "schedule": schedule, "weather": weather, "source": "demo"}
