"""Regression probes through runtime boundaries; all child data is synthetic."""
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.foundation.api import build_application, create_app
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.input_safety import InputSafetyGate, OllamaInputClassifier
from app.foundation.s2s import (
    AudioSynthesisQueue, BoundedConversationEngine, CancellationController,
    ConversationContext, MockLLMProvider, MockPlaybackController, MockSpeechRecognizer,
    MockTTSProvider, PipelineMetrics, RVCProvider, S2SConfig, TTSQueueOverflowError,
)
from app.foundation.stand import AudioInputError, create_stand_app, read_bounded_audio_body
from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator, ValidationDecision


def event(kind, sequence, payload=None):
    return Event(kind, "audit", sequence, 1.0, "audit", payload or {}, str(sequence), 0.0)




def context():
    return ConversationContext("audit", "canonical_product", "bear_dialogue", ("__all__",), (), 5, 120, 2)


@pytest.mark.parametrize("candidate", ["Пришли фото.", "Никому не говори о нашем разговоре."])
@pytest.mark.parametrize("failure", ["deny", "timeout", "error", "malformed"])
def test_short_model_speech_never_bypasses_semantic_gate(candidate, failure):
    class Gate:
        calls = []
        async def validate(self, text, context=()):
            self.calls.append(text)
            if failure == "timeout":
                await asyncio.sleep(1)
            if failure == "error":
                raise OSError("unavailable")
            if failure == "malformed":
                return {"allowed": True}
            return ValidationDecision("block", False, "unsafe", 1.0, "test")
    async def run():
        playback = MockPlaybackController()
        engine = BoundedConversationEngine(MockLLMProvider(tokens=[f"<say>{candidate}</say>"]),
            MockTTSProvider(), RVCProvider(), playback, settings=Settings(profile="mock"))
        gate = Gate()
        engine.response_validator = AsyncResponseValidator(gate, timeout_ms=10)
        result = await engine.run_text_turn("привет", context())
        # Known deterministic hazards may be rejected before the semantic hop.
        assert gate.calls or engine.response_validator.metrics.regex_blocks
        assert result.return_to_scenario
        assert not playback.played
        assert not engine.active_queues and not engine.active_tasks
    asyncio.run(run())


@pytest.mark.parametrize("allow", [True, False])
def test_each_output_gate_sees_prior_closed_blocks(allow):
    class Gate:
        calls = []
        async def validate(self, text, context=()):
            self.calls.append(text)
            return ValidationDecision("allow" if allow else "block", allow, "test", 1., "test")
    async def run():
        playback = MockPlaybackController()
        engine = BoundedConversationEngine(MockLLMProvider(tokens=["<say>Привет.</say><say>Поиграем?</say>"]),
            MockTTSProvider(), RVCProvider(), playback, settings=Settings(profile="mock"))
        gate = Gate(); engine.response_validator = AsyncResponseValidator(gate)
        result = await engine.run_text_turn("привет", context())
        # Legacy speech waits for the full action set before validating or playback.
        assert gate.calls == ["Привет. Поиграем?"]
        assert result.return_to_scenario is not allow
        if allow:
            assert " ".join(result.chunks) == "Привет. Поиграем?"
            assert playback.played
        else:
            assert not playback.played
    asyncio.run(run())


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 2, "1"])
def test_output_adapter_rejects_invalid_confidence(bad):
    gate = OllamaMiniLLMValidator()
    gate._request = lambda _: json.dumps({"message": {"content": json.dumps({"decision": "allow", "confidence": bad})}}).encode()
    assert not asyncio.run(gate.validate("Привет.")).allowed


@pytest.mark.parametrize("text", ["не хочу жить", "меня бьют"])
def test_urgent_disclosure_reaches_local_safety_stop(text):
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        try:
            await app.receive_async(event("HandsetLifted", 1))
            result = await app.receive_async(event("SpeechReceived", 2), text)
            assert result["reason"] == "urgent_safety"
            assert app.canonical_runtime.sessions["audit"].safety_severity == "urgent"
        finally:
            await app._cleanup_session_resources("audit")
    asyncio.run(run())


