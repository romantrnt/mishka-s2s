"""Configured Yandex endpoint contracts; synthetic HTTP, no credentials/network."""
import asyncio
import time

import httpx
import pytest

from app.foundation.schedule_weather import (
    ScheduleWeatherProviderError, YandexWeatherProvider,
)
from test_weather_config import weather_http, weather_payload


@pytest.mark.parametrize('trailing_slash', [False, True])
def test_informers_uses_documented_query_and_current_fact(monkeypatch, trailing_slash):
    payload = weather_payload()
    payload['forecast'] = {'parts': []}
    del payload['forecasts']
    calls = []
    def handle(request):
        calls.append(request)
        assert dict(request.url.params) == {'lat': '1.25', 'lon': '-2.5', 'lang': 'ru_RU'}
        assert request.url.path.rstrip('/') == '/v2/informers'
        assert request.headers['X-Yandex-Weather-Key'] == 'synthetic-key'
        return httpx.Response(200, json=payload)
    weather_http(monkeypatch, handle)
    provider = YandexWeatherProvider('synthetic-key', 1.25, -2.5,
        endpoint='https://api.weather.yandex.ru/v2/informers' + ('/' if trailing_slash else ''))
    result = asyncio.run(provider.current({}, ''))
    assert len(calls) == 1 and result['source'] == 'yandex_weather'
    assert result['condition'] == 'rain' and result['temperature_c'] == 3
    assert set(result) == {'summary', 'temperature_c', 'condition', 'source', 'observed_at'}


@pytest.mark.parametrize('prediction', ['forecasts', 'forecast', None])
def test_unused_prediction_does_not_gate_valid_current_observation(prediction):
    payload = weather_payload()
    del payload['forecasts']
    if prediction is not None:
        payload[prediction] = [] if prediction == 'forecasts' else {}
    assert YandexWeatherProvider._normalize(payload)['condition'] == 'rain'


@pytest.mark.parametrize('condition,phrase', [
    ('drizzle', 'морось'), ('moderate-rain', 'умеренный дождь'),
    ('continuous-heavy-rain', 'сильный продолжительный дождь'),
])
def test_documented_additional_rain_conditions(condition, phrase):
    payload = weather_payload()
    payload['fact']['condition'] = condition
    assert YandexWeatherProvider._normalize(payload)['summary'] == phrase + ', +3 °C'


@pytest.mark.parametrize('prediction', ['forecast', 'forecasts'])
@pytest.mark.parametrize('failure', ['missing_fact', 'unknown_condition', 'stale', 'future', 'invalid_temperature'])
def test_neither_prediction_shape_substitutes_for_valid_current_fact(prediction, failure):
    payload = weather_payload()
    del payload['forecasts']
    payload[prediction] = [] if prediction == 'forecasts' else {}
    if failure == 'missing_fact': del payload['fact']
    elif failure == 'unknown_condition': payload['fact']['condition'] = 'synthetic-unknown'
    elif failure == 'stale': payload['fact']['obs_time'] = time.time() - 7201
    elif failure == 'future': payload['fact']['obs_time'] = time.time() + 301
    else: payload['fact']['temp'] = True
    with pytest.raises(ScheduleWeatherProviderError):
        YandexWeatherProvider._normalize(payload)


@pytest.mark.parametrize('endpoint', ['forecast', 'informers'])
@pytest.mark.parametrize('status', [302, 401, 403, 429, 500])
def test_http_denial_has_no_alternate_endpoint_or_demo_fallback(monkeypatch, endpoint, status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={'location': 'https://synthetic.invalid/'},
                             text='SYNTHETIC_PRIVATE_ERROR')
    weather_http(monkeypatch, handle)
    provider = YandexWeatherProvider('SYNTHETIC_PRIVATE_KEY', 1.25, -2.5,
        endpoint='https://api.weather.yandex.ru/v2/' + endpoint)
    with pytest.raises(ScheduleWeatherProviderError) as caught:
        asyncio.run(provider.current({}, ''))
    assert len(calls) == 1 and calls[0].url.path == '/v2/' + endpoint
    assert str(caught.value) == 'yandex_weather_network_failure'
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)
