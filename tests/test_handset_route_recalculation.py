"""Handset lift/place route recalculation regressions across panel and handset visits."""
import time
import pytest

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from test_panel_session_actions import PanelStand


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [False, True])
async def test_panel_game_lift_preserves_progress_hold7_returns_and_place_ends_cleanly(tmp_path, identity):
    h = PanelStand(tmp_path, identity=identity)
    try:
        if identity:
            await h.ready()
        await h.control("start")
        await h.menu()
        await h.key(1)
        assert h.state.mode == "game_menu"
        await h.key(1)
        assert h.state.mode == "game_active" and h.state.active_game_id == "free_lights"

        # Play with handset down
        for btn in (1, 2):
            await h.key(btn)
        version_before_lift = h.state.state_version
        press_history = list(h.state.game_engine_state.selected_buttons) if hasattr(h.state.game_engine_state, "selected_buttons") else None
        assert h.state.handset == "down" and h.state.interaction_route == "panel"

        # Lift handset mid-game: preserve visit, auth, game, and progress
        lifted = await h.simulator.action({"action": "handset", "state": "up"})
        assert lifted["reason"] == "panel_handset_lifted"
        assert h.state.mode == "game_active" and h.state.active_game_id == "free_lights"
        assert h.state.handset == "up" and h.state.interaction_route == "panel"
        assert h.state.state_version == version_before_lift
        if press_history is not None:
            assert list(h.state.game_engine_state.selected_buttons) == press_history
        if identity:
            assert h.state.profile_status == "identified"

        # Continue playing and exit via hold 7 -> returns to game_menu (not global hangup)
        await h.key(3)
        await h.stand.inject("button", {"button_id": 7, "state": "pressed"})
        await h.harness.advance(5.5)
        await h.stand.inject("button_released", {"button_id": 7, "state": "released"})
        assert h.state.mode == "game_menu" and h.state.handset == "up"

        # Select story from root menu, then place handset down -> clean single reset
        await h.key(7)
        assert h.state.mode == "root_menu"
        await h.key(2)
        assert h.state.mode == "fixed_story_menu"
        story_btn = 1
        await h.key(story_btn)
        assert h.state.mode == "fixed_story_playing" and "stand" in h.app._active_audio

        placed = await h.simulator.action({"action": "handset", "state": "down"})
        assert placed["reason"] == "exhibition_reset"
        assert h.state is None
        assert not h.app._active_audio
        assert not h.app._canonical_profiles
        assert not h.app.conversation_contexts

        # Next guest lifts handset -> clean fresh visit
        next_lift = await h.simulator.action({"action": "handset", "state": "up"})
        assert next_lift["reason"] == "exhibition_handset_lifted"
        assert h.state.mode == "voice_welcome" and h.state.profile_status == "anonymous"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_panel_story_playing_paused_and_dialogue_survive_handset_lift(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.ready()
        await h.control("start")
        await h.menu()

        # Enter story, pause it, lift handset -> remains paused without restarting
        await h.key(2)
        await h.key(1)
        assert h.state.mode == "fixed_story_playing"
        story_audio = dict(h.app._active_audio["stand"])
        await h.key(6)
        while h.state.mode == "story_pause_announcement":
            await h.done()
        assert h.state.mode == "story_paused"
        lifted = await h.simulator.action({"action": "handset", "state": "up"})
        assert lifted["reason"] == "panel_handset_lifted"
        assert h.state.mode == "story_paused" and h.state.handset == "up"
        assert h.state.profile_status == "identified"
        assert h.state.active_story_id is not None
        # Resume story -> plays without repeating welcome
        await h.key(6)
        while h.state.mode == "story_resume_announcement":
            await h.done()
        assert h.state.mode == "fixed_story_playing"
        assert h.app._active_audio["stand"]["content_id"] == story_audio["content_id"]

        # Exit story -> fixed_story_choice_wave, then place handset down -> clean reset
        await h.key(7)
        assert h.state.mode == "fixed_story_choice_wave"
        await h.simulator.action({"action": "handset", "state": "down"})
        assert h.state is None and not h.app._canonical_profiles
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_panel_bear_dialogue_survives_handset_lift_without_second_welcome(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.ready()
        await h.control("start")
        await h.menu()
        await h.key(6)
        while "stand" in h.app._active_audio:
            await h.done()
        assert h.state.mode == "bear_dialogue" and h.state.handset == "down"
        version_before = h.state.state_version
        lifted = await h.simulator.action({"action": "handset", "state": "up"})
        assert lifted["reason"] == "panel_handset_lifted"
        assert h.state.mode == "bear_dialogue" and h.state.handset == "up"
        assert h.state.state_version == version_before
        assert h.state.profile_status == "identified"
        assert h.stand.panel_snapshot()["simulator"]["microphone"]["eligible"]
        await h.simulator.action({"action": "handset", "state": "down"})
        assert h.state is None and not h.app._canonical_profiles
    finally:
        await h.close()


@pytest.mark.parametrize("identified", [False, True])
def test_awaiting_handset_with_pending_mode_enters_activity_on_lift_without_second_welcome(identified):
    runtime = CanonicalRuntime(exhibition_mode=True, optional_identity=True)
    sid = "awaiting-lift"
    now = time.monotonic()
    profile = {"profile_ref": "p1", "display_name": "Рома", "age_years": 7, "grammatical_gender": "male"} if identified else None
    if identified:
        runtime.handle(CanonicalEvent("ClientProfileReady", sid, 1, 0, "max", {
            "request_id": "req-1", "expires_monotonic": now + 100.0, "ready_deadline": now + 100.0,
        }, "c1", now), profile=profile, identity_event_authorized=True)
    started = runtime.handle(CanonicalEvent("SessionStarted", sid, 2, 1 if identified else 0, "panel", {}, "s1", now + 1), session_action_authorized=True)
    assert started["reason"] == "panel_session_started"
    state = runtime.sessions[sid]
    state.mode = "awaiting_handset"
    state.pending_mode = "support_intake"

    lifted = runtime.handle(CanonicalEvent("HandsetLifted", sid, 3, state.state_version, "handset", {}, "h1", now + 2))
    assert lifted["reason"] == "voice_open_pending_activity"
    assert lifted["state"]["mode"] == "support_intake"
    assert lifted["state"]["handset"] == "up"
    assert lifted["state"]["profile_status"] == ("identified" if identified else "anonymous")


def test_session_cancel_while_up_without_optional_identity_allows_handset_placed_and_reopen():
    runtime = CanonicalRuntime(exhibition_mode=True, optional_identity=False)
    sid = "cancel-up-no-opt-id"
    now = time.monotonic()
    lifted = runtime.handle(CanonicalEvent("HandsetLifted", sid, 1, 0, "handset", {}, "h1", now))
    assert lifted["reason"] == "exhibition_handset_lifted"
    ended = runtime.handle(CanonicalEvent("SessionCancel", sid, 2, lifted["state"]["state_version"], "panel", {}, "c1", now + 1), session_action_authorized=True)
    assert ended["reason"] == "soft_end" and ended["state"]["mode"] == "session_closed"
    placed = runtime.handle(CanonicalEvent("HandsetPlaced", sid, 3, ended["state"]["state_version"], "handset", {}, "p1", now + 2))
    assert placed["reason"] == "closed_handset_placed" and placed["state"]["mode"] == "idle"
    reopened = runtime.handle(CanonicalEvent("HandsetLifted", sid, 4, placed["state"]["state_version"], "handset", {}, "h2", now + 3))
    assert reopened["reason"] == "exhibition_handset_lifted" and reopened["state"]["mode"] == "voice_welcome"
