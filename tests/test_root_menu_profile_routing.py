"""Existing root destinations, with real application authorization boundaries."""
import asyncio

import pytest

from test_profile_onboarding import Harness, card
from app.foundation.domain import Event


async def menu(h, authorized):
    if authorized:
        await h.ready()
    await h.lift()
    await h.complete()
    if authorized:
        assert h.state.mode == "exhibition_menu_tutorial_prompt"
    else:
        # Complete the existing guest interview through its actual app path.
        await h.complete(); await h.complete()
        result = await h.app.receive_async(Event(
            "SpeechReceived", h.boundary.session_id, h.sequence + 1, 1., "test", {}, "guest-name", h.clock[0]), "Меня зовут Тест")
        h.sequence += 1
        assert result["reason"] == "exhibition_age_question_started"
        await h.complete(); await h.complete()
        await h.send("ButtonPressed", {"button_id": 4})
        await h.complete()
        await h.send("ExhibitionOnboardingComplete")
    await h.send("ButtonPressed", {"button_id": 1})
    assert h.state.mode == "menu_ready"
    h.played.clear()


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("button,target", [(1,"game_menu"),(2,"fixed_story_menu"),(3,"support_intake"),
    (4,"personal_story_theme"),(5,"wellbeing_check"),(6,"bear_dialogue"),(7,"schedule_weather")])
def test_all_root_buttons_reach_existing_destinations(authorized, button, target):
    async def run():
        h = Harness(card() if authorized else None)
        try:
            await menu(h, authorized)
            result = await h.send("ButtonPressed", {"button_id": button})
            assert h.state.mode == target
            assert result["reason"] != "transition_rejected"
            assert "PHRASE_099" not in h.played
            if button == 4:
                assert not any(x.get("type") == "generate_personal_story" for x in result["outputs"])
                assert h.state.display_name == ("Синтетик" if authorized else "Тест")
            assert any(x.get("type") == "exhibition_menu_selected" and x.get("target_state") == target for x in result["outputs"])
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("selection", [3,4])
def test_schedule_selection_never_bypasses_missing_schedule_grant(authorized, selection):
    async def run():
        h = Harness(card() if authorized else None)
        class ForbiddenProvider:
            def __getattr__(self, name):
                raise AssertionError("No schedule grant: provider must not be accessed")
        try:
            await menu(h, authorized)
            await h.send("ButtonPressed", {"button_id": 7})
            h.app.schedule_weather = ForbiddenProvider()
            result = await h.send("ButtonPressed", {"button_id": selection})
            assert h.state.mode == "schedule_weather"
            if authorized:
                assert any(x.get("resolution") == "typed_information_blocked_by_identity" for x in result["outputs"])
            else: assert result["reason"] == "schedule_requires_profile"
            assert "PHRASE_099" not in h.played
        finally: await h.close()
    asyncio.run(run())
