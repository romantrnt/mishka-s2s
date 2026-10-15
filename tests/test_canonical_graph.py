from __future__ import annotations

import asyncio

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer


def test_compiled_langgraph_authorizes_only_allowlisted_transition() -> None:
    graph = CanonicalLangGraphAuthorizer()
    assert graph.status()["ready"] is True
    assert graph.authorize("idle", "game_menu", "ButtonPressed")["ok"] is True
    assert graph.authorize("game_menu", "safety_handoff", "ButtonPressed")["ok"] is False


def test_langgraph_allows_headless_onboarding_voice_and_tutorial_skip() -> None:
    graph = CanonicalLangGraphAuthorizer()
    assert graph.authorize("exhibition_age_selection", "exhibition_age_complete", "VoiceIntentReady")["ok"] is True
    for mode in (
        "exhibition_menu_tutorial_intro", "exhibition_menu_tutorial_prompt",
        "exhibition_menu_tutorial_buttons", "exhibition_menu_wave",
        "exhibition_menu_repeat_prompt", "exhibition_menu_repeat_listen",
        "exhibition_menu_replay_buttons",
    ):
        assert graph.authorize(mode, "menu_ready", "ButtonPressed")["ok"] is True


def test_headless_audio_gate_includes_spoken_age_selection() -> None:
    from app.foundation.stand import HEADLESS_AUDIO_NODES

    assert "exhibition_age_selection" in HEADLESS_AUDIO_NODES


def test_tutorial_skip_and_handset_down_survive_stuck_audio_cleanup() -> None:
    app, _, _, _ = build_application(
        settings=Settings(profile="mock", runtime_mode="exhibition", content_root="content"),
        background_scheduler=False,
    )

    class HangingAudioOwner:
        async def cancel_session(self, _session_id: str) -> None:
            await asyncio.sleep(60)

    async def scenario() -> None:
        app.audio_cleanup_timeout_seconds = 0.01
        app.bounded_s2s = HangingAudioOwner()
        session_id = "stuck-audio-skip"
        state = app.canonical_runtime._new_state(session_id, None)
        state.handset = "up"
        state.mode = "exhibition_menu_tutorial_buttons"
        state.substate = "explaining_button_1"
        state.state_version = 1
        app.canonical_runtime.sessions[session_id] = state
        skipped = await asyncio.wait_for(app.receive_async(Event(
            "ButtonPressed", session_id, 1, 0.0, "hid", {"button_id": 4}, "skip", 0.0,
        )), 0.5)
        assert skipped["reason"] == "exhibition_menu_tutorial_skipped"
        assert skipped["state"] == "menu_ready"
        down = await asyncio.wait_for(app.receive_async(Event(
            "HandsetPlaced", session_id, 2, 0.1, "hid", {}, "down", 0.1,
        )), 0.5)
        assert down["reason"] == "exhibition_reset"
        assert down["state"] == "idle"
        assert any(item["kind"] == "audio_owner_cleanup_timeout" for item in app.audio_runtime_diagnostics)

    asyncio.run(scenario())


def test_exhibition_application_ignores_buttons_before_handset_lift() -> None:
    app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition", content_root="content"))

    async def scenario() -> None:
        first = await app.receive_async(Event(
            "ButtonPressed", "graph-test", 1, 0.0, "test", {"button_id": 1}, "one",
        ))
        assert first["state"] == "idle"
        assert first["reason"] == "exhibition_age_not_ready"
        assert first["outputs"] == []
        second = await app.receive_async(Event(
            "ButtonPressed", "graph-test", 2, 0.0, "test", {"button_id": 1}, "two",
        ))
        assert second["state"] == "idle"
        assert second["reason"] == "exhibition_age_not_ready"
        assert second["outputs"] == []
        assert app.sessions.get("graph-test") is None
        assert "graph-test" not in app.canonical_runtime.sessions

    asyncio.run(scenario())



def test_unknown_rfid_stays_anonymous_and_safety_handoff_survives_removal() -> None:
    from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime

    runtime = CanonicalRuntime()
    def event(kind: str, sequence: int, version: int, payload=None):
        return CanonicalEvent(kind, "unknown", sequence, version, "test", payload or {}, str(sequence), float(sequence))

    unknown = runtime.handle(event("RfidPresented", 1, 0, {"tag_id": "unresolved"}), None)
    assert unknown["reason"] == "unknown_tag"
    assert unknown["state"]["mode"] == "anonymous_visual"
    runtime.handle(event("HandsetLifted", 2, 1), None)
    handoff = runtime.handle(event("VoiceIntentReady", 3, 2, {"intent": "request_adult", "safety_severity": "none"}), None)
    assert handoff["state"]["mode"] == "safety_handoff"
    removed = runtime.handle(event("RfidRemoved", 4, 3), None)
    assert removed["state"]["mode"] == "safety_handoff"


def test_story_validator_rejects_ip_and_invalid_schema_before_playback() -> None:
    from app.foundation.canonical_contract import validate_generated_story

    brief = {"age_band": "3-5", "theme": "dinosaurs"}
    unsafe = {"title": "Айболит", "text": "Короткая сказка", "age_band": "3-5", "estimated_duration_sec": 70, "safety_tags": [], "ending_state": "safe_resolved"}
    malformed = {"title": "Добрая история", "text": "Ок", "age_band": "3-5", "estimated_duration_sec": True, "safety_tags": "safe", "ending_state": "safe_resolved"}
    assert validate_generated_story(unsafe, brief)[0] is False
    assert validate_generated_story(malformed, brief)[0] is False

