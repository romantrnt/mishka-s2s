from __future__ import annotations

import asyncio
import pytest

from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lighting import (
    EXHIBITION_BREATHING_PHASE_MS, EXHIBITION_PWM_SLOTS, LIGHTING_ELEMENT_IDS, ExhibitionLightingEngine,
    FakeLightingOutput, LightingCommand, quantize_brightness_percent,
)
from app.foundation.simulator import SimulatorAcceptanceHarness
from app.foundation.stand import HardwareIntegrationStand


def ce(kind: str, sequence: int, version: int, now: float, source: str = "gr02") -> CanonicalEvent:
    return CanonicalEvent(kind, "gr02", sequence, version, source, {}, f"{source}-{sequence}", now, now)


def test_exhibition_runtime_down_is_hard_reset_and_stale_timer_is_inert() -> None:
    clock = [10.0]
    runtime = CanonicalRuntime(monotonic_now=lambda: clock[0], exhibition_mode=True)
    lifted = runtime.handle(ce("HandsetLifted", 1, 0, 10.0))
    assert lifted["reason"] == "exhibition_handset_lifted"
    down = runtime.handle(ce("HandsetPlaced", 2, 1, 10.1))
    assert down["reset_session"] is True
    assert down["reason"] == "exhibition_reset"
    assert down["state"]["mode"] == "idle"
    assert down["state"]["handset"] == "down"
    assert down["state"]["welcome_deadline_monotonic"] is None
    assert down["state"]["dialogue_memory"]["status"] == "active"
    assert down["state"]["dialogue_memory"]["storage"] == "ram_only"
    assert {e["type"] for e in down["effects"]} >= {"audio_stop", "capture_close", "exhibition_effects_cancelled", "exhibition_reset", "standby_breathing_started"}
    stale = runtime.handle(ce("WelcomeTimerFired", 1, 2, 15.0, "timer"))
    assert stale["effects"] == []


def test_application_reset_deletes_session_and_ram_resources() -> None:
    async def scenario() -> None:
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        app.monotonic_now = lambda: 100.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        lifted = await app.receive_async(Event("HandsetLifted", "reset", 1, 0.0, "hid", {}, "up", 100.0))
        assert lifted["reason"] == "exhibition_handset_lifted"
        app.conversation_contexts["reset"] = object()
        down = await app.receive_async(Event("HandsetPlaced", "reset", 2, 0.1, "hid", {}, "down", 100.1))
        assert down["reason"] == "exhibition_reset"
        assert app.sessions.get("reset") is None
        assert "reset" not in app.canonical_runtime.sessions
        assert "reset" not in app.conversation_contexts
        assert "reset" not in app._welcome_timers
        assert down["canonical_state"]["mode"] == "idle"
        assert down["reset_cleanup"]["session_id"] == "reset"
        repeated = await app.receive_async(Event("HandsetPlaced", "reset", 3, 0.2, "hid", {}, "down-2", 100.2))
        assert repeated["reason"] == "handset_already_placed"
        assert app.sessions.get("reset") is None
    asyncio.run(scenario())


def test_stale_application_timer_cannot_touch_second_physical_session() -> None:
    async def scenario() -> None:
        clock = [0.0]
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        await app.receive_async(Event("HandsetLifted", "race", 1, 0.0, "hid", {}, "up-1", 0.0))
        old_deadline = app.canonical_runtime.sessions["race"].welcome_deadline_monotonic
        await app.receive_async(Event("HandsetPlaced", "race", 2, 0.1, "hid", {}, "down", 0.1))
        clock[0] = 10.0
        await app.receive_async(Event("HandsetLifted", "race", 3, 0.2, "hid", {}, "up-2", 10.0))
        assert app.canonical_runtime.sessions["race"].welcome_deadline_monotonic != old_deadline
        assert await app._fire_exhibition_welcome("race", old_deadline) is None
        assert app.canonical_runtime.sessions["race"].welcome_status == "pending"
    asyncio.run(scenario())


def test_breathing_is_deterministic_all_sixteen_binary_leds_and_idempotent():
    output = FakeLightingOutput()
    engine = ExhibitionLightingEngine(output, monotonic_now=lambda: 0.0)
    first = engine.start_breathing(now=0.0)
    assert first["effect"] == "breathing" and first["elements"] == list(LIGHTING_ELEMENT_IDS)
    assert first["protocol"] == "binary_transition"
    assert first["state"] == 1
    assert first["transition_ms"] == EXHIBITION_BREATHING_PHASE_MS
    assert first["brightness_percent"] == 100
    assert len(output.batches[-1]) == 16
    assert {c.element_id for c in output.batches[-1]} == set(LIGHTING_ELEMENT_IDS)
    assert {c.state for c in output.batches[-1]} == {1}
    assert {c.transition_ms for c in output.batches[-1]} == {EXHIBITION_BREATHING_PHASE_MS}
    generation, frames = first["generation"], first["frame_count"]
    assert engine.start_breathing(now=0.0)["generation"] == generation
    assert engine.snapshot()["frame_count"] == frames
    off = engine.advance(1.0)
    on = engine.advance(2.0)
    assert off["state"] == 0 and off["brightness_percent"] == 0
    assert on["state"] == 1 and on["brightness_percent"] == 100
    assert off["transition_ms"] == on["transition_ms"] == EXHIBITION_BREATHING_PHASE_MS
    assert all(len(batch) == 16 for batch in output.batches)
    assert all(command.transition_ms == EXHIBITION_BREATHING_PHASE_MS for batch in output.batches[-2:] for command in batch)
    engine.cancel_effects()
    commands_after_reset = len(output.commands)
    assert engine.advance(3.0) is None and len(output.commands) == commands_after_reset

