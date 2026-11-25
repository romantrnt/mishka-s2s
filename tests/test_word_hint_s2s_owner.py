"""Real bounded S2S queue with synthetic audio; app routing is checked separately."""
import asyncio
import pytest

from app.foundation.config import Settings
from app.foundation.s2s import (BoundedConversationEngine, ConversationContext, MockPlaybackController,
    MockTTSProvider, ProfileContext, RVCProvider)
from app.foundation.word_game import WordState


class NoWordLLM:
    async def stream(self, *_args, **_kwargs):
        raise AssertionError('authored word hints must not use an LLM')
        yield ''


def setup(tts=None):
    context = ConversationContext('hint-s2s', 'canonical_product', 'bear_dialogue', ('__all__',),
        ('diagnosis', 'medicine'), 8, 120, 2, profile=ProfileContext(age_band='6-7'))
    context._game_id = 'word_game'
    context._word_state = WordState()
    engine = BoundedConversationEngine(NoWordLLM(), tts or MockTTSProvider(), RVCProvider(),
        MockPlaybackController(), settings=Settings(profile='mock', voice_conversion='off'))
    return engine, context


@pytest.mark.asyncio
async def test_completed_hints_escalate_without_moves_and_valid_word_resets_hint_level():
    engine, context = setup()
    previous = context._word_state
    try:
        first = await engine.run_text_turn('Помоги', context)
        assert first.fallback_reason is None and first.word_accepted is False
        assert getattr(context, '_word_hint_level', 0) == 1
        assert context._word_state is previous and context.turns == 0
        second = await engine.run_text_turn('Помоги', context)
        assert second.fallback_reason is None and second.word_accepted is False
        assert context._word_hint_level == 2 and 'Например,' in ' '.join(second.chunks)
        assert context._word_state is previous and context.turns == 0
        accepted = await engine.run_text_turn('кот', context)
        assert accepted.word_accepted is True and accepted.fallback_reason is None
        assert context._word_hint_level == 0 and context.turns == 1
        assert context._word_state.used[0] == 'кот'
    finally:
        await engine.cancel_session(context.session_id)


@pytest.mark.asyncio
async def test_cancelled_hint_before_queue_finish_does_not_commit_hint_or_word_state():
    entered, release = asyncio.Event(), asyncio.Event()
    class HeldTTS(MockTTSProvider):
        async def synthesize(self, text, token, cancelled):
            entered.set()
            await release.wait()
            return await super().synthesize(text, token, cancelled)
    engine, context = setup(HeldTTS())
    previous = context._word_state
    task = asyncio.create_task(engine.run_text_turn('Помоги', context))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert getattr(context, '_word_hint_level', 0) == 0
        assert context._word_state is previous and context.turns == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await engine.cancel_session(context.session_id)
