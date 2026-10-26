from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.foundation.api import create_app
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.hardware import encode_frame
from app.foundation.s2s import TurnResult
from app.foundation.stand import build_application, create_stand_app


def test_dialogue_fallback_is_internal_only_across_uart_and_websocket():
    sid = "dialogue-forgery"
    stand_api = create_stand_app(Settings(
        profile="mock", hardware_mode="simulator", stand_access_token="test-token",
        mutation_auth_policy="token_required",
    ))
    stand_api.state.lifecycle.admits_users = lambda: True
    stand = stand_api.state.stand
    stand.active_session_id = sid
    stand.application.canonical_runtime.sessions[sid] = CanonicalState(
        sid, mode="bear_dialogue", handset="up",
    )
    try:
        with TestClient(stand_api) as client:
            client.headers.update({"x-stand-token": "test-token"})
            frame = encode_frame("DialogueFallback", stand.adapter._last_received_sequence + 1, {
                "reason": "bounded_limit", "authorized": True,
                "source": "bounded_dialogue", "actor": "provider",
            }).decode()
            response = client.post("/stand/raw", json={"frame": frame})
            assert response.status_code == 422
            assert stand.application.canonical_runtime.sessions[sid].mode == "bear_dialogue"
    finally:
        stand.close()

    app, _, _, _ = build_application(settings=Settings(
        profile="mock", runtime_mode="clinical", stand_access_token="test-token",
        mutation_auth_policy="token_required",
    ))
    app.canonical_runtime.sessions[sid] = CanonicalState(
        sid, mode="bear_dialogue", handset="up",
    )
    api = create_app(app)
    api.state.lifecycle.admits_users = lambda: True
    with TestClient(api) as client:
        with client.websocket_connect(f"/ws/{sid}", headers={"x-stand-token": "test-token"}) as ws:
            ws.send_json({"type": "DialogueFallback", "sequence": 1,
                          "reason": "bounded_limit", "authorized": True,
                          "source": "bounded_dialogue", "actor": "provider"})
            rejected = ws.receive_json()
            assert rejected["error"]["code"] == "invalid_event"
            assert app.canonical_runtime.sessions[sid].mode == "bear_dialogue"


def test_real_bounded_provider_fallback_still_emits_approved_phrase():
    class Limited:
        async def run_text_turn(self, _text, _context):
            return TurnResult((), (), True, "bounded_limit")

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "provider-limit"
        app.canonical_runtime.sessions[sid] = CanonicalState(
            sid, mode="bear_dialogue", handset="up",
        )
        app.bounded_s2s = Limited()
        result = await app.receive_async(Event(
            "VoiceIntentReady", sid, 1, 1.0, "test", {"intent": "free_speech"},
            "provider-limit-1", 0.0,
        ), transient_text="ещё")
        assert result["reason"] == "dialogue_turn"
        assert any(effect.get("type") == "speak" and effect.get("phrase_id") == "PHRASE_095"
                   for effect in result["outputs"])
        assert app.canonical_runtime.sessions[sid].mode == "root_menu"

    asyncio.run(run())

def test_story_menu_transition_lights_digit_channels_and_only_resume_stop_fills():
    from app.foundation.canonical_services import LED_PRESETS
    from app.foundation.lighting import BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "story-menu-lights"
        app.canonical_runtime.sessions[sid] = CanonicalState(
            sid, mode="root_menu", handset="up",
        )
        result = await app.receive_async(Event(
            "ButtonPressed", sid, 1, 1.0, "test", {"button_id": 2}, "story-menu-1", 0.0,
        ))
        assert result["canonical_state"]["mode"] == "fixed_story_menu"
        assert LED_PRESETS["STORY_MENU"]
        frame = app.led.output.state.elements
        for button in range(1, 8):
            assert frame[BUTTON_DIGIT_ELEMENTS[button][1]] == 1
            assert frame[BUTTON_FILL_ELEMENTS[button]] == (1 if button in {6, 7} else 0)

    asyncio.run(run())

def test_validated_guest_age_controls_prompt_and_profile_context_without_trusting_raw_age():
    from app.foundation.age_policy import policy_for_band
    from app.foundation.s2s import ProfileContext

    app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
    sid = "guest-seven"
    state = CanonicalState(sid, mode="bear_dialogue", handset="up")
    state.age_years = 7
    state.age_band = "6-7"
    state.age_band_source = "exhibition_button"
    state.age_policy = policy_for_band("6-7", source="exhibition_button").as_dict()
    app.canonical_runtime.sessions[sid] = state
    context = app._canonical_audio_context(sid)
    assert context.profile.age == 7
    assert context.profile.age_band == "6-7"
    assert context.profile.age_source == "exhibition_button"
    assert '"age_years":7' in context.system_prompt
    assert '"age_band":"6-7"' in context.system_prompt
    assert '"age_band_source":"exhibition_button"' in context.system_prompt

    state.age_years = 99
    invalid = app._canonical_audio_context(sid)
    assert invalid.profile.age is None
    assert invalid.profile.age_band == "3-5"
    assert '"age_band":"3-5"' in invalid.system_prompt

    assert ProfileContext.from_profile({"age": 16}).age is None


