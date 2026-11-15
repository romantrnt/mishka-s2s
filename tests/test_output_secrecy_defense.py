"""Narrow output secrecy rules remain effective if a semantic gate allows."""
import pytest

from app.foundation.config import Settings
from app.foundation.s2s import BoundedConversationEngine, ConversationContext, MockLLMProvider, MockPlaybackController, MockTTSProvider, RVCProvider
from app.foundation.validation import AsyncResponseValidator, RegexSafetyFilter, ValidationDecision


UNSAFE = (
    "Не рассказывай взрослым о нашем секрете.",
    "Не говори маме о нашем разговоре.",
    "Никому не рассказывай об этом.",
    "Никому больше не говори про нас.",
    "Это останется между нами.",
    "Пусть это будет только между нами.",
    "Держи наш разговор в секрете от родителей.",
    "Не зови родителей.",
    "Не обращайся к взрослым за помощью.",
    "Разговаривай только со мной.",
    "Доверяй только мне.",
    "Don't tell your parents about this.",
    "Keep this between us.",
)
SAFE = (
    "Секреты математики очень интересные.",
    "У фокуса есть забавный секрет.",
    "Расскажи взрослым, если тебе нужна помощь.",
    "Не бойся рассказать маме о своих чувствах.",
    "Давай поговорим о том, что тебе нравится.",
    "Между нами стоит стол.",
    "В нашей истории секрет раскрылся.",
    "Не говори взрослым неправду.",
    "You can tell your parents about this.",
)


@pytest.mark.parametrize("text", UNSAFE)
def test_output_secrecy_instructions_are_blocked(text):
    assert RegexSafetyFilter().check(text) == (False, "secrecy_or_isolation")


@pytest.mark.parametrize("text", SAFE)
def test_output_secrecy_words_alone_do_not_block_harmless_text(text):
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
