"""Content contracts: real variety, physical targets, age boundaries and seeds."""
from dataclasses import FrozenInstanceError
import random

import pytest

from app.foundation.game_content import (
    BANDS, COLORS, GameChallenge, build_deck, normalize_band,
)


def _primary(kind, band, seed=23):
    return tuple(item for item in build_deck(kind, band, seed=seed)
                 if item.age_band == band)


def _answer_text(item):
    return dict(item.options)[item.answer]


def test_three_canonical_age_bands_and_legacy_profiles():
    assert BANDS == ('1-3', '4-5', '6-7')
    assert normalize_band('3-5') == '4-5'
    assert normalize_band('5-7') == '6-7'
    assert build_deck('quiz', '3-5', seed=5) == build_deck('quiz', '4-5', seed=5)
    assert build_deck('odd_one_out', '5-7', seed=6) == build_deck('odd_one_out', '6-7', seed=6)
    with pytest.raises(ValueError, match='age band'):
        build_deck('quiz', '0-99', seed=1)
    with pytest.raises(ValueError, match='game kind'):
        build_deck('placeholder', '1-3', seed=1)


def test_fifty_distinct_original_odd_sets_per_age_not_reordered_duplicates():
    all_sets = set()
    all_ids = set()
    for band in BANDS:
        primary = _primary('odd_one_out', band)
        assert len(primary) == 50
        for item in primary:
            assert len(item.options) == 5
            signature = frozenset(text.casefold().strip() for _, text in item.options)
            assert len(signature) == 5
            assert signature not in all_sets
            assert item.id not in all_ids
            all_sets.add(signature)
            all_ids.add(item.id)
            # The intended classification is audible before the item list and
            # the final hint explicitly names the selected outsider.
            assert 'Четыре варианта — ' in item.prompt
            assert _answer_text(item) in item.hints[-1]
    assert len(all_sets) == 150


def test_quiz_reserve_has_twenty_four_primary_prompts_per_age():
    for band in BANDS:
        primary = _primary('quiz', band)
        assert len(primary) == 24
        assert len({item.id for item in primary}) == 24
        assert len({item.prompt for item in primary}) == 24


@pytest.mark.parametrize('kind', ('quiz', 'odd_one_out'))
@pytest.mark.parametrize('band', BANDS)
def test_decks_have_unique_ids_valid_answers_hints_and_bounded_adjacent_mix(kind, band):
    deck = build_deck(kind, band, seed=111)
    assert isinstance(deck, tuple)
    assert len({item.id for item in deck}) == len(deck)
    adjacent = [item for item in deck if item.age_band != band]
    assert .15 <= len(adjacent) / len(deck) <= .20
    source_index = BANDS.index(band)
    for item in deck:
        assert isinstance(item, GameChallenge)
        assert abs(source_index - BANDS.index(item.age_band)) <= 1
        assert item.kind == kind
        assert item.display in {'digits', 'colors', 'pictograms'}
        assert isinstance(item.options, tuple)
        assert all(type(button) is int and 1 <= button <= 7 for button, _ in item.options)
        assert len({button for button, _ in item.options}) == len(item.options)
        assert len({text for _, text in item.options}) == len(item.options)
        assert item.answer in dict(item.options)
        assert item.prompt.strip() and len(item.hints) >= 2
        assert all(text.strip() for _, text in item.options)
        assert all(hint.strip() for hint in item.hints)


def test_toddlers_and_their_adjacent_quizzes_never_require_number_labels():
    for seed in range(8):
        for kind in ('quiz', 'odd_one_out'):
            for item in build_deck(kind, '1-3', seed=seed):
                assert item.display == 'colors'
                if kind == 'quiz':
                    assert len(item.options) == 7
                    assert tuple(b for b, _ in item.options) == (1, 2, 3, 4, 5, 6, 7)
                    assert not any(token in item.prompt.lower() for token in (
                        'сколько', 'плюс', 'минус', 'число', 'слог', 'букв',
                    ))


def test_colored_odd_options_do_not_conflict_with_spoken_color_names():
    color_words = {text for _, text in COLORS} | {'белый', 'чёрный'}
    for seed in range(12):
        for item in build_deck('odd_one_out', '1-3', seed=seed):
            assert item.display == 'colors'
            assert not color_words.intersection(text for _, text in item.options)


