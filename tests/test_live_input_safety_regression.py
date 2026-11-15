"""Synthetic real application/adapter paths for the first live safety failure."""
import asyncio
import json

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.input_safety import InputSafetyGate
from app.foundation.stand import HardwareIntegrationStand

SID = "synthetic-live-regression"


def event(kind, seq, payload=None, source="phone"):
    return Event(kind, SID, seq, 1., source, payload or {}, f"{source}:{seq}", 0.)


def wire_gate(outcome):
    gate = InputSafetyGate(Settings(profile="local_cpu", validation_timeout_ms=15))
    def request(_payload):
        if isinstance(outcome, Exception):
            raise outcome
        return json.dumps({"message": {"content": json.dumps(outcome)}}).encode()
    gate.semantic._request = request
    return gate


async def ready_app(mode="exhibition_name_capture"):
    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    await app.receive_async(event("HandsetLifted", 1))
    state = app.canonical_runtime.sessions[SID]
    state.mode = mode
    played = []
    play = app.audio.play_owned
    app.audio.play_owned = lambda handle, asset, **kw: (played.append(handle.response_id), play(handle, asset, **kw))[-1]
    return app, played


@pytest.mark.parametrize("outcome,code", [
    (OSError("PRIVATE transport detail"), "input_safety_unavailable"),
    (TimeoutError(), "input_safety_timeout"),
    ({"severity": "bogus", "confidence": 1}, "input_safety_invalid"),
    ({"severity": "none", "confidence": True}, "input_safety_invalid"),
    ({"severity": "none", "confidence": .4}, "input_safety_uncertain"),
])
def test_wire_gate_failure_neither_accepts_name_nor_invents_incident(outcome, code):
    async def run():
        app, played = await ready_app()
        app.safety_classifier = wire_gate(outcome)
        try:
            result = await app.receive_async(event("SpeechReceived", 2), "Меня зовут Костя")
            state = app.canonical_runtime.sessions[SID]
            assert result["reason"] == "input_safety_retry"
            assert state.mode == "exhibition_name_capture"
            assert state.display_name is None and not state.onboarding_name_collected
            assert state.incident_id is None and state.safety_severity == "none"
            assert not app.adult_handoff.outbox
            assert played == ["PHRASE_074"]
            diagnostic = [x for x in app.canonical_diagnostics if x["kind"] == "input_safety_decision"][-1]
            safe = HardwareIntegrationStand._safe_diagnostic_item(diagnostic)
            assert safe["code"] == code and safe["safety_verified"] is False
            assert "Костя" not in json.dumps(list(app.canonical_diagnostics), ensure_ascii=False)
            assert "PRIVATE" not in json.dumps(safe)
            # The next independently verified utterance can recover normally.
            app.safety_classifier = wire_gate({"severity": "none", "confidence": .99})
            recovered = await app.receive_async(event("SpeechReceived", 3), "Меня зовут Костя")
            assert recovered["reason"] == "exhibition_age_question_started"
            assert played[-1] == "PHRASE_108" and "PHRASE_089" not in played
        finally:
            await app._cleanup_session_resources(SID)
    asyncio.run(run())


@pytest.mark.parametrize("mode,text", [
    ("exhibition_age_selection", "Мне шесть лет"),
    ("exhibition_menu_repeat_listen", "Да"),
    ("bear_dialogue", "Привет"),
    ("personal_story_theme", "Сказку о лесе"),
])
def test_unverified_none_retries_without_normal_routing(mode, text):
    async def run():
        app, played = await ready_app(mode)
        app.safety_classifier = wire_gate({"severity": "none", "confidence": .5})
        try:
            result = await app.receive_async(event("SpeechReceived", 2), text)
            state = app.canonical_runtime.sessions[SID]
            assert result["reason"] == "input_safety_retry" and state.mode == mode
            assert not state.incident_id and not app.conversation_contexts
            assert played == ["PHRASE_074"]
        finally: await app._cleanup_session_resources(SID)
    asyncio.run(run())


