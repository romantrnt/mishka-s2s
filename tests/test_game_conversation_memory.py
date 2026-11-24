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


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['dialogue', 'menu', 'pending', 'menu_back'])
async def test_actual_success_wave_settles_on_caller_without_replacing_finish_audio(origin):
    from app.foundation.canonical_services import LED_PRESETS, DIALOGUE_ANCHOR_ELEMENTS
    from test_stage1_nested_game_integrity import NestedGame
    h = NestedGame()
    game_voice(h)
    try:
        if origin == 'dialogue':
            caller, history, turns, _ = await h.launch('quiz')
        else:
            if origin in {'menu', 'menu_back'}:
                await h.send('HandsetLifted')
            await h.key(1); await h.key(2)
            if origin == 'pending':
                await h.send('HandsetLifted')
            started = await h.terminal()
            assert h.sid in h.app._active_audio, started
        await h.terminal()
        for index in range(8):
            pressed, released = await h.key(current_challenge(h.state.game_engine_state).answer)
            result = released if released['reason'] == 'game_completed' else pressed
            if index < 7:
                assert h.sid in h.app._active_audio, (index, h.app._audio_completion_diagnostics)
                await h.terminal()
        destination = {'dialogue': 'bear_dialogue', 'menu': 'game_menu', 'pending': 'root_menu', 'menu_back': 'game_menu'}[origin]
        preset = {'dialogue': 'BEAR_DIALOGUE', 'menu': 'GAME_MENU', 'pending': 'ROOT_MENU', 'menu_back': 'GAME_MENU'}[origin]
        assert h.state.mode == destination and h.state.dialogue_memory.last_game['completed_rounds'] == 8
        wave = next(effect for effect in result['outputs'] if effect['type'] == 'game_lighting')
        assert wave['next_preset'] == preset
        if origin != 'dialogue':
            assert h.app.led.effect_snapshot()['wave_phase'] == 'game_success'
        owner = dict(h.app._active_audio[h.sid])
        if origin == 'menu_back':
            await h.key(7)
            assert h.state.mode == 'root_menu' and h.sid not in h.app._active_audio
            h.clock += 1.
            h.app.led.exhibition_effects.advance(h.clock)
            assert h.output.snapshot() == {str(i): dict(LED_PRESETS['ROOT_MENU']).get(i, 0) for i in range(18)}
            return
        h.clock += 1.
        h.app.led.exhibition_effects.advance(h.clock)
        assert h.app._active_audio[h.sid] == owner and h.state.mode == destination
        if origin == 'dialogue':
            assert h.app.conversation_contexts[h.sid] is caller
            assert caller.history == history and caller.turns == turns
            assert h.app.runtime_status['dialogue_lighting_phase'] == 'output'
            assert all(h.output.snapshot()[str(element)] == 1 for element in DIALOGUE_ANCHOR_ELEMENTS)
            assert h.render.texts.count(QUESTION) == 1
        else:
            assert h.output.snapshot() == {str(i): dict(LED_PRESETS[preset]).get(i, 0) for i in range(18)}
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['typed', 'recognized_pcm'])
async def test_actual_stop_inside_word_utterance_does_not_exit_but_explicit_exit_restores_topic(route):
    from test_stage1_nested_game_integrity import NestedGame, CALLER_TEXT
    from test_transition_priority_acceptance import user_input
    h = NestedGame()
    game_voice(h)
    async def utterance(text):
        h.seq = max(h.app.sessions.get(h.sid).last_sequence,
                    h.state.last_sequence_by_source.get('uart_audio', 0), h.sequence)
        return await user_input(h, route, text)
    try:
        caller, history, turns, _ = await h.launch('word_game')
        await h.terminal()
        run_id = h.state.game_engine_state.run_id
        result = await utterance('Стоп, я вспомнил слово: яблоко')
        assert h.state.mode == 'game_active' and h.state.game_engine_state.run_id == run_id, result
        assert h.state.game_engine_state.round == 0  # Authored rules ask to clarify this multiword input.
        assert h.state.dialogue_memory.last_game is None
        assert 'Не уверен' in h.render.texts[-1]
        result = await utterance('Выйди из игры')
        assert result['reason'] == 'game_exited_by_voice' and h.state.mode == 'bear_dialogue'
        assert h.app.conversation_contexts[h.sid] is caller
        assert caller.history == history and caller.turns == turns
        await h.terminal()
        h.model.answers.append('<say>Продолжим рисовать кота.</say>')
        await utterance('Продолжим прежний разговор')
        assert CALLER_TEXT in h.model.calls[-1][0]['content']
        assert h.app.conversation_contexts[h.sid].turns == turns + 1
    finally:
        await h.close()


