"""Only observed game events reach private conversation RAM, never public history."""
import json
from copy import deepcopy

import pytest

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, CanonicalState
from app.foundation.game_engine import current_challenge
from app.foundation.system_prompt import build_system_prompt


QUESTION = 'Как тебе игра? Семь — в меню.'
NEUTRAL = 'Вернулись к разговору. Чтобы вернуться в меню, нажми семь.'


class Flow:
    def __init__(self):
        self.clock, self.wall, self.seq = 10., 100., 0
        self.runtime = CanonicalRuntime(exhibition_mode=True, now=lambda: self.wall,
            monotonic_now=lambda: self.clock)
        self.runtime.sessions['game-memory'] = CanonicalState('game-memory', mode='bear_dialogue',
            handset='up', age_band='6-7')

    @property
    def state(self): return self.runtime.sessions['game-memory']

    def send(self, kind, payload=None, **kwargs):
        self.seq += 1; self.clock += .01
        return self.runtime.handle(CanonicalEvent(kind, 'game-memory', self.seq, self.state.state_version,
            'pure', payload or {}, str(self.seq), self.wall, self.clock), **kwargs)

    def done(self):
        return self.send('AudioCompleted', {'response_id': self.state.active_audio_id,
            'playback_lease': self.state.playback_lease})

    def start(self, game='quiz'):
        self.send('DialogueFallback', {'reason': 'game_offer', 'game_id': game},
            dialogue_fallback_authorized=True)
        self.done()
        self.send('ButtonPressed', {'button_id': 1}); self.send('ButtonReleased', {'button_id': 1})
        self.done(); self.done()
        assert self.state.mode == 'game_active'
        return self

    def round(self):
        answer = current_challenge(self.state.game_engine_state).answer
        self.send('ButtonPressed', {'button_id': answer}); self.send('ButtonReleased', {'button_id': answer})

    def exit(self):
        self.send('ButtonPressed', {'button_id': 7}); self.clock += 6
        return self.send('ButtonReleased', {'button_id': 7})


def data(prompt):
    return json.loads(prompt.split('\nLAST_GAME_RESULT_DATA=', 1)[1].split('\n', 1)[0])


def private_prompt(state):
    snapshot = state.snapshot()
    snapshot['dialogue_memory']['last_game'] = state.dialogue_memory.last_game
    return build_system_prompt(profile=None, state=snapshot)


def game_voice(h):
    # The real assembly targets 24k; synthetic speech uses that same rate.
    from dataclasses import replace
    from test_stage1_nested_game_integrity import SyntheticVoice
    class Voice(SyntheticVoice):
        async def synthesize(self, text, token, cancelled):
            return replace(await super().synthesize(text, token, cancelled), sample_rate=24000)
    h.render = Voice()
    h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = h.render


def test_pure_real_correct_round_and_exit_are_private_structured_facts():
    h = Flow().start(); h.round()
    assert h.state.game_engine_state.round == 1
    run_id = h.state.game_engine_state.run_id
    result = h.exit()
    assert h.state.mode == 'bear_dialogue' and h.state.game_engine_state is None
    assert h.state.dialogue_memory.last_game == {
        'game_id': 'quiz', 'run_id': run_id, 'completed_rounds': 1, 'terminal_reason': 'game_exited_by_long_button',
        'caller': 'bear_dialogue', 'feedback_question_issued': True}
    assert [e['text'] for e in result['effects'] if e['type'] == 'dialogue_prompt'] == [QUESTION]
    assert data(private_prompt(h.state)) == h.state.dialogue_memory.last_game
    assert 'LAST_GAME_RESULT_DATA=' not in build_system_prompt(profile=None, state=h.state.snapshot())
    assert 'last_game' not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('game', ['quiz', 'free_lights', 'word_game'])
def test_pure_zero_progress_never_claims_success_or_asks_feedback(game):
    h = Flow().start(game)
    result = h.exit()
    memory = h.state.dialogue_memory.last_game
    assert memory['game_id'] == game and not memory['feedback_question_issued']
    assert 'score' not in memory and 'wrong_attempts' not in memory
    assert ('completed_rounds' not in memory) if game == 'free_lights' else memory['completed_rounds'] == 0
    assert [e['text'] for e in result['effects'] if e['type'] == 'dialogue_prompt'] == [NEUTRAL]


def test_pure_wrong_answer_and_stale_callbacks_do_not_count_or_reissue_question():
    h = Flow().start()
    question = current_challenge(h.state.game_engine_state)
    wrong = next(button for button, _ in question.options if button != question.answer)
    h.send('ButtonPressed', {'button_id': wrong}); h.send('ButtonReleased', {'button_id': wrong})
    assert h.state.game_engine_state.round == 0
    old = {'response_id': h.state.active_audio_id, 'playback_lease': h.state.playback_lease}
    h.exit()
    before = deepcopy(h.state.dialogue_memory.last_game)
    assert before['completed_rounds'] == 0 and not before['feedback_question_issued']
    assert not h.send('AudioCompleted', old)['effects']
    assert h.state.dialogue_memory.last_game == before


def test_pure_next_game_replaces_memory_only_at_its_own_exit():
    h = Flow().start(); h.round(); h.exit()
    first = deepcopy(h.state.dialogue_memory.last_game)
    h.done(); h.start('free_lights')
    assert h.state.dialogue_memory.last_game == first
    h.exit()
    assert h.state.dialogue_memory.last_game['game_id'] == 'free_lights'
    assert 'completed_rounds' not in h.state.dialogue_memory.last_game


