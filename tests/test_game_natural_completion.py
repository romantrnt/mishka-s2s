"""Finite public games finish through real application owners and panel projection."""
import copy
import json

import pytest

from app.foundation.config import Settings
from app.foundation.game_engine import current_challenge
from app.foundation.stand import HardwareIntegrationStand
from test_stage1_nested_game_integrity import NestedGame


@pytest.mark.asyncio
@pytest.mark.parametrize("game,slot", [("quiz", 2), ("odd_one_out", 4)])
@pytest.mark.parametrize("origin", ["menu", "dialogue", "pending"])
@pytest.mark.parametrize("terminal", ["AudioCompleted", "AudioFailed"])
async def test_eight_real_answers_finish_with_one_owned_prompt(game, slot, origin, terminal):
    h = NestedGame()
    try:
        nested = origin == "dialogue"
        destination = "bear_dialogue" if nested else "root_menu" if origin == "pending" else "game_menu"
        caller = None
        if nested:
            caller, history, turns, _ = await h.launch(game)
        else:
            if origin != "pending":
                await h.send("HandsetLifted")
            await h.key(1)
            await h.key(slot)
            if origin == "pending":
                assert h.state.mode == "awaiting_handset"
                await h.send("HandsetLifted")
            assert h.state.game_return_mode == (None if origin == "pending" else "game_menu")
            await h.terminal()  # PHRASE_020 → actual question.
        await h.terminal()
        challenge = current_challenge(h.state.game_engine_state)
        wrong = next(button for button, _ in challenge.options if button not in {challenge.answer, 7})
        assert (await h.key(wrong))[0]["reason"] == "game_retry"
        assert h.state.game_engine_state.round == 0
        await h.terminal()
        for index in range(8):
            engine = h.state.game_engine_state
            assert engine.round == index and h.state.game_audio_stage == "playing"
            answer = current_challenge(engine).answer
            pressed, released = await h.key(answer)
            result = released if answer == 7 else pressed
            if index < 7:
                assert result["reason"] == "game_progress"
                assert h.state.game_engine_state.round == index + 1
                prompt = dict(h.app._active_audio[h.sid])
                # A fast second key is still owned by the unheard next prompt.
                await h.key(answer)
                assert h.state.game_engine_state.round == index + 1
                assert h.app._active_audio[h.sid] == prompt
                await h.terminal()
        assert result["reason"] == "game_completed"
        assert h.state.mode == destination
        assert h.state.game_engine_state is None and not h.app._game_timer_scopes
        assert not h.app._game_callers
        if nested:
            assert h.app.conversation_contexts[h.sid] is caller
            assert caller.history == history and caller.turns == turns
        finish = dict(h.app._active_audio[h.sid])
        assert finish["content_id"] == ("DIALOGUE_RETURN" if nested else "PHRASE_028" if origin == "pending" else "PHRASE_019")
        audio = [effect for effect in result["outputs"] if effect["type"] in {"speak", "dialogue_prompt"}]
        assert len(audio) == 1 and audio[0]["playback_lease"] == finish["playback_lease"]
        assert all(effect.get("resolution") != "audio_dispatch_failed" for effect in result["outputs"])
        frame = copy.deepcopy(h.output.snapshot())
        for stale_terminal in ("AudioCompleted", "AudioFailed"):
            assert (await h.terminal(stale_terminal, prompt))["reason"] == "stale_audio_callback"
            assert h.app._active_audio[h.sid] == finish and h.output.snapshot() == frame
        await h.terminal(terminal)
        if origin == "menu":
            # The existing menu invitation may start its own catalogue narration.
            if h.sid in h.app._active_audio:
                assert h.app._active_audio[h.sid]["content_id"] == "MENU_GAME"
                await h.terminal()
        assert h.sid not in h.app._active_audio and h.state.active_audio_id is None
        assert h.state.mode == destination
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_panel_exposes_only_a_bounded_zero_based_game_round():
    stand = HardwareIntegrationStand(settings=Settings(profile="mock"), background_scheduler=False)
    try:
        game = {"phase": "playing", "round": 0, "answer": "PRIVATE_ANSWER", "deck": ["PRIVATE_DECK"]}
        stand.application.last_canonical_state = {
            "mode": "game_active", "active_game_id": "quiz", "game_audio_stage": "playing",
            "game_engine_state": game,
        }
        for round_number in range(8):
            game["round"] = round_number
            projected = stand.panel_snapshot()["canonical_state"]
            assert projected["game_round"] == round_number
            public = json.dumps(projected)
            assert "PRIVATE_" not in public and "game_engine_state" not in projected
        for invalid in (-1, 8, True, "2", 1.5, None):
            game["round"] = invalid
            assert "game_round" not in stand.panel_snapshot()["canonical_state"]
    finally:
        await stand.aclose()
