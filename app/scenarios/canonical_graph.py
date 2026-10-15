"""Canonical production LangGraph transition authorization.

The graph has no I/O nodes. It receives an already-classified event and proposed
state transition, authorizes it against the product contract, and returns a
decision. Hardware, audio, providers and persistence stay in application code.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, TypedDict

from langgraph.graph import END, StateGraph

from app.foundation.canonical_contract import STATE_NAMES


class AuthorizationState(TypedDict, total=False):
    current_mode: str
    target_mode: str
    event_type: str
    allowed: bool
    reason: str
    trace: list[str]


ROOT_ENTRY_MODES = frozenset({
    "idle", "visual_welcome", "anonymous_visual", "root_menu",
    "dialogue_memory_expired", "dialogue_suspended_ram", "voice_welcome",
    "exhibition_introduction", "exhibition_name_question",
    "exhibition_name_capture", "exhibition_age_prompt",
    "exhibition_age_hint", "exhibition_age_selection",
    "exhibition_age_complete", "exhibition_onboarding_complete",
    "exhibition_menu_tutorial_intro", "exhibition_menu_tutorial_prompt",
    "exhibition_menu_tutorial_buttons", "menu_ready",
    "automatic_mood_check", "awaiting_handset",
})
PIPELINE_NODES = ("ingress", "normalize_event", "session_guard", "safety_precheck", "route_mode", "policy_validate", "emit_effects", "persist_snapshot", "await_event")
_SCENARIO_PATH = Path(__file__).resolve().parents[2] / "content" / "scenarios" / "canonical_product_v1.json"

def load_canonical_scenario_document(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path is not None else _SCENARIO_PATH
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"canonical scenario document unavailable: {source}") from exc
    if not isinstance(document, dict) or document.get("scenario_id") != "canonical_product":
        raise RuntimeError("canonical scenario document has invalid scenario_id")
    if document.get("version") != 1 or document.get("contract") != "canonical_product/v1" or document.get("langgraph_authorization") is not True:
        raise RuntimeError("canonical scenario document is not authorization-enabled")
    states = document.get("states")
    if not isinstance(states, dict) or set(states) != set(STATE_NAMES):
        raise RuntimeError("canonical scenario document must define every canonical state")
    required = {"allowed_events", "expected_input", "lighting_preset", "audio_policy", "timeout_policy", "persistence_policy", "success_transition", "retry_transition", "cancel_transition", "error_transition", "state_version"}
    for name, state in states.items():
        if not isinstance(state, dict) or not required.issubset(state) or state.get("state_version") != 1 or not isinstance(state.get("allowed_events"), list):
            raise RuntimeError(f"canonical state contract is incomplete: {name}")
    return document


BUTTON_TARGETS = frozenset({
    "game_menu", "fixed_story_menu", "support_intake",
    "personal_story_theme", "wellbeing_check", "bear_dialogue",
    "schedule_weather",
})


def _is_explicitly_allowed(current: str, target: str, event_type: str) -> bool:
    """The whole allowlist for state-changing canonical scenario events."""
    if current not in STATE_NAMES or target not in STATE_NAMES:
        return False
    if current == target:
        return True
    if event_type == "RfidPresented":
        return target in {"visual_welcome", "anonymous_visual"}
    if event_type == "RfidRemoved":
        return target in {"anonymous_visual", "safety_handoff"}
    if event_type == "ExhibitionOnboardingComplete":
        return current == "exhibition_onboarding_complete" and target == "exhibition_menu_tutorial_buttons"
    if event_type == "AudioCompleted":
        return (
            (current == "voice_welcome" and target == "exhibition_introduction")
            or (current == "exhibition_introduction" and target == "exhibition_name_question")
            or (current == "exhibition_name_question" and target == "exhibition_name_capture")
            or (current == "exhibition_name_capture" and target == "exhibition_name_capture")
            or (current == "exhibition_menu_repeat_listen" and target in {"exhibition_menu_repeat_listen", "exhibition_menu_replay_buttons", "menu_ready"})
            or (current == "exhibition_age_prompt" and target == "exhibition_age_hint")
            or (current == "exhibition_age_hint" and target == "exhibition_age_selection")
            or (current == "exhibition_age_complete" and target == "exhibition_onboarding_complete")
            or (current == "exhibition_menu_tutorial_intro" and target == "exhibition_menu_tutorial_prompt")
            or (current == "exhibition_menu_tutorial_prompt" and target == "exhibition_menu_tutorial_buttons")
            or (current in {"exhibition_menu_tutorial_buttons", "exhibition_menu_replay_buttons"} and target == "exhibition_menu_wave")
            or (current == "exhibition_menu_repeat_prompt" and target == "exhibition_menu_repeat_listen")
            or (current in {"fixed_story_playing", "story_speech_analysis"} and target == "fixed_story_choice_wave")
            or (current == "story_pause_announcement" and target in {"story_paused", "fixed_story_choice_wave"})
            or (current == "story_resume_announcement" and target in {"fixed_story_playing", "fixed_story_choice_wave"})
        )
    if event_type == "ExhibitionMenuWaveComplete":
        return current == "exhibition_menu_wave" and target in {"exhibition_menu_repeat_prompt", "menu_ready"}
    if event_type == "StoryChoiceWaveComplete":
        return current == "fixed_story_choice_wave" and target == "fixed_story_menu"
    if event_type == "MenuTutorialStep":
        return current in {"exhibition_menu_tutorial_buttons", "exhibition_menu_replay_buttons"} and target in {"exhibition_menu_tutorial_buttons", "exhibition_menu_replay_buttons", "menu_ready"}
    if event_type == "TimeoutFired":
        return current in {"dialogue_suspended_ram", "story_suspended_ram"} and target == "dialogue_memory_expired"
    if event_type == "AnonymousStart":
        return target == "anonymous_visual"
    if event_type == "SessionCancel":
        return target == "soft_end"
    if event_type == "SessionReset":
        return target == "session_closed"
    if event_type == "HandsetPlaced":
        return (
            target == "idle"
            or (current == "voice_welcome" and target == "idle")
            or (current == "fixed_story_playing" and target == "story_suspended_ram")
            or (current == "safety_handoff" and target == "safety_handoff")
            or (current not in {"game_active", "safety_handoff"} and target == "dialogue_suspended_ram")
        )
    if event_type == "HandsetLifted":
        return target in {"awaiting_handset", "voice_welcome", "automatic_mood_check", "story_paused", "game_menu", "fixed_story_menu", "fixed_story_playing", "support_intake", "personal_story_theme", "wellbeing_check", "bear_dialogue", "schedule_weather"}
    if event_type == "HardwareDisconnected":
        return target == "safety_handoff"
    if event_type == "AdultHandoffAcknowledged":
        return current == target == "safety_handoff"
    if event_type == "AdultResolved":
        return current == "safety_handoff" and target == "root_menu"
    if event_type == "ButtonPressed":
        return (
            target == "wellbeing_check"
            or (current == "wellbeing_check" and target in {"support_intake", "safety_handoff"})
            or (current == "exhibition_age_selection" and target == "exhibition_age_complete")
            or (current in ROOT_ENTRY_MODES and target in BUTTON_TARGETS | {"awaiting_handset"})
            or (current == "personal_story_theme" and target in {"personal_story_generating", "root_menu"})
            or (current == "automatic_mood_check" and target == "root_menu")
            or (current == "game_menu" and target == "game_active")
            or (current == "game_active" and target == "root_menu")
            or (current == "fixed_story_menu" and target in {"fixed_story_playing", "awaiting_handset", "root_menu"})
            or (current in {"fixed_story_playing", "story_speech_analysis"} and target in {"story_pause_announcement", "fixed_story_choice_wave", "root_menu"})
            or (current in {"story_pause_announcement", "story_resume_announcement", "story_paused"} and target in {"story_resume_announcement", "fixed_story_choice_wave", "root_menu"})
            or (current in {"support_intake", "bear_dialogue"} and target == "root_menu")
            or (current in {"game_menu", "fixed_story_menu"} and target == "root_menu")
            or (current == "story_paused" and target in {"story_resume_announcement", "fixed_story_choice_wave", "root_menu"})
            or (current == "schedule_weather" and target == "root_menu")
        )
    if event_type == "VoiceIntentReady":
        return (
            target in {"safety_handoff", "support_intake"}
            or (current == "exhibition_name_capture" and target == "exhibition_age_prompt")
            or (current == "exhibition_name_capture" and target == "exhibition_name_capture")
            or (current == "exhibition_menu_repeat_listen" and target in {"exhibition_menu_repeat_listen", "exhibition_menu_replay_buttons", "menu_ready"})
            or (current in {"fixed_story_playing", "story_speech_analysis"} and target in {"story_paused", "story_speech_analysis"})
            or (current == "story_speech_analysis" and target == "support_intake")
            or (current == "personal_story_theme" and target == "personal_story_generating")
            or (current == "automatic_mood_check" and target == "mood_report")
            or (current == "personal_story_theme" and target in {"personal_story_generating", "root_menu"})
            or (current == "personal_story_generating" and target in {"personal_story_validating", "personal_story_theme"})
            or (current == "personal_story_validating" and target == "personal_story_playing")
            or (current == "story_paused" and target in {"root_menu", "fixed_story_playing"})
        )
    return False


def _node_for(mode: str):
    def authorize(state: AuthorizationState) -> AuthorizationState:
        current = str(state.get("current_mode") or "")
        target = str(state.get("target_mode") or "")
        event_type = str(state.get("event_type") or "")
        allowed = current == mode and _is_explicitly_allowed(current, target, event_type)
        return {
            **state,
            "allowed": allowed,
            "reason": "transition_allowed" if allowed else "transition_not_allowlisted",
            "trace": [current, event_type, target],
        }

    return authorize


def _identity(state: AuthorizationState) -> AuthorizationState:
    return state


def build_canonical_graph(document: Mapping[str, Any] | None = None):
    """Compile the full canonical pipeline and state authorization nodes."""
    document = dict(document or load_canonical_scenario_document())
    graph = StateGraph(AuthorizationState)
    for node in PIPELINE_NODES:
        graph.add_node(node, _identity)
    for mode in sorted(STATE_NAMES):
        graph.add_node(mode, _node_for(mode))
        graph.add_edge(mode, "policy_validate")
    graph.add_edge("ingress", "normalize_event")
    graph.add_edge("normalize_event", "session_guard")
    graph.add_edge("session_guard", "safety_precheck")
    graph.add_edge("safety_precheck", "route_mode")
    graph.add_conditional_edges("route_mode", lambda state: str(state.get("current_mode") or "idle"),
                                {mode: mode for mode in STATE_NAMES})
    graph.add_edge("policy_validate", "emit_effects")
    graph.add_edge("emit_effects", "persist_snapshot")
    graph.add_edge("persist_snapshot", "await_event")
    graph.add_edge("await_event", END)
    graph.set_entry_point("ingress")
    return graph.compile()


class CanonicalLangGraphAuthorizer:
    """Fail-closed LangGraph gate used before application effects are emitted."""

    def __init__(self, scenario_path: str | Path | None = None) -> None:
        self.document = load_canonical_scenario_document(scenario_path)
        self.graph = build_canonical_graph(self.document)

    def authorize(self, current_mode: str | None, target_mode: str, event_type: str) -> dict[str, Any]:
        current = str(current_mode or "idle")
        target = str(target_mode)
        if current not in STATE_NAMES or target not in STATE_NAMES:
            return {"ok": False, "reason": "transition_not_allowlisted",
                    "trace": [current, str(event_type), target], "provider": "langgraph",
                    "scenario_id": self.document["scenario_id"], "scenario_version": self.document["version"]}
        result = self.graph.invoke(
            {"current_mode": current, "target_mode": target, "event_type": str(event_type)}
        )
        return {
            "ok": bool(result.get("allowed")),
            "reason": str(result.get("reason") or "transition_rejected"),
            "trace": list(result.get("trace") or []),
            "provider": "langgraph",
            "scenario_id": self.document["scenario_id"],
            "scenario_version": self.document["version"],
        }

    def status(self) -> dict[str, Any]:
        return {"provider": "langgraph", "strict": True, "ready": True,
                "scenario_id": self.document["scenario_id"], "scenario_version": self.document["version"],
                "nodes": sorted(STATE_NAMES), "topology": list(PIPELINE_NODES),
                "authorization_boundary": "session_guard→safety_precheck→policy_validate"}

