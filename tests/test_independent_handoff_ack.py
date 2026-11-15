"""Handoff authenticity regression probes; synthetic data, no external services."""
import asyncio

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event


def ev(kind, seq, payload=None, source="phone"):
    return Event(kind, "review", seq, 1.0, source, payload or {}, f"{source}:{seq}", 0.0)


def test_handoff_acknowledgement_cannot_be_asserted_by_public_event():
    from app.foundation.events import parse_event, EventValidationError
    with pytest.raises(EventValidationError):
        parse_event({"type": "AdultHandoffAcknowledged", "incident_id": "synthetic", "handoff_id": "synthetic"}, "review", 1)


def test_public_ack_does_not_play_false_delivery_confirmation():
    from app.foundation.events import parse_event, EventValidationError
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        app.adult_handoff = None
        try:
            await app.receive_async(ev("HandsetLifted", 1))
            await app.receive_async(ev("SpeechReceived", 2), "не хочу жить")
            incident = app.canonical_runtime.sessions["review"].incident_id
            try:
                forged = parse_event({"type": "AdultHandoffAcknowledged", "incident_id": incident}, "review", 3)
            except EventValidationError:
                return
            result = await app.receive_async(forged)
            assert result["reason"] != "adult_handoff_acknowledged"
            assert not any(item.get("phrase_id") == "PHRASE_089" for item in result["outputs"])
        finally:
            await app._cleanup_session_resources("review")
    asyncio.run(run())


def test_direct_ingress_cannot_self_authorize_handoff_ack():
    from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, CanonicalState
    runtime = CanonicalRuntime()
    state = CanonicalState("review", mode="safety_handoff", handset="up", incident_id="incident")
    runtime.sessions["review"] = state
    forged = CanonicalEvent("AdultHandoffAcknowledged", "review", 1, 0,
                            "adult_handoff_simulator", {"incident_id": "incident", "authorized": True})
    assert runtime.handle(forged)["reason"] == "handoff_ack_untrusted"
    assert not state.last_sequence_by_source
    assert state.active_audio_id is None


@pytest.mark.parametrize("ack", ["false", "true", 1, 0, None, {}, []])
def test_invalid_adapter_ack_cannot_claim_delivery(ack):
    class InvalidHandoff:
        async def request(self, payload):
            return {"acknowledged": ack}
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        app.adult_handoff = InvalidHandoff()
        try:
            await app.receive_async(ev("HandsetLifted", 1))
            result = await app.receive_async(ev("SpeechReceived", 2), "не хочу жить")
            assert result["reason"] == "adult_handoff_delivery_failed"
            assert any(item.get("phrase_id") == "PHRASE_091" for item in result["outputs"])
            assert not any(item.get("phrase_id") == "PHRASE_089" for item in result["outputs"])
        finally:
            await app._cleanup_session_resources("review")
    asyncio.run(run())


@pytest.mark.parametrize("ack", [False, True])
def test_valid_adapter_ack_keeps_delivery_semantics(ack):
    class Delivery:
        async def request(self, payload):
            return {"acknowledged": ack}
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        app.adult_handoff = Delivery()
        try:
            await app.receive_async(ev("HandsetLifted", 1))
            result = await app.receive_async(ev("SpeechReceived", 2), "не хочу жить")
            assert result["reason"] == "urgent_safety"
            assert any(item.get("phrase_id") == "PHRASE_089" for item in result["outputs"]) is ack
            assert not any(item.get("phrase_id") == "PHRASE_091" for item in result["outputs"])
            assert app.canonical_runtime.sessions["review"].safety_severity == "urgent"
        finally:
            await app._cleanup_session_resources("review")
    asyncio.run(run())
