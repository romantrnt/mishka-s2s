"""A fresh game choice selects immediately while its menu voice is active."""
import pytest

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, CanonicalState


def menu_at(phase):
    runtime = CanonicalRuntime()
    runtime.menu_voice_capable = True
    state = CanonicalState("direct-game", mode="root_menu", handset="up", age_band="5-7")
    runtime.sessions[state.session_id] = state
    sequence = 0
    def send(kind, payload=None):
        nonlocal sequence
        sequence += 1
        return runtime.handle(CanonicalEvent(kind, state.session_id, sequence, state.state_version,
            "direct-choice-test", payload or {}, str(sequence), timestamp=float(sequence)))
    def complete():
        return send("AudioCompleted", {"response_id": state.active_audio_id,
                                      "playback_lease": state.playback_lease})
    send("ButtonPressed", {"button_id": 1})
    send("ButtonReleased", {"button_id": 1})
    for _ in range({"invite": 0, "narrating": 1, "repeat_prompt": 2, "repeat_listen": 3}[phase]):
        complete()
    assert state.scene_menu_phase == phase
    # Authored question quiz-6-7-08 has a real button-one answer option.
    state.game_entry_seed = 0
    return runtime, state, send, complete


@pytest.mark.parametrize("phase", ["invite", "narrating", "repeat_prompt", "repeat_listen"])
def test_single_quiz_choice_interrupts_menu_and_starts_exactly_once(phase):
    runtime, state, send, complete = menu_at(phase)
    old_audio, old_lease = state.active_audio_id, state.playback_lease
    result = send("ButtonPressed", {"button_id": 2})
    assert result["reason"] == "game_started"
    assert state.mode == "game_active" and state.active_game_id == "quiz"
    assert state.game_audio_stage == "starting" and state.active_audio_id == "PHRASE_020"
    assert [x["type"] for x in result["effects"]][:2] == ["audio_stop", "capture_close"]
    assert state.playback_lease != old_lease
    run_id, current_lease = state.game_engine_state.run_id, state.playback_lease
    repeated = send("ButtonPressed", {"button_id": 2})
    assert state.game_engine_state.run_id == run_id and state.playback_lease == current_lease
    assert not any(x["type"] == "game_started" for x in repeated["effects"])
    if old_audio:
        send("AudioCompleted", {"response_id": old_audio, "playback_lease": old_lease})
        assert state.playback_lease == current_lease and state.game_audio_stage == "starting"
    send("ButtonReleased", {"button_id": 2})
    assert complete()["reason"] == "game_prompt_started"
    assert complete()["reason"] == "game_prompt_finished"
    assert state.game_audio_stage == "playing"
    answer = send("ButtonPressed", {"button_id": 1})
    assert answer["reason"] in {"game_retry", "game_progress", "game_completed"}
    assert state.game_engine_state.run_id == run_id


def test_held_entry_key_cannot_become_a_fresh_direct_game_choice():
    runtime, state, send, complete = menu_at("invite")
    state.scene_menu_held.append(2)
    result = send("ButtonPressed", {"button_id": 2})
    assert result["reason"] == "scene_menu_release_required"
    assert state.mode == "game_menu"
    send("ButtonReleased", {"button_id": 2})
    assert send("ButtonPressed", {"button_id": 2})["reason"] == "game_started"


def test_invalid_game_slot_still_only_skips_menu_voice():
    runtime, state, send, complete = menu_at("invite")
    result = send("ButtonPressed", {"button_id": 6})
    assert result["reason"] == "scene_menu_skipped"
    assert state.mode == "game_menu" and state.scene_menu_phase == "ready"
    assert state.active_game_id is None


def test_changed_mapping_cannot_be_selected_by_direct_choice():
    runtime, state, send, complete = menu_at("narrating")
    state.scene_menu_descriptor["items"][1]["id"] = "stale-slot"
    result = send("ButtonPressed", {"button_id": 2})
    assert result["reason"] != "game_started"
    assert state.mode == "game_menu" and state.active_game_id is None


@pytest.mark.asyncio
async def test_actual_application_one_two_one_keeps_one_game_owner():
    from test_scene_menu_runtime import Harness
    h = Harness("5-7")
    try:
        await h.button(1)
        await h.release(1)
        h.state.game_entry_seed = 0
        invitation = dict(h.app._active_audio[h.sid])
        selected = await h.button(2)
        await h.release(2)
        assert selected["reason"] == "game_started" and h.state.active_game_id == "quiz"
        assert any(x["type"] == "audio_stop" for x in selected["outputs"])
        current = dict(h.app._active_audio[h.sid])
        old = {"response_id": invitation["content_id"], **{key: invitation[key] for key in (
            "token", "generation", "playback_id", "playback_lease")}}
        await h.send("AudioCompleted", old)
        assert h.app._active_audio[h.sid] == current
        assert (await h.complete())["reason"] == "game_prompt_started"
        assert h.played[-1].response_id == "GAME_PROMPT"
        assert (await h.complete())["reason"] == "game_prompt_finished"
        result = await h.button(1)
        assert result["reason"] in {"game_retry", "game_progress", "game_completed"}
        assert h.state.active_game_id == "quiz"
    finally:
        await h.close()
