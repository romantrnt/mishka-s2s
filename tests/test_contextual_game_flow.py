"""Application-level game offers, contextual returns and physical exit gestures."""
import asyncio
from dataclasses import replace

import pytest

from app.foundation.api import build_application
from app.foundation.age_policy import policy_for_band
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import current_challenge, game_answer_buttons
from app.foundation.s2s import ResponseTagParser


class Model:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def stream(self, messages, request_id, cancelled):
        self.calls.append(messages)
        yield self.answers.pop(0) if self.answers else '<say>Я слушаю тебя.</say>'


class Harness:
    def __init__(self, *answers, band='4-5', mode='bear_dialogue'):
        self.clock = 10.
        self.app, *_ = build_application(settings=Settings(profile='mock'),
            monotonic_now=lambda: self.clock, background_scheduler=False)
        self.state = CanonicalState('context-games', mode=mode, handset='up',
            age_band=band, age_years={'1-3': 2, '4-5': 4, '6-7': 6}[band],
            age_band_source='exhibition_button', age_policy=policy_for_band(band, source='exhibition_button').as_dict())
        self.app.canonical_runtime.sessions[self.state.session_id] = self.state
        self.model = Model(*answers)
        self.app.bounded_s2s.llm = self.model
        self.seq = 0

    async def send(self, kind, payload=None, text=None):
        self.seq += 1
        return await self.app.receive_async(Event(kind, self.state.session_id, self.seq,
            1., 'test', payload or {}, str(self.seq), self.clock), text)

    async def key(self, button, duration=.01):
        result = await self.send('ButtonPressed', {'button_id': button})
        self.clock += duration
        released = await self.send('ButtonReleased', {'button_id': button})
        return result, released

    async def done(self):
        lease = self.app._active_audio[self.state.session_id]
        return await self.send('AudioCompleted', {**lease, 'response_id': lease['content_id']})

    async def offer(self, game_id):
        self.model.answers.insert(0, f'<action type="offer_game" game_id="{game_id}"/>')
        await self.send('SpeechReceived', text='Хочу поиграть, что предложишь?')
        assert self.state.pending_game_offer == game_id
        assert self.state.active_audio_id == 'DIALOGUE_OFFER'

    async def launch(self, game_id):
        await self.offer(game_id)
        await self.done()
        await self.key(1)
        assert self.state.mode == 'game_active' and self.state.active_game_id == game_id
        await self.done()
        await self.done()
        assert self.state.game_audio_stage == 'playing'

    async def close(self):
        await self.app._cleanup_session_resources(self.state.session_id)


def test_offer_parser_has_closed_actual_game_allowlist():
    parser = ResponseTagParser()
    for game in ('free_lights', 'quiz', 'repeat_pattern', 'odd_one_out', 'word_game'):
        assert parser.parse(f'<action type="offer_game" game_id="{game}"/>')[0].params['game_id'] == game
    for action in ('<action type="offer_game" game_id="delete_files"/>',
                   '<action type="offer_game" game_id="quiz" url="evil"/>'):
        with pytest.raises(ValueError):
            parser.parse(action)