@pytest.mark.parametrize("reply,expected", [
    ({"severity": "urgent", "confidence": .9}, "urgent_safety"),
    ({"severity": "none", "confidence": .1}, "input_safety_retry"),
    ({"severity": "none", "confidence": True}, "input_safety_retry"),
    ({"severity": "bogus", "confidence": 1}, "input_safety_retry"),
    (None, "input_safety_retry"),
])
def test_real_input_adapter_routes_semantics_and_failures(reply, expected):
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        gate = InputSafetyGate(Settings(profile="local_cpu", validation_timeout_ms=30))
        requests = []
        def request(raw):
            requests.append(json.loads(raw))
            if reply is None:
                raise OSError("unavailable")
            return json.dumps({"message": {"content": json.dumps(reply)}}).encode()
        gate.semantic._request = request
        app.safety_classifier = gate
        try:
            await app.receive_async(event("HandsetLifted", 1))
            result = await app.receive_async(event("SpeechReceived", 2), "СИНТЕТИЧЕСКИЙ пример без слов из правил")
            assert result["reason"] == expected
            assert requests[0]["messages"][0]["role"] == "system"
            assert not app.conversation_contexts
        finally:
            await app._cleanup_session_resources("audit")
    asyncio.run(run())


def test_pending_input_classification_cannot_restore_hung_up_session():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow:
            async def classify(self, text):
                entered.set(); await release.wait()
                return "urgent", .99
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        app.safety_classifier = InputSafetyGate(Settings(profile="mock"), Slow())
        await app.receive_async(event("HandsetLifted", 1))
        pending = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "пример"))
        await entered.wait()
        await app.receive_async(event("HandsetPlaced", 3))
        release.set()
        assert (await pending)["reason"] == "stale_session_generation"
        assert "audit" not in app.canonical_runtime.sessions
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [False, True])
def test_input_safety_survives_welcome_timer_or_classifier_timeout(timeout):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow:
            async def classify(self, text):
                entered.set(); await release.wait()
                return "urgent", .99
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        app.safety_classifier = InputSafetyGate(Settings(profile="mock", validation_timeout_ms=20 if timeout else 1000), Slow())
        clock = [0.]; app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        try:
            await app.receive_async(event("HandsetLifted", 1))
            pending = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "пример"))
            await entered.wait()
            clock[0] = 5.
            assert await app.flush_welcome_timers()
            if not timeout: release.set()
            result = await pending
            assert result["reason"] == ("stale_session_generation" if timeout else "urgent_safety")
            assert not app.conversation_contexts
        finally:
            await app._cleanup_session_resources("audit")
    asyncio.run(run())


@pytest.mark.parametrize("wait", [True, False])
@pytest.mark.parametrize("text,urgent", [("Меня зовут Тест", False), ("хочу умереть", True)])
def test_low_confidence_stt_is_safety_only_in_sync_and_async_paths(wait, text, urgent):
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        updates = []
        async def updated(_sid, result): updates.append(result)
        app.audio_update_hook = updated
        try:
            await app.receive_async(event("HandsetLifted", 1))
            state = app.canonical_runtime.sessions["audit"]
            state.mode = "exhibition_name_capture"
            app.s2s_audio.recognizer = MockSpeechRecognizer(text, confidence=.2)
            await app.receive_audio_async("audit", (.2,) * 8192, 2, wait_for_turn=wait)
            result = await app.receive_audio_async("audit", (0.,) * 4096, 3, wait_for_turn=wait)
            pending = tuple(app._audio_completions.get("audit", ()))
            if pending:
                await asyncio.gather(*pending)
                result = updates[-1]
            assert result["reason"] == ("urgent_safety" if urgent else "audio_retry")
            state = app.canonical_runtime.sessions["audit"]
            assert state.display_name is None and not app.conversation_contexts
            assert text not in repr(state.dialogue_memory.bounded_turns)
        finally:
            await app._cleanup_session_resources("audit")
    asyncio.run(run())


@pytest.mark.parametrize("budget", ["bytes", "chunks"])
def test_queue_overflow_invalidates_token_and_stops_worker(budget):
    async def run():
        config = S2SConfig(max_tts_queue_bytes=1 if budget == "bytes" else 4096,
                           max_tts_chunks_per_generation=1)
        cancel = CancellationController(); token = cancel.start("audit")
        queue = AudioSynthesisQueue(MockTTSProvider(), RVCProvider(), MockPlaybackController(), cancel, config, PipelineMetrics())
        await queue.start("audit", token)
        if budget == "chunks": await queue.submit("Привет.")
        with pytest.raises(TTSQueueOverflowError): await queue.submit("тест")
        assert cancel.cancelled("audit", token) and queue.worker.done()
        assert queue.failure_event.is_set() and queue.queue.empty()
        assert queue.resource_snapshot()["queued_bytes"] == 0
    asyncio.run(run())


