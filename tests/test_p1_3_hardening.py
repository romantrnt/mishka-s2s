from pathlib import Path

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, FixedStoryManifest
from app.foundation.led_contract import canonical_led_state_from_lighting
from app.safety.incidents import IncidentLogger


def _event(kind, seq, version, payload=None):
    return CanonicalEvent(kind, "p1-3", seq, version, "test", payload or {}, f"p1-3-{seq}", 100.0)


def test_story_analysis_preserves_confirmed_controls_and_completion():
    runtime = CanonicalRuntime(
        stories=FixedStoryManifest("content/stories/fixed_manifest.json", "."),
        allow_development_placeholders=True,
    )
    runtime.handle(_event("AnonymousStart", 1, 0))
    runtime.handle(_event("ButtonPressed", 2, 1, {"button_id": 2}))
    runtime.handle(_event("ButtonPressed", 3, 2, {"button_id": 1}))
    started = runtime.handle(_event("HandsetLifted", 4, 3))
    assert started["state"]["mode"] == "fixed_story_playing"
    unknown = runtime.handle(_event("VoiceIntentReady", 5, started["state"]["state_version"], {"intent": "free_speech", "safety_severity": "none"}))
    assert unknown["state"]["mode"] == "story_speech_analysis"
    paused = runtime.handle(_event("VoiceIntentReady", 6, unknown["state"]["state_version"], {"intent": "pause_story", "safety_severity": "none"}))
    assert paused["state"]["mode"] == "story_paused"

    resumed = runtime.handle(_event("VoiceIntentReady", 7, paused["state"]["state_version"], {"intent": "resume_story", "safety_severity": "none"}))
    assert resumed["state"]["mode"] == "fixed_story_playing"
    analysis = runtime.handle(_event("VoiceIntentReady", 8, resumed["state"]["state_version"], {"intent": "free_speech", "safety_severity": "none"}))
    completed = runtime.handle(_event("AudioCompleted", 9, analysis["state"]["state_version"], {"response_id": "fixed_aibolit_v1"}))
    assert completed["state"]["mode"] == "fixed_story_menu"


def test_incident_log_is_metadata_only(tmp_path):
    path = tmp_path / "incidents.jsonl"
    IncidentLogger(path).log("s", "input", "block", "urgent", "секрет ребёнка")
    raw = path.read_text(encoding="utf-8")
    assert "секрет ребёнка" not in raw
    assert '"text_length"' in raw and '"text_sha256"' in raw and '"text":' not in raw


def test_ui_projection_is_derived_from_canonical_elements():
    state = canonical_led_state_from_lighting({str(element): 0 for element in (10, 11, 20, 21, 30, 31, 40, 41, 50, 51, 60, 61, 62, 70, 71, 72)})
    assert state["targets"] == []
    state = canonical_led_state_from_lighting({"10": 1, "11": 1, **{str(element): 0 for element in (20, 21, 30, 31, 40, 41, 50, 51, 60, 61, 62, 70, 71, 72)}})
    assert state["targets"] == [1] and state["colors"]["1"] == "red"
