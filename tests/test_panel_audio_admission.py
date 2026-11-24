"""A real scoped browser-audio action never changes the physical hook."""
import pytest

from app.foundation.domain import Event
from app.foundation.events import EventValidationError, parse_event
from test_panel_session_actions import PanelStand


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [False, True])
async def test_browser_sound_enables_current_quiet_panel_visit_without_lifting(tmp_path, identity):
    h = PanelStand(tmp_path, identity=identity)
    try:
        if identity:
            await h.ready()
        await h.control("start")
        await h.menu()
        assert h.state.silent_mode and h.state.handset == "down"
        old = h.scope()
        result = await h.control("enable_audio", {**old, "state_version": h.state.state_version})
        assert result["reason"] == "panel_audio_enabled", result
        assert h.state.audio_open and not h.state.silent_mode
        assert h.state.interaction_route == "panel" and h.state.handset == "down"
        assert not h.app.sessions.get("stand").handset_up
        assert not h.stand._headless_audio_accepting()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_sound_button_resumes_pending_dialogue_and_late_action_cannot_reopen_end(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        await h.menu()
        await h.key(6)
        assert h.state.mode == "awaiting_handset" and h.state.pending_mode == "bear_dialogue"
        scope = {**h.scope(), "state_version": h.state.state_version}
        result = await h.control("enable_audio", scope)
        assert result["reason"] == "panel_audio_pending_started", result
        assert h.state.mode == "bear_dialogue" and h.state.pending_mode is None
        assert h.state.active_audio_id == "DIALOGUE_INTRO" and h.state.handset == "down"
        await h.control("end")
        result = await h.control("enable_audio", scope)
        assert result["error"]["code"] == "stale_simulator_epoch"
        assert not h.state.interaction_open and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_audio_enable_is_internal_only_and_cannot_start_an_inactive_visit(tmp_path):
    h = PanelStand(tmp_path)
    try:
        with pytest.raises(EventValidationError):
            parse_event({"type": "SessionAudioEnabled"}, "stand", 1)
        result = await h.app.receive_async(Event("SessionAudioEnabled", "stand", 1, 1., "forged", {}, "forged"))
        assert result["reason"] == "session_action_untrusted"
        result = await h.control("enable_audio", {**h.scope(), "state_version": 0})
        assert result["reason"] == "panel_audio_unavailable"
        assert h.state is None or not h.state.interaction_open
        assert not h.app._active_audio
    finally:
        await h.close()


async def enable(h):
    return await h.control("enable_audio", {**h.scope(), "state_version": h.state.state_version})


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["fixed", "authored", "quiz", "word"])
@pytest.mark.parametrize("join", ["sound", "lift"])
async def test_quiet_selected_story_or_game_waits_then_resumes_exact_choice(tmp_path, selection, join):
    h = PanelStand(tmp_path)
    streams = []
    # This application test captures the owned output port; browser transport
    # streaming itself is exercised independently, without an invented WS owner.
    h.app.audio._private_stream_play = lambda handle, stream, on_cue=None: streams.append((handle, stream))
    try:
        await h.control("start")
        await h.menu()
        await h.key(2 if selection == "fixed" else 3 if selection == "authored" else 1)
        assert not h.app._active_audio and not h.stand._interaction_audio_accepting()
        renderer = h.app.bounded_s2s.tts
        assert not renderer.texts
        slot = {"fixed": 1, "authored": 1, "quiz": 2, "word": 5}[selection]
        chosen = h.state.scene_menu_descriptor["items"][slot - 1]["id"]
        await h.key(slot)
        assert h.state.mode == "awaiting_handset"
        assert not renderer.texts and not h.app._personal_stories
        if join == "sound":
            await enable(h)
            assert h.state.handset == "down" and not h.app.sessions.get("stand").handset_up
        else:
            await h.simulator.action({"action": "handset", "state": "up"})
            assert h.state.handset == "up" and h.app.sessions.get("stand").handset_up
        assert h.state.pending_mode is None and h.state.pending_story_theme is None
        if selection == "fixed":
            assert h.state.mode == "fixed_story_playing" and h.state.active_story_id == chosen
        elif selection == "authored":
            assert h.state.mode == "personal_story_playing" and renderer.texts
            handle, stream = streams[-1]
            first, _ = await anext(stream)
            assert first.samples and first.token == handle.token and first.session_id == "stand"
        else:
            assert h.state.mode == "game_active" and h.state.active_game_id == chosen
        assert h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_quiet_help_day_stay_usable_without_audio_or_microphone(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        await h.key(5)
        assert h.state.mode == "wellbeing_check" and h.state.support_question_kind == "pain_or_discomfort"
        assert not h.app._active_audio and not h.stand._interaction_audio_accepting()
        await h.key(1)
        assert h.state.support_question_kind == "fear" and not h.app._active_audio
        await h.key(1)
        assert h.state.mode == "root_menu"
        await h.key(7)
        await h.key(1)
        assert h.state.mode == "schedule_weather" and not h.app._active_audio
        assert not h.app.bounded_s2s.tts.texts
        assert not h.stand.panel_snapshot()["simulator"]["microphone"]["eligible"]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_audio_enable_rechecks_readiness_after_delay_and_rejects_idle_and_stale_state(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        scope = {**h.scope(), "state_version": h.state.state_version}
        await h.key(7)
        assert (await h.control("enable_audio", scope))["reason"] == "stale_state_version"
        assert h.state.silent_mode
        async def lost_readiness(*args):
            h.simulator.admits_users = lambda: False
        h.simulator.before_input = lost_readiness
        assert (await enable(h))["error"]["code"] == "runtime_unavailable"
        assert h.state.silent_mode
        h.simulator.admits_users = lambda: True
        async def no_delay(*args):
            return None
        h.simulator.before_input = no_delay
        h.state.interaction_idle = True
        assert "enable_audio" not in h.stand.panel_snapshot()["simulator"]["session"]["session_actions"]
        assert (await enable(h))["reason"] == "panel_audio_unavailable"
        assert h.state.silent_mode
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_sound_enable_replays_current_menu_once_and_duplicate_does_not_extend_idle(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        await h.key(2)
        assert not h.app._active_audio
        await enable(h)
        assert h.state.scene_menu_phase == "narrating"
        lease = dict(h.app._active_audio["stand"])
        owner = dict(h.app._engagement_owners["stand"])
        h.harness.clock.advance(10)
        assert (await enable(h))["reason"] == "panel_audio_already_enabled"
        assert h.app._active_audio["stand"] == lease
        assert h.app._engagement_owners["stand"] == owner
    finally:
        await h.close()
