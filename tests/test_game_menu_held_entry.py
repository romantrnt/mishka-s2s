"""Real menu-to-game held-key ownership, not seeded engine state."""
import pytest
from test_scene_menu_runtime import Harness
from app.foundation.game_engine import start, advance

@pytest.mark.asyncio
async def test_wrong_key_held_in_menu_cannot_disappear_and_score_simultaneous_game():
    h = Harness('3-5')
    try:
        await h.button(1); await h.release(1)
        await h.button(1); await h.release(1)  # Explicit narration interruption.
        await h.button(6); await h.release(6)
        await h.button(1); await h.release(1)
        assert h.state.scene_menu_descriptor['items'][1]['id'] == 'press_together'
        await h.button(3)  # Not a choice on this page, but physically still down.
        result = await h.button(2)
        assert result['reason'] == 'game_started'
        assert h.state.game_engine_state.held == (2, 3)
        assert h.state.game_engine_state.down_times == ()
        await h.release(2)
        await h.button(1); blocked = await h.button(2)
        assert blocked['reason'] != 'game_progress'
        assert h.state.game_engine_state.round == 0
        assert set(h.state.game_engine_state.held) == {1, 2, 3}
        await h.release(3)
        assert h.state.game_engine_state.round == 0  # Release is not a score.
        await h.release(1); await h.release(2)
        await h.button(1); recovered = await h.button(2)
        assert recovered['reason'] == 'game_progress'
        assert h.state.game_engine_state.round == 1
    finally:
        await h.close()

@pytest.mark.parametrize('game', ['catch_light', 'find_pair', 'press_together', 'hold_light', 'repeat_pattern'])
def test_inherited_playable_hold_is_not_a_fresh_gameplay_down(game):
    state = start(game, sequence=0, now=0., selection_button=2,
                  held_buttons=(1, 2, 3, 5, 6, 7))
    assert state.held == (1, 2, 3)
    assert state.down_times == ()
    result = advance(state, edge='ButtonPressed', button=1, sequence=1, now=100.)
    assert result.status == 'held'
    assert result.state.round == 0
    assert result.state.down_times == ()
    released = advance(result.state, edge='ButtonReleased', button=1, sequence=2, now=101.)
    assert released.state.round == 0
    assert 1 not in released.state.held


def test_inherited_hold_has_no_elapsed_duration_until_fresh_press():
    state = start('hold_light', sequence=0, now=0., selection_button=2, held_buttons=(1, 2))
    released = advance(state, edge='ButtonReleased', button=1, sequence=1, now=100.)
    assert released.state.round == 0
    pressed = advance(released.state, edge='ButtonPressed', button=1, sequence=2, now=101.)
    early = advance(pressed.state, edge='ButtonReleased', button=1, sequence=3, now=101.1)
    assert early.status == 'early_release'
    assert early.state.round == 0


@pytest.mark.asyncio
async def test_held_key_survives_game_exit_root_and_second_game_entry():
    h = Harness('3-5')
    try:
        await h.button(1); await h.release(1)
        await h.button(1); await h.release(1)
        await h.button(6); await h.release(6)
        await h.button(1); await h.release(1)
        await h.button(3)
        assert (await h.button(2))['reason'] == 'game_started'
        await h.release(2)
        await h.button(7); await h.release(7)
        assert (await h.button(7))['reason'] == 'game_exit_confirmed'
        await h.release(7)
        assert h.state.mode == 'root_menu'
        assert 3 in h.state.accepted_buttons_down
        await h.button(1); await h.release(1)
        assert h.state.mode == 'game_menu'
        assert h.state.scene_menu_descriptor['items'][1]['id'] == 'press_together'
        assert (await h.button(2))['reason'] == 'game_started'
        assert h.state.game_engine_state.held == (2, 3)
        assert h.state.game_engine_state.down_times == ()
        await h.release(2)
        await h.button(1)
        assert (await h.button(2))['reason'] != 'game_progress'
        assert h.state.game_engine_state.round == 0
        await h.release(3); await h.release(1); await h.release(2)
        await h.button(1)
        assert (await h.button(2))['reason'] == 'game_progress'
    finally:
        await h.close()