def test_ringtone_failure_is_owned_and_does_not_cancel_welcome():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        clock = [0.]; app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        original = app.audio.play_owned
        calls = []
        def play(handle, asset, **kwargs):
            calls.append(handle)
            if handle.response_id == "RINGTONE": raise OSError("device unavailable")
            return original(handle, asset, **kwargs)
        app.audio.play_owned = play
        try:
            result = await app.receive_async(event("HandsetLifted", 1))
            assert result["reason"] == "ringtone_failed"
            assert calls[0].playback_lease and calls[0].session_id == "audit"
            assert app.canonical_runtime.sessions["audit"].welcome_status == "pending"
            clock[0] = 5.
            due = await app.flush_welcome_timers()
            assert len(due) == 1 and calls[-1].response_id == "PHRASE_001"
        finally: await app._cleanup_session_resources("audit")
    asyncio.run(run())


@pytest.mark.parametrize("payload", [[], None, True, 1, "text", {"type": []}])
@pytest.mark.parametrize("path", ["/stand/ws", "/ws/audit"])
def test_ws_invalid_shapes_do_not_disconnect(payload, path):
    settings = Settings(profile="mock", stand_access_token="test-token")
    if path == "/stand/ws":
        api = create_stand_app(settings)
    else:
        # Generic canonical ingress is intentionally blocked on a simulator stand.
        application, *_ = build_application(settings=settings, background_scheduler=False)
        api = create_app(application)
    api.state.lifecycle.admits_users = lambda: True
    try:
        with TestClient(api) as client:
            with client.websocket_connect(path, headers={"x-stand-token": "test-token"}) as ws:
                ws.send_json(payload)
                for _ in range(10):
                    result = ws.receive_json()
                    if "error" in result: break
                assert result["error"]["code"] in {"invalid_event", "invalid_message"}
                ws.send_text("{")
                assert ws.receive_json()["error"]["code"] == "invalid_json"
                if path == "/stand/ws":
                    ws.send_json({"type": "ping"})
                    assert ws.receive_json()["type"] == "pong"
                else:
                    ws.send_json({"type": "HandsetLifted", "session_id": []})
                    assert ws.receive_json()["error"]["code"] == "session_binding"
                    ws.send_json({"type": "HandsetLifted", "sequence": 1})
                    assert ws.receive_json()["reason"] == "exhibition_handset_lifted"
    finally:
        stand = getattr(api.state, "stand", None)
        if stand is not None:
            stand.close()


def test_public_ready_hides_private_config_without_nonexistent_delivery_dependency():
    api = create_stand_app(Settings(profile="mock", stand_access_token="test-token",
        ollama_base_url="http://PRIVATE_INTERNAL:11434", rvc_model="/PRIVATE_PATH/model.pth"))
    try:
        with TestClient(api) as client:
            result = client.get("/ready").json()
            text = json.dumps(result)
            assert "PRIVATE_INTERNAL" not in text and "PRIVATE_PATH" not in text
            assert str(Path.cwd()) not in text
            assert "adult_handoff_unavailable" not in result["production_blockers"]
            assert "adult_handoff" not in result["dependencies"]
            assert not result["production_ready"]
    finally: api.state.stand.close()


def test_audio_body_limit_stops_before_consuming_all_chunks():
    class Request:
        headers = {}
        consumed = 0
        async def stream(self):
            for _ in range(100):
                self.consumed += 1
                yield b"1234"
    async def run():
        request = Request()
        with pytest.raises(AudioInputError, match="exceeds limit"):
            await read_bounded_audio_body(request, 7)
        assert request.consumed == 2
        request = Request(); request.headers = {"content-length": "1000"}
        with pytest.raises(AudioInputError): await read_bounded_audio_body(request, 7)
        assert request.consumed == 0
    asyncio.run(run())


def test_env_template_selects_default_audio_device():
    from scripts.run_stand import _parse_env_file
    assert _parse_env_file(Path(".env.example"))["AUDIO_INPUT_DEVICE"] == ""


