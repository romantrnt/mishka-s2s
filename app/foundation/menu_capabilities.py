"""Read-only day menu capabilities from known adapters and loaded facts.

No provider is invoked and no private facts leave this boundary.
"""
from collections.abc import Mapping
import math

from .canonical_services import TypedInfoRenderer
from .schedule_weather import (
    DemoScheduleWeatherService, ProductionScheduleWeatherService,
    YandexWeatherProvider, ScheduleWeatherProviderError, validate_schedule,
)


def _coordinate(value, lower, upper):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and lower <= value <= upper
    except OverflowError:
        return False


def day_menu_items(app, sid):
    items = [('date', 'Дата')]
    service = getattr(app, 'schedule_weather', None)
    mock = getattr(getattr(app, 'settings', None), 'profile', None) == 'mock'
    demo = type(service) is DemoScheduleWeatherService and mock
    production = type(service) is ProductionScheduleWeatherService
    weather = getattr(service, 'weather_provider', None) if production else None
    if demo or (type(weather) is YandexWeatherProvider
                and isinstance(weather.api_key, str) and bool(weather.api_key.strip())
                and all(_coordinate(value, lower, upper)
                        for value, lower, upper in ((weather.latitude, -90, 90),
                                                    (weather.longitude, -180, 180)))):
        items.append(('weather', 'Погода'))
    if not (production or demo):
        return tuple(items)
    runtime = getattr(app, 'canonical_runtime', None)
    state = runtime.sessions.get(sid) if runtime is not None else None
    if state is None or state.profile_status != 'identified' or not app._identity_scope_live(sid, 'schedule'):
        return tuple(items)
    profile = app._canonical_profiles.get(sid)
    if not isinstance(profile, Mapping):
        return tuple(items)
    try:
        schedule = validate_schedule(profile.get('schedule'))
        renderer = TypedInfoRenderer(None, profile, None)
    except (ScheduleWeatherProviderError, ValueError, TypeError, KeyError):
        return tuple(items)
    for kind, label in (('what_next', 'Следующее занятие'), ('where_now', 'Текущее занятие')):
        try:
            renderer._select_schedule_item(schedule, kind)
        except (ScheduleWeatherProviderError, ValueError, TypeError, OverflowError):
            continue
        items.append((kind, label))
    return tuple(items)
