"""Direct caregiver commands are intents, never evidence of delivery."""
import asyncio
import json

import pytest

from app.foundation.api import build_application
from app.foundation.canonical_services import SafetyClassifier, classify_intent
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.input_safety import InputSafetyGate

COMMANDS = [
    "Позови маму", "Позови папу!", "Позовите маму", "Попроси папу подойти",
    "Попросите маму подойти", "Мишка, пожалуйста, позови маму",
    "Пожалуйста, Мишка, попроси папу подойти!", "Позови, пожалуйста, маму",
    "Позови папу, пожалуйста", "Позови маму ко мне", "Попроси папу подойти сюда",
    "Мишка позови маму?",
]
BENIGN = [
    "В сказке герой позвал маму", "Как позвать маму?", "Как попросить папу подойти?",
    "Расскажи сказку про маму", "Мама читает сказку", "Не позови маму",
    "Не надо звать папу", "Герой сказал: позови маму", 'Скажи «позови маму»',
    'В сказке герой сказал: «Позови маму»', "Позови маму в сказке",
    "Почему ты говоришь позови маму?", "Я хочу узнать, как позвать маму",
]


@pytest.mark.parametrize("text", COMMANDS)
def test_direct_caregiver_command_is_request_not_new_severity(text):
    assert classify_intent(text) == "request_adult"
    assert SafetyClassifier().classify(text).severity == "none"


@pytest.mark.parametrize("text", BENIGN)
def test_parent_mentions_and_advice_are_not_call_requests(text):
    assert classify_intent(text) != "request_adult"


def event(kind, seq):
    return Event(kind, "synthetic-caregiver", seq, 1., "phone", {}, f"phone:{seq}", 0.)


def gate(available):
    result = InputSafetyGate(Settings(profile="local_cpu", validation_timeout_ms=50))
    def request(_payload):
        if not available:
            raise OSError("synthetic unavailable")
        return json.dumps({"message": {"content": json.dumps({"severity": "none", "confidence": .99})}}).encode()
    result.semantic._request = request
    return result


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("text", ["Позови маму", "Позови папу", "Попроси папу подойти", "Мишка, пожалуйста, позови маму"])
def test_actual_request_path_does_not_generate_or_claim_delivery(text, available):
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        try:
            await app.receive_async(event("HandsetLifted", 1))
            state = app.canonical_runtime.sessions["synthetic-caregiver"]
            state.mode = "bear_dialogue"
            app.safety_classifier = gate(available)
            played = []
            original = app.audio.play_owned
            app.audio.play_owned = lambda handle, asset, **kw: (played.append(handle.response_id), original(handle, asset, **kw))[-1]
            async def no_generation(*args, **kwargs):
                pytest.fail("Caregiver request must not enter dialogue generation")
            if app.bounded_s2s is not None:
                app.bounded_s2s.run_text_turn = no_generation
            result = await app.receive_async(event("SpeechReceived", 2), text)
            assert result["reason"] == "adult_handoff_delivery_failed"
            assert state.mode == "safety_handoff" and state.incident_id
            assert len(app.adult_handoff.outbox) == 1
            assert played == ["PHRASE_091"] and "PHRASE_089" not in played
            assert not app.conversation_contexts
        finally:
            await app._cleanup_session_resources("synthetic-caregiver")
    asyncio.run(run())


@pytest.mark.parametrize("text", ["Как позвать маму?", "В сказке герой позвал маму"])
def test_advice_or_fiction_does_not_force_handoff_in_actual_app(text):
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        try:
            await app.receive_async(event("HandsetLifted", 1))
            state = app.canonical_runtime.sessions["synthetic-caregiver"]
            state.mode = "exhibition_name_capture"
            app.safety_classifier = gate(True)
            await app.receive_async(event("SpeechReceived", 2), text)
            assert state.mode != "safety_handoff"
            assert not state.incident_id and not app.adult_handoff.outbox
        finally:
            await app._cleanup_session_resources("synthetic-caregiver")
    asyncio.run(run())
