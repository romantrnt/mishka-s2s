from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, VoiceManifest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.events import parse_event
from app.foundation.input_adapters import KeyboardHIDInputAdapter
import app.foundation.stand as stand_module


def canonical_event(kind: str, sequence: int, version: int, monotonic: float, source: str = "test") -> CanonicalEvent:
    return CanonicalEvent(kind, "gr01", sequence, version, source, {}, f"{source}-{sequence}", 100.0, monotonic)


def test_space_edges_are_explicit_and_autorepeat_is_ignored() -> None:
    received = []
    adapter = KeyboardHIDInputAdapter("/dev/null", received.append, debounce_ms=0)

    adapter.feed_key(adapter.KEY_SPACE, 1, timestamp=1.0)
    adapter.feed_key(adapter.KEY_SPACE, 2, timestamp=2.0)
    adapter.feed_key(adapter.KEY_SPACE, 1, timestamp=3.0)
    adapter.feed_key(adapter.KEY_SPACE, 0, timestamp=4.0)
    adapter.feed_key(adapter.KEY_SPACE, 0, timestamp=5.0)

    assert [event.type for event in received] == ["SpacePressed", "SpaceReleased"]
    assert parse_event({"type": "SpacePressed"}, "gr01", 1, "hid").event_type == "HandsetPlaced"
    assert parse_event({"type": "SpaceReleased"}, "gr01", 2, "hid").event_type == "HandsetLifted"


def test_canonical_exhibition_timer_is_monotonic_exact_and_cancelable() -> None:
    clock = [10.0]
    runtime = CanonicalRuntime(monotonic_now=lambda: clock[0])

    lifted = runtime.handle(canonical_event("HandsetLifted", 1, 0, 10.0))
    assert lifted["reason"] == "exhibition_handset_lifted"
    assert lifted["state"]["mode"] == "voice_welcome"
    assert lifted["state"]["welcome_t0_monotonic"] == 10.0
    assert lifted["state"]["welcome_deadline_monotonic"] == 15.0
    assert [effect["type"] for effect in lifted["effects"]] == ["lighting_wave_intent", "play_asset", "welcome_timer_scheduled"]

    clock[0] = 14.999
    early = runtime.handle(canonical_event("WelcomeTimerFired", 1, 1, 14.999, "timer"))
    assert early["reason"] == "welcome_not_due"
    assert early["effects"] == []

    clock[0] = 15.0
    due = runtime.handle(canonical_event("WelcomeTimerFired", 2, 1, 15.0, "timer"))
    assert due["reason"] == "welcome_started"
    assert [effect["phrase_id"] for effect in due["effects"]] == ["PHRASE_001"]
    assert due["effects"][0]["scheduled_deadline_monotonic"] == 15.0

    cancelled = CanonicalRuntime(monotonic_now=lambda: clock[0], exhibition_mode=True)
    cancelled.handle(canonical_event("HandsetLifted", 1, 0, 10.0))
    down = cancelled.handle(canonical_event("HandsetPlaced", 2, 1, 10.5))
    assert down["reason"] == "exhibition_reset"
    assert down["state"]["welcome_status"] == "idle"
    assert down["state"]["mode"] == "idle"
    no_welcome = cancelled.handle(canonical_event("WelcomeTimerFired", 1, 2, 15.0, "timer"))
    assert no_welcome["effects"] == []


