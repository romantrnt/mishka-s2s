from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.identity import IdentityDecision, SyntheticIdentityResolver
from app.foundation.lighting import FakeLightingOutput, LIGHTING_ELEMENT_IDS
from app.foundation.stand import UARTLEDController


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


def build_chain():
    clock = Clock()
    output = FakeLightingOutput()
    adapter = SimpleNamespace(send=lambda _message: None)
    led = UARTLEDController(adapter, output=output, monotonic_now=clock, background_scheduler=False)
    resolver = SyntheticIdentityResolver({"synthetic-card": IdentityDecision(
        "authorized", {"profile_ref": "synthetic-subject", "display_name": "PRIVATE_NAME"},
        frozenset({"personalize"}), 100.0)}, clock=clock, lease_s=100.0)
    app, _, _, _ = build_application(settings=Settings(profile="mock", hardware_mode="fake",
        identity_mode="optional_rfid", identity_provider="synthetic"), led_controller=led,
        identity_resolver=resolver, monotonic_now=clock, background_scheduler=False)
    return app, clock, led, output


async def send(app, kind, sequence, payload=None):
    return await app.receive_async(Event(kind, "software", sequence, 1.0, "software-test",
        payload or {}, f"software:{sequence}", app.monotonic_now()))


def test_authorized_canonical_to_real_effect_engine_handoff_lift_and_hangup():
    async def run():
        app, clock, led, output = build_chain()
        try:
            assert set(output.snapshot().values()) == {0}
            pending = await send(app, "RfidPresented", 1, {"tag_id": "synthetic-card"})
            assert pending["canonical_state"]["mode"] == "rfid_lookup"
            task = app._identity_lookup_tasks.get("software")
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), 1.0)
            assert app.canonical_runtime.sessions["software"].mode == "rfid_ready"
            generation = led.exhibition_effects.generation
            for stage in range(1, 8):
                clock.value = stage * 0.12
                led.advance_effects(clock())
            assert led.effect_snapshot()["effect"] == "breathing"
            assert led.exhibition_effects.generation == generation
            assert all(command.state == 1 for batch in output.batches[-2:] for command in batch)
            assert [output.batches[-2][0].transition_ms, output.batches[-1][0].transition_ms] == [120, 1000]
            before = len(output.batches)
            lifted = await send(app, "HandsetLifted", 2)
            assert lifted["canonical_state"]["mode"] == "voice_welcome"
            assert lifted["canonical_state"]["profile_status"] == "identified"
            assert sum(effect.get("pattern") == "handset_lift_wave" for effect in lifted["outputs"]) == 1
            assert all(any(command.state for command in batch) for batch in output.batches[before:])
            assert led.effect_snapshot()["effect"] == "wave"
            await send(app, "HandsetPlaced", 3)
            assert set(output.snapshot().values()) == {0}
            assert led.effect_snapshot()["effect"] == "idle"
            clock.value = 100.0
            led.advance_effects(clock())
            assert set(output.snapshot().values()) == {0}
            assert not app._canonical_profiles
        finally:
            await app._cleanup_session_resources("software")
            await app.shutdown_identity()
            led.cancel_effects(clear=True)
    asyncio.run(run())


def test_guest_dark_to_lift_uses_same_real_effect_engine_without_identity():
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
