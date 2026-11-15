"""Absent story provider is an honest owned failure, never fake success."""
import asyncio

import pytest

from app.foundation.domain import Event
from test_profile_onboarding import Harness, card
from test_root_menu_profile_routing import menu


async def speech(h, text):
    h.sequence += 1
    return await h.app.receive_async(Event("SpeechReceived", h.boundary.session_id, h.sequence,
        1., "test", {}, f"story-speech:{h.sequence}", h.clock[0]), text)


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("input_kind", ["speech", 1, 2, 3, 4, 6])
def test_root_new_story_returns_owned_failure_and_working_fixed_chooser(authorized, input_kind):
    async def run():
        h = Harness(card() if authorized else None)
        try:
            await menu(h, authorized)
            await h.send("ButtonPressed", {"button_id": 4})
            h.played.clear()
            result = (await speech(h, "Про динозавров") if input_kind == "speech"
                      else await h.send("ButtonPressed", {"button_id": input_kind}))
            assert result["reason"] == "personal_story_unavailable"
            assert result["state"] == h.state.mode == "fixed_story_menu"
            assert h.played == ["PHRASE_080"]
            assert h.app._active_audio[h.boundary.session_id]["content_id"] == "PHRASE_080"
            assert h.state.active_audio_id == "PHRASE_080"
            assert "Маленькое доброе путешествие" not in repr(result)
            assert not any(x.get("candidate") or x.get("validated") for x in result["outputs"])
            assert not any(x.get("type") == "play_personal_story" for x in result["outputs"])
            assert "personal_story_playing" not in [x.get("state_after") for x in h.app.canonical_trace]
            await h.complete()
            assert h.state.mode == "fixed_story_menu"
            assert h.state.active_audio_id is None and h.state.playback_lease is None
            assert h.state.audio_status == "idle"
            assert h.boundary.session_id not in h.app._active_audio
            chosen = await h.send("ButtonPressed", {"button_id": 1})
            assert h.state.mode == "fixed_story_playing"
            assert chosen["reason"] in {"fixed_story_started", "fixed_story_placeholder"}
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("authorized", [False, True])
def test_urgent_theme_goes_to_safety_not_unavailable_story(authorized):
    async def run():
        h = Harness(card() if authorized else None)
        try:
            await menu(h, authorized)
            await h.send("ButtonPressed", {"button_id": 4})
            h.played.clear()
            await speech(h, "Я не хочу жить")
            assert h.state.mode == "safety_handoff" and h.state.safety_severity == "urgent"
            assert "PHRASE_080" not in h.played
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("interrupt", ["HandsetPlaced", "urgent"])
def test_interruption_during_fallback_cleanup_cannot_play_stale_failure(interrupt):
    async def run():
        h = Harness(card())
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.app._cancel_audio_owner
        blocked = False
        async def cancel(owner, sid, **kwargs):
            nonlocal blocked
            if kwargs.get("operation") == "audio_stop" and not blocked:
                blocked = True; entered.set(); await release.wait()
            return await original(owner, sid, **kwargs)
        task = None
        try:
            await menu(h, True)
            await h.send("ButtonPressed", {"button_id": 4})
            h.played.clear()
            h.app._cancel_audio_owner = cancel
            task = asyncio.create_task(speech(h, "Про динозавров"))
            await asyncio.wait_for(entered.wait(), 3)
            if interrupt == "urgent": await speech(h, "Я не хочу жить")
            else: await h.send("HandsetPlaced")
            release.set(); await task
            assert "PHRASE_080" not in h.played
            if interrupt == "urgent": assert h.state.mode == "safety_handoff"
            else: assert not h.app._canonical_profiles
        finally:
            release.set()
            if task: await asyncio.gather(task, return_exceptions=True)
            h.app._cancel_audio_owner = original
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("exit_button,target", [(5,"wellbeing_check"),(7,"root_menu")])
def test_failed_space_story_has_working_help_and_back(exit_button, target):
    async def run():
        h = Harness(card())
        try:
            await menu(h, True)
            await h.send("ButtonPressed", {"button_id": 4})
            result = await h.send("ButtonPressed", {"button_id": 2})
            assert result["reason"] == "personal_story_unavailable"
            assert h.state.mode == "fixed_story_menu"
            await h.send("ButtonPressed", {"button_id": exit_button})
            assert h.state.mode == target
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode,intent", [
    ("personal_story_generating","generated"),
    ("personal_story_validating","validated"),
    ("personal_story_generating","validation_failed"),
])
def test_old_pipeline_signals_cannot_report_or_play_stub_success(mode, intent):
    async def run():
        h = Harness(card())
        try:
            await menu(h, True)
            h.state.mode = mode
            h.played.clear()
            result = await h.send("VoiceIntentReady", {"intent": intent, "safety_severity": "none"})
            assert h.state.mode in {"fixed_story_menu", "personal_story_theme"}
            assert h.played == ["PHRASE_080"]
            assert h.app._active_audio[h.boundary.session_id]["content_id"] == "PHRASE_080"
            assert "validated_development_story" not in repr(result)
            assert "development_only_story_visual_placeholder" not in repr(result)
            assert "Маленькое доброе путешествие" not in repr(result)
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["personal_story_generating", "personal_story_validating", "personal_story_playing"])
@pytest.mark.parametrize("button,target", [(5, "wellbeing_check"), (7, "root_menu")])
def test_old_or_interrupted_story_states_always_keep_help_and_exit(mode, button, target):
    async def run():
        h = Harness(None)
        try:
            await menu(h, False)
            h.state.mode = mode
            result = await h.send("ButtonPressed", {"button_id": button})
            assert result["reason"] in {"help_priority", "activity_back"}
            assert h.state.mode == target
            assert any(x.get("type") == "audio_stop" for x in result["outputs"])
        finally: await h.close()
    asyncio.run(run())
