"""Pure gameplay scenarios for the five-button immersive game chooser."""
from dataclasses import replace

import pytest

from app.foundation.game_engine import (
    IMMERSIVE_GAME_SPECS, advance, current_challenge, desired_buttons,
    finish_prompt, game_answer_buttons, next_deadline, start, timer_step,
)


def edge(state, button, *, released=False, now=None):
    return advance(state, edge='ButtonReleased' if released else 'ButtonPressed',
                   button=button, sequence=state.last_sequence + 1,
                   now=state.last_time + .1 if now is None else now)


def ready_pattern(state):
    if state.phase == 'instruction':
        state = finish_prompt(state, state.last_time).state
    while state.phase == 'demonstrate':
        state = timer_step(state, next_deadline(state)).state
    return state


def test_five_direct_games_keep_their_distinct_ids():
    assert [s.game_id for s in IMMERSIVE_GAME_SPECS] == [
        'free_lights', 'quiz', 'repeat_pattern', 'odd_one_out', 'word_game']


def test_free_lights_fresh_press_emits_wave_and_release_does_not_replace_it():
    state = start('free_lights', sequence=0, now=0., selection_button=1)
    assert edge(state, 1).wave_origin is None
    state = edge(state, 1, released=True).state
    pressed = edge(state, 1)
    assert pressed.wave_origin == 1
    assert edge(pressed.state, 1).wave_origin is None
    assert edge(pressed.state, 1, released=True).wave_origin is None
    assert desired_buttons(pressed.state) == ()
    assert 5 in game_answer_buttons(state) and 7 in game_answer_buttons(state)
    assert next_deadline(state) is None


@pytest.mark.parametrize('kind', ['quiz', 'odd_one_out'])
def test_prompt_blocks_scoring_and_inherited_keys_must_be_released(kind):
    state = start(kind, sequence=0, now=0., selection_button=7, age_band='6-7', seed=91)
    answer = current_challenge(state).answer
    assert state.phase == 'awaiting_prompt'
    pressed = edge(state, answer)
    assert pressed.state.round == 0
    assert next_deadline(pressed.state) is None
    state = finish_prompt(pressed.state, pressed.state.last_time).state
    assert state.phase == 'playing'
    assert edge(state, answer).state.round == 0
    state = edge(state, answer, released=True).state
    result = edge(state, answer)
    if answer == 7:
        assert result.state.round == 0
        result = edge(result.state, answer, released=True)
    assert result.state.round == 1
    assert result.state.phase == 'awaiting_prompt'
    assert result.speech_text == current_challenge(result.state).prompt
    assert result.feedback_id == 'PHRASE_022'
    assert not result.completed
    assert next_deadline(result.state) is None


def test_offered_seven_and_five_are_answers_not_reserved_controls():
    state = start('quiz', sequence=0, now=0., selection_button=1, age_band='6-7', seed=21)
    question = current_challenge(state)
    custom = replace(question, options=((5, 'пять'), (7, 'семь')), answer=7)
    state = replace(state, deck=(custom,), challenge_index=0)
    assert game_answer_buttons(state) == (5, 7)
    assert desired_buttons(state) == (5, 7)
    state = finish_prompt(state, 0.).state
    result = edge(state, 7)
    assert result.state.round == 0
    result = edge(result.state, 7, released=True)
    assert result.state.round == 1 and result.status == 'progress'
    assert not result.completed


def test_wrong_answer_gives_ordered_hints_and_does_not_fake_success():
    state = start('odd_one_out', sequence=0, now=0., selection_button=7, age_band='4-5', seed=4)
    question = current_challenge(state)
    question = replace(question, hints=('Подсказка один.', 'Подсказка два.'))
    state = replace(state, deck=(question,))
    wrong = next(b for b in game_answer_buttons(state) if b != question.answer)
    for wanted in question.hints + question.hints[-1:]:
        state = finish_prompt(state, state.last_time).state
        state = edge(state, wrong, released=True).state
        result = edge(state, wrong)
        assert result.speech_text == wanted
        assert result.state.round == 0
        assert current_challenge(result.state).id == question.id
        assert result.state.phase == 'awaiting_prompt'
        state = result.state


def test_quiz_timeout_gives_hint_then_waits_for_owned_prompt_terminal():
    state = start('quiz', sequence=0, now=0., selection_button=7, age_band='1-3', seed=3)
    state = finish_prompt(state, 1.).state
    result = timer_step(state, next_deadline(state))
    assert result.status == 'wait_hint'
    assert result.speech_text
    assert result.state.phase == 'awaiting_prompt'
    assert result.state.round == 0
    assert next_deadline(result.state) is None
    resumed = finish_prompt(result.state, result.state.last_time).state
    assert resumed.hints_emitted == 1
    assert next_deadline(resumed) is not None


