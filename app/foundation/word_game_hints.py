"""Small authored clues; examples never become a child's played word."""
from __future__ import annotations

import re
from .word_game import ANIMALS, FOOD, WORDS, noun, spelling


def local_hint_request(text: str) -> bool:
    text = ' '.join(str(text).casefold().split()).strip(' .!?…')
    return re.fullmatch(
        r'(?:мишка[,!:]?\s+)?(?:помоги(?: мне)?|подскажи|дай(?: мне)? подсказку|'
        r'нужна подсказка|не знаю(?: слово| что сказать)?)(?:,? пожалуйста)?', text) is not None


def hint_level(value) -> int:
    return min(2, max(0, value)) if type(value) is int else 0


def example_for(state, band: str) -> str | None:
    # A partly emitted proposal can be continued without committing it as heard.
    current = state.pending or state
    used = set(state.used) | set(current.used)
    chain = band in {'5-7', '6-7'}
    bank = ANIMALS + FOOD if band == '1-3' else WORDS if chain else ANIMALS
    for word in bank:
        key = spelling(word)
        if key in used or (chain and current.required and not key.startswith(current.required)):
            continue
        lemma, error = noun(word, band)
        if error is None and spelling(lemma) == key:
            return lemma
    return None


def hint_for(state, band: str, level: int = 0, *, reason: str | None = None) -> tuple[str, int]:
    level = hint_level(level)
    chain = band in {'5-7', '6-7'}
    if level:
        example = example_for(state, band)
        text = (f'Например, {example}. Теперь твоя очередь.' if example
                else 'Не могу проверить пример, для выхода держи семь.')
    elif reason == 'repeat':
        required = (state.pending or state).required
        text = (f'Это слово уже было. Вспомни слово на букву {required.upper()}.' if chain and required
                else 'Это слово уже было. Вспомни название предмета, животного или растения.' if chain
                else 'Это слово уже было. Вспомни другое животное.' if band != '1-3'
                else 'Это слово уже было. Вспомни животное или еду.')
    elif chain and (state.pending or state).required:
        letter = (state.pending or state).required.upper()
        text = f'Вспомни слово на букву {letter}.'
    elif band == '1-3':
        text = 'Вспомни знакомое животное или еду.'
    elif chain:
        text = 'Вспомни название предмета, животного или растения.'
    else:
        text = 'Вспомни знакомое домашнее или лесное животное.'
    return text, min(2, level + 1)