def test_application_headless_path_schedules_one_welcome_and_new_lift_gets_new_deadline() -> None:
    async def scenario() -> None:
        clock = [20.0]
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now

        lifted = await app.receive_async(Event("HandsetLifted", "app-gr01", 1, 100.0, "headless", {}, "lift", 20.0))
        assert lifted["reason"] == "exhibition_handset_lifted"
        assert sum(effect.get("type") == "welcome_timer_scheduled" for effect in lifted["outputs"]) == 1
        assert not any(effect.get("type") == "speak" and effect.get("phrase_id") == "PHRASE_001" for effect in lifted["outputs"])
        assert app.last_canonical_event["source"] == "headless"

        clock[0] = 24.999
        assert await app.flush_welcome_timers() == []
        clock[0] = 25.0
        due = await app.flush_welcome_timers()
        assert len(due) == 1
        assert app.last_canonical_state["welcome_status"] == "started"
        assert any(effect.get("phrase_id") == "PHRASE_001" for effect in app.last_canonical_effects)

        await app.reset_session("app-gr01")

        clock[0] = 30.0
        lifted = await app.receive_async(Event("HandsetLifted", "app-gr01", 1, 110.0, "headless", {}, "lift-2", 30.0))
        assert lifted["canonical_state"]["welcome_deadline_monotonic"] == 35.0
        down = await app.receive_async(Event("HandsetPlaced", "app-gr01", 2, 111.0, "headless", {}, "down-2", 30.1))
        assert down["reason"] == "exhibition_reset"
        assert await app.flush_welcome_timers() == []
        await app.reset_session("app-gr01")

    asyncio.run(scenario())


def test_physical_launcher_starts_one_hid_reader_and_simulator_stays_headless(monkeypatch) -> None:
    class FakeReader:
        instances = 0
        def __init__(self, device_path, callback):
            type(self).instances += 1
            self.device_path = device_path
            self.callback = callback
            self.opened = False
        def open(self):
            self.opened = True
        def close(self):
            self.opened = False
        def is_ready(self):
            return self.opened

    monkeypatch.setattr(stand_module, "KeyboardHIDInputAdapter", FakeReader)

    async def scenario() -> None:
        physical = stand_module.HardwareIntegrationStand.__new__(stand_module.HardwareIntegrationStand)
        physical.hardware_mode = "physical"
        physical.settings = SimpleNamespace(hid_device="/dev/input/event-test")
        physical.input_adapter = None
        physical._physical_loop = None
        physical.lighting_serial = None
        await physical.start_hardware()
        await physical.start_hardware()
        assert FakeReader.instances == 1
        assert physical.input_adapter.opened is True
        await physical.stop_hardware()
        assert physical.input_adapter is None

        missing = stand_module.HardwareIntegrationStand.__new__(stand_module.HardwareIntegrationStand)
        missing.hardware_mode = "physical"
        missing.settings = SimpleNamespace(hid_device="")
        missing.input_adapter = None
        missing._physical_loop = None
        missing.lighting_serial = None
        missing.errors = []
        with pytest.raises(RuntimeError, match="MISHKA_HID_DEVICE"):
            await missing.start_hardware()
        assert missing._input_start_error["exception_type"] == "RuntimeError"
        assert missing.errors[-1]["stage"] == "hid_start"

        simulator = stand_module.HardwareIntegrationStand.__new__(stand_module.HardwareIntegrationStand)
        simulator.hardware_mode = "simulator"
        simulator.settings = SimpleNamespace(hid_device="")
        simulator.input_adapter = None
        simulator._physical_loop = None
        await simulator.start_hardware()
        assert simulator.input_adapter is None

    asyncio.run(scenario())


def test_initial_pressed_and_duplicate_edges_never_start_exhibition_audio() -> None:
    async def scenario() -> None:
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        initial = await app.receive_async(Event("HandsetPlaced", "edges", 1, 0.0, "hid", {}, "down", 0.0))
        assert initial["reason"] == "handset_already_placed"
        assert initial["outputs"] == []
        lifted = await app.receive_async(Event("HandsetLifted", "edges", 2, 1.0, "hid", {}, "up", 1.0))
        assert lifted["reason"] == "exhibition_handset_lifted"
        assert sum(item.get("type") == "lighting_wave_intent" for item in lifted["outputs"]) == 1
        placed = await app.receive_async(Event("HandsetPlaced", "edges", 3, 2.0, "hid", {}, "down-2", 2.0))
        assert placed["reason"] == "exhibition_reset"
        assert not any(item.get("type") == "speak" for item in placed["outputs"])
        assert not any(item.get("type") == "lighting_wave_intent" for item in placed["outputs"])

    asyncio.run(scenario())