def test_yellow_sun_question_lights_all_seven_physical_colors():
    sun = next(item for item in _primary('quiz', '4-5')
               if 'солнышко' in item.prompt)
    assert sun.prompt == 'Нарисуем жёлтое солнышко. Нажми этот цвет.'
    assert sun.display == 'colors' and sun.answer == 3
    assert sun.options == COLORS


def test_color_and_pictogram_choices_keep_real_board_positions_for_every_seed():
    physical_colors = dict(COLORS)
    for band in BANDS:
        for seed in (2, 10, 31):
            for item in build_deck('quiz', band, seed=seed):
                if item.display == 'colors':
                    assert all(physical_colors[button] == text for button, text in item.options)
                elif item.display == 'pictograms':
                    assert all((button, text) in {(6, 'мишка'), (7, 'значок погоды')}
                               for button, text in item.options)
                    assert len(item.options) == 1
                    assert 'тучк' not in item.prompt.lower()


def test_seven_remains_an_answer_for_rainbow_and_days_not_a_missing_option():
    older = _primary('quiz', '6-7')
    rainbow = next(item for item in older if 'радуг' in item.prompt)
    days = next(item for item in older if 'дней в неделе' in item.prompt)
    assert (rainbow.answer, days.answer) == (7, 7)
    assert dict(rainbow.options)[7] == dict(days.options)[7] == 'семь'
    assert rainbow.display == days.display == 'digits'


def test_curated_science_language_and_classification_answers():
    middle = {item.prompt: _answer_text(item) for item in _primary('quiz', '4-5')}
    assert middle['Сколько лап у собаки?'] == 'четыре'
    assert middle['Сколько времён года?'] == 'четыре'
    assert middle['Сколько ножек у жука?'] == 'шесть'
    older = {item.prompt: _answer_text(item) for item in _primary('quiz', '6-7')}
    assert older['Сколько будет два плюс три?'] == 'пять'
    assert older['Сколько слогов в слове «машина»?'] == 'три'
    assert older['Какой цвет получится при смешивании красной и синей краски?'] == 'фиолетовый'
    odd = {item.prompt: _answer_text(item) for item in _primary('odd_one_out', '6-7')}
    assert odd['Найди лишнее. Четыре варианта — птицы.'] == 'крокодил'
    assert odd['Найди лишнее. Четыре варианта — чётные числа.'] == 'пять'
    assert odd['Найди лишнее. Четыре варианта — слова из трёх слогов.'] == 'стол'
    assert odd['Найди лишнее. Четыре варианта — планеты Солнечной системы.'] == 'Луна'


def test_only_confirmed_existing_question_assets_are_referenced():
    assets = {item.phrase_id: item.prompt for band in BANDS
              for item in _primary('quiz', band) if item.phrase_id}
    assert assets == {
        'PHRASE_043': 'Сколько лап у собаки?',
        'PHRASE_046': 'Сколько времён года?',
        'PHRASE_049': 'Сколько дней в неделе?',
    }
    assert all(item.phrase_id is None for band in BANDS
               for item in _primary('odd_one_out', band))


@pytest.mark.parametrize('kind', ('quiz', 'odd_one_out'))
def test_same_seed_is_stable_new_seed_changes_order_and_does_not_touch_global_rng(kind):
    random.seed(143)
    before = random.getstate()
    first = build_deck(kind, '4-5', seed=5)
    assert build_deck(kind, '4-5', seed=5) == first
    second = build_deck(kind, '4-5', seed=6)
    assert [item.id for item in first] != [item.id for item in second]
    assert random.getstate() == before


def test_odd_answer_tracks_its_text_through_shuffles_and_all_positions_are_reachable():
    original = {item.id: _answer_text(item) for band in BANDS
                for item in _primary('odd_one_out', band)}
    observed_positions = set()
    for seed in range(12):
        for band in BANDS:
            for item in build_deck('odd_one_out', band, seed=seed):
                assert _answer_text(item) == original[item.id]
                assert tuple(button for button, _ in item.options) == (1, 2, 3, 4, 5)
                observed_positions.add(item.answer)
    assert observed_positions == {1, 2, 3, 4, 5}


def test_challenges_are_immutable():
    item = build_deck('quiz', '1-3', seed=1)[0]
    with pytest.raises(FrozenInstanceError):
        item.answer = 1
