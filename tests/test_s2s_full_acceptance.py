import asyncio
from pathlib import Path

from app.foundation.adapters import MockProfileProvider, RecordingAudio, RecordingLED, RuleBasedIntentClassifier
from app.foundation.application import ScenarioApplication
from app.foundation.config import Settings
from app.foundation.content import FileScenarioRepository
from app.foundation.events import parse_event
from app.foundation.observability import StructuredEventPublisher
from app.foundation.repositories import InMemorySessionRepository


def _strict_application():
    sessions = InMemorySessionRepository()
    audio = RecordingAudio()
    publisher = StructuredEventPublisher()
    scenarios = FileScenarioRepository(Path(__file__).parents[1] / "content")
    settings = Settings(profile="production_candidate")
    application = ScenarioApplication(
        sessions, scenarios, MockProfileProvider(), RuleBasedIntentClassifier(),
        audio, RecordingLED(), publisher, now=lambda: 100.0, settings=settings,
    )
    class StrictEntryGate:
        def __init__(self):
            self.calls = []

        def authorize(self, scenario_id, current_node, target, reason):
            self.calls.append((scenario_id, current_node, target, reason))
            allowed = (
                current_node is None and target == "fixture_welcome"
            ) or (
                current_node == "fixture_choose"
                and target == "fixture_free"
                and reason == "button_6_dialogue"
            )
            return {
                "ok": allowed,
                "environment": "production",
                "reason": "transition_allowed" if allowed else "transition_rejected",
            }

    application.langgraph = StrictEntryGate()
    return application, audio, publisher


def _event(payload, session_id, sequence):
    return parse_event({**payload, "sequence": sequence}, session_id, sequence, now=100.0)


def test_strict_child_handset_lift_delivers_one_named_greeting():
    application, audio, publisher = _strict_application()
    gate = application.langgraph
    session_id = "strict-child-greeting"
    auth = application.receive(_event({"type": "rfid", "tag_id": "mock-child-001"}, session_id, 1))
    lifted = application.receive(_event({"type": "handset_up"}, session_id, 2))

    assert auth["state"] == "AUTHENTICATED"
    assert lifted["reason"] == "handset_lifted"
    assert lifted["node"] == "fixture_welcome"
    assert gate.calls == [("fixture_core", None, "fixture_welcome", "handset_lifted")]
    assert not any(item.get("action") == "scenario_rejected" for item in lifted["outputs"])
    assert [item["response_id"] for item in audio.commands if item.get("response_id") == "welcome_named"] == ["welcome_named"]
    assert sum(event.get("type") == "SessionStarted" for event in publisher.events) == 1

    duplicate = application.receive(_event({"type": "handset_up"}, session_id, 3))
    assert duplicate["reason"] == "idempotent_duplicate"
    assert [item["response_id"] for item in audio.commands if item.get("response_id") == "welcome_named"] == ["welcome_named"]

    button = application.receive(_event({"type": "button", "button_id": 6}, session_id, 4))
    assert button["node"] == "fixture_free"
    assert button["reason"] == "button_6_dialogue"
    assert not any(item.get("action") == "scenario_rejected" for item in button["outputs"])
    assert gate.calls[-1] == ("fixture_core", "fixture_choose", "fixture_free", "button_6_dialogue")


def test_strict_child_greeting_path_has_no_detached_audio_work():
    application, _, _ = _strict_application()
    session_id = "strict-child-cleanup"
    application.receive(_event({"type": "rfid", "tag_id": "mock-child-001"}, session_id, 1))
    application.receive(_event({"type": "handset_up"}, session_id, 2))
    result = asyncio.run(application._cleanup_session_resources(session_id))
    assert result["audio_completion_cleanup"]["remaining"] == 0
    assert result["audio_completion_registry"] == 0


def test_deterministic_chunk_trace_has_ordered_single_owner_and_terminal_state():
    from app.foundation.s2s import BoundedConversationEngine, ConversationContext, MockLLMProvider, MockPlaybackController, MockTTSProvider, RVCProvider

    engine = BoundedConversationEngine(
        MockLLMProvider(tokens=["<say>Первый чанк.</say>", "<say>Второй чанк.</say>"]),
        MockTTSProvider(), RVCProvider(), MockPlaybackController(),
        profile="mock", allow_quick_replies=False,
    )
    context = ConversationContext("trace-session", "fixture_core", "fixture_free", ("greetings",), (), 2, 120, 2)
    result = asyncio.run(engine.run_text_turn("Привет", context))
    assert result.fallback_reason is None
    traces = engine.metrics.snapshot()["chunk_traces"]
    assert [item["chunk_index"] for item in traces] == [0, 1]
    for item in traces:
        assert item["session_id"] == "trace-session"
        assert item["turn_id"] == "trace-session:turn:1"
        assert item["owner"] == "s2s-tts:trace-session:1"
        assert item["owner_conflict"] is False
        assert item["terminal_state"] == "finished"
        assert item["terminal_callbacks"] == 1
        assert item["duplicate_callbacks"] == 0
        assert item["sample_count"] > 0 and item["byte_count"] > 0
        assert list(item["timestamps"]) == ["enqueue", "start", "audio_ready", "play_start", "play_finish"]
        assert all(left < right for left, right in zip(item["timestamps"].values(), list(item["timestamps"].values())[1:]))
    assert not engine.active_tasks and not engine.active_queues


def test_cancelled_chunk_trace_has_one_terminal_cancel_and_no_live_resources():
    from app.foundation.s2s import BoundedConversationEngine, ConversationContext, MockLLMProvider, MockPlaybackController, MockTTSProvider, RVCProvider

    class SlowTTS(MockTTSProvider):
        async def synthesize(self, text, token, cancelled):
            await asyncio.sleep(10)
            return await super().synthesize(text, token, cancelled)

    engine = BoundedConversationEngine(
        MockLLMProvider(tokens=["<say>Отменяемый чанк.</say>"]),
        SlowTTS(), RVCProvider(), MockPlaybackController(), profile="mock", allow_quick_replies=False,
    )
    context = ConversationContext("cancel-trace", "fixture_core", "fixture_free", ("greetings",), (), 2, 120, 2)

    async def flow():
        task = asyncio.create_task(engine.run_text_turn("Привет", context))
        for _ in range(100):
            await asyncio.sleep(0.001)
            if engine.active_queues:
                break
        await engine.cancel_session("cancel-trace")
        with pytest.raises(asyncio.CancelledError):
            await task

    import pytest
    asyncio.run(flow())
    traces = engine.metrics.snapshot()["chunk_traces"]
    assert len(traces) == 1
    assert traces[0]["terminal_state"] == "cancelled"
    assert traces[0]["terminal_callbacks"] == 1
    assert traces[0]["owner_conflict"] is False
    assert not engine.active_tasks and not engine.active_queues
