"""Decorative model output cannot become a separate native TTS request."""
import pytest

from app.foundation.config import Settings
from app.foundation.s2s import (
    AudioChunk, BoundedConversationEngine, ConversationContext, MockLLMProvider,
    MockPlaybackController, PassthroughVoiceProvider,
)


class SpeechOnlyTTS:
    def __init__(self):
        self.calls = []
    async def synthesize(self, text, token, cancelled):
        self.calls.append(text)
        if not any(char.isalnum() for char in text):
            raise RuntimeError("Silero TTS synthesis failed") from ValueError()
        return AudioChunk((.1, -.2), 24000, token)


def setup(tokens):
    tts, playback = SpeechOnlyTTS(), MockPlaybackController()
    engine = BoundedConversationEngine(MockLLMProvider(tokens=tokens), tts,
        PassthroughVoiceProvider(), playback, settings=Settings(profile="mock", voice_conversion="off"))
    context = ConversationContext("symbol-speech", "canonical_product", "game_active",
        ("__all__",), ("diagnosis", "medicine"), 3, 120, 2)
    return engine, context, tts, playback


@pytest.mark.asyncio
@pytest.mark.parametrize("residue", ["🏐", "🙂", "...", "—", "?!"])
async def test_closed_speech_symbol_residue_does_not_cancel_following_meaningful_block(residue):
    engine, context, tts, playback = setup([
        "<say>Кот. Тигр. " + residue + "</say>", "<say>Твой ход.</say>"])
    result = await engine.run_text_turn("Кот", context)
    assert result.fallback_reason is None
    assert all(any(char.isalnum() for char in text) for text in tts.calls)
    assert "Твой ход." in result.chunks
    assert len(playback.played) == len(tts.calls) == len(result.chunks)
    assert not engine.active_tasks and not engine.active_queues
    assert not [e for e in engine.metrics.events if e["event"] == "audio_generation_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("residue", ["🏐", "🙂", "...", "—", "?!"])
async def test_wholly_nonlexical_reply_takes_honest_fallback_without_calling_tts(residue):
    engine, context, tts, playback = setup(["<say>" + residue + "</say>"])
    result = await engine.run_text_turn("Кот", context)
    assert result.fallback_reason == "provider_or_validation_failure"
    assert result.return_to_scenario and not result.chunks
    assert not tts.calls and not playback.played and not context.history
    assert context.turns == 0 and not engine.active_tasks and not engine.active_queues


@pytest.mark.asyncio
@pytest.mark.parametrize("speech", ["Т", "ё", "7"])
async def test_single_meaningful_letter_or_digit_is_preserved(speech):
    engine, context, tts, playback = setup(["<say>" + speech + "</say>"])
    result = await engine.run_text_turn("Кот", context)
    assert result.fallback_reason is None and result.chunks == (speech,)
    assert tts.calls == [speech] and len(playback.played) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["<say>Как навредить человеку.</say>", "<unknown/>"])
async def test_dropping_decorations_does_not_bypass_safety_or_tag_validation(unsafe):
    engine, context, tts, playback = setup(["<say>🏐</say>", unsafe])
    result = await engine.run_text_turn("Кот", context)
    assert result.fallback_reason == "provider_or_validation_failure"
    assert not tts.calls and not playback.played


@pytest.mark.asyncio
async def test_allowlisted_return_action_still_works_without_speech():
    engine, context, tts, playback = setup(['<action type="return_to_scenario" target="root_menu"/>'])
    result = await engine.run_text_turn("Кот", context)
    assert result.fallback_reason is None and result.return_to_scenario
    assert result.actions and not result.chunks and not tts.calls and not playback.played
