"""Real app input/timers with synthetic24k audio; helper/S2S ownership remains real."""
from copy import deepcopy
import pytest
from test_contextual_game_flow import Harness
from test_game_conversation_memory import game_voice


@pytest.mark.asyncio
@pytest.mark.parametrize('queued', [False, True])
async def test_current_word_help_and_idle_hint_do_not_consume_a_move(queued):
    h = Harness()
    game_voice(h)
    try:
        await h.launch('word_game')
        await h.send('SpeechReceived', text='Кот')
        sid = h.state.session_id
        context = h.app.conversation_contexts[sid]
        words = deepcopy(context._word_state)
        turns, rounds = context.turns, h.state.game_engine_state.round
        assert turns == rounds == 1
        await h.send('SpeechReceived', text='Помоги')
        context = h.app.conversation_contexts[sid]
        assert h.state.mode == 'game_active' and context._word_hint_level == 1
        assert context._word_state == words and context.turns == turns
        assert h.state.game_engine_state.round == rounds
        if not queued:
            async def unavailable(*args): return None
            h.app._render_game_prompt = unavailable
        h.clock = h.app._game_timer_scopes[sid][-1]
        await h.app.flush_game_timers()
        context = h.app.conversation_contexts[sid]
        assert context._word_hint_level == (2 if queued else 1)
        assert context._word_state == words and context.turns == turns
        assert h.state.game_engine_state.round == rounds
        if queued:
            assert any('Например,' in value for value in h.render.texts)
    finally:
        await h.close()