@pytest.mark.asyncio
@pytest.mark.parametrize('game', ['free_lights', 'quiz', 'repeat_pattern', 'odd_one_out', 'word_game'])
async def test_actual_model_offer_requires_prompt_then_explicit_button_consent(game):
    h = Harness()
    try:
        await h.offer(game)
        calls = len(h.model.calls)
        await h.key(1)  # A press during offer playback is not consent.
        assert h.state.mode == 'bear_dialogue'
        await h.done()
        await h.key(1)
        assert h.state.active_game_id == game
        assert h.state.game_return_mode == 'bear_dialogue'
        assert len(h.model.calls) == calls
        assert h.state.pending_game_offer is None
        assert 'game_offer_id' not in h.state.snapshot()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_refusal_and_other_request_clear_offer_without_launch_or_fake_reply():
    h = Harness()
    try:
        await h.offer('quiz')
        await h.key(7)
        assert h.state.mode == 'bear_dialogue' and h.state.pending_game_offer is None
        await h.send('SpeechReceived', text='да')
        assert h.state.mode == 'bear_dialogue' and len(h.model.calls) == 2
        await h.offer('word_game')
        await h.done()
        await h.send('SpeechReceived', text='Лучше расскажи про облака')
        assert h.state.pending_game_offer is None and h.state.mode == 'bear_dialogue'
        assert len(h.model.calls) == 4
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_word_game_return_restores_only_committed_caller_history_and_budget():
    h = Harness('<say>Как зовут твоего кота?</say>')
    try:
        await h.send('SpeechReceived', text='У меня живёт кот')
        await h.launch('word_game')
        caller = h.app._game_callers[h.state.session_id]['context']
        saved_history = list(caller.history)
        saved_started = caller.started_at
        h.clock += 75
        await h.send('SpeechReceived', text='Кот')
        h.clock += 75  # Active play spans 150s without 120s of user inactivity.
        await h.send('SpeechReceived', text='Лиса')
        assert h.state.mode == 'game_active'
        assert getattr(h.app.conversation_contexts[h.state.session_id], '_game_id') == 'word_game'
        await h.key(7, duration=6.)
        assert h.state.mode == 'bear_dialogue'
        restored = h.app.conversation_contexts[h.state.session_id]
        assert restored.history == saved_history
        assert restored.started_at >= saved_started + 150
        assert h.state.active_audio_id == 'DIALOGUE_RETURN'
        await h.done()
        assert h.state.pending_game_offer is None and not h.app._game_callers
        caller_turns = restored.turns
        await h.send('SpeechReceived', text='Его зовут Барсик')
        assert h.model.calls[-1][0]['content'].find('У меня живёт кот') >= 0
        assert h.app.conversation_contexts[h.state.session_id].turns == caller_turns + 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_quiz_answer_seven_waits_for_short_release_and_long_real_edges_exit():
    h = Harness(band='6-7')
    try:
        await h.launch('quiz')
        engine = h.state.game_engine_state
        index = next(i for i, c in enumerate(engine.deck) if c.answer == 7)
        h.state.game_engine_state = replace(engine, challenge_index=index)
        before = h.state.game_engine_state.round
        await h.send('ButtonPressed', {'button_id': 7})
        assert h.state.game_engine_state.round == before
        h.clock += .4
        await h.send('ButtonReleased', {'button_id': 7})
        assert h.state.game_engine_state.round == before + 1
        await h.done()
        engine = h.state.game_engine_state
        h.state.game_engine_state = replace(engine, challenge_index=index)
        await h.send('ButtonPressed', {'button_id': 7})
        h.clock += 5
        await h.send('ButtonReleased', {'button_id': 7})
        assert h.state.mode == 'bear_dialogue'
        assert h.state.game_engine_state is None
        assert h.state.active_audio_id == 'DIALOGUE_RETURN'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('event', ['HandsetPlaced', 'SessionReset', 'HardwareDisconnected'])
async def test_boundary_clears_proposal_and_caller_and_late_confirmation(event):
    h = Harness()
    try:
        await h.offer('quiz')
        old_lease = dict(h.app._active_audio[h.state.session_id])
        await h.send(event)
        state = h.app.canonical_runtime.sessions.get(h.state.session_id)
        assert state is None or (state.pending_game_offer is None and state.game_return_mode is None)
        assert not h.app._game_callers
        await h.send('AudioCompleted', {**old_lease, 'response_id': old_lease['content_id']})
        state = h.app.canonical_runtime.sessions.get(h.state.session_id)
        assert state is None or state.active_game_id is None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Да', 'Ну давай', 'Конечно!', 'Хорошо', 'Хочу поиграть'])