def test_wave_is_one_foreground_effect_and_cancelled_by_reset() -> None:
    output = FakeLightingOutput()
    engine = ExhibitionLightingEngine(output, monotonic_now=lambda: 0.0)
    engine.start_breathing(now=0.0)
    previous = engine.generation
    wave = engine.start_wave(now=5.0)
    assert wave["effect"] == "wave" and wave["generation"] > previous
    assert wave["last_frame"]["effect"] == "wave"
    assert len(output.batches[-1]) == 16
    assert sum(c.state for c in output.batches[-1]) > 0
    engine.cancel_effects()
    commands_after_cancel = len(output.commands)
    assert engine.advance(100.0) is None
    assert len(output.commands) == commands_after_cancel
    assert engine.snapshot()["effect"] == "idle"


def test_csv_lf_protocol_and_brightness_quantization_are_exact() -> None:
    assert LightingCommand(10, 1, 120).to_wire() == b"10,1,120\n"
    assert LightingCommand(72, 0, 0).to_wire() == b"72,0,0\n"
    assert quantize_brightness_percent(0) == (0.0, 0)
    assert quantize_brightness_percent(30) == (30.0, 3)
    assert quantize_brightness_percent(80) == (80.0, 8)
    assert quantize_brightness_percent(100) == (100.0, EXHIBITION_PWM_SLOTS)
    with pytest.raises(ValueError): quantize_brightness_percent(-1)
    with pytest.raises(ValueError): quantize_brightness_percent(101)
    output = FakeLightingOutput()
    engine = ExhibitionLightingEngine(output, monotonic_now=lambda: 0.0)
    engine.start_breathing(now=0.0)
    assert len(output.batches[-1]) == 16
    assert all(set(c.as_dict()) == {"element_id", "state", "transition_ms"} for c in output.batches[-1])
    assert [c.element_id for c in output.batches[-1]] == list(LIGHTING_ELEMENT_IDS)


def test_stand_start_lift_down_lift_trace_uses_simulator_effect_owner() -> None:
    async def scenario() -> None:
        stand = HardwareIntegrationStand(Settings(profile="mock", hardware_mode="simulator", content_root="content"))
        try:
            startup = stand.snapshot()
            assert startup["lighting_effects"]["effect"] == "breathing"
            assert startup["lighting_effects"]["elements"] == list(LIGHTING_ELEMENT_IDS)
            up = await stand.inject_physical({"type": "SpaceReleased", "_source": "gr02", "_sequence": 1})
            assert up["reason"] == "exhibition_handset_lifted"
            assert sum(e.get("type") == "lighting_wave_intent" for e in up["outputs"]) == 1
            assert stand.snapshot()["lighting_effects"]["effect"] == "wave"
            duplicate = await stand.inject_physical({"type": "SpaceReleased", "_source": "gr02", "_sequence": 2})
            assert duplicate["reason"] == "handset_already_lifted"
            assert sum(e.get("type") == "lighting_wave_intent" for e in duplicate["outputs"]) == 0
            down = await stand.inject_physical({"type": "SpacePressed", "_source": "gr02", "_sequence": 3})
            assert down["reason"] == "exhibition_reset"
            assert stand.snapshot()["lighting_effects"]["effect"] == "breathing"
            assert stand.snapshot()["canonical_state"]["mode"] == "idle"
            up2 = await stand.inject_physical({"type": "SpaceReleased", "_source": "gr02", "_sequence": 4})
            assert up2["reason"] == "exhibition_handset_lifted"
            assert sum(e.get("type") == "lighting_wave_intent" for e in up2["outputs"]) == 1
        finally:
            stand.close()
    asyncio.run(scenario())


def test_aud03_lossless_physical_projection_and_fake_clock_smoke() -> None:
    async def scenario() -> None:
        harness = SimulatorAcceptanceHarness()
        try:
            ids = set(str(element_id) for element_id in LIGHTING_ELEMENT_IDS)

            def assert_projection() -> None:
                snapshot = harness.snapshot()
                assert set(snapshot["physical_lighting"]) == ids
                assert set(snapshot["lighting"]) == ids
                assert set(harness.diagnostics()["physical_lighting"]) == ids
                assert set(harness.stand.panel_snapshot()["physical_lighting"]) == ids

            startup = harness.snapshot()
            assert startup["lighting_effects"]["effect"] == "breathing"
            assert startup["lighting_effects"]["scheduler"] == "manual_fake_clock"
            assert startup["lighting_effects"]["task_active"] is False
            assert_projection()

            lifted = await harness.space_released()
            assert lifted["reason"] == "exhibition_handset_lifted"
            assert sum(item.get("type") == "lighting_wave_intent" for item in lifted["outputs"]) == 1
            assert harness.snapshot()["lighting_effects"]["effect"] == "wave"
            await harness.advance(0.240)
            assert harness.snapshot()["lighting_effects"]["effect"] == "wave"
            assert_projection()

            down = await harness.space_pressed()
            assert down["reason"] == "exhibition_reset"
            assert harness.snapshot()["lighting_effects"]["effect"] == "breathing"
            assert harness.snapshot()["lighting_effects"]["scheduler"] == "manual_fake_clock"
            assert_projection()
        finally:
            harness.close()

    asyncio.run(scenario())
