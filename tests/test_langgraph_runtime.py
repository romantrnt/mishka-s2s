import json
from pathlib import Path

from app.scenarios.langgraph_runtime import LangGraphRuntime


ROOT = Path(__file__).parents[1]


def _runtime():
    docs = {}
    for name in ("fixture_core", "dev_core"):
        docs[name] = json.loads((ROOT / "content" / "scenarios" / f"{name}.json").read_text())
    return LangGraphRuntime(docs, enabled=True, strict=True), docs


def test_real_langgraph_compiles_all_release_scenarios():
    runtime, _ = _runtime()
    status = runtime.status()
    assert status["provider"] == "langgraph"
    assert status["compiled"] == ["dev_core", "fixture_core"]
    assert status["errors"] == {}


def test_entry_transition_is_allowlisted():
    runtime, docs = _runtime()
    doc = docs["fixture_core"]
    result = runtime.authorize("fixture_core", None, doc["start"], "handset_lifted")
    assert result["ok"] is True
    assert result["next_node"] == doc["start"]


def test_button_transition_uses_current_node_edges():
    runtime, _ = _runtime()
    result = runtime.authorize("fixture_core", "fixture_choose", "fixture_story_menu", "button:2")
    assert result["ok"] is True
    assert result["trigger"] == "button_2"


def test_button6_is_a_bound_target():
    runtime, _ = _runtime()
    result = runtime.authorize("fixture_core", "fixture_choose", "fixture_free", "button:6")
    assert result["ok"] is True
    assert result["next_node"] == "fixture_free"


def test_fallback_target_is_allowlisted_for_unknown_input():
    runtime, _ = _runtime()
    result = runtime.authorize("fixture_core", "fixture_choose", "fixture_fallback", "unknown_button")
    assert result["ok"] is True


def test_story_selection_and_completion_are_graph_edges():
    runtime, _ = _runtime()
    selected = runtime.authorize("fixture_core", "fixture_story_menu", "fixture_story_5", "button:5")
    done = runtime.authorize("fixture_core", "fixture_story_5", "fixture_story_done", "button:7")
    assert selected["ok"] is True
    assert done["ok"] is True


def test_game_success_and_failure_edges_are_separate():
    runtime, _ = _runtime()
    success = runtime.authorize("fixture_core", "fixture_game_one", "fixture_game_done", "game_success")
    failure = runtime.authorize("fixture_core", "fixture_game_one", "fixture_game_done", "game_failure")
    assert success["ok"] is True
    assert failure["ok"] is True


def test_dev_intent_transition_is_allowlisted():
    runtime, _ = _runtime()
    result = runtime.authorize("dev_core", "dev_dialogue", "dev_end", "return")
    assert result["ok"] is True