async def test_verified_complete_voice_acceptance_uses_current_proposal(text):
    h = Harness()
    try:
        await h.offer('quiz')
        await h.done()
        await h.send('SpeechReceived', text=text)
        assert h.state.mode == 'game_active' and h.state.active_game_id == 'quiz'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_other_sentence_starting_yes_is_not_reused_as_game_consent():
    h = Harness()
    try:
        await h.offer('quiz')
        await h.done()
        await h.send('SpeechReceived', text='Да, но лучше поговорим про облака')
        assert h.state.mode == 'bear_dialogue' and h.state.pending_game_offer is None
        assert len(h.model.calls) == 2
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_voice_accepted_before_prompt_finishes_cannot_become_late_consent():
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app.safety_classifier.classify
    async def slow(text, **kwargs):
        entered.set()
        await release.wait()
        return await original(text, **kwargs)
    try:
        await h.offer('quiz')
        h.app.safety_classifier.classify = slow
        task = asyncio.create_task(h.send('SpeechReceived', text='Да, давай.'))
        await entered.wait()
        await h.done()
        release.set()
        await task
        assert h.state.mode == 'bear_dialogue' and h.state.pending_game_offer == 'quiz'
        assert h.state.game_offer_ready
        h.app.safety_classifier.classify = original
        await h.send('SpeechReceived', text='Давай')
        assert h.state.mode == 'game_active'
    finally:
        release.set()
        await h.close()