def test_pure_help_revokes_game_feedback_and_private_result():
    h = Flow().start(); h.round(); h.exit()
    result = h.send('ButtonPressed', {'button_id': 5})
    assert result['reason'] == 'help_priority' and h.state.mode == 'wellbeing_check'
    assert h.state.dialogue_memory.last_game is None


@pytest.mark.parametrize('boundary', ['ClientProfileCleared', 'IdentityAuthorizationExpired'])
def test_pure_authorization_boundary_clears_result_even_in_public_menu(boundary):
    h = Flow().start(); h.round(); h.exit()
    h.runtime.optional_identity = True
    h.state.mode = 'game_menu'
    h.state.profile_ref = 'synthetic-profile'
    h.state.identity_request_id = 'synthetic-request'
    h.state.identity_expires_monotonic = h.clock
    payload = ({'reason_code': 'session_expired'} if boundary == 'ClientProfileCleared'
               else {'request_id': 'synthetic-request'})
    h.send(boundary, payload, identity_event_authorized=True)
    assert h.state.dialogue_memory.last_game is None


def test_pure_private_result_alone_is_scrubbed_on_profile_clear():
    h = Flow().start(); h.round(); h.exit()
    h.runtime.optional_identity = True
    h.state.game_progress.clear()
    h.state.dialogue_memory.bounded_turns.clear()
    h.send('ClientProfileCleared', identity_event_authorized=True)
    assert h.state.dialogue_memory.last_game is None


@pytest.mark.parametrize('reason', ['word_retry_complete', 'word_turn_complete'])
def test_pure_late_old_run_result_cannot_advance_reopened_word_game(reason):
    h = Flow().start('word_game')
    old_run = h.state.game_engine_state.run_id
    h.exit(); h.done(); h.start('word_game')
    engine = h.state.game_engine_state
    assert engine.run_id != old_run
    memory = deepcopy(h.state.dialogue_memory.last_game)
    result = h.send('DialogueFallback', {'reason': reason, 'game_run_id': old_run},
        dialogue_fallback_authorized=True)
    assert result['reason'] == 'word_game_result_stale' and not result['effects']
    assert h.state.game_engine_state is engine and h.state.dialogue_memory.last_game == memory


def test_pure_expired_result_is_cleared_before_next_input():
    h = Flow().start(); h.round(); h.exit()
    h.state.dialogue_memory.suspend(h.wall)
    h.wall = h.state.dialogue_memory.expires_at + .1
    h.send('ButtonReleased', {'button_id': 1})
    assert h.state.dialogue_memory.last_game is None


def test_pure_saved_level_seeds_next_launch_without_becoming_score():
    h = Flow()
    h.state.game_progress['quiz'] = 5
    h.start(); h.round(); h.exit()
    memory = h.state.dialogue_memory.last_game
    assert memory['completed_rounds'] == 1
    assert not {'level', 'score', 'answers', 'words'} & memory.keys()


