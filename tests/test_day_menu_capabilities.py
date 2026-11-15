from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.foundation.menu_capabilities import day_menu_items
from app.foundation.schedule_weather import DemoScheduleWeatherService, ProductionScheduleWeatherService, YandexWeatherProvider


def app(service=None, profile=None, *, grant=True, identified=True, mode='mock'):
    return SimpleNamespace(schedule_weather=service, settings=SimpleNamespace(profile=mode),
        canonical_runtime=SimpleNamespace(sessions={'s': SimpleNamespace(profile_status='identified' if identified else 'anonymous')}),
        _identity_scope_live=lambda sid, scope: grant,
        _canonical_profiles={'s': profile})


def ids(application): return tuple(key for key, _ in day_menu_items(application, 's'))


@pytest.mark.parametrize('service,mode,expected', [(None,'mock',('date',)),
    (DemoScheduleWeatherService(),'mock',('date','weather')),
    (DemoScheduleWeatherService(),'production',('date',)),
    (ProductionScheduleWeatherService(),'mock',('date',)),
    (SimpleNamespace(weather=lambda: None),'mock',('date',))])
def test_known_services_only(service, mode, expected):
    assert ids(app(service, mode=mode)) == expected


@pytest.mark.parametrize('key,lat,lon,available', [('key',55,37,True),(' ',55,37,False),
    ('key',None,37,False),('key',True,37,False),('key',float('nan'),37,False),
    ('key',55,float('inf'),False),('key',91,37,False),('key',55,-181,False),
    ('key',-90,180,True),('key','55',37,False),('key',10**400,37,False)])
def test_weather_configuration_without_queries(key, lat, lon, available):
    provider = YandexWeatherProvider(key, lat, lon)
    async def forbidden(*args, **kwargs): raise AssertionError('provider called')
    provider.current = forbidden
    service = ProductionScheduleWeatherService(provider)
    service.weather = service.schedule = forbidden
    assert ('weather' in ids(app(service))) is available


def facts():
    now = datetime.now(timezone.utc)
    return {'timezone':'UTC', 'schedule': [
        {'time':'00:00','title':'PRIVATE_NEXT','start_at':(now+timedelta(hours=1)).isoformat(),'status':'scheduled'},
        {'time':'00:00','title':'PRIVATE_CURRENT','start_at':now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),'status':'in_progress'}]}


@pytest.mark.parametrize('grant,identified', [(False,True),(True,False),(False,False)])
def test_private_items_require_live_identified_grant(grant, identified):
    assert ids(app(ProductionScheduleWeatherService(), facts(), grant=grant, identified=identified)) == ('date',)


def test_loaded_facts_only_no_provider_calls_and_no_private_result():
    service = ProductionScheduleWeatherService()
    async def forbidden(*args, **kwargs): raise AssertionError('provider called')
    service.schedule = service.weather = service.build = forbidden
    result = day_menu_items(app(service, facts()), 's')
    assert result == (('date','Дата'),('what_next','Следующее занятие'),('where_now','Текущее занятие'))
    assert 'PRIVATE_' not in repr(result)


@pytest.mark.parametrize('profile', [None, {}, {'timezone':'invalid/zone','schedule':[{'time':'01:00','title':'private'}]},
    {'schedule':[{'time':'25:00','title':'private'}]}, {'schedule':[{'time':'01:00','title':'private','start_at':'tomorrow','status':'scheduled'}]},
    {'schedule':[{'time':'01:00','title':'private','start_at':'2035-01-01T01:00:00','status':'scheduled'}]}])
def test_invalid_or_undated_facts_not_advertised(profile):
    assert ids(app(ProductionScheduleWeatherService(), profile)) == ('date',)


def test_ambiguous_facts_not_advertised():
    profile = facts()
    profile['schedule'] *= 2
    assert ids(app(ProductionScheduleWeatherService(), profile)) == ('date',)