@pytest.mark.asyncio
async def test_old_cancellation_resistant_model_offer_cannot_reenter_after_scene_exit():
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    class LateModel:
        async def stream(self, messages, request_id, cancelled):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            yield '<say>Давай играть.</say><action type="offer_game" game_id="quiz"/>'
    h.app.bounded_s2s.llm = LateModel()
    task = asyncio.create_task(h.send('SpeechReceived', text='Хочу поиграть'))
    try:
        await entered.wait()
        await h.key(7)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert h.state.mode == 'root_menu' and h.state.pending_game_offer is None
        assert not h.app._game_callers and h.state.active_game_id is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_inherited_held_confirmation_needs_release_and_new_press():
    h = Harness()
    try:
        await h.offer('quiz')
        await h.send('ButtonPressed', {'button_id': 1})
        await h.done()
        await h.send('ButtonPressed', {'button_id': 1})
        assert h.state.mode == 'bear_dialogue'
        await h.send('ButtonReleased', {'button_id': 1})
        await h.key(1)
        assert h.state.mode == 'game_active'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_word_reminders_are_bounded_and_do_not_renew_engagement_deadline():
    h = Harness()
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        owner = h.app._engagement_owners[sid]
        h.app._refresh_engagement_busy(sid)
        deadline = owner['deadline']
        limit = h.state.game_engine_state.hint_limit
        for count in range(1, limit + 1):
            scope = h.app._game_timer_scopes[sid]
            h.clock = scope[-1]
            result = (await h.app.flush_game_timers())[0]
            assert result['reason'] == 'game_wait_hint'
            assert h.state.active_audio_id == 'GAME_PROMPT'
            assert h.state.game_engine_state.hints_emitted == count
            await h.done()
            assert owner['deadline'] == deadline
            assert h.state.game_engine_state.round == 0
        assert sid not in h.app._game_timer_scopes
        h.clock = deadline
        await h.app.expire_engagement_sessions()
        assert h.state.mode == 'root_menu' and not h.app._game_callers
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('band,turns', [('1-3', 4), ('4-5', 5), ('6-7', 6)])
async def test_word_game_has_explicit_small_round_and_soft_contextual_completion(band, turns):
    h = Harness(band=band)
    try:
        await h.launch('word_game')
        for index in range(turns):
            result = await h.send('SpeechReceived', text=f'Кот {index}')
            assert h.state.mode == ('bear_dialogue' if index == turns - 1 else 'game_active')
        assert h.state.active_audio_id is None
        assert not any(e.get('type') in {'audio_stop', 'dialogue_prompt'} for e in result['outputs'])
        assert not h.app._game_callers
        assert len(h.model.calls) == turns + 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_game_timer_cannot_consume_seven_hold_as_answer_after_round_changes():
    h = Harness(band='6-7')
    try:
        await h.launch('quiz')
        engine = h.state.game_engine_state
        index = next(i for i, c in enumerate(engine.deck) if c.answer == 7)
        h.state.game_engine_state = replace(engine, challenge_index=index)
        await h.send('ButtonPressed', {'button_id': 7})
        before = h.state.game_engine_state.round
        # Gesture progress owns this deadline; no round hint or answer may fire.
        scope = h.app._game_timer_scopes[h.state.session_id]
        h.clock = scope[-1]
        await h.app.flush_game_timers()
        released = await h.send('ButtonReleased', {'button_id': 7})
        assert released['reason'] == 'game_exit_aborted'
        assert h.state.mode == 'game_active' and h.state.game_engine_state.round == before
        assert h.state.game_progress.get('quiz', 0) == engine.level
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['starting', 'prompt', 'cross_to_playing'])
async def test_quiz_long_seven_real_press_release_exits_even_during_prompt(stage):
    h = Harness(band='6-7')
    try:
        await h.offer('quiz')
        await h.done()
        await h.key(1)
        if stage != 'starting':
            await h.done()
        engine = h.state.game_engine_state
        index = next(i for i, c in enumerate(engine.deck) if 7 in dict(c.options))
        h.state.game_engine_state = replace(engine, challenge_index=index)
        old = dict(h.app._active_audio[h.state.session_id])
        await h.send('ButtonPressed', {'button_id': 7})
        if stage == 'cross_to_playing':
            h.clock += .1
            await h.send('AudioCompleted', {**old,'response_id':old['content_id']})
            assert h.state.game_engine_state.seven_exit_phase == 'pending'
        h.clock += 5.1
        await h.send('ButtonReleased', {'button_id': 7})
        assert h.state.mode == 'bear_dialogue'
        assert h.state.active_audio_id == 'DIALOGUE_RETURN'
        assert h.state.game_progress.get('quiz', 0) == engine.level
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_action_only_model_offer_has_no_ordinary_pcm_to_cut_before_owned_confirmation():
    from app.foundation.stand import BrowserPlaybackController
    h = Harness()
    class Hub:
        def __init__(self): self.messages = []
        async def publish(self, value): self.messages.append(value)
    hub = Hub()
    h.app.bounded_s2s.playback = BrowserPlaybackController(hub)
    try:
        await h.offer('quiz')
        assert not any(message['type'] == 'audio_pcm_output' for message in hub.messages)
        assert h.app._active_audio[h.state.session_id]['content_id'] == 'DIALOGUE_OFFER'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_natural_word_completion_keeps_last_real_browser_pcm_without_stop():
    from app.foundation.stand import BrowserPlaybackController
    h = Harness(band='1-3')
    class Hub:
        def __init__(self): self.messages = []
        async def publish(self, value): self.messages.append(value)
    hub = Hub()
    h.app.bounded_s2s.playback = BrowserPlaybackController(hub)
    try:
        await h.launch('word_game')
        for index in range(3):
            await h.send('SpeechReceived', text='Кот')
        hub.messages.clear()
        await h.send('SpeechReceived', text='Лиса')
        assert h.state.mode == 'bear_dialogue'
        assert any(message['type'] == 'audio_pcm_output' for message in hub.messages)
        assert not any(message['type'] == 'audio_stop' for message in hub.messages)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['<say>Давай играть.</say><action type="offer_game" game_id="quiz"/>',
    '<action type="offer_game" game_id="quiz"/><say>Давай играть.</say>',
    '<action type="offer_game" game_id="quiz"/><action type="offer_game" game_id="word_game"/>'])
