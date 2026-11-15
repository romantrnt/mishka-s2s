"""Game exit is an explicit visible choice, not a silently frozen target."""
import pytest
from app.foundation.api import build_application
from app.foundation.canonical_services import LED_PRESETS, BUTTON_ELEMENTS
from app.foundation.config import Settings
from app.foundation.domain import Event


@pytest.mark.asyncio
@pytest.mark.parametrize("handset_up", [False, True])
@pytest.mark.parametrize("choice", [6, 7, 5])
async def test_actual_game_exit_has_visible_controls_and_optional_owned_prompt(handset_up, choice):
    app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
    sid = "exit-affordance"
    sequence = 0
    async def send(kind, payload=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind, sid, sequence, 1., "test", payload or {}, f"exit:{sequence}", 1.))
    async def press(button):
        result = await send("ButtonPressed", {"button_id": button})
        await send("ButtonReleased", {"button_id": button})
        return result
    try:
        await press(1)
        await press(1)
        if handset_up:
            await send("HandsetLifted")
        result = await press(7)
        assert result["reason"] == "game_exit_confirmation"
        expected = {str(i): 0 for i in range(18)}
        expected.update({str(i): value for i, value in LED_PRESETS["PAUSED"]})
        assert dict(app.led.output.snapshot()) == expected
        prompts = [x for x in result["outputs"] if x.get("type") == "speak"]
        assert [x["phrase_id"] for x in prompts] == (["PHRASE_062"] if handset_up else [])
        if handset_up:
            owner = dict(app._active_audio[sid])
            assert owner["content_id"] == "PHRASE_062" and owner["playback_lease"]
        else:
            assert sid not in app._active_audio
        ignored = await press(1)
        assert ignored["reason"] == "game_exit_confirmation_pending"
        assert app.canonical_runtime.sessions[sid].game_success_streak == 0
        result = await press(choice)
        assert result["reason"] == {6:"game_exit_cancelled", 7:"game_exit_confirmed", 5:"help_priority"}[choice]
        assert not any(v.get("content_id") == "PHRASE_062" for v in app._active_audio.values())
        if choice == 6:
            target = app.canonical_runtime.sessions[sid].game_target
            assert all(app.led.output.snapshot()[str(i)] == 1 for i in BUTTON_ELEMENTS[target])
    finally:
        await app._cleanup_session_resources(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["AudioCompleted", "AudioFailed"])
async def test_owned_exit_audio_terminal_keeps_explicit_choice_and_lighting(terminal):
    app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
    sid = "exit-audio"
    sequence = 0
    async def send(kind, payload=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind, sid, sequence, 1., "test", payload or {}, f"exit:{sequence}", 1.))
    try:
        await send("ButtonPressed", {"button_id":1})
        await send("ButtonPressed", {"button_id":1})
        await send("HandsetLifted")
        await send("ButtonPressed", {"button_id":7})
        owner = dict(app._active_audio[sid])
        before = dict(app.led.output.snapshot())
        result = await send(terminal, {**owner, "response_id":"PHRASE_062", "error_code":"audio_playback_failed"})
        assert result["canonical_state"]["substate"] == "game_exit_confirmation"
        assert dict(app.led.output.snapshot()) == before
        assert sid not in app._active_audio
        result = await send("ButtonPressed", {"button_id":1})
        assert result["reason"] == "game_exit_confirmation_pending"
        result = await send("ButtonPressed", {"button_id":6})
        assert result["reason"] == "game_exit_cancelled"
    finally:
        await app._cleanup_session_resources(sid)


@pytest.mark.asyncio
async def test_game_exit_hint_dispatch_failure_remains_a_visual_choice():
    app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
    sid = "exit-dispatch-failure"
    seq = 0
    async def send(kind, payload=None):
        nonlocal seq
        seq += 1
        return await app.receive_async(Event(kind,sid,seq,1.,"test",payload or {},f"exit:{seq}",1.))
    try:
        await send("ButtonPressed", {"button_id":1})
        await send("ButtonPressed", {"button_id":1})
        await send("HandsetLifted")
        def fail(*args, **kwargs):
            raise OSError("speaker unavailable")
        app.audio.play_owned = fail
        result = await send("ButtonPressed", {"button_id":7})
        assert result["reason"] == "game_exit_audio_failed"
        assert result["canonical_state"]["substate"] == "game_exit_confirmation"
        assert result["canonical_state"]["audio_status"] == "failed"
        assert app.led.output.snapshot()["14"] == 0
        assert (await send("ButtonPressed", {"button_id":6}))["reason"] == "game_exit_cancelled"
    finally:
        await app._cleanup_session_resources(sid)


@pytest.mark.asyncio
async def test_hangup_and_stale_prompt_completion_do_not_answer_game_exit():
    app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
    sid = "exit-hangup"
    seq = 0
    async def send(kind, payload=None):
        nonlocal seq
        seq += 1
        return await app.receive_async(Event(kind,sid,seq,1.,"test",payload or {},f"exit:{seq}",1.))
    try:
        await send("ButtonPressed", {"button_id":1})
        await send("ButtonPressed", {"button_id":1})
        await send("HandsetLifted")
        await send("ButtonPressed", {"button_id":7})
        owner = dict(app._active_audio[sid])
        lights = dict(app.led.output.snapshot())
        await send("HandsetPlaced")
        assert sid not in app._active_audio
        await send("AudioCompleted", {**owner,"response_id":"PHRASE_062"})
        assert app.canonical_runtime.sessions[sid].substate == "game_exit_confirmation"
        assert app.led.output.snapshot() == lights
        await send("HandsetLifted")
        assert sid not in app._active_audio
        assert app.led.output.snapshot() == lights
        assert (await send("ButtonPressed", {"button_id":7}))["reason"] == "game_exit_confirmed"
    finally:
        await app._cleanup_session_resources(sid)
