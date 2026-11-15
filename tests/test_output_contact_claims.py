"""Narrow output external contact rules remain effective if a semantic gate allows."""
import pytest

from app.foundation.config import Settings
from app.foundation.s2s import BoundedConversationEngine, ConversationContext, MockLLMProvider, MockPlaybackController, MockTTSProvider, RVCProvider
from app.foundation.validation import AsyncResponseValidator, RegexSafetyFilter, ValidationDecision


UNSAFE = (
    "Я уже позвал взрослого. Он сейчас придёт.",
    "Хорошо, сейчас позову маму.",
    "Я попрошу папу подойти.",
    "Твоя мама получила моё сообщение.",
    "Взрослый получил просьбу.",
    "Мама сейчас придёт.",
    "Я отправлю сообщение маме.",
)
SAFE = (
    "Я напишу сказку о динозаврах.",
    "Можно попросить папу подойти?",
    "Позови маму, если тебе нужна помощь.",
    "Ты можешь обратиться к взрослому рядом.",
    "Я не могу отправить сообщение маме.",
    "В сказке медведь позвал маму зайчонка.",
    "Сказочный почтальон доставил письмо королю.",
)



@pytest.mark.parametrize("text", UNSAFE)
def test_output_contact_instructions_are_blocked(text):
    assert RegexSafetyFilter().check(text) == (False, "unverified_external_contact")


@pytest.mark.parametrize("text", SAFE)
def test_output_contact_words_alone_do_not_block_harmless_text(text):
    assert RegexSafetyFilter().check(text) == (True, "regex_allow")


class WrongAllow:
    async def validate(self, text, context=()):
        return ValidationDecision("allow", True, "synthetic_wrong_allow", .9999, "test")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", UNSAFE)
async def test_streamed_output_is_silent_despite_wrong_semantic_allow(text):
    # Provider token boundaries split the unsafe instruction. Only a completed
    # say block is considered, so raw streaming cannot bypass the regex gate.
    tokens = ["<say>"] + [text[i:i+4] for i in range(0, len(text), 4)] + ["</say>"]
    playback = MockPlaybackController()
    engine = BoundedConversationEngine(MockLLMProvider(tokens=tokens), MockTTSProvider(),
        RVCProvider(), playback, settings=Settings(profile="mock"))
    engine.response_validator = AsyncResponseValidator(WrongAllow())
    context = ConversationContext("secrecy", "canonical_product", "bear_dialogue", ("__all__",), (), 5, 120, 2)
    result = await engine.run_text_turn("Привет", context)
    assert result.return_to_scenario
    assert not playback.played
    assert engine.response_validator.metrics.regex_blocks == 1
    assert not engine.active_queues and not engine.active_tasks


@pytest.mark.asyncio
async def test_harmless_secret_title_still_reaches_audio():
    playback = MockPlaybackController()
    engine = BoundedConversationEngine(MockLLMProvider(tokens=["<say>Секреты математики интересные.</say>"]),
        MockTTSProvider(), RVCProvider(), playback, settings=Settings(profile="mock"))
    engine.response_validator = AsyncResponseValidator(WrongAllow())
    context = ConversationContext("secrecy", "canonical_product", "bear_dialogue", ("__all__",), (), 5, 120, 2)
    result = await engine.run_text_turn("Привет", context)
    assert not result.return_to_scenario
    assert playback.played
