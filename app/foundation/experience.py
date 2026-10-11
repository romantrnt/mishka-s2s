"""Compatibility import for the canonical schedule/weather service.

Weather configuration belongs to app.foundation.config.Settings and the
canonical adapters in schedule_weather.py. This module intentionally does not
read environment variables or contact a second weather provider.
"""

from .schedule_weather import DemoScheduleWeatherService

__all__ = ["DemoScheduleWeatherService"]