@pytest.mark.asyncio
async def test_actual_unconfirmed_word_result_cannot_fabricate_an_accepted_round():
    from test_stage1_nested_game_integrity import NestedGame
    from app.foundation.s2s import TurnResult
    h = NestedGame()
    game_voice(h)
    try:
        await h.launch('word_game'); await h.terminal()
        async def unconfirmed(text, context):
            return TurnResult((), (), False)
        h.app.bounded_s2s.run_text_turn = unconfirmed
        result = await h.send('SpeechReceived', text='Кот')
        assert result['reason'] == 'word_game_retry_finished'
        assert h.state.game_engine_state.round == 0
        await h.key(7, duration=6.)
        assert h.state.dialogue_memory.last_game['completed_rounds'] == 0
        assert QUESTION not in h.render.texts
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_old_word_tts_after_exit_and_reopen_cannot_publish_or_advance_new_run():
    import asyncio
    from test_stage1_nested_game_integrity import NestedGame
    h = NestedGame()
    game_voice(h)
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    class LateTTS:
        delayed = False
        async def synthesize(self, text, token, cancelled):
            audio = await h.render.synthesize(text, token, cancelled)
            if not self.delayed:
                self.delayed = True
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            return audio
    try:
        await h.launch('word_game'); await h.terminal()
        old_run = h.state.game_engine_state.run_id
        h.app.bounded_s2s.tts = LateTTS()
        pending = asyncio.create_task(h.send('SpeechReceived', text='Кот'))
        await asyncio.wait_for(entered.wait(), 2)
        await h.key(7, duration=6.); await h.terminal()
        h.app.bounded_s2s.tts = h.render
        h.model.answers.append('<action type="offer_game" game_id="word_game"/>')
        await h.send('SpeechReceived', text='Предложи игру')
        await h.terminal(); await h.key(1); await h.terminal(); await h.terminal()
        engine = h.state.game_engine_state
        assert engine.run_id != old_run and engine.round == 0
        memory = deepcopy(h.state.dialogue_memory.last_game)
        context = h.app.conversation_contexts.get(h.sid)
        caller = h.app._game_callers[h.sid]
        h.messages.clear()
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
        assert h.state.game_engine_state is engine and engine.round == 0
        assert h.app.conversation_contexts.get(h.sid) is context
        assert h.app._game_callers[h.sid] is caller
        assert h.state.dialogue_memory.last_game == memory
        assert not h.messages and QUESTION not in h.render.texts
    finally:
        release.set()
        if pending:
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['typed', 'recognized_pcm', 'native_tts'])
async def test_repeated_natural_word_games_keep_caller_and_private_journal_cache(tmp_path, monkeypatch, route):
    """Actual runtime/PCM queues; classifier labels and native I/O are fixtures."""
    from test_stage1_nested_game_integrity import NestedGame
    from test_contextual_game_flow import WORD_GAME_WORDS
    from test_transition_priority_acceptance import user_input
    from test_activity_request_gate import classified
    from app.foundation.session_journal import SessionJournal
    from app.foundation.s2s import CachedTTSProvider, AudioChunk, SoundDevicePlaybackController
    h = NestedGame()
    game_voice(h)
    writes = []
    if route == 'native_tts':
        import sys
        from types import SimpleNamespace
        from importlib.machinery import ModuleSpec
        class Stream:
            def __init__(self, **kwargs): pass
            def start(self): pass
            def write(self, values): writes.append(len(values))
            def stop(self): pass
            def abort(self): pass
            def close(self): pass
        monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(
            __spec__=ModuleSpec('sounddevice', loader=None), OutputStream=Stream,
            query_devices=lambda *_: {'max_output_channels': 1}))
        async def native(text, token, cancelled, *, first_chunk):
            h.render.texts.append(text)
            for _ in range(2):
                assert not cancelled()
                yield AudioChunk((.1,) * 240, 24000, token)
        h.render.synthesize_audio_stream = native
        h.app.bounded_s2s.playback = SoundDevicePlaybackController()
    try:
        caller, history, turns, _ = await h.launch('word_game')
        h.app._dialogue_journal = SessionJournal(root=tmp_path / 'synthetic-journal', enabled=True)
        cache = CachedTTSProvider(h.render)
        h.app.bounded_s2s.tts = cache
        h.app.bounded_s2s.activity_request_gate, requests = classified('allow')
        for cycle in range(2):
            if cycle:
                offered = await h.send('SpeechReceived', text='Поиграем в слова')
                assert offered['reason'] == 'game_offered', offered
                history.append(('Поиграем в слова', ''))
                await h.terminal(); await h.key(1); await h.terminal()
                caller = h.app._game_callers[h.sid]['context']
                assert caller.history == history and caller.turns == turns
            await h.terminal()
            assert h.state.mode == 'game_active'
            journal_before = deepcopy(h.app._dialogue_journal._sessions)
            cache_before = deepcopy(cache.cache)
            limit = h.app._canonical_audio_context(h.sid).max_turns
            band = {'3-5': '4-5', '5-7': '6-7'}.get(h.state.age_band, h.state.age_band)
            for word in WORD_GAME_WORDS[band][:limit]:
                before_writes = len(writes)
                h.seq = max(h.sequence, h.app.sessions.get(h.sid).last_sequence,
                            h.state.last_sequence_by_source.get('uart_audio', 0))
                h.messages.clear()
                result = await user_input(h, 'recognized_pcm' if route == 'recognized_pcm' else 'typed', word)
                assert 'game_run_id' not in json.dumps(result)
                if route == 'native_tts':
                    assert len(writes) > before_writes
            assert h.state.mode == 'bear_dialogue' and h.state.dialogue_memory.last_game['completed_rounds'] == limit
            assert h.app.conversation_contexts[h.sid] is caller
            assert caller.history == history and caller.turns == turns
            assert h.app._dialogue_journal._sessions == journal_before and cache.cache == cache_before
            assert QUESTION not in h.render.texts and h.sid not in h.app._active_audio
            assert not any(value['type'] == 'audio_stop' for value in h.messages)
            if route == 'native_tts':
                assert writes and h.app.bounded_s2s.playback.last_playback_completed
        assert requests  # Actual classifier protocol, with explicit fixture labels.
    finally:
        await h.close()
        await h.app.shutdown_identity()
