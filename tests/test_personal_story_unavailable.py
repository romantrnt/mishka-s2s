"""Absent authored audio capability is honest; stale failures never play."""
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
def test_absent_tts_offers_no_fake_choice_and_fixed_chooser_remains_reachable(authorized, input_kind):
    async def run():
        h = Harness(card() if authorized else None)
        try:
            await menu(h, authorized)
            h.app._owned_menu_voice_capable = lambda: False
            h.app.bounded_s2s.tts.synthesize = None
            await h.send("ButtonPressed", {"button_id": 3})
            await h.send("ButtonReleased", {"button_id": 3})
            assert h.state.scene_menu_descriptor["items"] == []
            assert h.state.scene_menu_descriptor["buttons"] == [7]
            h.played.clear()
            result = (await speech(h, "Про динозавров") if input_kind == "speech"
                      else await h.send("ButtonPressed", {"button_id": input_kind}))
            assert h.state.mode == "personal_story_theme"
            assert not h.app._personal_stories
            assert "Маленькое доброе путешествие" not in repr(result)
            assert not any(x.get("candidate") or x.get("validated") for x in result["outputs"])
            assert not any(x.get("type") == "play_personal_story" for x in result["outputs"])
            assert "personal_story_playing" not in [x.get("state_after") for x in h.app.canonical_trace]
            if input_kind != "speech":
                await h.send("ButtonReleased", {"button_id": input_kind})
            await h.send("ButtonPressed", {"button_id": 7})
            await h.send("ButtonReleased", {"button_id": 7})
            await h.send("ButtonPressed", {"button_id": 2})
            await h.send("ButtonReleased", {"button_id": 2})
            chosen = await h.send("ButtonPressed", {"button_id": 1})
            assert h.state.mode == "fixed_story_playing"
            assert chosen["reason"] == "fixed_story_started"
        finally: await h.close()
    asyncio.run(run())


async def failing_story_menu(h):
    from test_personal_story_service import LLM, TTS, Voice, Mini, DURATION_BOUNDS
    from app.foundation.validation import AsyncResponseValidator
    await menu(h, True)
    engine = h.app.bounded_s2s
    engine.llm = LLM(h.state.age_band)
    engine.tts = TTS(DURATION_BOUNDS[h.state.age_band][0])
    engine.voice = Voice()
    engine.response_validator = AsyncResponseValidator(mini=Mini())
    h.app._owned_menu_voice_capable = lambda: False
    await h.send("ButtonPressed", {"button_id": 3})
    await h.send("ButtonReleased", {"button_id": 3})
    assert h.state.scene_menu_phase == "ready"
    async def unavailable(*args):
        return None
    engine.tts.synthesize = unavailable
    h.played.clear()


@pytest.mark.parametrize("authorized", [False, True])
def test_urgent_theme_goes_to_safety_not_unavailable_story(authorized):
    async def run():
        h = Harness(card() if authorized else None)
        try:
            await menu(h, authorized)
            await h.send("ButtonPressed", {"button_id": 3})
            h.played.clear()
            await speech(h, "Я не хочу жить")
            assert h.state.mode == "safety_stop" and h.state.safety_severity == "urgent"
            assert "PHRASE_080" not in h.played
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("interrupt", ["HandsetPlaced", "urgent"])
@pytest.mark.parametrize("failure_stage", ["tts", "output"])
def test_interruption_during_fallback_cleanup_cannot_play_stale_failure(interrupt, failure_stage):
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
            await failing_story_menu(h)
            if failure_stage == "output":
                from test_personal_story_service import TTS, DURATION_BOUNDS
                h.app.bounded_s2s.tts = TTS(DURATION_BOUNDS[h.state.age_band][0])
                def unavailable(*args): raise OSError("synthetic output failure")
                h.app.audio.play_pcm_owned = unavailable
            h.app._cancel_audio_owner = cancel
            task = asyncio.create_task(h.send("ButtonPressed", {"button_id": 1}))
            await asyncio.wait_for(entered.wait(), 3)
            if interrupt == "urgent": await speech(h, "Я не хочу жить")
            else: await h.send("HandsetPlaced")
            release.set(); await asyncio.wait_for(task, 3)
            assert "PHRASE_080" not in h.played
            if interrupt == "urgent": assert h.state.mode == "safety_stop"
            else: assert not h.app._canonical_profiles
        finally:
            release.set()
            if task: await asyncio.gather(task, return_exceptions=True)
            h.app._cancel_audio_owner = original
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("exit_button,target", [(5,"wellbeing_check"),(7,"root_menu")])
def test_failed_authored_story_has_working_back_and_root_help(exit_button, target):
    async def run():
        h = Harness(card())
        try:
            await failing_story_menu(h)
            result = await h.send("ButtonPressed", {"button_id": 2})
            assert result["reason"] == "personal_story_unavailable"
            assert h.state.mode == "fixed_story_menu"
            assert h.played == ["PHRASE_080"]
            await h.complete()  # Failed authored audio offers the real fixed menu.
            await h.send("ButtonPressed", {"button_id": 7})
            await h.send("ButtonReleased", {"button_id": 7})
            assert h.state.mode == "root_menu"
            if exit_button == 5:
                await h.send("ButtonPressed", {"button_id": 5})
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
            assert result["reason"] == "personal_story_result_untrusted"
            assert h.state.mode == mode  # external claims cannot advance any stage
            assert not h.played
            assert not h.app._personal_stories
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
            expected = "help_priority" if button == 5 else "personal_story_back" if mode == "personal_story_playing" else "activity_back"
            assert result["reason"] == expected
            assert h.state.mode == target
            assert any(x.get("type") == "audio_stop" for x in result["outputs"])
        finally: await h.close()
    asyncio.run(run())
