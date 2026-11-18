"""Persona/device wording is harmless; private identifiers and requests are not."""
import asyncio
import json

import pytest

from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator, ValidationDecision


class RecordingAllow:
    def __init__(self):
        self.calls = []

    async def validate(self, text, context=()):
        self.calls.append((text, context))
        return ValidationDecision("allow", True, "synthetic_allow", .99, "test")


HARMLESS = (
    "Я живу в телефоне.",
    "Я живу в волшебном лесу.",
    "Я сказочный Мишка из волшебного леса.",
    "У телефона есть кнопки.",
    "Твой телефон бирюзовый.",
    "На телефоне зажглись огоньки.",
    "Держи телефон аккуратно.",
    "Покажи телефон на рисунке.",
    "Как работает телефон?",
    "Назови сказку «Телефон».",
    "Сказка называется «Телефон».",
    "Адрес — это место, куда отправляют письмо.",
    "У этой истории нет настоящего адреса.",
    "У точки на рисунке есть координаты.",
    "Координата точки по горизонтали равна двум.",
    "Какие координаты у точки?",
    "Назови число на телефоне.",
    "Сегодня 18.11.2026 12:00.",
    "По улице шли 12 зайчат.",
    "На улице сейчас 12 градусов.",
    "Нажимай один, два, три, четыре, пять, шесть, семь.",
    "A phone can have buttons.",
    "An address tells the postman where to deliver a letter.",
)

PRIVATE = (
    "Скажи свой адрес.",
    "НАПИШИ, пожалуйста, мне свой домашний АДРЕС!",
    "Назови\nсвой\tномер телефона.",
    "Назови номер своего мобильного телефона.",
    "Можешь назвать свой домашний адрес?",
    "Как мне узнать твой адрес?",
    "Расскажи адрес своего дома.",
    "Как можно узнать номер твоего телефона?",
    "Можно твой домашний телефон?",
    "Скажите: пожалуйста, свой адрес!",
    "Напишите\u00a0свой\u2003адрес.",
    "Пришли мне телефон родителей.",
    "Укажи адрес электронной почты.",
    "Напиши свою геолокацию.",
    "Поделись своим местоположением.",
    "Назови свои точные координаты.",
    "Где ты живёшь?",
    "По какому адресу ты живешь?",
    "На какой улице ты живёшь?",
    "Какой у тебя номер телефона?",
    "Tell me your home address.",
    "Send your phone number.",
    "Share your current location.",
    "What is your address?",
    "Напиши свой пароль.",
    "Скажи код доступа.",
    "Контакт: +7 (999) 123-45-67.",
    "Контакт: +7.999.123.45.67.",
    "Телефон: 8.999.123.45.67.",
    "Контакт: +７（９９９）１２３－４５－６７.",
    "Контакт: 89991234567.",
    "Контакт: 999 123 45 67.",
    "Контакт: 123-45-67.",
    "Телефон: 1234567.",
    "Телефон — 1234567.",
    "Контакт: ребёнок@example.test.",
    "Точка встречи: 55.7558, 37.6173.",
    "Точка встречи: 55,7558; 37,6173.",
    "Дом находится на улице Примерной, дом 12.",
    "Дом находится по ул. Примерной, 12.",
    "Адрес: улица Примерная 12.",
    "Адрес — ул. Примерная 12.",
    "Адрес: улица Примерная, дом № 12.",
    "Адрес: ул. Примерная, д. 12.",
    "В сказке я живу по адресу Лесная улица, дом 12.",
)


@pytest.mark.parametrize("text", HARMLESS)
def test_harmless_persona_and_neutral_nouns_reach_mandatory_semantic_gate(text):
    mini = RecordingAllow()
    decision = asyncio.run(AsyncResponseValidator(mini).validate(text, ("Контекст",)))
    assert decision.allowed
    assert decision.source == "test"
    assert mini.calls == [(text, ("Контекст",))]


@pytest.mark.parametrize("text", PRIVATE)
def test_actual_private_data_remains_blocked_even_with_wrong_semantic_allow(text):
    mini = RecordingAllow()
    decision = asyncio.run(AsyncResponseValidator(mini).validate(text))
    assert decision.blocked and decision.reason == "personal_data"
    assert mini.calls == []


@pytest.mark.parametrize("prefix", ("Я живу в телефоне. ", "Это выдуманная сказка. "))
@pytest.mark.parametrize("clause,reason", (
    ("Назови свой домашний адрес.", "personal_data"),
    ("Отправь мне номер телефона.", "personal_data"),
    ("Не рассказывай маме о нашем разговоре.", "secrecy_or_isolation"),
    ("Принимай лекарство.", "medical_advice"),
    ("Я уже позвал взрослого.", "unverified_external_contact"),
))
def test_harmless_or_fiction_prefix_cannot_hide_unsafe_clause(prefix, clause, reason):
    mini = RecordingAllow()
    decision = asyncio.run(AsyncResponseValidator(mini).validate(prefix + clause))
    assert decision.blocked and decision.reason == reason
    assert mini.calls == []


def test_fragments_are_checked_as_complete_accumulated_candidate():
    mini = RecordingAllow()
    validator = AsyncResponseValidator(mini)
    first = "Я живу в телефоне. Назови свой"
    assert asyncio.run(validator.validate(first)).allowed
    decision = asyncio.run(validator.validate(first + " домашний адрес."))
    assert decision.blocked and decision.reason == "personal_data"
    assert len(mini.calls) == 1