def test_dob_profile_age_policy_remains_derived_from_dob():
    app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
    sid = "dob-profile-age"
    profile = {"profile_ref": "opaque-test", "date_of_birth": "2022-01-01",
               "age": 17, "display_name": "Тест"}
    state = app.canonical_runtime._new_state(sid, profile)
    state.mode = "bear_dialogue"
    state.handset = "up"
    app.canonical_runtime.sessions[sid] = state
    app._canonical_profiles[sid] = profile
    context = app._canonical_audio_context(sid)
    assert state.age_band_source == "profile_dob"
    assert context.profile.age == state.age_years
    assert context.profile.age_band == state.age_band
    assert context.profile.age_source == "profile_dob"
    assert f'"age_years":{state.age_years}' in context.system_prompt

def test_private_dialogue_name_stays_out_of_public_state_and_audio_completion():
    import json

    private_name = "PRIVATE_CHILD_NAME_247"
    sid = "private-projection"
    api = create_stand_app(Settings(
        profile="mock", hardware_mode="simulator", stand_access_token="test-token",
        mutation_auth_policy="token_required",
    ))
    stand = api.state.stand
    app = stand.application
    state = CanonicalState(sid, mode="bear_dialogue", handset="up")
    state.display_name = private_name
    state.dialogue_memory.bounded_turns.append({"kind": "name", "value": private_name})
    state.history_tail.append(private_name)
    app.canonical_runtime.sessions[sid] = state
    app._canonical_profiles[sid] = {"display_name": private_name}
    app.last_canonical_effects = [{"type": "speak_dynamic", "text": private_name}]
    app.last_canonical_event = {"type": "VoiceIntentReady", "payload": {"text": private_name}}
    stand.raw_frames.append({"raw": private_name, "decoded": {
        "type": "text", "sequence": 1, "payload": {"text": private_name},
    }})
    app.canonical_trace.append({"input": private_name, "normalized_event": private_name,
                                "canonical_event": "VoiceIntentReady", "state_before": "bear_dialogue",
                                "state_after": "bear_dialogue", "effects": [private_name],
                                "audio": private_name})
    app.audio_runtime_diagnostics.append({"kind": "audio_playback_failed", "detail": private_name})
    assert private_name in app._canonical_audio_context(sid).system_prompt
    assert private_name not in json.dumps(state.snapshot(), ensure_ascii=False)

    published = []
    async def capture(message):
        published.append(message)
        return []

    async def run():
        app.last_canonical_state = state.snapshot()
        stand.hub.publish = capture
        await stand._publish_audio_completion(sid, {
            "reason": "test_completion", "session_id": sid,
            "canonical_state": state.snapshot(),
            "outputs": [{"type": "speak_dynamic", "text": private_name}],
            "debug": {"child_name": private_name},
        })

    try:
        asyncio.run(run())
        assert private_name not in json.dumps(app.last_canonical_state, ensure_ascii=False)
        assert private_name not in json.dumps(list(stand.results), ensure_ascii=False)
        assert private_name not in json.dumps(stand.snapshot(), ensure_ascii=False)
        assert private_name not in json.dumps(stand.diagnostics_snapshot(), ensure_ascii=False)
        assert private_name not in json.dumps(published, ensure_ascii=False)
    finally:
        stand.close()


