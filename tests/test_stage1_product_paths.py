"""Meaningful root-3 answers, typed day speech and scoped wellbeing replies."""
import asyncio

import pytest

from app.foundation.canonical_services import wellbeing_reassurance
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.input_safety import InputSafetyGate
from test_contextual_game_flow import Model, Harness as GameHarness
from test_product_typed_speech import Harness as Day
from test_scene_menu_runtime import Harness as Menu


@pytest.mark.asyncio
async def test_root_three_answers_two_actual_safe_questions_in_bounded_engine():
    h = GameHarness(mode='root_menu')
    model = Model('<say>Облака состоят из капелек воды.</say>', '<say>Листья помогают дереву получать свет.</say>')
    h.app.bounded_s2s.llm = model
    try:
        await h.key(3)
        for text in ('Почему облака белые?', 'А зачем деревьям листья?'):
            result = await h.send('SpeechReceived', text=text)
            assert result['reason'] == 'support_dialogue_turn'
            assert any(e.get('resolution') == 'bounded_dialogue_completed' for e in result['outputs'])
        assert len(model.calls) == 2
        assert len(h.app.conversation_contexts[h.state.session_id].history) == 2
        await h.send('SpeechReceived', text='Мне больно')
        assert h.state.safety_severity == 'concern'
        assert h.state.active_audio_id == 'PHRASE_071'
        assert len(model.calls) == 2
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,content', [('Какое сегодня число?', 'TYPED_DATE'),
    ('Какая сегодня погода?', 'TYPED_WEATHER'), ('Что будет дальше?', 'PHRASE_101')])
async def test_day_voice_requests_use_actual_typed_provider_or_privacy_fallback(text, content):
    h = Day()
    try:
        await h.start()
        h.seq += 1
        await h.app.receive_async(Event('SpeechReceived', 'typed', h.seq, 1., 'test', {}, str(h.seq), 1.), text)
        assert h.app.audio.commands[-1]['response_id'] == content
        assert h.state.mode == 'schedule_weather'
        assert not h.app.conversation_contexts
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_day_back_revokes_already_queued_audio_and_late_completion():
    h = Day()
    try:
        await h.start()
        await h.button(1)
        old = h.callback()
        assert h.app._active_audio['typed']['content_id'] == 'TYPED_DATE'
        result = await h.button(7)
        assert result['reason'] == 'info_back'
        assert h.state.mode == 'root_menu' and h.state.active_audio_id is None
        assert 'typed' not in h.app._active_audio
        assert any(e['type'] == 'audio_stop' for e in result['outputs'])
        await h.send('AudioCompleted', old)
        assert h.state.mode == 'root_menu' and h.state.active_audio_id is None
    finally:
        await h.close()


class Semantic:
    def __init__(self, severity='none', confidence=1.):
        self.severity, self.confidence, self.seen = severity, confidence, []

    async def classify(self, text):
        self.seen.append(text)
        return self.severity, self.confidence


@pytest.mark.asyncio
@pytest.mark.parametrize('question,text', [('pain_or_discomfort', 'Нет, ничего не болит'), ('fear', 'Нет, мне не страшно!')])
async def test_scoped_reassurance_exempts_only_deterministic_concern_before_semantic_merge(question, text):
    semantic = Semantic()
    gate = InputSafetyGate(Settings(profile='mock'), semantic=semantic)
    result = await gate.classify(text, wellbeing_question=question)
    assert result.severity == 'none' and result.verified
    assert semantic.seen == [text]  # No rewritten shortcut is classified.
    assert (await gate.classify(text)).severity == 'concern'
    for severity in ('concern', 'urgent'):
        semantic.severity = severity
        result = await gate.classify(text, wellbeing_question=question)
        assert result.severity == severity and result.verified
    semantic.severity, semantic.confidence = 'none', .2
    result = await gate.classify(text, wellbeing_question=question)
    assert not result.verified


@pytest.mark.parametrize('text,question', [('Нет, ничего не болит, но мне страшно', 'pain_or_discomfort'),
    ('Нет, ничего не болит, меня бьют', 'pain_or_discomfort'), ('Нет, ничего не болит?', 'pain_or_discomfort'),
    ('Нет, ничего не болит', 'fear'), ('Нет, мне не страшно', 'pain_or_discomfort')])