@pytest.mark.parametrize('boundary', ['SessionCancel', 'SessionReset', 'AnonymousStart', 'HandsetPlaced', 'HardwareDisconnected'])
def test_pure_visit_and_safety_boundaries_clear_last_game(boundary):
    h = Flow().start(); h.round(); h.exit()
    h.send(boundary)
    assert h.state.dialogue_memory.last_game is None


def test_pure_active_game_speech_window_does_not_expire_visit_result():
    h = Flow().start(); h.round(); h.exit()
    before = deepcopy(h.state.dialogue_memory.last_game)
    h.wall += 150; h.clock += 150
    h.done()
    assert h.state.dialogue_memory.last_game == before
    assert data(private_prompt(h.state)) == before


def test_pure_concern_clears_previous_result_without_asking_about_play():
    h = Flow().start(); h.round(); h.exit()
    result = h.send('VoiceIntentReady', {'intent': 'free_speech', 'safety_severity': 'concern'},
        transient_text='Мне больно')
    assert h.state.mode == 'support_intake' and h.state.safety_severity == 'concern'
    assert h.state.dialogue_memory.last_game is None
    assert not any(e.get('type') == 'dialogue_prompt' for e in result['effects'])


@pytest.mark.parametrize('change', [
    {'score': 10}, {'game_id': []}, {'game_id': 'invented'}, {'completed_rounds': True},
    {'completed_rounds': -1}, {'completed_rounds': 1_000_001}, {'caller': 'foreign'},
    {'terminal_reason': 'won'}, {'feedback_question_issued': 'yes'}, {'completed_rounds': 0},
    {'game_id': 'free_lights'}, {'child_answer': 'SYNTHETIC_NOT_A_GAME_FACT'},
])
def test_pure_prompt_projection_rejects_unknown_or_inconsistent_fields(change):
    h = Flow().start(); h.round(); h.exit()
    h.state.dialogue_memory.last_game.update(change)
    assert 'LAST_GAME_RESULT_DATA=' not in private_prompt(h.state)


@pytest.mark.asyncio
async def test_actual_return_preserves_caller_and_next_outgoing_model_request_has_only_result():
    from test_stage1_nested_game_integrity import NestedGame, CALLER_TEXT
    h = NestedGame()
    game_voice(h)
    try:
        caller, history, turns, _ = await h.launch('quiz')
        await h.terminal()
        answer = current_challenge(h.state.game_engine_state).answer
        await h.key(answer)
        assert h.state.game_engine_state.round == 1
        pressed, result = await h.key(7, duration=6.)
        assert result['reason'] == 'game_exited_by_long_button'
        assert h.app.conversation_contexts[h.sid] is caller
        assert caller.history == history and caller.turns == turns
        memory = deepcopy(h.state.dialogue_memory.last_game)
        assert memory['completed_rounds'] == 1 and memory['feedback_question_issued']
        assert h.render.texts.count(QUESTION) == 1
        public = json.dumps([pressed, result, h.state.snapshot(), h.messages], ensure_ascii=False, default=str)
        assert 'feedback_question_issued' not in public and 'LAST_GAME_RESULT_DATA=' not in public
        await h.terminal()
        h.model.answers.append('<say>Мы можем нарисовать коту усы.</say>')
        await h.send('SpeechReceived', text='Что мы делали перед этим?')
        outgoing = h.model.calls[-1]
        assert data(outgoing[0]['content']) == memory
        assert CALLER_TEXT in outgoing[0]['content']
        assert outgoing[1] == {'role': 'user', 'content': 'Что мы делали перед этим?'}
        assert h.render.texts.count(QUESTION) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_menu_game_result_waits_for_real_conversation_without_redirect():
    from test_contextual_game_flow import Harness
    h = Harness('<say>Мы можем поговорить об игре.</say>', mode='root_menu')
    game_voice(h)
    try:
        await h.key(1); await h.key(2); await h.done(); await h.done()
        assert h.state.active_game_id == 'quiz'
        await h.key(current_challenge(h.state.game_engine_state).answer)
        await h.key(7, duration=6.)
        assert h.state.mode == 'game_menu'
        memory = deepcopy(h.state.dialogue_memory.last_game)
        assert memory['caller'] == 'game_menu' and not memory['feedback_question_issued']
        await h.key(7); await h.key(6); await h.done()
        await h.send('SpeechReceived', text='Что мы делали перед этим?')
        assert data(h.model.calls[-1][0]['content']) == memory
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_natural_word_completion_keeps_final_pcm_and_exact_accepted_count():
    from test_stage1_nested_game_integrity import NestedGame
    from test_contextual_game_flow import WORD_GAME_WORDS
    h = NestedGame()
    game_voice(h)
    try:
        caller, history, turns, _ = await h.launch('word_game')
        await h.terminal()
        limit = h.app._canonical_audio_context(h.sid).max_turns
        band = {'3-5': '4-5', '5-7': '6-7'}.get(h.state.age_band, h.state.age_band)
        for word in WORD_GAME_WORDS[band][:limit]:
            h.messages.clear()
            await h.send('SpeechReceived', text=word)
        assert h.state.mode == 'bear_dialogue' and h.state.game_engine_state is None
        assert h.app.conversation_contexts[h.sid] is caller and caller.history == history and caller.turns == turns
        memory = h.state.dialogue_memory.last_game
        assert memory['completed_rounds'] == limit and memory['terminal_reason'] == 'word_game_finished'
        assert not memory['feedback_question_issued'] and QUESTION not in h.render.texts
        assert any(value['type'] == 'audio_pcm_output' for value in h.messages)
        assert not any(value['type'] == 'audio_stop' for value in h.messages)
        assert h.sid not in h.app._active_audio
    finally:
        await h.close()
