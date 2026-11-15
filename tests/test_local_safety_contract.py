from __future__ import annotations

import inspect

import pytest

from app.foundation.canonical_contract import ALLOWED_EVENTS, CanonicalEvent, CanonicalRuntime, STATE_NAMES
from app.foundation.domain import EVENT_TYPES
from app.foundation.events import EventValidationError, parse_event
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer, load_canonical_scenario_document


def send(runtime, kind, payload=None, **authority):
    state = runtime.sessions["local-safety"]
    sequence = max(state.last_sequence_by_source.values(), default=0) + 1
    event = CanonicalEvent(kind, state.session_id, sequence, state.state_version,
                           "test", payload or {}, f"local-{sequence}", float(sequence), 10.0)
    return runtime.handle(event, **authority)


def active_runtime(*, exhibition=False, optional_identity=False, handset="up"):
    runtime = CanonicalRuntime(exhibition_mode=exhibition, optional_identity=optional_identity)
    state = runtime._new_state("local-safety", {"profile_ref": "private-profile"})
    state.handset = handset
    state.mode = "bear_dialogue"
    state.active_audio_id = "PHRASE_095"
    state.playback_lease = "old-scene-lease"
    state.pending_tool = "weather"
    state.pending_mode = "game_menu"
    state.welcome_status = "pending"
    state.welcome_deadline_monotonic = 15.0
    state.dialogue_memory.bounded_turns = [{"intent": "question"}]
    runtime.sessions[state.session_id] = state
    return runtime


@pytest.mark.parametrize("exhibition", [False, True])
@pytest.mark.parametrize("optional_identity", [False, True])
def test_urgent_local_stop_cancels_scene_and_guides_once(exhibition, optional_identity):
    runtime = active_runtime(exhibition=exhibition, optional_identity=optional_identity)
    result = send(runtime, "VoiceIntentReady", {"intent": "free_speech", "safety_severity": "urgent"})
    assert result["reason"] == "urgent_safety"
    assert result["state"]["mode"] == "safety_stop"
    assert result["state"]["safety_severity"] == "urgent"
    assert result["state"]["expected_inputs"] == ["handset_down", "reset"]
    assert [effect["type"] for effect in result["effects"]] == ["audio_stop", "capture_close", "lighting_preset", "speak"]
    assert result["effects"][-1]["phrase_id"] == "PHRASE_091"
    assert result["effects"][2]["preset"] == "SAFE_STOP"
    assert result["state"]["playback_lease"] != "old-scene-lease"
    assert result["state"]["pending_tool"] is None
    assert result["state"]["pending_mode"] is None
    assert result["state"]["welcome_deadline_monotonic"] is None
    assert runtime.sessions["local-safety"].dialogue_memory.bounded_turns == []
    for kind, payload, authority in [
        ("ButtonPressed", {"button_id": 5}, {}),
        ("ButtonReleased", {"button_id": 5}, {}),
        ("ButtonHeld", {"button_id": 7}, {}),
        ("VoiceIntentReady", {"intent": "help_request", "safety_severity": "concern"}, {}),
        ("VoiceIntentReady", {"intent": "free_speech", "safety_severity": "urgent"}, {}),
        ("SpeechStarted", {}, {}),
        ("TimeoutFired", {}, {}),
        ("WelcomeTimerFired", {}, {}),
        ("GameTimer", {}, {"game_timer_authorized": True}),
        ("AudioCompleted", {"response_id": "PHRASE_095", "playback_lease": "old-scene-lease"}, {}),
    ]:
        ignored = send(runtime, kind, payload, **authority)
        assert ignored["effects"] == []
        assert ignored["state"] == result["state"]


def test_help_request_and_wellbeing_yes_use_same_local_contract():
    for kind, payload in [
        ("VoiceIntentReady", {"intent": "help_request", "safety_severity": "none"}),
        ("VoiceIntentReady", {"intent": "yes", "safety_severity": "none"}),
        ("ButtonPressed", {"button_id": 7}),
    ]:
        runtime = active_runtime()
        runtime.sessions["local-safety"].mode = "wellbeing_check"
        stopped = send(runtime, kind, payload)
        assert stopped["state"]["mode"] == "safety_stop"
        assert stopped["state"]["safety_severity"] == "concern"
        assert [effect["phrase_id"] for effect in stopped["effects"] if effect["type"] == "speak"] == ["PHRASE_091"]
        raised = send(runtime, "VoiceIntentReady", {"intent": "free_speech", "safety_severity": "urgent"})
        assert raised["state"]["safety_severity"] == "urgent"
        assert raised["state"]["state_version"] == stopped["state"]["state_version"] + 1
        assert raised["effects"] == []
        repeated = send(runtime, "VoiceIntentReady", {"safety_severity": "urgent"})
        assert repeated["state"] == raised["state"]
        assert repeated["effects"] == []


