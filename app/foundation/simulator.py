"""Deterministic acceptance harness over the canonical exhibition stand.

The harness owns no domain state and does not introduce a second runtime. It
only drives the existing canonical ingress with fake adapters and an injectable
monotonic clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .domain import Event
from .stand import HardwareIntegrationStand


@dataclass
class FakeMonotonicClock:
    value: float = 0.0

    def now(self) -> float:
        return float(self.value)

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("fake clock cannot move backwards")
        self.value += float(seconds)
        return self.value


class SimulatorAcceptanceHarness:
    """Small reusable driver for GR-01–GR-07 regression scenarios."""

    def __init__(self, settings: Settings | None = None):
        self.clock = FakeMonotonicClock()
        self.stand = HardwareIntegrationStand(
            settings=settings or Settings(profile="mock", runtime_mode="exhibition", hardware_mode="simulator", content_root="content"),
            monotonic_now=self.clock.now,
            background_scheduler=False,
        )
        self._sequence = 0
        self.stand.application.monotonic_now = self.clock.now
        self.stand.application.canonical_runtime.monotonic_now = self.clock.now
        self.stand.application.led.exhibition_effects.monotonic_now = self.clock.now

    def _next_sequence(self, sequence: int | None) -> int:
        if sequence is None:
            self._sequence += 1
            return self._sequence
        self._sequence = max(self._sequence, int(sequence))
        return int(sequence)

    async def space_pressed(self, sequence: int | None = None) -> dict[str, Any]:
        return await self.stand.inject_physical({"type": "SpacePressed", "_source": "simulator", "_sequence": self._next_sequence(sequence)})

    async def space_released(self, sequence: int | None = None) -> dict[str, Any]:
        return await self.stand.inject_physical({"type": "SpaceReleased", "_source": "simulator", "_sequence": self._next_sequence(sequence)})

    async def button(self, button_number: int, sequence: int | None = None) -> dict[str, Any]:
        return await self.stand.inject("button", {"button_id": button_number}, "stand")

    async def speech_result(self, text: str) -> dict[str, Any]:
        # parse_event redacts speech to length before canonical ingress.
        return await self.stand.inject("text", {"text": text}, "stand")

    async def audio_completed(self, response_id: str, token: int | None = None) -> dict[str, Any]:
        sequence = self.stand._audio_event_sequence + 1
        self.stand._audio_event_sequence = sequence
        result = await self.stand.application.receive_async(
            Event("AudioCompleted", "stand", sequence, self.clock.now(), "simulator",
                  {"response_id": response_id, "token": token}, f"simulator-audio:{sequence}", self.clock.now())
        )
        self.stand._collect_events()
        return result

    async def advance(self, seconds: float) -> list[dict[str, Any]]:
        self.clock.advance(seconds)
        self.stand.application.led.advance_effects(self.clock.now(), force=True)
        return await self.stand.application.flush_welcome_timers()

    def disconnect_hid(self, reason: str = "simulated_disconnect") -> None:
        generation = getattr(self.stand, "_hid_generation", 0)
        self.stand._on_hid_error({"kind": "device_disconnected", "reason": reason}, generation)

    def reconnect_hid(self) -> None:
        self.stand._hid_generation = getattr(self.stand, "_hid_generation", 0) + 1
        self.stand._hid_status = {"state": "active", "generation": self.stand._hid_generation, "backend": "simulated"}

    def snapshot(self) -> dict[str, Any]:
        return self.stand.snapshot()

    def health(self) -> dict[str, Any]:
        return self.stand.health_snapshot()

    def diagnostics(self) -> dict[str, Any]:
        return self.stand.diagnostics_snapshot()

    def close(self) -> None:
        self.stand.close()

