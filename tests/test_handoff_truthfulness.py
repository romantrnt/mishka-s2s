"""No developer simulator may tell a child that an adult received a request.

Build the actual CPU/GPU factories; inject only the semantic result and playback
sink. No models, GPU inference, physical device, or real recipient is used.
"""
import asyncio

import pytest

from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, CanonicalState
from app.foundation.canonical_services import SimulatedAdultHandoff
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import MockPlaybackController


def event(kind, sequence, payload=None, source="phone", monotonic=15.0):
    return Event(kind, "handoff-truth", sequence, 100.0, source, payload or {},
                 f"{source}:{sequence}", monotonic)


async def name_capture(app):
    await app.receive_async(event("HandsetLifted", 1, monotonic=10.0))
    await app.receive_async(event("WelcomeTimerFired", 1, source="timer"))
    for sequence, phrase in enumerate(("PHRASE_001", "PHRASE_078", "PHRASE_099"), 1):
        lease = app._active_audio["handoff-truth"]
        callback = {key: lease[key] for key in ("token", "generation", "playback_lease", "playback_id")}
        callback["response_id"] = phrase
        assert callback["response_id"] == phrase
        await app.receive_async(event("AudioCompleted", sequence, callback, "audio"))
    assert app.canonical_runtime.sessions["handoff-truth"].mode == "exhibition_name_capture"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["mock", "local_cpu", "local_gpu"])
@pytest.mark.parametrize("utterance,failure", [("Меня зовут Аня", False), ("Меня зовут Аня", True), ("Меня зовут Аня", "uncertain"),
                                               ("Позвать взрослого", False), ("не хочу жить", False)])
async def test_real_factory_name_request_and_urgent_never_fabricate_delivery(profile, utterance, failure):
    app, *_ = build_application(settings=Settings(profile=profile, voice_conversion="off"),
                                playback_override=MockPlaybackController(), background_scheduler=False)
    class Semantic:
        async def classify(self, text):
            if failure is True:
                raise TimeoutError("synthetic semantic unavailability")
            return "none", 0.5 if failure == "uncertain" else 1.0
    app.safety_classifier.semantic = Semantic()
    try:
        await name_capture(app)
        result = await app.receive_async(event("SpeechReceived", 2), utterance)
        assert not any(item.get("phrase_id") == "PHRASE_089" for item in result["outputs"])
        assert not any(item.get("resolution") == "adult_handoff_acknowledged" for item in result["outputs"])
        if utterance == "Меня зовут Аня" and not failure:
            assert result["reason"] == "exhibition_age_question_started"
            assert app.canonical_runtime.sessions["handoff-truth"].display_name == "Аня"
        elif failure:
            assert result["reason"] == "input_safety_retry"
            state = app.canonical_runtime.sessions["handoff-truth"]
            assert state.incident_id is None
            assert state.display_name is None
            assert state.mode == "exhibition_name_capture"
            assert not any(item.get("phrase_id") == "PHRASE_091" for item in result["outputs"])
        else:
            assert app.canonical_runtime.sessions["handoff-truth"].incident_id
            assert any(item.get("phrase_id") == "PHRASE_091" for item in result["outputs"])
        if profile != "mock":
            assert app.adult_handoff is None
    finally:
        await app.shutdown_identity()
        await app._cleanup_session_resources("handoff-truth")


@pytest.mark.asyncio
async def test_demo_outbox_is_not_a_delivery_receipt():
    transport = SimulatedAdultHandoff()
    result = await transport.request({"incident_id": "synthetic"})
    assert result["simulated"] is True
    assert result["acknowledged"] is False
    assert transport.outbox[0]["delivery"] != "acknowledged"
    assert transport.outbox[0]["incident_id"] == "synthetic"


@pytest.mark.asyncio
@pytest.mark.parametrize("simulation", [True, "true", "false", 1])
async def test_adapter_simulation_cannot_emit_real_ack(simulation):
    class LegacySimulator:
        async def request(self, payload):
            return {"acknowledged": True, "simulated": simulation}
    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    app.adult_handoff = LegacySimulator()
    try:
        await app.receive_async(event("HandsetLifted", 1))
        result = await app.receive_async(event("SpeechReceived", 2), "не хочу жить")
        assert not any(item.get("phrase_id") == "PHRASE_089" for item in result["outputs"])
        assert any(item.get("phrase_id") == "PHRASE_091" for item in result["outputs"])
    finally:
        await app.shutdown_identity()
        await app._cleanup_session_resources("handoff-truth")


def test_runtime_rejects_simulated_ack_even_when_authorized():
    runtime = CanonicalRuntime()
    state = CanonicalState("handoff-truth", mode="safety_handoff", handset="up", incident_id="synthetic")
    runtime.sessions[state.session_id] = state
    result = runtime.handle(CanonicalEvent("AdultHandoffAcknowledged", state.session_id, 1, 0,
        "internal", {"incident_id": "synthetic", "simulated": True}), handoff_ack_authorized=True)
    assert not any(item.get("phrase_id") == "PHRASE_089" for item in result["effects"])
    assert state.incident_id == "synthetic"