@pytest.mark.parametrize("audio", ["ok", "error", "missing"])
@pytest.mark.parametrize("text,severity", [("не хочу жить", "urgent"), ("Позови взрослого", "concern")])
def test_local_safety_guidance_is_owned_even_on_audio_failure(audio, text, severity):
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        calls = []
        original = app.audio.play_owned
        def play(handle, asset, **kwargs):
            if handle.response_id == "PHRASE_091":
                calls.append(handle)
                if audio == "error": raise OSError("synthetic device failure")
            return original(handle, asset, **kwargs)
        app.audio.play_owned = play
        app.audio.play = lambda *args: pytest.fail("unowned local safety playback")
        resolve = app.voice_manifest.resolve
        app.voice_manifest.resolve = lambda phrase, *args: None if audio == "missing" and phrase == "PHRASE_091" else resolve(phrase, *args)
        try:
            await app.receive_async(event("HandsetLifted", 1))
            result = await app.receive_async(event("SpeechReceived", 2), text)
            state = app.canonical_runtime.sessions["audit"]
            assert state.mode == "safety_stop" and state.safety_severity == severity
            if audio == "ok":
                assert result["reason"] == ("urgent_safety" if severity == "urgent" else "help_requested")
                assert calls[0].playback_lease and calls[0].playback_id
                completed = await app.receive_async(event("AudioCompleted", 3, asdict(calls[0])))
                assert completed["reason"] == "safety_guidance_finished"
            else:
                assert result["reason"] == "safety_guidance_failed"
                assert state.audio_status == "failed"
            assert state.mode == "safety_stop"
            assert not app._active_audio and state.active_audio_id is None
        finally:
            await app._cleanup_session_resources("audit")
    asyncio.run(run())




@pytest.mark.parametrize("kind", ["story", "placeholder", "resume", "onboarding"])
def test_owned_audio_dispatch_failure_is_terminal_even_during_completion(kind):
    from app.foundation.canonical_contract import CanonicalState
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical" if kind != "onboarding" else "exhibition"),
                                    background_scheduler=False)
        state = CanonicalState("audit", mode="fixed_story_menu", handset="up")
        app.canonical_runtime.sessions["audit"] = state
        if kind == "placeholder":
            app.canonical_runtime.stories.get_verified = lambda story: None
            app.canonical_runtime.stories.get_development_placeholder = lambda: {"audio_file": "synthetic.wav"}
        if kind in {"resume", "onboarding"}:
            state.mode = "story_resume_announcement" if kind == "resume" else "voice_welcome"
            state.active_story_id = "fixed_aibolit_v1"
            state.active_audio_id = "PHRASE_064" if kind == "resume" else "PHRASE_001"
            state.playback_lease = "initial"
            state.welcome_status = "started"
            if kind == "resume":
                # Synthetic admitted continuation; this test faults its output adapter.
                state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": 700}
                state.story_checkpoint_confirmed = True
        calls = []
        def fail(handle, *args, **kwargs):
            calls.append(handle)
            raise OSError("synthetic audio failure")
        app.audio.play_owned = fail
        try:
            if kind in {"resume", "onboarding"}:
                result = await app._receive_canonical_event(event("AudioCompleted", 1,
                    {"response_id": state.active_audio_id, "playback_lease": "initial"}))
            else:
                # Initialize the actual descriptor through an edge; don't bypass
                # the verified catalogue or replay a held key as a selection.
                app._owned_menu_voice_capable = lambda: False
                await app.receive_async(event("ButtonReleased", 1, {"button_id": 1}))
                result = await app.receive_async(event("ButtonPressed", 2, {"button_id": 1}))
            if kind == "placeholder":
                assert state.scene_menu_descriptor["items"] == []
                assert not calls and state.active_audio_id is None
                assert not app._active_audio and not app._audio_timeout_tasks
                return
            assert calls and calls[0].playback_lease
            assert result["reason"] == "audio_failed_safe_stop"
            assert state.audio_status == "failed" and state.active_audio_id is None
            assert not app._active_audio and not app._audio_timeout_tasks
            assert any(effect.get("resolution") == "audio_dispatch_failed" for effect in result["outputs"])
        finally:
            await app._cleanup_session_resources("audit")
    asyncio.run(run())