def test_gate_failure_preserves_approved_story_and_pending_game_choice():
    async def run(mode):
        app, played = await ready_app(mode)
        app.safety_classifier = wire_gate(OSError())
        state = app.canonical_runtime.sessions[SID]
        state.substate = "game_exit_confirmation" if mode == "game_active" else "story_in_progress"
        previous = state.substate
        try:
            result = await app.receive_async(event("SpeechReceived", 2), "Привет")
            assert result["reason"] == "input_safety_retry"
            assert state.mode == mode and state.substate == previous
            assert not state.incident_id and not played
        finally: await app._cleanup_session_resources(SID)
    for mode in ("fixed_story_playing", "game_active"): asyncio.run(run(mode))


@pytest.mark.parametrize("text,outcome,severity", [
    ("Я не хочу жить", OSError(), "urgent"),
    ("Мне страшно", OSError(), "concern"),
    ("Синтетическое сообщение", {"severity": "urgent", "confidence": .1}, "urgent"),
    ("Синтетическое сообщение", {"severity": "concern", "confidence": .4}, "concern"),
])
def test_retry_does_not_suppress_detected_danger(text, outcome, severity):
    async def run():
        app, played = await ready_app()
        app.safety_classifier = wire_gate(outcome)
        try:
            await app.receive_async(event("SpeechReceived", 2), text)
            state = app.canonical_runtime.sessions[SID]
            assert state.mode == "safety_handoff" and state.safety_severity == severity
            assert state.incident_id and app.adult_handoff.outbox
            assert "PHRASE_089" not in played
            assert "PHRASE_091" in played
            safe = [HardwareIntegrationStand._safe_diagnostic_item(x) for x in app.canonical_diagnostics]
            assert any(x.get("kind") == "input_safety_decision" for x in safe)
        finally: await app._cleanup_session_resources(SID)
    asyncio.run(run())


def test_diagnostic_allowlist_cannot_publish_classifier_payloads():
    item = {"kind": "input_safety_decision", "code": "PRIVATE text", "text": "PRIVATE text",
            "safety_severity": "PRIVATE text", "safety_verified": "PRIVATE text",
            "latency_ms": float("nan"), "stt_trusted": True}
    safe = HardwareIntegrationStand._safe_diagnostic_item(item)
    assert safe == {"kind": "input_safety_decision", "code": "unknown", "stt_trusted": True}


@pytest.mark.parametrize("outcome", [OSError(), {"severity": "none", "confidence": .4}])
@pytest.mark.parametrize("untrusted_stt", [True, False])
def test_explicit_adult_request_survives_classifier_failure(outcome, untrusted_stt):
    async def run():
        app, played = await ready_app()
        app.safety_classifier = wire_gate(outcome)
        try:
            await app.receive_async(event("SpeechReceived", 2, {"_stt_untrusted": untrusted_stt}), "Позови взрослого")
            state = app.canonical_runtime.sessions[SID]
            assert state.mode == "safety_handoff" and state.incident_id
            assert app.adult_handoff.outbox
            assert played == ["PHRASE_091"]
            assert state.display_name is None
        finally: await app._cleanup_session_resources(SID)
    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["WelcomeTimerFired", "HandsetPlaced"])
@pytest.mark.parametrize("verified", [True, False])
def test_explicit_help_survives_ordinary_revision_but_not_session_end(interruption, verified):
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        entered, release = asyncio.Event(), asyncio.Event()
        class Semantic:
            async def classify(self, text):
                entered.set()
                await release.wait()
                if not verified: raise OSError()
                return "none", .99
        app.safety_classifier = InputSafetyGate(Settings(profile="mock", validation_timeout_ms=1000), Semantic())
        task = None
        try:
            await app.receive_async(event("HandsetLifted", 1))
            task = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "Позови взрослого"))
            await entered.wait()
            deadline = app.canonical_runtime.sessions[SID].welcome_deadline_monotonic
            await app.receive_async(Event(interruption, SID, 1, 5., "timer" if interruption == "WelcomeTimerFired" else "hid", {}, "interrupt", deadline + .01))
            release.set()
            result = await task
            if interruption == "HandsetPlaced":
                assert result["reason"] == "stale_session_generation"
                assert not app.adult_handoff.outbox
            else:
                state = app.canonical_runtime.sessions[SID]
                assert state.incident_id and state.mode == "safety_handoff"
                assert len(app.adult_handoff.outbox) == 1
                assert result["reason"] == "adult_handoff_delivery_failed"
        finally:
            release.set()
            if task is not None: await asyncio.gather(task, return_exceptions=True)
            await app._cleanup_session_resources(SID)
    asyncio.run(run())