def test_full_exhibition_trace_has_one_wave_one_timer_and_phrase_at_deadline() -> None:
    async def scenario() -> None:
        clock = [100.0]
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        event = parse_event(
            {"type": "handset_up", "_input_type": "SpaceReleased", "_normalized_event": "handset_up"},
            "trace", 1, "headless_mock",
        )
        lifted = await app.receive_async(event)
        assert lifted["reason"] == "exhibition_handset_lifted"
        assert [item["type"] for item in lifted["outputs"]] == ["lighting_wave_intent", "play_asset", "welcome_timer_scheduled"]
        assert lifted["canonical_state"]["welcome_t0_monotonic"] == 100.0
        assert lifted["canonical_state"]["welcome_deadline_monotonic"] == 105.0
        assert not any(item.get("type") == "speak" and item.get("phrase_id") == "PHRASE_001" for item in lifted["outputs"])

        clock[0] = 104.999
        assert await app.flush_welcome_timers() == []
        clock[0] = 105.0
        fired = await app.flush_welcome_timers()
        assert len(fired) == 1
        assert [item.get("phrase_id") for item in fired[0]["outputs"] if item.get("type") == "speak"] == ["PHRASE_001"]
        assert await app.flush_welcome_timers() == []
        trace = app.canonical_trace
        assert trace[0]["input"] == "SpaceReleased"
        assert trace[0]["normalized_event"] == "handset_up"
        assert trace[0]["canonical_event"] == "HandsetLifted"
        assert trace[0]["state_before"] == "idle"
        assert trace[0]["state_after"] == "voice_welcome"
        assert trace[0]["effects"] == ["lighting_wave_intent", "play_asset", "welcome_timer_scheduled"]
        assert trace[-1]["canonical_event"] == "WelcomeTimerFired"
        assert trace[-1]["audio"] == "PHRASE_001"

    asyncio.run(scenario())


def test_timer_boundary_races_and_audio_failure_are_terminal() -> None:
    async def scenario() -> None:
        clock = [0.0]
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        calls = []

        class FailingAudio:
            def play_owned(self, handle, asset_id, offset_ms=0):
                calls.append((handle.response_id, asset_id, handle.token))
                raise OSError("audio device unavailable")
            def cancel(self, token):
                pass

        app.audio = FailingAudio()
        await app.receive_async(Event("HandsetLifted", "failure", 1, 0.0, "test", {}, "lift", 0.0))
        clock[0] = 4.999
        assert await app.flush_welcome_timers() == []
        clock[0] = 5.0
        due = await app.flush_welcome_timers()
        assert len(due) == 1
        assert [call[0] for call in calls] == ["RINGTONE", "PHRASE_001"]
        assert due[0]["outputs"][0]["resolution"] == "audio_dispatch_failed"
        assert await app.flush_welcome_timers() == []
        assert app.canonical_diagnostics[-1]["kind"] == "audio_dispatch_failed"

        # Down at the exact deadline wins if accepted first; no later timer can replay.
        race = build_application(settings=Settings(profile="mock", content_root="content"))[0]
        race.monotonic_now = lambda: 0.0
        race.canonical_runtime.monotonic_now = race.monotonic_now
        await race.receive_async(Event("HandsetLifted", "race", 1, 0.0, "test", {}, "lift", 0.0))
        down = await race.receive_async(Event("HandsetPlaced", "race", 2, 5.0, "test", {}, "down", 5.0))
        assert down["reason"] == "exhibition_reset"
        assert await race.flush_welcome_timers() == []

        # lift -> cancel -> lift: only the second deadline is live.
        clock2 = [10.0]
        cycle = build_application(settings=Settings(profile="mock", content_root="content"))[0]
        cycle.monotonic_now = lambda: clock2[0]
        cycle.canonical_runtime.monotonic_now = cycle.monotonic_now
        await cycle.receive_async(Event("HandsetLifted", "cycle", 1, 0.0, "test", {}, "lift-1", 10.0))
        clock2[0] = 11.0
        await cycle.receive_async(Event("HandsetPlaced", "cycle", 2, 1.0, "test", {}, "down-1", 11.0))
        clock2[0] = 20.0
        second = await cycle.receive_async(Event("HandsetLifted", "cycle", 3, 2.0, "test", {}, "lift-2", 20.0))
        assert second["canonical_state"]["welcome_deadline_monotonic"] == 25.0
        clock2[0] = 24.999
        assert await cycle.flush_welcome_timers() == []
        clock2[0] = 25.0
        assert len(await cycle.flush_welcome_timers()) == 1

    asyncio.run(scenario())


