"""Nonverbal/voice parity and unambiguous optional onboarding slots."""
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.canonical_contract import normalize_exhibition_age, normalize_exhibition_name

@pytest.mark.parametrize('text',['Не хочу','Не скажу','Не знаю','Можно играть','Я не хочу говорить'])
def test_a_decline_is_not_a_name(text):
    assert normalize_exhibition_name(text) is None

@pytest.mark.parametrize('text',['Мне не три года, а шесть','У меня два кота','Можно один вопрос','Раз два три','3 или 6','Мне 16','Мне 6 с половиной'])
def test_age_requires_one_direct_unambiguous_slot(text):
    assert normalize_exhibition_age(text) is None

@pytest.mark.parametrize('text,age',[('мне семь лет',7),('6',6),('мне уже четыре',4),('две',2),('  Мне 5 лет. ',5)])
def test_direct_age_answers_still_work(text,age):
    assert normalize_exhibition_age(text)==age

class Harness:
    def __init__(self,mode='clinical'):
        self.app,*_=build_application(settings=Settings(profile='mock',runtime_mode=mode),background_scheduler=False)
        self.sequence=0
    async def send(self,kind,payload=None,text=None):
        self.sequence+=1
        return await self.app.receive_async(Event(kind,'questions',self.sequence,1.,'test',payload or {},f'q:{self.sequence}',1.),text)
    async def button(self,value):
        result=await self.send('ButtonPressed',{'button_id':value})
        await self.send('ButtonReleased',{'button_id':value})
        return result
    async def answer(self,answer,source):
        return await self.button(7 if answer=='yes' else 1) if source=='button' else await self.send('SpeechReceived',text='Да' if answer=='yes' else 'Нет')
    @property
    def state(self):return self.app.canonical_runtime.sessions['questions']
    async def close(self):await self.app._cleanup_session_resources('questions')

@pytest.mark.asyncio
@pytest.mark.parametrize('source',['button','voice'])
@pytest.mark.parametrize('answer',['yes','no'])
async def test_fear_answer_has_same_semantics_for_voice_and_buttons(source,answer):
    h=Harness()
    try:
        await h.send('HandsetLifted');await h.button(5)
        first=await h.answer('no',source)
        assert first['reason']=='support_continue'
        assert h.state.mode=='wellbeing_check' and h.state.support_question_kind=='fear'
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_085'
        result=await h.answer(answer,source)
        if answer=='yes':
            assert h.state.mode=='safety_stop'
            assert h.state.safety_severity=='concern'
            assert result['reason']=='help_requested'
            assert h.app.audio.commands[-1]['response_id']=='PHRASE_091'
            assert 'incident_id' not in h.state.snapshot()
            assert not any(effect.get('type')=='adult_handoff_request' for effect in result['outputs'])
        else:
            assert result['reason']=='support_declined' and h.state.mode=='root_menu'
            assert h.app.audio.commands[-1]['response_id']=='PHRASE_087'
    finally:await h.close()

@pytest.mark.asyncio
async def test_repeat_help_repeats_current_question_not_old_pain_question():
    h=Harness()
    try:
        await h.send('HandsetLifted');await h.button(5);await h.button(1);await h.button(5)
        assert h.state.support_question_kind=='fear'
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_085'
    finally:await h.close()

@pytest.mark.asyncio
async def test_exhibition_help_entry_really_speaks_and_exposes_yes_no():
    h=Harness('exhibition')
    try:
        await h.send('HandsetLifted');h.state.mode='menu_ready'
        await h.button(5)
        assert h.state.support_question_kind=='pain_or_discomfort'
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_084'
        assert 'button_1' in h.state.expected_inputs and 'button_7' in h.state.expected_inputs
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['exhibition_name_capture','exhibition_age_selection'])
@pytest.mark.parametrize('answer',['Не хочу','Не знаю','Можно играть'])
async def test_declined_onboarding_continues_without_fabricated_identity(phase,answer):
    h=Harness('exhibition')
    try:
        await h.send('HandsetLifted');h.state.mode=phase;h.state.welcome_status='started'
        result=await h.send('SpeechReceived',text=answer)
        assert result['reason']=='exhibition_onboarding_declined'
        assert h.state.mode=='exhibition_menu_tutorial_prompt'
        assert h.state.display_name is None and h.state.age_years is None
        assert not h.state.dialogue_memory.bounded_turns
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_007'
    finally:await h.close()

@pytest.mark.asyncio
async def test_onboarding_decline_does_not_hide_danger():
    h=Harness('exhibition')
    try:
        await h.send('HandsetLifted');h.state.mode='exhibition_name_capture'
        await h.send('SpeechReceived',text='Не хочу говорить, я не могу дышать')
        assert h.state.mode=='safety_stop' and h.state.safety_severity=='urgent'
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("confirm_before_lift", [False, True])
async def test_onhook_help_has_button_confirmation_and_question_after_optional_lift(confirm_before_lift):
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    seq=0
    async def send(kind,payload=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,'onhook-help',seq,1.,'test',payload or {},f'h:{seq}',1.))
    try:
        result=await send('ButtonPressed',{'button_id':5})
        state=app.canonical_runtime.sessions['onhook-help']
        assert result['reason']=='help_priority' and state.mode=='wellbeing_check'
        assert not app.audio.commands and not state.active_audio_id
        assert not state.audio_failure_code
        assert state.support_question_kind=='pain_or_discomfort'
        if confirm_before_lift:
            assert (await send('ButtonPressed',{'button_id':7}))['reason']=='help_requested'
            assert state.mode=='safety_stop' and state.support_alert_kind=='help_requested'
        else:
            assert (await send('HandsetLifted'))['reason']=='support_question_after_handset'
            assert app.audio.commands[-1]['response_id']=='PHRASE_084'
    finally: await app.reset_session('onhook-help')