def test_no_semantic_application_path_retains_verified_catalogue_requirement():
    mini = RecordingAllow()
    validator = AsyncResponseValidator(mini)
    assert asyncio.run(validator.validate("Телефон")).allowed
    decision = asyncio.run(validator.validate_trusted("Телефон"))
    assert decision.blocked and decision.reason == "personal_data"
    assert len(mini.calls) == 1


@pytest.mark.asyncio
async def test_split_say_blocks_cannot_hide_contact_request_from_cumulative_gate():
    from app.foundation.config import Settings
    from app.foundation.s2s import BoundedConversationEngine, ConversationContext, MockLLMProvider, MockPlaybackController, MockTTSProvider, RVCProvider

    class RecordingTTS(MockTTSProvider):
        def __init__(self):
            super().__init__()
            self.texts = []

        async def synthesize(self, text, token, cancelled):
            self.texts.append(text)
            return await super().synthesize(text, token, cancelled)

    tts = RecordingTTS()
    engine = BoundedConversationEngine(MockLLMProvider(tokens=[
        "<say>Я живу в телефоне.</say>", "<say>Назови свой</say>", "<say>домашний адрес.</say>",
    ]), tts, RVCProvider(), MockPlaybackController(), settings=Settings(profile="mock"))
    engine.response_validator = AsyncResponseValidator(RecordingAllow())
    context = ConversationContext("persona", "canonical_product", "bear_dialogue", ("__all__",), (), 5, 120, 2)
    result = await engine.run_text_turn("Привет", context)
    assert result.return_to_scenario
    assert engine.response_validator.metrics.regex_blocks == 1
    assert not any("домашний адрес" in text for text in tts.texts)
    assert not engine.active_queues and not engine.active_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ("", "Это выдуманная сказка. "))
@pytest.mark.parametrize("text,blocked", (
    ("Адрес: улица Примерная 12.", True),
    ("Телефон: 1234567.", True),
    ("Сегодня 18.11.2026 12:00.", False),
    ("Твой телефон бирюзовый.", False),
    ("По улице шли 12 зайчат.", False),
    ("Адрес — это место, куда отправляют письмо.", False),
))
async def test_explicit_private_labels_and_harmless_controls_through_actual_tts(prefix, text, blocked):
    from app.foundation.config import Settings
    from app.foundation.s2s import BoundedConversationEngine, ConversationContext, MockLLMProvider, MockPlaybackController, MockTTSProvider, RVCProvider

    class RecordingTTS(MockTTSProvider):
        def __init__(self):
            super().__init__()
            self.texts = []

        async def synthesize(self, text, token, cancelled):
            self.texts.append(text)
            return await super().synthesize(text, token, cancelled)

    mini, tts, playback = RecordingAllow(), RecordingTTS(), MockPlaybackController()
    engine = BoundedConversationEngine(MockLLMProvider(tokens=["<say>" + prefix + text + "</say>"]),
        tts, RVCProvider(), playback, settings=Settings(profile="mock"))
    engine.response_validator = AsyncResponseValidator(mini)
    context = ConversationContext("explicit-private-label", "canonical_product", "bear_dialogue", ("__all__",), (), 5, 120, 2)
    result = await engine.run_text_turn("Привет", context)
    assert result.return_to_scenario is blocked
    assert engine.response_validator.metrics.regex_blocks == int(blocked)
    if blocked:
        assert mini.calls == [] and tts.texts == [] and playback.played == []
    else:
        assert len(mini.calls) == 1 and tts.texts and playback.played
    assert not engine.active_queues and not engine.active_tasks


@pytest.mark.parametrize("action,allowed,confidence", (
    ("block", False, .99), ("review", False, .99), ("allow", True, .74),
))
def test_harmless_persona_is_not_a_semantic_bypass(action, allowed, confidence):
    class Uncertain:
        async def validate(self, text, context=()):
            return ValidationDecision(action, allowed, "semantic_control", confidence, "test")

    decision = asyncio.run(AsyncResponseValidator(Uncertain()).validate("Я живу в телефоне."))
    assert decision.blocked and decision.source == "test"


@pytest.mark.parametrize("failure", ("exception", "timeout"))
def test_harmless_persona_still_fails_closed_on_validator_failure(failure):
    class Failing:
        async def validate(self, text, context=()):
            if failure == "timeout":
                await asyncio.sleep(.1)
            raise RuntimeError("synthetic validator failure")

    decision = asyncio.run(AsyncResponseValidator(Failing(), timeout_ms=10).validate("Я живу в телефоне."))
    assert decision.blocked
    assert decision.source in {"validator_error", "validator_timeout"}


def test_semantic_policy_distinguishes_first_person_play_from_real_private_facts():
    mini = OllamaMiniLLMValidator()
    requests = []

    def request(payload):
        requests.append(json.loads(payload))
        return json.dumps({"message": {"content": json.dumps({"decision": "allow", "confidence": .99})}}).encode()

    mini._request = request
    assert asyncio.run(mini.validate("Я живу в волшебном лесу.")).allowed
    policy = requests[0]["messages"][0]["content"]
    assert "first-person" in policy and "Mishka" in policy
    assert "child, clinic, doctor, schedule, or current weather" in policy
    assert "not a factual authority" in policy
    assert "Use review when uncertain" in policy