def test_scoped_reassurance_has_no_clause_wrong_question_or_questionmark_bypass(text, question):
    assert not wellbeing_reassurance(text, question)


@pytest.mark.asyncio
async def test_actual_wellbeing_full_negative_answers_advance_questions_without_false_disclosure():
    h = Menu()
    try:
        await h.button(5)
        assert h.state.support_question_kind == 'pain_or_discomfort'
        await h.complete()
        await h.send('SpeechReceived', text='Нет, ничего не болит')
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'fear'
        assert h.state.active_audio_id == 'PHRASE_085' and h.state.safety_severity == 'none'
        await h.complete()
        await h.send('SpeechReceived', text='Нет, мне не страшно')
        assert h.state.mode == 'root_menu' and h.state.safety_severity == 'none'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('severity,confidence', [('none', .2), ('concern', 1.), ('urgent', 1.)])
async def test_actual_wellbeing_semantic_uncertainty_or_disclosure_never_advances_as_no(severity, confidence):
    h = Menu()
    semantic = Semantic(severity, confidence)
    h.app.safety_classifier.semantic = semantic
    try:
        await h.button(5)
        await h.complete()
        await h.send('SpeechReceived', text='Нет, ничего не болит')
        assert h.state.support_question_kind != 'fear'
        if severity == 'none':
            assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'pain_or_discomfort'
        else:
            assert h.state.safety_severity == severity
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_current_question_change_during_semantic_wait_cannot_authorize_old_no():
    h = Menu()
    entered, release = asyncio.Event(), asyncio.Event()
    class Slow(Semantic):
        async def classify(self, text):
            entered.set()
            await release.wait()
            return 'none', 1.
    h.app.safety_classifier.semantic = Slow()
    task = None
    try:
        await h.button(5)
        await h.complete()
        task = asyncio.create_task(h.send('SpeechReceived', text='Нет, ничего не болит'))
        await entered.wait()
        await h.button(7)  # New urgent/local guidance owner replaces question.
        release.set()
        await task
        assert h.state.mode == 'safety_stop' and h.state.active_audio_id == 'PHRASE_091'
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'invalid', 'untrusted_stt'])
async def test_scoped_full_negative_requires_valid_semantics_and_trusted_stt(failure):
    h = Menu()
    class FailedSemantic:
        async def classify(self, text):
            if failure == 'timeout':
                await asyncio.sleep(1)
            return ('none', 'invalid') if failure == 'invalid' else ('none', 1.)
    h.app.safety_classifier.semantic = FailedSemantic()
    h.app.safety_classifier.timeout_s = .01
    try:
        await h.button(5)
        await h.complete()
        await h.send('SpeechReceived', {'_stt_untrusted': True} if failure == 'untrusted_stt' else {},
                     text='Нет, ничего не болит')
        assert h.state.mode == 'wellbeing_check'
        assert h.state.support_question_kind == 'pain_or_discomfort'
        assert h.state.safety_severity == 'none'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_scoped_reassurance_cannot_answer_new_same_mode_question_after_await():
    h = Menu()
    entered, release = asyncio.Event(), asyncio.Event()
    class Slow:
        async def classify(self, text):
            entered.set()
            await release.wait()
            return 'none', 1.
    h.app.safety_classifier.semantic = Slow()
    task = None
    try:
        await h.button(5)
        await h.complete()
        task = asyncio.create_task(h.send('SpeechReceived', text='Нет, ничего не болит'))
        await entered.wait()
        # A different physical source answers the old question while semantics waits.
        await h.app.receive_async(Event('ButtonPressed', h.sid, 1, 1., 'hid', {'button_id': 1}, 'fresh-hid-no'))
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'fear'
        lease = h.state.playback_lease
        release.set()
        await task
        assert h.state.support_question_kind == 'fear' and h.state.active_audio_id == 'PHRASE_085'
        assert h.state.playback_lease == lease
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()