def test_recover_handles_async_and_sync_readiness_and_rejects_invalid_results():
    import warnings
    from types import SimpleNamespace
    from app.foundation.runtime import S2SRuntimeManager

    class Component:
        def __init__(self, value, async_read=False):
            self.value = value
            self.async_read = async_read
        def readiness(self):
            if self.async_read:
                async def value():
                    return self.value
                return value()
            return self.value

    class Engine:
        async def cancel_all_sessions(self):
            return {}

    class Lifecycle:
        async def begin_recovery(self, *_args):
            return 1
        def finish_recovery(self, _generation, result):
            self.result = result

    async def run(value, async_read):
        good = Component({"ready": True})
        manager = S2SRuntimeManager(SimpleNamespace(profile="production_candidate"),
                                    Component(value, async_read), good, good, Engine(),
                                    vad=good, stt=good, playback=good)
        async def resources():
            return {"ready": True}
        async def warmup():
            return {"ok": True}
        manager.resource_readiness = resources
        manager.warmup = warmup
        return await manager.recover(Lifecycle(), "test")

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert asyncio.run(run({"ready": True}, True))["ok"] is True
        assert asyncio.run(run({"ready": True}, False))["ok"] is True
        assert asyncio.run(run({"ready": False}, True))["reason"] == "dependency_unavailable"
        assert asyncio.run(run({"ready": "yes"}, True))["reason"] == "dependency_unavailable"
        assert asyncio.run(run(["ready"], False))["reason"] == "dependency_unavailable"


def test_lifespan_closes_owned_stand_pty_and_executors_on_normal_and_failed_start():
    import os
    import pytest

    settings = Settings(profile="mock", hardware_mode="simulator", stand_access_token="test-token",
                        mutation_auth_policy="token_required")
    api = create_stand_app(settings)
    stand = api.state.stand
    with TestClient(api):
        assert not stand._closed
    assert stand._closed
    assert stand._uart_executor._shutdown and stand._audio_executor._shutdown
    with pytest.raises(OSError):
        os.fstat(stand.pty.master_fd)

    failed_api = create_stand_app(settings)
    failed_stand = failed_api.state.stand
    async def fail_start():
        raise RuntimeError("simulated startup failure")
    failed_stand.start_hardware = fail_start
    with pytest.raises(RuntimeError, match="simulated startup failure"):
        with TestClient(failed_api):
            pass
    assert failed_stand._closed
    with pytest.raises(OSError):
        os.fstat(failed_stand.pty.master_fd)


def test_lifespan_does_not_close_external_hardware_owner():
    app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
    calls = []
    async def start():
        calls.append("start")
    async def stop():
        calls.append("stop")
    app.start_hardware = start
    app.stop_hardware = stop
    app.close = lambda: calls.append("close")
    with TestClient(create_app(app)):
        pass
    assert calls == ["start", "stop"]


def test_lifespan_closes_owned_stand_once_when_manager_close_fails():
    import os
    import pytest

    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
                                   stand_access_token="test-token", mutation_auth_policy="token_required"))
    stand = api.state.stand
    calls = []
    original_aclose = stand.aclose
    async def counted_aclose(**kwargs):
        calls.append("stand_close")
        await original_aclose(**kwargs)
    async def failed_voice_close():
        raise RuntimeError("simulated voice shutdown failure")
    stand.aclose = counted_aclose
    stand.application.runtime_manager.voice.close = failed_voice_close
    with pytest.raises(RuntimeError, match="simulated voice shutdown failure"):
        with TestClient(api):
            pass
    assert calls == ["stand_close"]
    assert stand._closed
    assert stand._uart_executor._shutdown and stand._audio_executor._shutdown
    with pytest.raises(OSError):
        os.fstat(stand.pty.master_fd)


def test_aclose_after_hardware_stop_does_not_clear_closed_lighting_again():
    import os
    import pytest

    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
                                    stand_access_token="test-token", mutation_auth_policy="token_required"))
    stand = api.state.stand
    clears = []

    def cancel_effects(*, clear):
        clears.append(clear)
        if len(clears) > 1:
            raise ConnectionError("lighting output is closed")

    stand.application.led.cancel_effects = cancel_effects

    async def run():
        await stand.stop_hardware()
        try:
            await stand.aclose(hardware_stopped=True)
        finally:
            if not stand._closed:
                await asyncio.to_thread(stand.close, cancel_tasks=False, hardware_stopped=True)

    asyncio.run(run())
    assert clears == [True]
    assert stand._closed
    with pytest.raises(OSError):
        os.fstat(stand.pty.master_fd)


def test_production_builder_completes_torch_import_before_threaded_vad_warmup():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = """
import sys
from pathlib import Path
from app.foundation.api import _build_s2s
from app.foundation.config import Settings
assert 'torch' not in sys.modules
_, _, manager = _build_s2s(Settings(profile='local_cpu', voice_conversion='off'), Path('content'))
assert 'torch' in sys.modules
import torch
assert hasattr(torch, '_subclasses')
assert manager.vad.model_loader is not None
assert 'torch' in sys.modules
"""
    # The builder must finish the heavy import on the startup thread before
    # the VAD loader runs in asyncio.to_thread during lifecycle warmup.
    result = subprocess.run([sys.executable, "-c", script], cwd=root,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-500:]
