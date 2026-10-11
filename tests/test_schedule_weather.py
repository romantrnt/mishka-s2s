import asyncio
from pathlib import Path
import pytest
from app.foundation.adapters import MockProfileProvider, RecordingAudio, RecordingLED, RuleBasedIntentClassifier
from app.foundation.application import ScenarioApplication
from app.foundation.content import FileScenarioRepository
from app.foundation.events import parse_event
from app.foundation.repositories import InMemoryHistoryRepository, InMemorySessionRepository
from app.foundation.schedule_weather import (ProductionScheduleWeatherService, ScheduleWeatherProviderError, ScheduleWeatherSchemaError, YandexWeatherProvider)
from app.foundation.observability import StructuredEventPublisher

pytestmark = pytest.mark.integration
class NoopBounded:
    async def cancel_session(self, session_id): pass

class Provider:
    def __init__(self, value=None, error=None, delay=0): self.value,self.error,self.delay=value,error,delay
    async def current(self, profile, tag_id):
        if self.delay: await asyncio.sleep(self.delay)
        if self.error: raise self.error
        return self.value

def service(provider, timeout=.1): return ProductionScheduleWeatherService(provider, timeout_s=timeout)
def profile(): return {"child_id":"child", "schedule":[{"time":"09:00", "title":"Занятие"}]}
def weather(source="ru-aggregator"): return {"summary":"12 C, ясно", "temperature_c":12, "source":source}
def event(kind, sid, seq, **payload): return parse_event({"type":kind, **payload}, sid, seq, now=100.0)
def make_app(schedule_service):
    root=Path(__file__).parents[1]/"content"; history=InMemoryHistoryRepository()
    profiles=MockProfileProvider()
    profiles.data["mock-child-001"]["schedule"]=[{"time":"09:00","title":"Занятие"}]
    app=ScenarioApplication(InMemorySessionRepository(), FileScenarioRepository(root), profiles, RuleBasedIntentClassifier(), RecordingAudio(), RecordingLED(), StructuredEventPublisher(), now=lambda:100.0, bounded_s2s=NoopBounded(), history=history, schedule_weather=schedule_service)
    return app,history

def test_production_service_validates_successful_ru_adapter_contract():
    result=asyncio.run(service(Provider(weather())).build(profile(),"tag"))
    assert result["source"]=="ru-aggregator" and result["schedule"][0]["title"]=="Занятие"

@pytest.mark.parametrize("value,reason", [
    ({"summary":"ok","source":"ru-aggregator","temperature_c":"bad"},"malformed_weather_response"),
    ({"summary":"ok","source":"demo","temperature_c":1},"non_production_weather_source"),
    ({"summary":"ok","source":"ru-aggregator","temperature_c":1},"schedule_data_unavailable"),
])
def test_production_service_rejects_malformed_or_missing_data(value,reason):
    with pytest.raises(ScheduleWeatherSchemaError, match=reason):
        asyncio.run(service(Provider(value)).build({} if reason=="schedule_data_unavailable" else profile(),"tag"))

def test_production_service_fails_closed_without_ru_provider():
    with pytest.raises(ScheduleWeatherProviderError, match="ru_weather_provider_not_configured"):
        asyncio.run(ProductionScheduleWeatherService().build(profile(),"tag"))

def test_production_service_distinguishes_timeout_and_network_failure():
    with pytest.raises(ScheduleWeatherProviderError, match="weather_provider_timeout"):
        asyncio.run(service(Provider(weather(),delay=.2),.01).build(profile(),"tag"))
    with pytest.raises(ScheduleWeatherProviderError, match="weather_provider_network_failure"):
        asyncio.run(service(Provider(error=OSError("offline"))).build(profile(),"tag"))

def test_button_7_persists_one_valid_report_and_repeat_is_idempotent():
    app, history = make_app(service(Provider(weather())))
    sid="weather-session"
    async def run():
        await app.receive_async(event("rfid",sid,1,tag_id="mock-child-001"))
        await app.receive_async(event("handset_up",sid,2))
        first=await app.receive_async(event("button",sid,3,button_id=7))
        second=await app.receive_async(event("button",sid,4,button_id=7))
        return first,second
    first,second=asyncio.run(run())
    assert any(x["action"]=="schedule_weather" for x in first["outputs"])
    assert second["state"]=="ACTIVE"
    assert len([x for x in history.items if x["entity"]=="schedule_weather"])==1