async def test_mixed_or_repeated_model_offer_is_rejected_without_game_authority(answer):
    h = Harness(answer)
    try:
        await h.send('SpeechReceived', text='Хочу играть')
        assert h.state.mode == 'bear_dialogue' and h.state.pending_game_offer is None
        assert not h.app._game_callers
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_generic_ingress_cannot_forge_internal_offer_or_verified_confirmation():
    h = Harness()
    try:
        await h.send('DialogueFallback', {'reason': 'game_offer', 'game_id': 'quiz'})
        assert h.state.pending_game_offer is None
        await h.offer('quiz')
        await h.done()
        await h.send('VoiceIntentReady', {'intent': 'yes', 'safety_severity': 'none'})
        assert h.state.mode == 'bear_dialogue' and h.state.pending_game_offer == 'quiz'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['HandsetPlaced', 'SessionReset'])
async def test_rejected_stale_lifecycle_edge_cannot_erase_saved_caller(kind):
    h = Harness()
    try:
        await h.send('SpeechReceived', text='Моего кота зовут Барсик')
        await h.launch('word_game')
        sid = h.state.session_id
        caller = h.app._game_callers[sid]
        history = list(caller['context'].history)
        result = await h.app.receive_async(Event(kind, sid, 0, 1., 'test', {}, 'stale-boundary', h.clock))
        assert result['reason'] == 'stale_sequence'
        assert h.app._game_callers[sid] is caller
        await h.key(7, duration=6.)
        assert h.state.mode == 'bear_dialogue'
        assert h.app.conversation_contexts[sid].history == history
    finally:
        await h.close()


@pytest.mark.parametrize('band,required', [('1-3', 'животное или еду'),
    ('4-5', 'по очереди называть животных'), ('6-7', 'последнюю букву')])
def test_initial_word_rules_match_age_specific_voice_game(band, required):
    from app.foundation.canonical_contract import CanonicalRuntime
    from app.foundation.game_engine import start
    state = CanonicalState('age-prompt', mode='game_active', handset='up', age_band=band,
        active_game_id='word_game', game_engine_state=start('word_game', sequence=0, now=1.,
        selection_button=0, age_band=band, immersive=True))
    effect = CanonicalRuntime()._game_prompt(state)
    assert required in effect['prompt']
    assert effect['exit_hint'] == 'А если захочешь выйти, зажми семёрку и подержи, пока я не скажу отпускать.'


def test_first_quiz_prompt_explains_hold_exit_even_when_correct_answer_is_not_seven():
    from app.foundation.canonical_contract import CanonicalRuntime
    from app.foundation.game_engine import start
    engine = start('quiz', sequence=0, now=1., selection_button=0, age_band='1-3', immersive=True)
    state = CanonicalState('quiz-exit', mode='game_active', handset='up', age_band='1-3',
        active_game_id='quiz', game_engine_state=engine)
    assert 7 in game_answer_buttons(engine)
    assert current_challenge(engine).answer != 7
    effect = CanonicalRuntime()._game_prompt(state)
    assert 'зажми семёрку и подержи, пока я не скажу отпускать' in effect['rules_intro']
    assert effect['exit_hint'] is None  # The first owned rules already explain the exit.


