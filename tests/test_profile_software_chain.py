from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lighting import FakeLightingOutput, LIGHTING_ELEMENT_IDS
from app.foundation.max_json import MaxJsonBoundary
from app.foundation.stand import UARTLEDController


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class PublicPhoneClientDouble:
    """Ready-JSON fixture, not a capture, UID lookup, or identity resolver.

    Only Max's public start/poll/patient/clear/close interface is exercised.
    The real client, HTTPS and SQLite are covered in test_max_json_integration.
    """

    def __init__(self):
        self.events = deque()
        self.card = None
        self.running = False
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.running:
            return False
        self.running = True
        return True

    def poll(self):
        self.calls.append("poll")
        return self.events.popleft() if self.events else None

    def patient(self):
        self.calls.append("patient")
        return deepcopy(self.card)

    def clear(self):
        self.calls.append("clear")
        self.events.clear()
        self.card = None

    def close(self):
        self.calls.append("close")
        self.clear()
        self.running = False
        return True

    def ready(self, *, first_name="PRIVATE_SYNTHETIC_NAME", age_years=6):
        self.card = {
            "schema_version": 1,
            "identity": {"first_name": first_name},
            "birth": {"age_years": age_years, "birth_data_state": "complete"},
            "record": {"state": "active"},
            "rfid": {"state": "active", "valid_until": "2026-11-10T01:00:00+00:00"},
            "visit": {"status": "open"},
            "acquaintance": {"kind": "first"},
            "adaptation": {"wheelchair_access": False, "participation_mode": "standing"},
        }
        self.events.append({"kind": "patient_ready"})

    def profile_cleared(self):
        self.card = None
        self.events.append({"kind": "clear", "reason_code": "no_personalization"})


def attach_client(app, *, session_id="software", admits_users=None):
    client = PublicPhoneClientDouble()
    boundary = MaxJsonBoundary(app, client, session_id=session_id,
        now=lambda: datetime(2026, 11, 10, tzinfo=timezone.utc), admits_users=admits_users)
    boundary.start(background=False)
    return client, boundary


def build_chain():
    clock = Clock()
    output = FakeLightingOutput()
    adapter = SimpleNamespace(send=lambda _message: None)
    led = UARTLEDController(adapter, output=output, monotonic_now=clock, background_scheduler=False)
    app, _, _, _ = build_application(settings=Settings(profile="mock", hardware_mode="fake",
        runtime_mode="exhibition", identity_mode="max_json"), led_controller=led,
        monotonic_now=clock, background_scheduler=False)
    return app, clock, led, output


async def send(app, kind, sequence, payload=None):
    return await app.receive_async(Event(kind, "software", sequence, 1.0, "software-test",
        payload or {}, f"software:{sequence}", app.monotonic_now()))


def test_ready_json_to_real_effect_engine_handoff_lift_and_hangup():
    async def run():
        app, clock, led, output = build_chain()
        client, boundary = attach_client(app)
        try:
            assert set(output.snapshot().values()) == {0}
            client.ready()
            ready = await boundary.step()
            assert ready["reason"] == "profile_ready"
            assert ready["canonical_state"]["mode"] == "profile_ready"
            assert client.calls[:4] == ["start", "poll", "patient", "poll"]
            generation = led.exhibition_effects.generation
            for stage in range(1, 8):
                clock.value = stage * 0.12
                led.advance_effects(clock())
            assert led.effect_snapshot()["effect"] == "breathing"
            assert led.exhibition_effects.generation == generation
            assert all(command.state == 1 for batch in output.batches[-2:] for command in batch)
            assert [output.batches[-2][0].transition_ms, output.batches[-1][0].transition_ms] == [120, 1000]
            before = len(output.batches)
            lifted = await send(app, "HandsetLifted", 1)
            assert lifted["canonical_state"]["mode"] == "voice_welcome"
            assert lifted["canonical_state"]["profile_status"] == "identified"
            assert sum(effect.get("pattern") == "handset_lift_wave" for effect in lifted["outputs"]) == 1
            assert all(any(command.state for command in batch) for batch in output.batches[before:])
            assert led.effect_snapshot()["effect"] == "wave"
            await send(app, "HandsetPlaced", 2)
            assert set(output.snapshot().values()) == {0}
            assert led.effect_snapshot()["effect"] == "idle"
            clock.value = 100.0
            led.advance_effects(clock())
            assert set(output.snapshot().values()) == {0}
            assert not app._canonical_profiles
            assert client.patient() is None and boundary.facts is None
        finally:
            await app.shutdown_identity()
            await app._cleanup_session_resources("software")
            led.cancel_effects(clear=True)
    asyncio.run(run())


def test_guest_dark_to_lift_uses_same_real_effect_engine_without_profile():
    async def run():
        app, clock, led, output = build_chain()
        try:
            assert set(output.snapshot().values()) == {0}
            lifted = await send(app, "HandsetLifted", 1)
            assert lifted["canonical_state"]["profile_status"] == "anonymous"
            assert lifted["canonical_state"]["mode"] == "voice_welcome"
            assert led.effect_snapshot()["effect"] == "wave"
            assert any(output.snapshot().values())
            await send(app, "HandsetPlaced", 2)
            assert set(output.snapshot()) == {str(i) for i in LIGHTING_ELEMENT_IDS}
            assert set(output.snapshot().values()) == {0}
        finally:
            await app._cleanup_session_resources("software")
            await app.shutdown_identity()
            led.cancel_effects(clear=True)
    asyncio.run(run())