def test_repeat_pattern_progresses_beyond_old_three_round_limit_and_resumes_level():
    state = start('repeat_pattern', sequence=0, now=0., selection_button=7,
                  age_band='6-7', level=3, seed=13, immersive=True)
    for _ in range(5):
        state = ready_pattern(state)
        for button in state.pattern:
            state = edge(state, button, released=True).state
            outcome = edge(state, button)
            state = outcome.state
        assert not outcome.completed
    assert state.level == 8 and state.round == 5
    resumed = start('repeat_pattern', sequence=0, now=state.last_time, selection_button=7,
                    age_band='6-7', level=state.level, seed=13, immersive=True)
    assert resumed.level == 8
    assert len(ready_pattern(resumed).pattern) >= len(ready_pattern(
        start('repeat_pattern', sequence=0, now=0., selection_button=7,
              age_band='6-7', level=0, seed=13, immersive=True)).pattern)


def test_patterns_have_real_variation_and_youngest_stay_short():
    patterns = set()
    for seed in range(12):
        state = ready_pattern(start('repeat_pattern', sequence=0, now=0., selection_button=7,
                                    age_band='1-3', level=20, seed=seed, immersive=True))
        assert 2 <= len(state.pattern) <= 3
        assert set(state.pattern) <= {1, 2, 3, 4}
        assert game_answer_buttons(state) == (1, 2, 3, 4)
        patterns.add(state.pattern)
    assert len(patterns) > 4


def test_stale_or_paused_answers_cannot_progress_new_quiz():
    state = finish_prompt(start('quiz', sequence=4, now=10., selection_button=7), 10.).state
    answer = current_challenge(state).answer
    for now in [float('nan'), float('inf'), 9.]:
        result = advance(state, edge='ButtonPressed', button=answer, sequence=5, now=now)
        assert result.state == state
    assert advance(state, edge='ButtonPressed', button=answer, sequence=4, now=11.).state == state
    paused = advance(state, edge='ButtonPressed', button=answer, sequence=5, now=11., paused=True).state
    assert paused.round == 0
    assert edge(paused, answer).state.round == 0


def test_word_game_has_no_button_scoring_or_fabricated_timeout():
    state = start('word_game', sequence=0, now=0., selection_button=5, age_band='4-5')
    assert game_answer_buttons(state) == ()
    assert next_deadline(state) is None
    for button in range(1, 8):
        assert edge(state, button).state.round == 0


def test_released_button_is_forgotten_even_when_not_offered_by_current_question():
    state = start('quiz', sequence=0, now=0., selection_button=7, seed=4)
    question = replace(current_challenge(state), options=((1, 'один'),), answer=1)
    state = replace(state, deck=(question,), held=(6, 7))
    state = edge(state, 6, released=True).state
    assert 6 not in state.held
    state = finish_prompt(state, state.last_time).state
    ignored = edge(state, 6)
    assert ignored.status == 'ignored' and ignored.state.round == 0


def test_prompt_terminal_is_idempotent_and_timers_never_advance_a_challenge():
    state = start('quiz', sequence=0, now=0., selection_button=7, hint_limit=2)
    state = finish_prompt(state, 1.).state
    assert finish_prompt(state, 1.).state == state
    question_id = current_challenge(state).id
    for expected in (1, 2):
        state = timer_step(state, next_deadline(state)).state
        assert state.hints_emitted == expected
        assert current_challenge(state).id == question_id and state.round == 0
        state = finish_prompt(state, state.last_time).state
    final = timer_step(state, next_deadline(state))
    assert final.state.round == 0 and not final.completed
    assert final.state.phase == 'playing' and next_deadline(final.state) is None


def test_wrong_answers_and_timer_use_one_ordered_hint_cursor():
    state = start('quiz', sequence=0, now=0., selection_button=7, age_band='6-7')
    question = replace(current_challenge(state), options=((1, 'один'), (2, 'два')),
                       answer=1, hints=('Подсказка один.', 'Подсказка два.', 'Подсказка три.'))
    state = replace(state, deck=(question,))
    for expected in question.hints[:2]:
        state = finish_prompt(state, state.last_time).state
        state = edge(state, 2, released=True).state
        result = edge(state, 2)
        assert result.speech_text == expected
        state = result.state
    state = finish_prompt(state, state.last_time).state
    result = timer_step(state, next_deadline(state))
    assert result.speech_text == question.hints[2]
    assert result.state.round == 0 and result.state.hints_emitted == 1