def test_application_accepts_first_rfid_and_deduplicates_repeated_tag() -> None:
    app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical", content_root="content"))

    async def scenario() -> None:
        first = await app.receive_async(Event(
            "RfidPresented", "rfid-test", 1, 0.0, "test",
            {"tag_id": "mock-child-001"}, "rfid-one",
        ))
        assert first["reason"] == "rfid_identified"
        assert first["state"] == "visual_welcome"
        assert any(output.get("action") == "led_pattern" and output.get("pattern") == "rfid_wave" for output in first["outputs"])

        repeated = await app.receive_async(Event(
            "RfidPresented", "rfid-test", 2, 1.0, "test",
            {"tag_id": "mock-child-001"}, "rfid-two",
        ))
        assert repeated["reason"] == "rfid_repeated"
        assert repeated["state"] == "visual_welcome"
        assert not repeated["outputs"]

    asyncio.run(scenario())


def test_handset_lift_from_visual_welcome_opens_current_voice_state_and_blocks_missing_phrase_honestly() -> None:
    app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical", content_root="content"))

    async def scenario() -> None:
        first = await app.receive_async(Event(
            "RfidPresented", "handset-test", 1, 0.0, "test",
            {"tag_id": "mock-child-001"}, "rfid-one",
        ))
        assert first["state"] == "visual_welcome"
        assert first["canonical_state"]["expected_inputs"] == ["handset_up"]
        assert all("unknown" not in str(output.get("effect_id", "")) for output in first["outputs"])

        lifted = await app.receive_async(Event(
            "HandsetLifted", "handset-test", 2, 1.0, "test", {}, "handset-one",
        ))
        assert lifted["reason"] == "automatic_mood_check"
        assert lifted["state"] == "automatic_mood_check"
        assert lifted["canonical_state"]["handset"] == "up"
        assert lifted["canonical_state"]["expected_inputs"] == ["button", "speech", "handset_down"]
        speak = next(output for output in lifted["outputs"] if output.get("type") == "speak")
        assert speak.get("resolution") in {None, "recorded_phrase_unavailable_visual_only"}

    asyncio.run(scenario())


def test_current_exhibition_onboarding_contract_reaches_age_completion():
    from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
    runtime = CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0)

    def event(kind, sequence, version, source="test", payload=None, monotonic=10.0):
        return CanonicalEvent(kind, "current-onboarding", sequence, version, source, payload or {}, f"{source}-{sequence}", 100.0, monotonic)

    assert runtime.handle(event("HandsetLifted", 1, 0))["reason"] == "exhibition_handset_lifted"
    assert runtime.handle(event("WelcomeTimerFired", 2, 1, "timer", monotonic=15.0))["reason"] == "welcome_started"
    started = runtime.handle(event("AudioCompleted", 3, 2, "audio", {"response_id": "PHRASE_001"}))
    assert [x["phrase_id"] for x in started["effects"] if x["type"] == "speak"] == ["PHRASE_078"]
    state = runtime.sessions["current-onboarding"]
    started = runtime.handle(event("AudioCompleted", 4, state.state_version, "audio", {"response_id": "PHRASE_078"}))
    assert [x["phrase_id"] for x in started["effects"] if x["type"] == "speak"] == ["PHRASE_099"]
    state = runtime.sessions["current-onboarding"]
    assert runtime.handle(event("AudioCompleted", 5, state.state_version, "audio", {"response_id": "PHRASE_099"}))["state"]["mode"] == "exhibition_name_capture"
    state = runtime.sessions["current-onboarding"]
    retry = runtime.handle(event("VoiceIntentReady", 6, state.state_version, "speech"), transient_text="")
    assert retry["reason"] == "exhibition_name_unrecognized"
    state = runtime.sessions["current-onboarding"]
    age = runtime.handle(event("VoiceIntentReady", 7, state.state_version, "speech"), transient_text="Меня зовут Аня")
    assert [x["phrase_id"] for x in age["effects"] if x["type"] == "speak"] == ["PHRASE_108"]
    state = runtime.sessions["current-onboarding"]
    hint = runtime.handle(event("AudioCompleted", 8, state.state_version, "audio", {"response_id": "PHRASE_108"}))
    assert [x["phrase_id"] for x in hint["effects"] if x["type"] == "speak"] == ["PHRASE_075"]
    state = runtime.sessions["current-onboarding"]
    digits = runtime.handle(event("AudioCompleted", 9, state.state_version, "audio", {"response_id": "PHRASE_075"}))
    assert digits["state"]["mode"] == "exhibition_age_selection"
    assert any(x.get("pattern") == "age_digits_wave" for x in digits["effects"] if x["type"] == "lighting_wave_intent")
    state = runtime.sessions["current-onboarding"]
    selected = runtime.handle(event("ButtonPressed", 10, state.state_version, "hid", {"button_id": 2}))
    assert selected["state"]["mode"] == "exhibition_age_complete"
    assert [x["phrase_id"] for x in selected["effects"] if x["type"] == "speak"] == ["PHRASE_107"]
    state = runtime.sessions["current-onboarding"]
    ready = runtime.handle(event("AudioCompleted", 11, state.state_version, "audio", {"response_id": "PHRASE_107"}))
    assert ready["reason"] == "exhibition_onboarding_ready"
