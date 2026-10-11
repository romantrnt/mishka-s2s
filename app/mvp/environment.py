from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MockClock:
    """Injectable deterministic clock for mock runs and tests."""
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MockWeatherProvider:
    """No external API. Kept behind an adapter for future weather scenarios."""
    def current(self) -> dict:
        return {"condition": "облачно", "temperature_c": 15, "source": "mock"}
