"""Actual uncorrelated UART ingress must not merge distinct internal followups."""
import pytest

from app.foundation.age_policy import policy_for_band
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import finish_prompt, start
from app.foundation.simulator import SimulatorAcceptanceHarness
from test_contextual_game_flow import Harness, Model


class Uncorrelated(Harness):
    async def send(self, kind, payload=None, text=None):
        self.seq += 1
        return await self.app.receive_async(Event(kind, self.state.session_id, self.seq,
            1., 'uart_pty', payload or {}, None, self.clock), text)


@pytest.mark.asyncio
async def test_actual_stand_text_parser_has_no_correlation_and_finishes_four_word_turns():
    h = SimulatorAcceptanceHarness(Settings(profile='mock', runtime_mode='clinical', hardware_mode='simulator'))
    app = h.stand.application
    model = Model(*['<say>Лиса.</say>'] * 4)
    app.bounded_s2s.llm = model
    state = CanonicalState('stand', mode='game_active', handset='up', active_game_id='word_game',
        age_band='1-3', age_years=2, age_band_source='exhibition_button',
        age_policy=policy_for_band('1-3', source='exhibition_button').as_dict(), game_audio_stage='playing',
        game_engine_state=finish_prompt(start('word_game', sequence=0, now=h.clock.now(),
            selection_button=0, run_id='real-stand-words', age_band='1-3', immersive=True), h.clock.now()).state)
    app.canonical_runtime.sessions['stand'] = state
    original = app.receive_async
    admitted = []
    async def observe(event, *args, **kwargs):
        if event.event_type == 'SpeechReceived':
            admitted.append(event)
        return await original(event, *args, **kwargs)
    app.receive_async = observe
    try:
        for index, text in enumerate(('Кот', 'Лиса', 'Заяц', 'Белка'), 1):
            result = await h.stand.inject('text', {'text': text}, 'stand')
            assert result['reason'] == ('word_game_finished' if index == 4 else 'word_game_turn_finished')
            if index < 4:
                assert state.game_engine_state.round == index
        assert state.mode == 'root_menu' and state.game_engine_state is None
        assert len(model.calls) == 4
        assert len(admitted) == 4
        assert all(event.source == 'uart_pty' and not event.correlation_id for event in admitted)
        assert len({event.sequence for event in admitted}) == 4
        replay = await original(admitted[-1], 'Белка')
        assert replay['reason'] == 'stale_sequence' and len(model.calls) == 4
    finally:
        await h.stand.aclose()


@pytest.mark.asyncio
async def test_uncorrelated_decline_then_reoffer_is_not_the_previous_offer():
    h = Uncorrelated()
    try:
        await h.offer('quiz')
        first = h.state.game_offer_id
        await h.key(7)
        await h.offer('word_game')
        assert h.state.pending_game_offer == 'word_game' and h.state.game_offer_id != first
        await h.done()
        await h.key(1)
        assert h.state.active_game_id == 'word_game'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_uncorrelated_provider_failure_can_recover_repeatedly_without_duplicate_followup():
    h = Uncorrelated('<invalid/>', '<invalid/>')
    try:
        for _ in range(2):
            result = await h.send('SpeechReceived', text='Почему небо голубое?')
            turn = next(item for item in result['outputs'] if item['type'] == 'bounded_dialogue_turn')
            assert turn['fallback']['reason'] != 'idempotent_duplicate'
            assert h.state.active_audio_id == 'PHRASE_104'
            await h.done()
        assert len(h.model.calls) == 2
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('correlation,reason', [(None, 'stale_sequence'), ('explicit-original', 'idempotent_duplicate')])
async def test_original_replay_remains_inert_with_or_without_explicit_correlation(correlation, reason):
    from dataclasses import replace
    h = Uncorrelated('<say>Облака состоят из капелек воды.</say>')
    try:
        event = Event('SpeechReceived', h.state.session_id, 1, 1., 'uart_pty', {}, correlation, h.clock)
        await h.app.receive_async(event, 'Почему облака белые?')
        before = list(h.app.conversation_contexts[h.state.session_id].history)
        replay = replace(event, sequence=2) if correlation else event
        result = await h.app.receive_async(replay, 'Почему облака белые?')
        assert result['reason'] == reason and len(h.model.calls) == 1
        assert h.app.conversation_contexts[h.state.session_id].history == before
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('flow', ['word', 'offer', 'fallback'])
async def test_internal_sources_order_high_then_low_distinct_original_sources(flow):
    h = Uncorrelated('<invalid/>', '<invalid/>') if flow == 'fallback' else Uncorrelated()
    async def fresh(source, sequence, text):
        return await h.app.receive_async(Event('SpeechReceived', h.state.session_id, sequence,
            1., source, {}, f'{source}-unique-{sequence}', h.clock), text)
    try:
        if flow == 'word':
            await h.launch('word_game')
            for index, (source, sequence, text) in enumerate((('physical-mic', 1000, 'Кот'), ('panel-text', 1, 'Лиса')), 1):
                result = await fresh(source, sequence, text)
                assert result['reason'] == 'word_game_turn_finished'
                assert h.state.game_engine_state.round == index
        elif flow == 'offer':
            for source, sequence, game in (('physical-mic', 1000, 'quiz'), ('panel-text', 1, 'word_game')):
                h.model.answers.insert(0, f'<action type="offer_game" game_id="{game}"/>')
                result = await fresh(source, sequence, 'Хочу поиграть')
                assert result['reason'] == 'game_offered' and h.state.pending_game_offer == game
                await h.key(7)
        else:
            for source, sequence in (('physical-mic', 1000), ('panel-text', 1)):
                result = await fresh(source, sequence, 'Почему небо голубое?')
                turn = next(item for item in result['outputs'] if item['type'] == 'bounded_dialogue_turn')
                assert turn['fallback']['reason'] not in {'stale_sequence', 'idempotent_duplicate'}
                assert h.state.active_audio_id == 'PHRASE_104'
                await h.done()
    finally:
        await h.close()