@pytest.mark.parametrize("mode", ["bear_dialogue", "exhibition_name_capture", "exhibition_age_selection", "personal_story_theme", "story_speech_analysis"])
def test_concern_is_nonterminal_local_support(mode):
    runtime = active_runtime()
    runtime.sessions["local-safety"].mode = mode
    result = send(runtime, "VoiceIntentReady", {"intent": "pain_or_discomfort", "safety_severity": "concern"})
    assert result["state"]["mode"] == "support_intake"
    assert result["state"]["safety_severity"] == "concern"
    assert [effect["type"] for effect in result["effects"]] == ["audio_stop", "lighting_preset", "support_response"]
    assert result["effects"][1]["preset"] == "SUPPORT"
    assert result["effects"][-1]["phrase_id"] == "PHRASE_071"


@pytest.mark.parametrize("exhibition", [False, True])
@pytest.mark.parametrize("kind", ["HandsetPlaced", "SessionReset", "SessionCancel"])
@pytest.mark.parametrize("optional_identity", [False, True])
def test_local_stop_lifecycle_finishes_without_adult_resolution(exhibition, kind, optional_identity):
    runtime = active_runtime(exhibition=exhibition, optional_identity=optional_identity)
    stopped = send(runtime, "VoiceIntentReady", {"safety_severity": "urgent"})
    guide_lease = stopped["state"]["playback_lease"]
    finished = send(runtime, kind)
    assert finished["reset_session"] is True
    assert finished["state"]["mode"] == ("idle" if kind == "HandsetPlaced" else "session_closed")
    assert finished["state"]["handset"] == ("down" if kind == "HandsetPlaced" else "up")
    assert finished["state"]["profile_ref"] is None
    assert finished["state"]["safety_severity"] == "none"
    assert finished["state"]["active_audio_id"] is None
    assert finished["state"]["playback_lease"] is None
    assert finished["state"]["welcome_deadline_monotonic"] is None
    assert [effect["type"] for effect in finished["effects"]] == [
        "audio_stop", "capture_close", "welcome_timer_cancelled",
        "exhibition_effects_cancelled", "identity_cleared", "lighting_dark",
    ]
    late = send(runtime, "AudioCompleted", {"response_id": "PHRASE_091", "playback_lease": guide_lease})
    assert not any(effect["type"] == "speak" for effect in late["effects"])
    assert late["state"]["mode"] == finished["state"]["mode"]


@pytest.mark.parametrize("mode", ["idle", "bear_dialogue", "session_closed", "soft_end"])
def test_hardware_fault_stops_without_clinical_severity_or_guidance(mode):
    runtime = active_runtime(handset="down")
    runtime.sessions["local-safety"].mode = mode
    stopped = send(runtime, "HardwareDisconnected")
    assert stopped["state"]["mode"] == "safety_stop"
    assert stopped["state"]["safety_severity"] == "none"
    assert [effect["type"] for effect in stopped["effects"]] == ["audio_stop", "capture_close", "lighting_preset"]
    closed = send(runtime, "HandsetPlaced")
    assert closed["state"]["mode"] == "idle"
    assert closed["reset_session"] is True
    # The physical fault latch belongs to the stand, not the scenario reset.
    assert not any(effect["type"] == "hardware_reconnected" for effect in closed["effects"])


def test_guidance_callbacks_are_owned_and_do_not_repeat_voice():
    runtime = active_runtime()
    stopped = send(runtime, "VoiceIntentReady", {"intent": "help_request"})
    payload = {"response_id": "PHRASE_091", "playback_lease": stopped["state"]["playback_lease"]}
    started = send(runtime, "AudioStarted", payload)
    assert started["state"]["audio_status"] == "playing"
    completed = send(runtime, "AudioCompleted", payload)
    assert completed["state"]["mode"] == "safety_stop"
    assert completed["state"]["audio_status"] == "idle"
    assert completed["state"]["active_audio_id"] is None
    assert completed["effects"] == []
    duplicate = send(runtime, "AudioCompleted", payload)
    assert duplicate["state"] == completed["state"]
    assert duplicate["effects"] == []


