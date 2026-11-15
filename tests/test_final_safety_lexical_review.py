"""Deterministic lexical regressions, not a semantic/clinical acceptance set."""
import pytest

from app.foundation.canonical_services import SafetyClassifier


@pytest.mark.parametrize("text", [
    "Это безопасно", "Это совершенно безопасно.",
    "Умеренная погода", "Умеренный ветер", "Я пожарный", "Пожарная машина красная",
    "Меня зовут Обид", "Привет, я Обид.",
    "Я не потерялся", "Я не потерялась.", "Не потерялся!", "  Я НЕ ПОТЕРЯЛСЯ!  ",
])
def test_harmless_lexical_collisions_do_not_trigger_safety(text):
    assert SafetyClassifier().classify(text).severity == "none"


@pytest.mark.parametrize("text", [
    "Тут кровь", "Рука покрыта кровью", "Не могу дышать", "Я не могу двигаться",
    "Я потерялся", "Я потерялась", "Тут опасно", "Опасный взрослый",
    "Пожар!", "Из-за пожара дым", "Пожарище", "Пожарная тревога!", "Меня ударили", "Он трогает меня", "Это насилие",
    "Он умер", "Мама умерла", "Меня хотят умертвить", "Я хочу умереть", "Я тебя убью", "Они меня убьют",
    "Я убьюсь", "Срочно помоги", "Не хочу жить", "Я не хочу жить", "Меня бьют",
    # Harmless fragments must not act as a global bypass for the rest.
    "Это безопасно, но меня бьют", "Я пожарный и не могу дышать",
    "Умеренная погода, а там пожар", "Меня зовут Обид, я не хочу жить",
    "Я не потерялся, но тут кровь", "Я не потерялся. Меня бьют.",
    "Я не потерялась, но не хочу жить", "Не говори, что меня бьют",
])
def test_genuine_lexical_threats_still_raise_urgent(text):
    assert SafetyClassifier().classify(text).severity == "urgent"


@pytest.mark.parametrize("text", [
    "Мне страшно", "Мне обидно", "Меня обидели", "Он хочет обидеть меня",
    "Он обидчик", "Это обида", "Я обиделась", "У меня много обид", "Я плачу", "Мне плохо",
    "Болит рука", "Мне больно", "Это секрет", "Не говори взрослым", "Не хочу идти",
    "Меня зовут Обид, и мне страшно", "Я не потерялся, мне больно",
])
def test_existing_concern_forms_remain_concern_or_stronger(text):
    # A negated loss plus additional context deliberately remains conservative.
    assert SafetyClassifier().classify(text).severity in {"concern", "urgent"}


@pytest.mark.parametrize("text", ["Это безопасно", "Я не потерялся", "Меня зовут Обид"])
@pytest.mark.parametrize("semantic", ["concern", "urgent"])
def test_lexical_exceptions_never_override_semantic_safety(text, semantic):
    assert SafetyClassifier().classify(text, semantic=semantic).severity == semantic


@pytest.mark.parametrize("text", ["Позови взрослого", "Позовите взрослого", "Пожалуйста, позови взрослого!", "Позовите взрослую", "Позвать взрослого", "Мне нужен взрослый", "Помоги"])
def test_explicit_adult_request_imperative_remains_reachable(text):
    from app.foundation.canonical_services import classify_intent
    assert classify_intent(text) == "help_request"


def test_question_or_extra_clause_is_not_a_negation_bypass():
    classifier = SafetyClassifier()
    assert classifier.classify("Я не потерялся?").severity == "urgent"
    assert classifier.classify("Я не потерялся, правда?").severity == "urgent"


@pytest.mark.parametrize("text", [
    "Мне не хватает воздуха", "Не хватает воздуха!", "Мне воздуха не хватает",
    "Мне трудно дышать", "Мне тяжело дышать", "Я задыхаюсь", "Задыхаюсь, помоги!",
    "  Задыхаюсь!  ", "Воздух свежий, но мне не хватает воздуха",
    "Я не потерялся, но я задыхаюсь", "Мне хватает воздуха, но меня бьют",
])
def test_explicit_breathing_distress_cannot_be_downgraded_to_none(text):
    assert SafetyClassifier().classify(text, semantic="none").severity == "urgent"


@pytest.mark.parametrize("text", [
    "Воздух свежий", "Мне хватает воздуха", "Мне легко дышать", "Я не задыхаюсь",
    "Мне не трудно дышать", "Мне не тяжело дышать", "Мне нравится свежий воздух",
])
def test_air_or_breathing_words_alone_are_not_an_urgent_disclosure(text):
    assert SafetyClassifier().classify(text).severity == "none"


@pytest.mark.parametrize("ordinary", [
    "Мне грустно", "Куда мне идти", "Поставь на паузу", "Продолжи сказку",
    "Выключи сказку", "Вау, класс", "Расскажи про космос", "Да",
])
@pytest.mark.parametrize("adult_request", ["позови взрослого", "позовите взрослого", "мне нужен взрослый"])
@pytest.mark.parametrize("request_first", [False, True])
def test_explicit_adult_request_outranks_other_clauses(ordinary, adult_request, request_first):
    from app.foundation.canonical_services import classify_intent
    text = f"{adult_request}, {ordinary}" if request_first else f"{ordinary}, {adult_request}"
    assert classify_intent(text) == "help_request"


@pytest.mark.parametrize("text,expected", [
    ("Мне грустно", "mood"), ("Куда мне идти", "where_now"),
    ("Поставь на паузу", "pause_story"), ("Продолжи сказку", "resume_story"),
    ("Выключи сказку", "stop_story"), ("Вау, класс", "emotional_reaction"),
    ("Помоги продолжить сказку", "resume_story"), ("Помоги", "help_request"),
])
def test_no_explicit_adult_request_keeps_existing_ordinary_priority(text, expected):
    from app.foundation.canonical_services import classify_intent
    assert classify_intent(text) == expected
