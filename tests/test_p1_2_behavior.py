from datetime import datetime, timezone

from app.foundation.age_policy import age_details, policy_for
from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, FixedStoryManifest
from app.foundation.system_prompt import build_system_prompt, validate_response_plan


def _event(kind, seq, version, payload=None):
    return CanonicalEvent(kind, "p1-2", seq, version, "test", payload or {}, f"p1-2-{seq}", 100.0)


def test_age_policy_and_prompt_are_runtime_ready():
    details = age_details({"date_of_birth": "2020-10-12", "timezone": "UTC", "language": "ru-RU"}, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert details["age_years"] == 6 and details["age_band"] == "5-7" and details["age_band_source"] == "profile_dob"
    assert policy_for({"date_of_birth": "2020-10-12"}, now=datetime(2026, 10, 13, tzinfo=timezone.utc)).first_hint_after_seconds == 15
    prompt = build_system_prompt(profile={"display_name": "Миша", "date_of_birth": "2020-10-12"}, state={"mode": "bear_dialogue", "handset": "up"})
    assert "PROFILE_CONTEXT" in prompt and "safety_intent" in prompt and "UART" not in prompt
    plan = validate_response_plan({"intent": "free_dialogue", "confidence": 0.8, "requested_transition": "bear_dialogue"}, allowed_transitions={"bear_dialogue"})
    assert plan.confidence == 0.8


def test_story_emotional_reaction_keeps_playback_and_game_has_safety_escape():
    manifest = FixedStoryManifest("content/stories/fixed_manifest.json", ".")
    runtime = CanonicalRuntime(stories=manifest, allow_development_placeholders=True)
    assert runtime.handle(_event("AnonymousStart", 1, 0))["state"]["mode"] == "anonymous_visual"
    game_menu = runtime.handle(_event("ButtonPressed", 2, 1, {"button_id": 1}))
    assert game_menu["state"]["mode"] == "game_menu"
    active = runtime.handle(_event("ButtonPressed", 3, 2, {"button_id": 1}))
    safety = runtime.handle(_event("ButtonPressed", 4, active["state"]["state_version"], {"button_id": 5}))
    assert safety["state"]["mode"] == "wellbeing_check"

    runtime.reset("p1-2")
    runtime.handle(_event("AnonymousStart", 1, 0))
    runtime.handle(_event("ButtonPressed", 2, 1, {"button_id": 2}))
    runtime.handle(_event("ButtonPressed", 3, 2, {"button_id": 1}))
    started = runtime.handle(_event("HandsetLifted", 4, 3))
    assert started["state"]["mode"] == "fixed_story_playing"
    reaction = runtime.handle(_event("VoiceIntentReady", 5, started["state"]["state_version"], {"intent": "emotional_reaction", "safety_severity": "none"}))
    assert reaction["state"]["mode"] == "fixed_story_playing"
    assert reaction["effects"][0]["playback_continues"] is True
