#!/usr/bin/env python3
"""Dependency-free acceptance checks for the Mishka canonical runtime."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.foundation.canonical_contract import (
    CanonicalEvent, CanonicalRuntime, FixedStoryManifest, VoiceManifest,
    validate_generated_story,
)
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer


def event(kind, sequence, *, version, payload=None, timestamp=1.0):
    return CanonicalEvent(kind, "acceptance", sequence, version, "test",
                          payload or {}, f"c-{sequence}", timestamp)

def main() -> None:
    clock = [0.0]
    runtime = CanonicalRuntime(now=lambda: clock[0],
        stories=FixedStoryManifest("content/stories/fixed_manifest.json", "."))
    state = runtime.handle(event("AnonymousStart", 1, version=0), None)["state"]
    assert state["age_band"] == "3-5"
    down = runtime.handle(event("ButtonPressed", 2, version=1, payload={"button_id": 3}), None)
    assert down["reason"] == "handset_required"
    lifted = runtime.handle(event("HandsetLifted", 3, version=2), None)
    assert lifted["state"]["mode"] == "support_intake"
    assert lifted["state"]["pending_mode"] is None
    game_runtime = CanonicalRuntime(now=lambda: clock[0],
        stories=FixedStoryManifest("content/stories/fixed_manifest.json", "."))
    game_runtime.handle(event("AnonymousStart", 1, version=0), None)
    game_runtime.handle(event("HandsetLifted", 2, version=1), None)
    game = game_runtime.handle(event("ButtonPressed", 3, version=2, payload={"button_id": 1}), None)
    assert game["state"]["mode"] == "game_menu"
    game_start = game_runtime.handle(event("ButtonPressed", 4, version=3, payload={"button_id": 1}), None)
    assert game_start["state"]["mode"] == "game_active"
    placed = game_runtime.handle(event("HandsetPlaced", 5, version=4), None)
    assert any(effect["type"] == "game_continue_visual" for effect in placed["effects"])
    clock[0] = 301.0
    resumed = game_runtime.handle(event("HandsetLifted", 6, version=5, timestamp=301.0), None)
    assert resumed["state"]["dialogue_memory"]["status"] == "expired"
    help_runtime = CanonicalRuntime(stories=FixedStoryManifest("content/stories/fixed_manifest.json", "."))
    help_runtime.handle(event("AnonymousStart", 1, version=0), None)
    help_runtime.handle(event("HandsetLifted", 2, version=1), None)
    help = help_runtime.handle(event("ButtonPressed", 3, version=2, payload={"button_id": 5}), None)
    assert help["reason"] == "help_priority" and help["state"]["mode"] == "wellbeing_check"
    help_runtime.handle(event("ButtonPressed", 4, version=3, payload={"button_id": 7}), None)
    down = help_runtime.handle(event("HandsetPlaced", 5, version=4), None)
    assert down["state"]["mode"] == "safety_handoff"
    stale = runtime.handle(event("ButtonPressed", 8, version=0, payload={"button_id": 1}), None)
    assert stale["reason"] == "stale_state_version"
    fixed = CanonicalRuntime(stories=FixedStoryManifest("content/stories/fixed_manifest.json", "."))
    fixed.handle(CanonicalEvent("AnonymousStart", "fixed", 1, 0, "test"), None)
    menu = fixed.handle(CanonicalEvent("ButtonPressed", "fixed", 2, 1, "test", {"button_id": 2}), None)
    blocked = fixed.handle(CanonicalEvent("ButtonPressed", "fixed", 3, 2, "test", {"button_id": 1}), None)
    assert menu["state"]["mode"] == "fixed_story_menu" and blocked["reason"] == "fixed_story_blocked"
    voice_readiness = VoiceManifest("content/audio/voice_manifest.json").readiness()
    assert voice_readiness["ready"]
    assert voice_readiness["approved_recorded_count"] == 108
    graph_status = CanonicalLangGraphAuthorizer().status()
    assert graph_status["scenario_id"] == "canonical_product" and graph_status["topology"][0] == "ingress"
    game_resume = CanonicalRuntime(stories=FixedStoryManifest("content/stories/fixed_manifest.json", "."))
    game_resume.handle(event("AnonymousStart", 1, version=0), None)
    game_resume.handle(event("HandsetLifted", 2, version=1), None)
    game_resume.handle(event("ButtonPressed", 3, version=2, payload={"button_id": 1}), None)
    game_resume.handle(event("ButtonPressed", 4, version=3, payload={"button_id": 1}), None)
    game_resume.handle(event("HandsetPlaced", 5, version=4), None)
    resumed_game = game_resume.handle(event("HandsetLifted", 6, version=5), None)
    assert resumed_game["state"]["mode"] == "game_active" and resumed_game["reason"] == "game_voice_join"
    help_guard = CanonicalRuntime(stories=FixedStoryManifest("content/stories/fixed_manifest.json", "."))
    help_guard.handle(event("AnonymousStart", 1, version=0), None)
    help_guard.handle(event("VoiceIntentReady", 2, version=1, payload={"intent": "request_adult", "safety_severity": "urgent"}), None)
    blocked_help = help_guard.handle(event("ButtonPressed", 3, version=2, payload={"button_id": 5}), None)
    assert blocked_help["state"]["mode"] == "safety_handoff"
    ok, reason = validate_generated_story(
        {"title": "Добрая прогулка", "text": "Мишка помог другу и они спокойно вернулись домой.",
         "age_band": "3-5", "estimated_duration_sec": 70, "safety_tags": [], "ending_state": "safe_resolved"},
        {"age_band": "3-5", "display_name": "Лена", "forbidden_topics": []},
    )
    assert ok and reason is None
    print("canonical contract acceptance: OK")

if __name__ == "__main__":
    main()