def test_phrase_001_manifest_is_real_valid_and_resolves_in_mock_policy() -> None:
    manifest_path = Path("content/audio/voice_manifest.json")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in document["assets"] if item["phrase_id"] == "PHRASE_001")
    asset_path = manifest_path.parent / entry["asset_path"]
    assert entry["transcript"] == "Привет, я Мишка!"
    assert asset_path.is_file()
    assert hashlib.sha256(asset_path.read_bytes()).hexdigest() == entry["asset_sha256"]
    inspected = VoiceManifest(manifest_path, allow_development_assets=True).resolve("PHRASE_001")
    assert inspected is not None
    assert inspected["asset_path"] == "voice/PHRASE_001.wav"
    assert inspected["validation_status"] == "valid"


def test_clean_process_mock_hid_reaches_canonical_timer_schedule() -> None:
    code = """
import asyncio, json
from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand
async def main():
    stand = HardwareIntegrationStand(Settings(profile='mock', hardware_mode='simulator', content_root='content'))
    # Test-only control admission: this unit harness has no lifecycle warmup.
    stand._admits_physical_users = lambda: True
    try:
        result = await stand.inject_physical({'type': 'SpaceReleased', '_source': 'clean_process_mock_hid', '_sequence': 1})
        print(json.dumps({'reason': result['reason'], 'timer': any(item.get('type') == 'welcome_timer_scheduled' for item in result['outputs'])}))
    finally:
        stand.close()
asyncio.run(main())
"""
    completed = subprocess.run([sys.executable, "-c", code], cwd=Path.cwd(), text=True, capture_output=True, timeout=20, check=True)
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload == {"reason": "exhibition_handset_lifted", "timer": True}


def test_missing_phrase_asset_is_visual_only_and_diagnostic() -> None:
    async def scenario() -> None:
        clock = [0.0]
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        app.voice_manifest.resolve = lambda *_args: None
        await app.receive_async(Event("HandsetLifted", "missing", 1, 0.0, "test", {}, "lift", 0.0))
        clock[0] = 5.0
        fired = await app.flush_welcome_timers()
        speak = next(item for item in fired[0]["outputs"] if item.get("type") == "speak")
        assert speak["resolution"] == "recorded_phrase_unavailable_visual_only"
        assert any(item["kind"] == "recorded_phrase_unavailable" for item in app.canonical_diagnostics)
        assert not app._active_audio, "failed welcome must also release the ringtone lease"

    asyncio.run(scenario())


@pytest.mark.parametrize("elapsed, expected", [(0.0, "welcome_not_due"), (4.0, "welcome_not_due"), (4.999999, "welcome_not_due"), (5.0, "welcome_started"), (5.000001, "welcome_started")])
def test_welcome_user_five_second_boundary_independent_of_constant(elapsed, expected):
    """Literal user contract: no imported production delay can mask a regression."""
    runtime = CanonicalRuntime(monotonic_now=lambda: 100.0)
    lifted = runtime.handle(canonical_event("HandsetLifted", 1, 0, 100.0))
    assert lifted["state"]["welcome_deadline_monotonic"] == 105.0
    scheduled = next(item for item in lifted["effects"] if item["type"] == "welcome_timer_scheduled")
    assert scheduled["delay_seconds"] == 5.0
    result = runtime.handle(canonical_event("WelcomeTimerFired", 1, 1, 100.0 + elapsed, "timer"))
    assert result["reason"] == expected
    spoken = [item["phrase_id"] for item in result["effects"] if item["type"] == "speak"]
    assert spoken == (["PHRASE_001"] if elapsed >= 5.0 else [])
