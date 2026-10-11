import json

from app.mvp.content import ContentRepository
from app.mvp.service import MishkaMVPService
from app.mvp.types import IntentResult


class Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


def service(clock=None, classifier=None):
    return MishkaMVPService(clock=clock or Clock(), classifier=classifier)


def start_known(s, sid="s1"):
    auth = s.process(sid, {"type": "rfid", "tag_id": "mock-child-001"})
    assert auth["outputs"][0]["status"] == "authenticated"
    return s.process(sid, {"type": "handset", "state": "up"})


def test_rfid_authentication_and_start_session():
    s = service()
    result = start_known(s)
    assert result["state"] == "DIALOGUE"
    assert result["node"] == "choose_mode"
    assert any("Саша" in event.get("text", "") for event in result["outputs"])


def test_unknown_rfid_enters_anonymous_mode_without_profile_history():
    s = service()
    s.process("anon", {"type": "rfid", "tag_id": "unknown"})
    result = s.process("anon", {"type": "handset", "state": "up"})
    snapshot = s.snapshot("anon")
    assert snapshot["anonymous"] is True
    assert snapshot["child_id"] is None
    assert any("Привет" in event.get("text", "") for event in result["outputs"])


def test_no_rfid_also_allows_safe_anonymous_start():
    s = service()
    result = s.process("anon-direct", {"type": "handset", "state": "up"})
    assert result["node"] == "choose_mode"
    assert s.snapshot("anon-direct")["anonymous"] is True


def test_mood_transition_and_safe_discomfort_response():
    s = service()
    start_known(s)
    s.process("s1", {"type": "button", "button_id": 1})
    positive = s.process("s1", {"type": "text", "text": "У меня отличное настроение"})
    assert positive["node"] == "choose_mode"
    assert any("Здорово" in event.get("text", "") for event in positive["outputs"])
    s.process("s1", {"type": "button", "button_id": 1})
    comfort = s.process("s1", {"type": "text", "text": "Мне больно"})
    assert any("взрослого" in event.get("text", "") for event in comfort["outputs"])
    assert "диагноз" not in json.dumps(comfort, ensure_ascii=False).lower()


def test_unknown_and_low_confidence_go_to_fallback_without_raw_text_logging():
    s = service()
    start_known(s)
    s.process("s1", {"type": "button", "button_id": 1})
    result = s.process("s1", {"type": "text", "text": "квантовый ананас"})
    assert result["node"] == "fallback"
    assert s.runtime.incidents[-1]["kind"] == "fallback"
    assert "квантовый" not in json.dumps(s.runtime.incidents, ensure_ascii=False)


class LowConfidence:
    def classify(self, text): return IntentResult("mood_positive", 0.2, "test")


def test_low_confidence_is_not_accepted_as_intent():
    s = service(classifier=LowConfidence())
    start_known(s)
    s.process("s1", {"type": "button", "button_id": 1})
    assert s.process("s1", {"type": "text", "text": "хорошо"})["node"] == "fallback"


def test_timeout_moves_to_fallback_then_ends_safely():
    clock = Clock(); s = service(clock)
    start_known(s)
    clock.advance(46)
    assert s.process("s1", {"type": "timeout"})["node"] == "fallback"
    clock.advance(26)
    assert s.process("s1", {"type": "timeout"})["state"] == "ENDING"


def test_game_correct_button_and_history_are_saved_only_for_profile():
    s = service(); start_known(s)
    s.process("s1", {"type": "button", "button_id": 2})
    started = s.process("s1", {"type": "button", "button_id": 1})
    assert started["state"] == "GAME" and started["node"] == "game_one"
    done = s.process("s1", {"type": "button", "button_id": 1})
    assert done["node"] == "game_repeat"
    profile = s.profiles.get_by_tag("mock-child-001")
    assert profile.game_history[-1] == {"game_id": "press_one", "success": True}
    assert any(command["action"] == "led_pattern" for command in s.hardware.commands)


def test_game_wrong_button_has_limited_attempts():
    s = service(); start_known(s)
    s.process("s1", {"type": "button", "button_id": 2})
    s.process("s1", {"type": "button", "button_id": 2})
    for _ in range(3): result = s.process("s1", {"type": "button", "button_id": 5})
    assert result["node"] == "game_repeat"
    assert s.profiles.get_by_tag("mock-child-001").game_history[-1]["success"] is False


def test_anonymous_game_does_not_save_profile_result():
    s = service()
    s.process("anon", {"type": "handset", "state": "up"})
    s.process("anon", {"type": "button", "button_id": 2})
    s.process("anon", {"type": "button", "button_id": 1})
    s.process("anon", {"type": "button", "button_id": 1})
    assert s.snapshot("anon")["child_id"] is None
    assert s.profiles.get_by_tag("mock-child-001").game_history == []


def test_story_selection_records_history_and_returns_to_menu():
    s = service(); start_known(s)
    s.process("s1", {"type": "button", "button_id": 3})
    result = s.process("s1", {"type": "button", "button_id": 4})
    assert result["node"] == "choose_mode"
    assert s.profiles.get_by_tag("mock-child-001").story_history[-1] == {"story_id": "story_4"}


def test_handset_down_barge_in_clears_old_audio_and_blocks_stale_token():
    s = service(); start_known(s)
    audio = s.audio_for("s1")
    old_token = s.snapshot("s1")["response_token"]
    assert audio.queue
    down = s.process("s1", {"type": "handset", "state": "down"})
    assert down["state"] == "ENDING"
    assert all(item["response_id"] == "ending" for item in audio.queue)
    assert audio.speak("old", "old", None, old_token) is None


def test_reset_and_sessions_are_isolated():
    s = service(); start_known(s, "a")
    s.process("b", {"type": "handset", "state": "up"})
    assert s.snapshot("a")["child_id"] == "child-001"
    assert s.snapshot("b")["anonymous"] is True
    assert s.reset("a")["reset"] is True
    assert s.snapshot("a") is None and s.snapshot("b") is not None


def test_content_validation_falls_back_instead_of_crashing(tmp_path):
    (tmp_path / "scenarios").mkdir(); (tmp_path / "responses").mkdir(); (tmp_path / "stories").mkdir(); (tmp_path / "games").mkdir()
    (tmp_path / "scenarios" / "bad.json").write_text("{not json")
    repo = ContentRepository(tmp_path)
    assert repo.data.errors and repo.scenario("anything")["id"] == "safe_fallback"


def test_mock_event_flow_matches_websocket_payload_format():
    s = service(); start_known(s, "ws-mock")
    response = s.process("ws-mock", {"type": "button", "button_id": 3, "state": "pressed"})
    assert response["session_id"] == "ws-mock"
    assert response["node"] == "choose_story"
    assert all("session_id" in item or item["action"].startswith("led") for item in response["outputs"])
