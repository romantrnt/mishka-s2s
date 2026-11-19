"""Real menu-to-game held-key ownership, not seeded engine state."""
import pytest
from test_scene_menu_runtime import Harness
from app.foundation.game_engine import start, advance, current_challenge


async def select_quiz(h):
    assert h.state.scene_menu_descriptor['items'][1]['id'] == 'quiz'
    h.state.game_entry_seed = 16  # quiz-4-5-06 offers the inherited button six.
    result = await h.button(2)
    assert result['reason'] == 'game_started'
    assert h.state.game_engine_state.held == (2, 6)
    assert h.state.game_engine_state.down_times == ()
    await h.release(2)
    assert (await h.complete())['reason'] == 'game_prompt_started'
    assert (await h.complete())['reason'] == 'game_prompt_finished'
    assert current_challenge(h.state.game_engine_state).answer == 6


@pytest.mark.asyncio
async def test_key_held_in_menu_cannot_disappear_and_score_current_quiz():
    h = Harness('3-5')
    try:
        await h.button(1); await h.release(1)
        assert (await h.button(6))['reason'] == 'scene_menu_skipped'
        await select_quiz(h)
        blocked = await h.button(6)
        assert blocked['reason'] != 'game_progress'
        assert h.state.game_engine_state.round == 0
        assert h.state.game_engine_state.held == (6,)
        assert h.state.game_engine_state.down_times == ()
        await h.release(6)
        assert h.state.game_engine_state.round == 0  # Release is not a score.
        recovered = await h.button(6)
        assert recovered['reason'] == 'game_progress'
        assert h.state.game_engine_state.round == 1
    finally:
        await h.close()


def test_legacy_simultaneous_game_requires_fresh_overlap_without_wrong_held_key():
    state = start('press_together', sequence=0, now=0., selection_button=2, held_buttons=(2, 3))
    assert state.held == (2, 3) and state.down_times == ()
    for sequence, (edge, button) in enumerate((('ButtonReleased', 2), ('ButtonPressed', 1), ('ButtonPressed', 2)), 1):
        result = advance(state, edge=edge, button=button, sequence=sequence, now=float(sequence))
        state = result.state
        assert state.round == 0 and result.status != 'progress'
    assert set(state.held) == {1, 2, 3}
    state = advance(state, edge='ButtonReleased', button=3, sequence=4, now=4.).state
    assert state.round == 0
    for sequence, (edge, button) in enumerate((('ButtonReleased', 1), ('ButtonReleased', 2), ('ButtonPressed', 1), ('ButtonPressed', 2)), 5):
        result = advance(state, edge=edge, button=button, sequence=sequence, now=float(sequence))
        state = result.state
    assert result.status == 'progress' and state.round == 1


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
    clock = [100.]
    h.app.monotonic_now = lambda: clock[0]
    try:
        await h.button(1); await h.release(1)
        assert (await h.button(6))['reason'] == 'scene_menu_skipped'
        await select_quiz(h)
        assert (await h.button(7))['reason'] == 'game_exit_pending'
        clock[0] += 6.
        assert (await h.release(7))['reason'] == 'game_exited_by_long_button'
        assert h.state.mode == 'game_menu'
        assert h.state.game_engine_state is None and h.app._active_audio[h.sid]["content_id"] == "PHRASE_019"
        assert 6 in h.state.accepted_buttons_down
        await select_quiz(h)
        assert (await h.button(6))['reason'] != 'game_progress'
        assert h.state.game_engine_state.round == 0
        assert h.state.game_engine_state.held == (6,) and h.state.game_engine_state.down_times == ()
        await h.release(6)
        assert h.state.game_engine_state.round == 0
        assert (await h.button(6))['reason'] == 'game_progress'
    finally:
        await h.close()