@pytest.mark.asyncio
@pytest.mark.parametrize('band,limit', [('1-3', 4), ('4-5', 5), ('6-7', 6)])
async def test_control_only_offer_preserves_last_spoken_caller_turn_without_resetting_budget(band, limit):
    h = Harness(band=band)
    try:
        sid = h.state.session_id
        for index in range(limit - 1):
            await h.send('SpeechReceived', text=f'Поговорим про кота {index}')
        context = h.app.conversation_contexts[sid]
        assert context.turns == limit - 1
        started = context.started_at
        await h.offer('free_lights')
        assert h.app.conversation_contexts[sid].turns == limit - 1
        assert h.app.conversation_contexts[sid].started_at == started
        await h.done()
        await h.key(1)
        assert h.app._game_callers[sid]['context'].turns == limit - 1
        await h.done()
        await h.done()
        await h.key(7, duration=6.)
        await h.done()
        result = await h.send('SpeechReceived', text='А кот умеет мурлыкать?')
        assert h.state.mode == 'bear_dialogue'
        assert any(effect.get('resolution') == 'bounded_dialogue_completed' for effect in result['outputs'])
        assert h.app.conversation_contexts[sid].turns == limit
        before = len(h.model.calls)
        result = await h.send('SpeechReceived', text='Ещё один вопрос')
        assert len(h.model.calls) == before
        assert any(effect.get('fallback_reason') == 'bounded_limit' for effect in result['outputs'])
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_bear_prompt_limits_action_only_exception_to_meaningful_capability_requests():
    h = Harness('<say>Облака состоят из капелек воды.</say>')
    try:
        result = await h.send('SpeechReceived', text='Почему облака белые?')
        prompt = ' '.join(h.model.calls[0][0]['content'].split())
        assert 'only for a meaningful request to use an available activity or sourced information' in prompt
        assert 'Ordinary conversation, including fictional Mishka home, uses SPEECH_FORM.' in prompt
        assert 'quiz не является выбором по умолчанию.' in prompt
        assert 'Без осмысленной просьбы ребёнка используй речевую форму, не предлагай действие каталога самовольно.' in prompt
        assert result['reason'] == 'dialogue_turn'
        assert h.state.mode == 'bear_dialogue' and h.state.pending_game_offer is None
        assert h.app.conversation_contexts[h.state.session_id].turns == 1
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Да, давай.', 'Да, хочу.', 'Ага, хочу поиграть',
    'Ну да, давай поиграем, пожалуйста', 'Хорошо и давай сыграем', 'Да хочу играть'])
async def test_complete_positive_compound_confirms_only_current_ready_offer(text):
    h = Harness(band='1-3')
    try:
        await h.offer('quiz')
        await h.done()
        result = await h.send('SpeechReceived', text=text)
        assert result['reason'] == 'game_started' and h.state.active_game_id == 'quiz'
        assert h.state.game_return_mode == 'bear_dialogue'
        assert len(h.model.calls) == 1
    finally:
        await h.close()


@pytest.mark.parametrize('text', ['Да, но лучше поговорим', 'Да не хочу', 'Да, нет',
    'Да, давай другую игру', 'Да, хочу в слова', 'Да, хочу играть, но не сейчас', 'Да? Давай'])
def test_compound_confirmation_cannot_accept_nonpositive_or_alternative_clauses(text):
    from app.foundation.canonical_services import game_confirmation_intent
    assert game_confirmation_intent(text) == 'free_speech'


@pytest.mark.asyncio
async def test_untrusted_compound_does_not_authorize_or_consume_current_proposal():
    h = Harness()
    try:
        await h.offer('quiz')
        await h.done()
        offer = h.state.game_offer_id
        await h.send('SpeechReceived', {'_stt_untrusted': True}, text='Да, давай.')
        assert h.state.mode == 'bear_dialogue' and h.state.game_offer_id == offer
        await h.send('SpeechReceived', text='Да, хочу поиграть')
        assert h.state.active_game_id == 'quiz'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Позови маму', 'Позови взрослого', 'Нужен взрослый'])
async def test_explicit_help_during_ready_offer_preempts_confirmation_and_model(text):
    h = Harness()
    try:
        await h.offer('quiz')
        await h.done()
        result = await h.send('SpeechReceived', text=text)
        assert result['reason'] == 'help_requested'
        assert h.state.mode == 'safety_stop' and h.state.active_audio_id == 'PHRASE_091'
        assert h.state.pending_game_offer is None and h.state.active_game_id is None
        assert len(h.model.calls) == 1
    finally:
        await h.close()
