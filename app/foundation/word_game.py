"""Authored word-game rules. Child words live only in the owned RAM context."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import re


# Public vocabulary for Mishka's own turns, not a dictionary of allowed child nouns.
ANIMALS = tuple("акула аист антилопа бобёр барсук белка верблюд волк воробей гусь голубь горилла дельфин дятел енот ехидна ёж жираф жаба заяц зебра индюк кабан кот кит конь корова коза лиса лев лось лягушка медведь мышь морж носорог обезьяна олень осёл панда петух попугай рысь рак рыба собака слон сова тигр тюлень утка улитка филин хомяк цапля черепаха шмель щука эму ягуар ящерица".split())
ANIMALS += tuple("бабочка бегемот бурундук гепард горностай динозавр кенгуру коала котёнок котик кошка кошечка крокодил кролик кузнечик курица лошадь муравей оса паук пингвин пони поросёнок пчела свинья собачка сорока страус стрекоза суслик тарантул тритон утёнок хамелеон цыплёнок чайка щенок ястреб".split())
FOOD = tuple("абрикос апельсин арбуз банан булка вишня груша дыня йогурт каша капуста картофель малина молоко морковь огурец персик помидор рис суп сыр хлеб яблоко ягода".split())
FOOD += tuple("блин брокколи варенье виноград горох гречка изюм клубника конфета кукуруза лапша лимон макароны масло мёд мороженое овсянка орех печенье пирог рыба свёкла сметана сосиска творог тыква фасоль яйцо".split())
WORDS = (*ANIMALS, *FOOD, *"автобус барабан велосипед дом дверь ель ёлка жук замок игрушка игла карандаш лампа лодка мяч небо окно облако поезд робот самолёт телефон фонарь цветок цирк чашка шар шапка щётка экран эскимо юла якорь".split())
MAX_SEEN_WORDS = 24
_FILLER = frozenset("я моё мое слово это называю назову выбираю скажу пусть будет тогда например".split())
INTRO_YOUNG = "Сыграем небольшую партию. Назови животное или еду. Я назову другое. Не повторяем слова."
INTRO_ANIMALS = "Сыграем небольшую партию. Будем по очереди называть животных. Не повторяем слова. Назови любое животное."
INTRO_CHAIN = "Сыграем небольшую партию. Называем обычные существительные на последнюю букву, без имён и повторов. Слова берём в начальной форме. Мягкий и твёрдый знак, ы пропускаем. Назови первое слово."


def introduction(band):
    return INTRO_YOUNG if band == "1-3" else INTRO_CHAIN if band in {"5-7", "6-7"} else INTRO_ANIMALS


def spelling(word):
    return word.casefold().replace("ё", "е")


def next_letter(word):
    return next((letter for letter in reversed(spelling(word)) if letter not in "ьъы"), "")


@lru_cache(maxsize=1)
def _morphology():
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except (ImportError, OSError):
        return None


def noun(text, band):
    """No guessed ASR corrections, names, negated alternatives or unknown words."""
    morph = _morphology()
    if morph is None:
        return None, "unavailable"
    if len(text) > 160 or re.search(r"[^А-Яа-яЁё\s,.!?…—-]", text):
        return None, "uncertain"
    tokens = re.findall(r"[а-яё]+", text.casefold())
    if not tokens or len(tokens) > 8 or any(token in {"не", "нет", "или"} for token in tokens):
        return None, "uncertain"
    tokens = [token for token in tokens if token not in _FILLER]
    if len(tokens) != 1:
        return None, "uncertain"
    original = morph.parse(tokens[0])
    parses = [parse for parse in original if parse.is_known and parse.tag.POS == "NOUN"
              and not any(tag in parse.tag for tag in ("Name", "Surn", "Patr", "Geox", "Orgn", "Abbr"))]
    lemmas = {spelling(parse.normal_form): parse.normal_form for parse in parses}
    familiar = {key: value for key, value in lemmas.items() if key in {spelling(word) for word in WORDS}}
    if len(familiar) == 1:
        # A public common-noun definition resolves dictionary name/lemma homographs.
        lemmas = familiar
    elif any(parse.is_known and any(tag in parse.tag for tag in ("Name", "Surn", "Patr", "Geox", "Orgn")) for parse in original):
        return None, "uncertain"
    # The young category is authored; morphology alone cannot identify an animal or food.
    category = ANIMALS + FOOD if band == "1-3" else ANIMALS if band in {"3-5", "4-5"} else None
    if category is not None:
        allowed = {spelling(word) for word in category}
        lemmas = {key: value for key, value in lemmas.items() if key in allowed}
    if len(lemmas) != 1:
        return None, "uncertain"
    return next(iter(lemmas.values())), None


@dataclass(frozen=True)
class WordState:
    used: tuple[str, ...] = field(default=(), repr=False)
    required: str = ""
    pending: WordState | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WordTurn:
    text: str
    state: WordState
    accepted: bool = False
    hint_level: int = 0


def play(text, state, band, *, final=False, return_to_dialogue=False, hint_level=0):
    from .word_game_hints import hint_for, hint_level as bounded_level, local_hint_request
    hint_level = bounded_level(hint_level)
    def assist(reason=None):
        hint, level = hint_for(state, band, hint_level, reason=reason)
        return WordTurn(hint, state, hint_level=level)
    if local_hint_request(text):
        return assist()
    word, error = noun(text, band)
    chain = band in {"5-7", "6-7"}
    if error == "unavailable":
        return WordTurn("Не могу проверить слово, для выхода держи семь.", state, hint_level=hint_level)
    if error:
        return assist()
    key = spelling(word)
    if state.pending is not None:
        # PCM delivery cannot prove the word was heard. Accept a continuation
        # from the proposed word, or a correction under the previous rule.
        pending = state.pending
        state = (pending if key not in pending.used and (not chain or key.startswith(pending.required))
                 else WordState(state.used, state.required))
    if key in state.used:
        return assist('repeat')
    if chain and state.required and not key.startswith(state.required):
        return assist('letter')
    used = (*state.used, key)
    if final:
        ending = "Спасибо, мы вернулись к разговору." if return_to_dialogue else "Спасибо за игру, теперь можно выбрать другое занятие."
        return WordTurn(ending, WordState(used, state.required), True)
    letter = next_letter(word) if chain else ""
    bank = ANIMALS + FOOD if band == "1-3" else WORDS if chain else ANIMALS
    answer = next((candidate for candidate in bank if spelling(candidate) not in used
                   and (not letter or spelling(candidate).startswith(letter))), None)
    if answer is None:
        # An authored vocabulary must not invent an answer to an uncovered letter.
        return WordTurn("Не нашёл подходящее слово, попробуй другое.", state, hint_level=hint_level)
    required = next_letter(answer) if chain else ""
    response = f"{answer.capitalize()}, теперь твоё слово на букву {required.upper()}." if chain else f"{answer.capitalize()}, теперь твоя очередь."
    return WordTurn(response, WordState((*used, spelling(answer)), required), True)


class AuthoredWordTurn:
    """Feed rule output through the existing speech validators and owned PCM queue."""
    def __init__(self, text, structured):
        self.text, self.structured = text, structured

    async def stream(self, messages, request_id, cancelled):
        if cancelled():
            import asyncio
            raise asyncio.CancelledError
        if self.structured:
            import json
            yield json.dumps({"type": "say", "text": self.text}, ensure_ascii=False)
        else:
            yield f"<say>{self.text}</say>"
