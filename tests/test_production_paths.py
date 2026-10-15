from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.lighting import (
    EXHIBITION_DIGIT_WAVE_GROUPS,
    BUTTON_DIGIT_ELEMENTS,
    EXHIBITION_WAVE_GROUPS,
    ExhibitionLightingEngine,
    FakeLightingOutput,
)


def _event(kind, seq, version, payload=None, source="test", monotonic=10.0):
    return CanonicalEvent(
        kind, "production-path", seq, version, source, payload or {},
        f"{source}-{seq}", 100.0, monotonic,
    )


def test_age_wave_has_full_button_phase_then_digit_hold():
    output = FakeLightingOutput()
    engine = ExhibitionLightingEngine(output, monotonic_now=lambda: 0.0, background_scheduler=False)
    engine.start_wave(
        now=0.0, groups=EXHIBITION_WAVE_GROUPS,
        follow_up_groups=EXHIBITION_DIGIT_WAVE_GROUPS, hold_on_complete=True,
    )
    assert len(EXHIBITION_WAVE_GROUPS) == 7
    assert engine.snapshot()["last_frame"]["active_elements"] == sorted(EXHIBITION_WAVE_GROUPS[0])
    engine.advance(0.84, force=True)
    assert engine.snapshot()["wave_phase"] == "follow_up"
    engine.advance(1.68, force=True)
    assert engine.snapshot()["effect"] == "wave_hold"
    assert engine.snapshot()["last_frame"]["active_elements"] == sorted(BUTTON_DIGIT_ELEMENTS[button_id][1] for button_id in range(1, 8))


def test_closing_wave_turns_every_channel_off():
    output = FakeLightingOutput()
    engine = ExhibitionLightingEngine(output, monotonic_now=lambda: 0.0, background_scheduler=False)
    engine.start_wave(now=0.0, hold_on_complete=False)
    engine.advance(0.84, force=True)
    assert engine.snapshot()["effect"] == "idle"
    assert all(value == 0 for value in output.snapshot().values())


def test_exhibition_session_keeps_onboarding_memory_and_closing_wave():
    runtime = CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0)
    runtime.handle(_event("HandsetLifted", 1, 0, source="hid"))
    timer = runtime.handle(_event("WelcomeTimerFired", 2, 1, source="timer", monotonic=15.0))
    assert [item["type"] for item in timer["effects"]] == ["speak"]
    for seq, phrase in ((3, "PHRASE_001"), (4, "PHRASE_078"), (5, "PHRASE_099")):
        state = runtime.sessions["production-path"]
        runtime.handle(_event("AudioCompleted", seq, state.state_version, {"response_id": phrase}, source="audio"))
    state = runtime.sessions["production-path"]
    runtime.handle(
        _event("VoiceIntentReady", 6, state.state_version, {"intent": "free_speech", "safety_severity": "none"}, source="speech"),
        transient_text="Меня зовут Аня",
    )
    state = runtime.sessions["production-path"]
    runtime.handle(_event("AudioCompleted", 7, state.state_version, {"response_id": "PHRASE_108"}, source="audio"))
    state = runtime.sessions["production-path"]
    runtime.handle(_event("AudioCompleted", 8, state.state_version, {"response_id": "PHRASE_075"}, source="audio"))
    state = runtime.sessions["production-path"]
    selected = runtime.handle(_event("ButtonPressed", 9, state.state_version, {"button_id": 2}, source="hid"))
    assert any(item["kind"] == "name" and item["value"] == "Аня" for item in state.dialogue_memory.bounded_turns)
    assert any(item["kind"] == "age_selection" and item["button"] == 2 for item in state.dialogue_memory.bounded_turns)
    assert any(item["type"] == "lighting_select_button" and item["button"] == 2 for item in selected["effects"])
    state = runtime.sessions["production-path"]
    closing = runtime.handle(_event("AudioCompleted", 10, state.state_version, {"response_id": "PHRASE_107"}, source="audio"))
    wave = closing["effects"][0]
    assert wave["type"] == "lighting_wave_intent"
    assert wave["hold_on_complete"] is False