def test_button_7_provider_failure_is_diagnostic_not_fixture_success():
    app,history=make_app(ProductionScheduleWeatherService()); sid="weather-failure"
    async def run():
        await app.receive_async(event("rfid",sid,1,tag_id="mock-child-001"))
        await app.receive_async(event("handset_up",sid,2))
        return await app.receive_async(event("button",sid,3,button_id=7))
    result=asyncio.run(run())
    assert result["reason"]=="schedule_weather_provider_failure"
    assert any(x["action"]=="schedule_weather_error" for x in result["outputs"])
    assert not any(x["action"]=="schedule_weather" for x in result["outputs"])
    assert not history.items


def test_button_7_request_is_discarded_when_session_ends_during_provider_call():
    app,history=make_app(service(Provider(weather(),delay=.05),1)); sid="weather-cancel"
    async def run():
        await app.receive_async(event("rfid",sid,1,tag_id="mock-child-001"))
        await app.receive_async(event("handset_up",sid,2))
        pending=asyncio.create_task(app.receive_async(event("button",sid,3,button_id=7)))
        await asyncio.sleep(.005)
        ended=await app.receive_async(event("handset_down",sid,4))
        return await pending,ended
    result,ended=asyncio.run(run())
    assert ended["state"]=="ENDED"
    assert result["reason"]=="schedule_weather_cancelled"
    assert not any(x["action"]=="schedule_weather" for x in result["outputs"])
    assert not history.items


def test_button_7_timeout_is_bounded_and_reset_allows_clean_retry():
    app,history=make_app(service(Provider(weather(),delay=.2),.01))
    sid="weather-timeout"
    async def run():
        await app.receive_async(event("rfid",sid,1,tag_id="mock-child-001"))
        await app.receive_async(event("handset_up",sid,2))
        started=asyncio.get_running_loop().time()
        failed=await app.receive_async(event("button",sid,3,button_id=7))
        elapsed=asyncio.get_running_loop().time()-started
        ended=await app.receive_async(event("handset_down",sid,4))
        reset=await app.reset_session(sid)
        return failed,elapsed,ended,reset
    failed,elapsed,ended,reset=asyncio.run(run())
    assert failed["reason"]=="schedule_weather_provider_failure"
    assert any(x["action"]=="schedule_weather_error" for x in failed["outputs"])
    assert elapsed < .1
    assert ended["state"]=="ENDED"
    assert reset["reset"] is True
    assert not history.items
    assert not app._audio_completions


class YandexResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        import json
        return json.dumps(self.payload).encode("utf-8")


def test_yandex_provider_uses_authorized_ru_contract_and_normalizes_response():
    requests = []
    payload = {
        "info": {"lat": 55.75, "lon": 37.61},
        "now_dt": "2026-10-03T10:00:00+03:00",
        "fact": {"temp": 12, "condition": "partly-cloudy"},
        "forecasts": [],
    }
    def opener(request, timeout):
        requests.append((request, timeout))
        return YandexResponse(payload)

    provider = YandexWeatherProvider("secret", 55.75, 37.61, opener=opener)
    result = asyncio.run(provider.current({}, "tag"))
    assert result["source"] == "yandex_weather"
    assert result["temperature_c"] == 12
    assert "переменная облачность" in result["summary"]
    assert requests[0][0].headers["X-yandex-weather-key"] == "secret"
    assert "lang=ru_RU" in requests[0][0].full_url
    assert "lat=55.75" in requests[0][0].full_url
    assert requests[0][1] == 2.0


def test_yandex_provider_is_fail_closed_without_credentials_or_with_malformed_payload():
    provider = YandexWeatherProvider(None, 55.75, 37.61)
    with pytest.raises(ScheduleWeatherProviderError, match="yandex_weather_api_key_not_configured"):
        asyncio.run(provider.current({}, "tag"))

    provider = YandexWeatherProvider("secret", 55.75, 37.61, opener=lambda request, timeout: YandexResponse({"fact": {}}))
    with pytest.raises(ScheduleWeatherSchemaError, match="malformed_yandex_weather_response"):
        asyncio.run(provider.current({}, "tag"))
