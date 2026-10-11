import json
import os
import shutil
from pathlib import Path

import pytest

from app.foundation.application import ScenarioApplication
from app.foundation.content import FileScenarioRepository
from app.foundation.domain import Lifecycle, SessionRecord
from app.foundation.adapters import RecordingAudio, RecordingLED, RuleBasedIntentClassifier
from app.foundation.observability import StructuredEventPublisher
from app.scenarios.langgraph_runtime import LangGraphRuntime


ROOT = Path(__file__).parents[1]
CONTENT = ROOT / "content"


def copy_content(tmp_path):
    root = tmp_path / "content"
    shutil.copytree(CONTENT, root)
    return root


def test_p1_13_valid_fixture_is_loaded_and_legacy_exclusion_is_explicit():
    repo = FileScenarioRepository(CONTENT, strict=True)
    assert {"fixture_core", "dev_core"} <= set(repo.scenarios)
    report = repo.report()
    assert report["error_count"] == 0
    assert any(issue["category"] == "legacy_excluded" for issue in report["notices"])
    assert json.loads(json.dumps(report))["partial_load"] is False


@pytest.mark.parametrize("kind", ["malformed_json", "empty_file"])
def test_p1_13_malformed_resource_is_reported_and_not_partially_loaded(tmp_path, kind):
    root = copy_content(tmp_path)
    target = root / "responses" / f"broken_{kind}.json"
    target.write_text("{" if kind == "malformed_json" else "", encoding="utf-8")
    repo = FileScenarioRepository(root)
    report = repo.report()
    assert report["error_count"] >= 1
    assert repo.scenarios == {}
    assert repo.has_errors
    assert any(issue["category"] == "malformed_json" for issue in report["issues"])
    json.dumps(report)


def test_p1_13_missing_fields_types_duplicate_ids_and_invalid_targets_are_bounded(tmp_path):
    root = copy_content(tmp_path)
    scenario = root / "scenarios" / "fixture_core.json"
    document = json.loads(scenario.read_text())
    document["nodes"][0].pop("prompt")
    document["nodes"][1]["transitions"]["next"] = "missing_node"
    document["nodes"].append(dict(document["nodes"][1]))
    document["nodes"][-1]["id"] = document["nodes"][1]["id"]
    document["nodes"][2]["allowed_intents"] = "not-a-list"
    scenario.write_text(json.dumps(document), encoding="utf-8")
    repo = FileScenarioRepository(root, max_issues=3)
    report = repo.report()
    assert report["issue_count"] > len(report["issues"]) == 3
    assert report["issues_truncated"] > 0
    categories = {issue["category"] for issue in report["issues"]}
    assert "schema" in categories
    assert repo.scenarios == {}
    assert all({"resource", "severity", "expected"} <= set(issue) for issue in report["issues"])


def test_p1_13_strict_mode_fails_closed_with_json_safe_diagnostics(tmp_path):
    root = copy_content(tmp_path)
    scenario = root / "scenarios" / "fixture_core.json"
    document = json.loads(scenario.read_text())
    document["nodes"][0]["prompt"] = 42
    scenario.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="content validation failed"):
        FileScenarioRepository(root, strict=True)


def test_p1_14_strict_disabled_engine_cannot_report_success():
    runtime = LangGraphRuntime({}, enabled=False, strict=True)
    result = runtime.authorize("fixture_core", None, "fixture_welcome", "handset_lifted")
    assert result["ok"] is False
    assert result["reason"] == "legacy_fallback_disabled"
    assert result["next_node"] is None
    assert runtime.status()["ready"] is False


def test_p1_14_strict_missing_document_and_mock_fallback_are_distinct():
    strict = LangGraphRuntime({}, enabled=True, strict=True)
    missing = strict.authorize("fixture_core", None, "fixture_welcome", "handset_lifted")
    assert missing["ok"] is False
    assert missing["reason"] == "scenario_document_missing"
    mock = LangGraphRuntime({}, enabled=False, strict=False)
    fallback = mock.authorize("fixture_core", None, "fixture_welcome", "handset_lifted")
    assert fallback["ok"] is True
    assert fallback["reason"] == "legacy_fallback"
    assert fallback["environment"] == "mock"
    assert fallback["evidence"] == "explicit_mock_or_local"


def test_p1_14_failed_graph_transition_has_no_audio_or_hardware_side_effect(monkeypatch):
    monkeypatch.setenv("MISHKA_PROFILE", "production_candidate")
    monkeypatch.setenv("MISHKA_SCENARIO_ENGINE", "off")
    monkeypatch.setenv("MISHKA_LANGGRAPH_STRICT", "1")
    repo = FileScenarioRepository(CONTENT)
    audio, led, publisher = RecordingAudio(), RecordingLED(), StructuredEventPublisher()
    application = ScenarioApplication(
        __import__("app.foundation.repositories", fromlist=["InMemorySessionRepository"]).InMemorySessionRepository(),
        repo,
        __import__("app.foundation.adapters", fromlist=["MockProfileProvider"]).MockProfileProvider(),
        RuleBasedIntentClassifier(),
        audio,
        led,
        publisher,
    )
    session = SessionRecord("strict-graph", "default", "corr", state=Lifecycle.ACTIVE, handset_up=True, current_node="fixture_choose", scenario_id="fixture_core")
    result = application._goto(session, repo.get("fixture_core"), "fixture_game_menu", "button:1")
    assert result[0]["action"] == "scenario_rejected"
    assert result[0]["reason"] == "legacy_fallback_disabled"
    assert session.current_node == "fixture_choose"
    assert audio.commands == []
    assert led.commands == []
    assert publisher.events[-1]["type"] == "ScenarioTransitionRejected"
