"""A complete reassuring answer belongs only to the current pain question."""
import pytest

from app.foundation.input_safety import InputSafetyGate
from app.foundation.config import Settings
from test_contextual_game_flow import Harness
from test_stage1_product_paths import Semantic


@pytest.mark.asyncio
async def test_actual_question_accepts_complete_compound_pain_reassurance():
    h = Harness(mode='root_menu')
    semantic = Semantic()
    try:
        await h.key(5)
        await h.done()
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'pain_or_discomfort'
        h.app.safety_classifier = InputSafetyGate(Settings(profile='mock'), semantic=semantic)
        text = 'Всё хорошо, у меня ничего не болит'
        result = await h.send('SpeechReceived', text=text)
        assert result['reason'] == 'support_continue'
        assert h.state.safety_severity == 'none' and h.state.support_question_kind == 'fear'
        assert semantic.seen == [text]
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('severity,confidence', [('concern',1.),('urgent',1.),('none',.2)])
async def test_compound_reassurance_cannot_lower_semantic_safety_or_authorize_uncertain_advance(severity, confidence):
    h = Harness(mode='root_menu')
    try:
        await h.key(5)
        await h.done()
        semantic = Semantic(severity, confidence)
        h.app.safety_classifier = InputSafetyGate(Settings(profile='mock'), semantic=semantic)
        await h.send('SpeechReceived', text='Всё хорошо, у меня ничего не болит')
        if severity == 'none':
            assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'pain_or_discomfort'
        else:
            assert h.state.safety_severity == severity
            assert h.state.mode == ('safety_stop' if severity == 'urgent' else 'support_intake')
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('question,text', [
    ('fear','Всё хорошо, у меня ничего не болит'),
    (None,'Всё хорошо, у меня ничего не болит'),
    ('pain_or_discomfort','Всё хорошо, у меня ничего не болит?'),
    ('pain_or_discomfort','Всё хорошо, у меня ничего не болит, но мне страшно'),
    ('pain_or_discomfort','Всё хорошо, у меня ничего не болит, меня бьют'),
])
async def test_complete_answer_exception_is_question_bound_and_forbids_added_clauses(question, text):
    semantic = Semantic()
    gate = InputSafetyGate(Settings(profile='mock'), semantic=semantic)
    result = await gate.classify(text, wellbeing_question=question)
    assert result.severity in {'concern','urgent'}
