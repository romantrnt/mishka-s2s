"""Existing root destinations, with real application authorization boundaries."""
import asyncio

import pytest

from test_profile_onboarding import Harness, card
from app.foundation.domain import Event


async def menu(h, authorized, *, release_skip=True):
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
        await h.send("ButtonReleased", {"button_id": 4})
        await h.complete()
        await h.send("ExhibitionOnboardingComplete")
    if not release_skip:
        await h.send("ButtonPressed", {"button_id": 1})
        assert h.state.mode == 'game_menu'
    else:
        # Reach the unselected root through actual owned tutorial terminals.
        if h.state.mode == 'exhibition_menu_tutorial_prompt':
            await h.complete()
        await h.complete()
        if h.state.mode == 'exhibition_menu_wave':
            token = h.app.sessions.get(h.boundary.session_id).cancellation_token
            await h.send('ExhibitionMenuWaveComplete', {'token': token})
        await h.complete()
        h.sequence += 1
        await h.app.receive_async(Event('SpeechReceived', h.boundary.session_id, h.sequence,
            1., 'test', {}, f'menu-answer:{h.sequence}', h.clock[0]), 'Нет')
        assert h.state.mode == "menu_ready"
    h.played.clear()


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("button,target", [(1,"game_menu"),(2,"fixed_story_menu"),(4,"support_intake"),
    (3,"personal_story_theme"),(5,"wellbeing_check"),(6,"bear_dialogue"),(7,"schedule_weather")])
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
        accesses = []
        class ForbiddenProvider:
            def __getattr__(self, name):
                accesses.append(name)
                raise AssertionError("No schedule grant: provider must not be accessed")
        try:
            await menu(h, authorized)
            # No capability change after narration: exercise the independent
            # no-grant boundary against the menu that was actually announced.
            h.app.schedule_weather = ForbiddenProvider()
            await h.send("ButtonPressed", {"button_id": 7})
            await h.send("ButtonReleased", {"button_id": 7})
            if h.state.scene_menu_phase in {"narrating", "repeat_prompt", "repeat_listen"}:
                await h.send("ButtonPressed", {"button_id": 1})
                await h.send("ButtonReleased", {"button_id": 1})
            result = await h.send("ButtonPressed", {"button_id": selection})
            await h.send("ButtonReleased", {"button_id": selection})
            assert h.state.mode == "schedule_weather"
            assert not any(item["id"] in {"what_next", "where_now"}
                           for item in h.state.scene_menu_descriptor["items"])
            assert not any(x.get("type") == "typed_info_request" for x in result["outputs"])
            assert not h.state.scene_menu_refresh_required
            # Hidden slots do not remove the independent authorization boundary:
            # a real spoken schedule request must still fail without a grant.
            h.sequence += 1
            result = await h.app.receive_async(Event("SpeechReceived", h.boundary.session_id,
                h.sequence, 1., "test", {}, f"schedule-request-{h.sequence}", h.clock[0]),
                "Куда мне сейчас идти?")
            assert not accesses
            assert not h.app._identity_scope_live(h.boundary.session_id, "schedule")
            if authorized:
                assert any(x.get("resolution") == "typed_information_blocked_by_identity" for x in result["outputs"])
            else: assert result["reason"] == "schedule_requires_profile"
            assert "PHRASE_099" not in h.played
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("authorized", [False, True])
def test_early_root_choice_requires_release_before_submenu_action(authorized):
    async def run():
        h = Harness(card() if authorized else None)
        try:
            await menu(h, authorized, release_skip=False)
            held = await h.send("ButtonPressed", {"button_id": 1})
            assert held["reason"] == "exhibition_menu_choice_held"
            assert h.state.mode == "game_menu"
            assert h.state.menu_early_choice_button == 1
            assert not any(x.get("type") == "exhibition_menu_selected" for x in held["outputs"])
            released = await h.send("ButtonReleased", {"button_id": 1})
            assert h.state.menu_early_choice_button is None
            selected = await h.send("ButtonPressed", {"button_id": 1})
            assert h.state.mode == "game_active"
            assert selected["reason"] == "game_started"
            assert not any(x.get("type") == "exhibition_menu_selected" for x in selected["outputs"])
        finally:
            await h.close()
    asyncio.run(run())


def test_physical_hid_one_emits_new_press_only_after_release():
    from app.foundation.input_adapters import KeyboardHIDInputAdapter
    events = []
    adapter = KeyboardHIDInputAdapter("/synthetic/input", events.append)
    adapter.feed_key(2, 1, timestamp=1.0)
    adapter.feed_key(2, 2, timestamp=2.0)  # Kernel autorepeat, not a new edge.
    adapter.feed_key(2, 1, timestamp=3.0)  # Duplicate down stays held.
    assert [(e.type, e.payload) for e in events] == [("ButtonPressed", {"button_id": 1})]
    adapter.feed_key(2, 0, timestamp=4.0)
    adapter.feed_key(2, 1, timestamp=5.0)  # Beyond existing physical debounce.
    assert [(e.type, e.payload) for e in events] == [
        ("ButtonPressed", {"button_id": 1}),
        ("ButtonReleased", {"button_id": 1}),
        ("ButtonPressed", {"button_id": 1}),
    ]
