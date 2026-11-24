"""A synthetic old wellbeing answer cannot answer a replacement question."""
import asyncio

import pytest

from test_menu_stt_phase import FinishedMenuSpeech
from test_game_audio_reminder_race import SPEECH, SILENCE


class WellbeingSpeech(FinishedMenuSpeech):
    def __init__(self, text='Нет', severity='none'):
        super().__init__(text, severity)
        self.state.mode = 'root_menu'

    async def question(self):
        await self.button(5)
        await self.send('ButtonReleased', {'button_id': 5})
        assert self.state.mode == 'wellbeing_check'
        assert self.state.support_question_kind == 'pain_or_discomfort'
        await self.complete()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Нет', 'Да'])
async def test_finished_old_answer_cannot_answer_replacement_fear_question(text):
    h = WellbeingSpeech(text)
    try:
        await h.question()
        await h.start_completion()
        await h.button(1)
        assert h.state.support_question_kind == 'fear'
        owner, version = dict(h.app._active_audio[h.sid]), h.state.state_version
        await h.finish_completion()
        assert h.classifications == [text], 'mandatory safety still classifies old same-visit speech'
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'fear'
        assert h.state.state_version == version and h.app._active_audio[h.sid] == owner
        assert not h.updates and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_same_kind_repeated_question_revokes_old_answer_version():
    h = WellbeingSpeech()
    try:
        await h.question()
        await h.start_completion()
        old_state, old_version = h.state, h.state.state_version
        await h.button(5)
        assert h.state is old_state and h.state.state_version > old_version
        assert h.state.support_question_kind == 'pain_or_discomfort'
        owner, version = dict(h.app._active_audio[h.sid]), h.state.state_version
        await h.finish_completion()
        assert h.classifications == ['Нет']
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'pain_or_discomfort'
        assert h.state.state_version == version and h.app._active_audio[h.sid] == owner
        assert not h.updates and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_buffered_answer_cannot_answer_replacement_question_after_audio_cleanup():
    h = WellbeingSpeech()
    try:
        await h.question()
        h.seq += 1
        assert (await h.app.receive_audio_async(h.sid, SPEECH, h.seq,
            wait_for_turn=False))['reason'] == 'collecting'
        pipeline = h.app.s2s_audio.ingress[h.sid]
        origin = pipeline._canonical_private_owner
        assert origin is not None and origin() and origin.lifecycle_current()
        assert origin.wellbeing_question == 'pain_or_discomfort'
        await h.button(1)
        await h.send('ButtonReleased', {'button_id': 1})
        await h.complete()
        assert h.state.support_question_kind == 'fear'
        assert not origin() and origin.lifecycle_current()
        # The actual question transition cancels capture and replaces its VAD
        # buffer. Trailing silence cannot revive the old answer in the new one.
        assert h.app.s2s_audio.ingress.get(h.sid) is not pipeline
        version = h.state.state_version
        h.seq += 1
        result = await h.app.receive_audio_async(h.sid, SILENCE, h.seq, wait_for_turn=False)
        assert result['reason'] == 'collecting' and not result['pending']
        assert not h.classifications
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'fear'
        assert h.state.state_version == version and not h.updates and not h.model.calls
        await h.start_completion()
        await h.finish_completion()
        assert h.state.mode == 'root_menu' and h.classifications == ['Нет']
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_fresh_no_answers_each_unchanged_question():
    h = WellbeingSpeech()
    try:
        await h.question()
        for expected in ['fear', None]:
            await h.start_completion()
            await h.finish_completion()
            assert h.state.support_question_kind == expected
            await h.complete()
            h.entered.clear()
            h.release.clear()
        assert h.state.mode == 'root_menu'
        assert h.classifications == ['Нет', 'Нет'] and not h.model.calls
        assert [value['reason'] for value in h.updates] == ['support_continue', 'support_declined']
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,severity,expected', [
    ('Мне угрожают', 'urgent', 'safety_stop'),
    ('Позови взрослого', 'none', 'safety_stop'),
    ('Мне грустно', 'concern', 'support_intake'),
])
async def test_replaced_question_still_prioritizes_same_visit_safety(text, severity, expected):
    h = WellbeingSpeech(text, severity)
    try:
        await h.question()
        await h.start_completion()
        await h.button(1)
        await h.finish_completion()
        assert h.classifications == [text] and h.state.mode == expected
        assert h.state.active_audio_id == ('PHRASE_071' if expected == 'support_intake' else 'PHRASE_091')
        assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,severity', [
    ('Нет', 'none'), ('Мне угрожают', 'urgent'), ('Позови взрослого', 'none'),
])
async def test_old_visit_cannot_classify_or_mutate_replacement_visit(text, severity):
    h = WellbeingSpeech(text, severity)
    h.app.audio_cleanup_timeout_seconds = .01
    try:
        await h.question()
        await h.start_completion()
        epoch = h.app._session_epochs.get(h.sid, 0)
        await h.send('HandsetPlaced')
        await h.send('HandsetLifted')
        assert h.app._session_epochs[h.sid] > epoch
        state, version = h.state, h.state.state_version
        await h.finish_completion()
        assert h.state is state and h.state.state_version == version
        assert h.state.mode == 'voice_welcome'
        assert not h.classifications and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_current_question_audio_completion_does_not_revoke_its_answer():
    h = WellbeingSpeech()
    try:
        await h.button(5)
        await h.send('ButtonReleased', {'button_id': 5})
        assert h.state.active_audio_id == 'PHRASE_084'
        await h.start_completion()
        await h.complete()
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'pain_or_discomfort'
        await h.finish_completion()
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'fear'
        assert h.classifications == ['Нет'] and not h.model.calls
        assert [value['reason'] for value in h.updates] == ['support_continue']
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,semantic_severity,replaced,expected', [
    ('Нет, ничего не болит', 'none', False, 'wellbeing_check'),
    ('Нет, ничего не болит', 'none', True, 'wellbeing_check'),
    ('Нет, ничего не болит', 'concern', True, 'support_intake'),
    ('Нет, ничего не болит. Не могу дышать.', 'none', True, 'safety_stop'),
])
async def test_actual_safety_gate_uses_original_question_without_erasing_danger(
        text, semantic_severity, replaced, expected):
    from app.foundation.config import Settings
    from app.foundation.input_safety import InputSafetyGate
    h = WellbeingSpeech(text)
    calls = []
    class Semantic:
        async def classify(self, value):
            calls.append(value)
            return semantic_severity, .99
    h.app.safety_classifier = InputSafetyGate(Settings(profile='mock'), Semantic())
    try:
        await h.question()
        await h.start_completion()
        if replaced:
            await h.button(1)
            owner = dict(h.app._active_audio[h.sid])
        await h.finish_completion()
        assert h.state.mode == expected
        if expected == 'wellbeing_check':
            assert h.state.support_question_kind == 'fear'
            assert calls == [text], 'semantic safety remains mandatory for the full reassurance'
            if replaced:
                assert h.app._active_audio[h.sid] == owner and not h.updates
            else:
                assert [value['reason'] for value in h.updates] == ['support_continue']
        elif expected == 'support_intake':
            assert h.state.safety_severity == 'concern' and calls == [text]
        else:
            assert h.state.safety_severity == 'urgent'
        assert not h.model.calls
    finally:
        await h.close()
