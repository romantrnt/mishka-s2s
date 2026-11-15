"""Natural short refusals are not an identity slot, including real speech ingress."""
import pytest
from app.foundation.canonical_contract import onboarding_declined, normalize_exhibition_name
from test_product_question_contract import Harness

REFUSALS = [
    'Не хочу, давай играть',
    'Нет, не хочу',
    'Нет спасибо',
    'Нет, спасибо!',
    'Спасибо, не хочу',
    'Я не хочу называть своё имя',
    'Не буду отвечать, спасибо',
    'Своё имя не скажу',
    'Давай лучше играть',
    'Можно сразу поиграть?',
    'Не знаю, пропустим',
]

@pytest.mark.parametrize('text',REFUSALS)
def test_compound_refusal_is_not_a_name(text):
    assert onboarding_declined(text)
    assert normalize_exhibition_name(text) is None

@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['exhibition_name_capture','exhibition_age_selection'])
@pytest.mark.parametrize('text',REFUSALS[:6])
async def test_actual_speech_refusal_skips_without_name_age_or_second_interrogation(phase,text):
    h=Harness('exhibition')
    try:
        await h.send('HandsetLifted'); h.state.mode=phase; h.state.welcome_status='started'
        result=await h.send('SpeechReceived',text=text)
        assert result['reason']=='exhibition_onboarding_declined'
        assert h.state.mode=='exhibition_menu_tutorial_prompt'
        assert h.state.display_name is None and h.state.age_years is None
        assert not h.state.onboarding_name_collected
        assert not h.state.dialogue_memory.bounded_turns
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_007'
        assert 'PHRASE_108' not in [command['response_id'] for command in h.app.audio.commands]
    finally: await h.close()

@pytest.mark.parametrize('text,name',[
    ('Нетта','Нетта'),('Нева','Нева'),('Нейла','Нейла'),('Анна-Мария','Анна-Мария'),
    ('Я Нева','Нева'),('Меня зовут Нетта','Нетта'),('Меня зовут Вася, мне семь','Вася'),
    ('Нет, меня зовут Вася','Вася'),('Нет меня зовут Анна','Анна'),
    ('Спасибо, меня зовут Нева','Нева'),('Я Вася, не хочу играть','Вася'),
])
def test_name_words_and_explicit_correction_are_not_refusal(text,name):
    assert not onboarding_declined(text)
    assert normalize_exhibition_name(text)==name

@pytest.mark.asyncio
@pytest.mark.parametrize('text,name',[
    ('Меня зовут Нетта','Нетта'),('Я Нева','Нева'),('Нет, меня зовут Вася','Вася')])
async def test_actual_name_answer_still_reaches_age_question(text,name):
    h=Harness('exhibition')
    try:
        await h.send('HandsetLifted');h.state.mode='exhibition_name_capture';h.state.welcome_status='started'
        result=await h.send('SpeechReceived',text=text)
        assert result['reason']=='exhibition_age_question_started'
        assert h.state.display_name==name and h.state.onboarding_name_collected
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_108'
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('text',[
    'Нет спасибо, я не могу дышать',
    'Не хочу, позови взрослого',
])
async def test_safety_or_explicit_adult_request_still_precedes_refusal(text):
    h=Harness('exhibition')
    try:
        await h.send('HandsetLifted');h.state.mode='exhibition_name_capture';h.state.welcome_status='started'
        await h.send('SpeechReceived',text=text)
        assert h.state.mode=='safety_stop'
        assert h.state.display_name is None and not h.state.onboarding_name_collected
    finally:await h.close()