def test_removed_delivery_contract_is_not_an_ingress_or_graph_capability():
    for name in ("AdultHandoffAcknowledged", "AdultHandoffFailed", "AdultResolved"):
        assert name not in ALLOWED_EVENTS | EVENT_TYPES
        with pytest.raises(EventValidationError, match="unknown event"):
            parse_event({"type": name}, "local-safety", 1)
    assert "incident_id" not in active_runtime().sessions["local-safety"].snapshot()
    assert not any("handoff" in field or "adult_resolution" in field for field in inspect.signature(CanonicalRuntime.handle).parameters)
    graph = CanonicalLangGraphAuthorizer()
    for mode in STATE_NAMES:
        assert graph.authorize(mode, "safety_stop", "VoiceIntentReady", safety_level="urgent")["ok"]
        assert graph.authorize(mode, "safety_stop", "HardwareDisconnected")["ok"]
    assert graph.authorize("safety_stop", "idle", "HandsetPlaced")["ok"]
    for mode in STATE_NAMES - {"safety_stop"}:
        assert not graph.authorize("safety_stop", mode, "ButtonPressed")["ok"]
    assert not graph.authorize_model_action("safety_stop", "led", "off")["ok"]
    assert not graph.authorize_model_action("safety_stop", "return_to_scenario", "root_menu")["ok"]
    assert set(load_canonical_scenario_document()["states"]) == set(STATE_NAMES)


@pytest.mark.asyncio
async def test_concern_cancels_story_wave_before_support_recording():
    from app.foundation.api import build_application
    from app.foundation.canonical_services import LED_PRESETS
    from app.foundation.config import Settings
    from app.foundation.domain import Event
    from app.foundation.stand import UARTLEDController

    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    sid = "concern-story-wave"
    led = UARTLEDController(None, background_scheduler=False)
    app.led = led
    state = app.canonical_runtime._new_state(sid, None)
    state.handset = "up"
    state.mode = "fixed_story_playing"
    state.active_story_id = "fixed_aibolit_v1"
    state.active_audio_id = "fixed_aibolit_v1"
    state.playback_lease = "old-story-lease"
    state.dialogue_memory.story_checkpoint = {"story_id": "fixed_aibolit_v1", "offset_ms": 1200}
    state.pending_tool = "weather"
    state.pending_mode = "fixed_story_menu"
    state.welcome_status = "pending"
    state.welcome_deadline_monotonic = 50.0
    app.canonical_runtime.sessions[sid] = state
    led.start_wave(now=0.0, loop=True)
    assert led.effect_snapshot()["active"]
    old_generation = led.effect_snapshot()["generation"]
    at_support_play = []
    original_play = app.audio.play_owned

    def play(handle, asset, **kwargs):
        if handle.response_id == "PHRASE_071":
            at_support_play.append((led.effect_snapshot(), led.output.snapshot()))
        return original_play(handle, asset, **kwargs)

    app.audio.play_owned = play
    try:
        result = await app.receive_async(Event("SpeechReceived", sid, 1, 1.0, "test", {}, "concern"), "Мне страшно")
        assert result["canonical_state"]["mode"] == "support_intake"
        assert result["canonical_state"]["safety_severity"] == "concern"
        assert not led.effect_snapshot()["active"], "old story loop must be cancelled"
        assert led.effect_snapshot()["generation"] > old_generation
        assert at_support_play, "the concern response remains available"
        expected_frame = {str(element): 0 for element in range(18)}
        expected_frame.update({str(element): value for element, value in LED_PRESETS["SUPPORT"]})
        assert at_support_play[0][0]["active"] is False
        assert at_support_play[0][1] == expected_frame
        current = app.canonical_runtime.sessions[sid]
        assert current.active_story_id is None
        assert current.active_game_id is None
        assert current.game_engine_state is None
        assert current.dialogue_memory.story_checkpoint is None
        assert current.pending_tool is None and current.pending_mode is None
        assert current.welcome_deadline_monotonic is None
        # The old story timer cannot re-arm its wave over local support.
        led.advance_effects(now=100.0, force=True)
        assert led.output.snapshot() == expected_frame
    finally:
        await app._cleanup_session_resources(sid)
        await app.shutdown_identity()
