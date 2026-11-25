"""Actual UP/onboarding→VAD/STT→safety→canonical; ASR text is a fixture, not accuracy evidence."""
import json
import pytest
from app.foundation.s2s import MockSpeechRecognizer
from app.foundation.canonical_contract import normalize_exhibition_age
from test_child_button_access import ChildHarness
from test_game_audio_reminder_race import SPEECH, SILENCE


async def speech(h, route, text, confidence=.99):
    if route == 'typed':
        return await h.send('SpeechReceived', text=text)
    h.app.s2s_audio.recognizer = MockSpeechRecognizer(text, confidence)
    h.seq = max(h.seq, h.app.sessions.get(h.sid).last_sequence,
                h.state.last_sequence_by_source.get('uart_audio', 0)) + 1
    return await h.app.receive_audio_async(h.sid, SPEECH + SILENCE, h.seq)


async def age_entry(h):
    await h.name()
    await h.button(7); await h.release(7)
    await h.complete()
    assert h.state.mode == 'exhibition_age_selection' and h.state.age_years is None


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['typed', 'recognized_pcm'])
@pytest.mark.parametrize('text,expected', [('4',4), ('четыре',4), ('мне четыре',4), ('мне шесть годиков',6), ('мне шесть лет',6), ('шесть годиков',6)])
async def test_real_age_entry_accepts_plain_existing_integer_answer(route, text, expected):
    h = ChildHarness()
    try:
        await age_entry(h)
        result = await speech(h, route, text)
        assert result['reason'] == 'exhibition_age_selected', result
        assert h.state.age_years == expected
        assert h.state.age_band == ('4-5' if expected == 4 else '6-7')
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_confidently_wrong_name_asr_stays_provisional_in_all_consumers():
    h = ChildHarness()
    try:
        await h.name(); await h.complete()  # Complete owned NAME_BUTTON_HINT.
        assert h.state.mode == 'exhibition_name_capture'
        result = await speech(h, 'recognized_pcm', 'Ома', .99)  # Synthetic Рома→Ома substitution.
        assert h.state.display_name is None, result
        assert not h.state.onboarding_name_collected
        assert h.state.mode == 'exhibition_name_capture'
        assert not h.state.dialogue_memory.bounded_turns
        assert h.state.visit_profile().display_name is None
        assert 'Ома' not in h.app._canonical_audio_context(h.sid).system_prompt
        assert 'Ома' not in json.dumps(h.state.snapshot(), ensure_ascii=False)
    finally:
        await h.close()

@pytest.mark.parametrize('text,expected', [
    ('4.5',4.5), ('4,5',4.5), ('мне четыре с половиной',4.5),
    ('четыре с половинкой',4.5), ('четыре года с половиной',4.5),
    ('мне шесть с половиной',6.5), ('три с половиной',3.5),
    ('пять с половиной',5.5), ('семь с половиной',7.5),
    ('мне один годик',1), ('два годика',2), ('мне уже шесть годиков',6),
    ('Ну, мне четыре',4), ('Эм, мне шесть годиков',6),
    ('мне четыре, нет, пять',5), ('четыре, точнее, шесть с половиной',6.5),
])
def test_age_parser_accepts_bounded_direct_values_and_explicit_correction(text,expected):
    assert normalize_exhibition_age(text) == expected


@pytest.mark.parametrize('text', [
    '4–5','4-5','четыре или пять','мне от четырех до пяти',
    'у меня четыре игрушки','я родился в 2020','мне четыре, брату шесть',
    'двадцать четыре','14','4.2','0','8','мне шесть годиков и два месяца',
    'нет, я не скажу','это не четыре','он сказал мне четыре',
])
def test_age_parser_never_guesses_ranges_or_unrelated_numbers(text):
    assert normalize_exhibition_age(text) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('route',['typed','recognized_pcm'])
@pytest.mark.parametrize('text,age,full,band',[
    ('мне шесть годиков',6,6,'6-7'),
    ('4.5',4.5,4,'4-5'),
    ('мне шесть с половиной',6.5,6,'6-7'),
])
async def test_exact_age_survives_actual_ingress_and_full_year_policy(route,text,age,full,band):
    from app.foundation.age_policy import policy_for_band, trusted_canonical_age
    from app.foundation.lighting import BUTTON_FILL_ELEMENTS
    h=ChildHarness()
    try:
        await age_entry(h)
        before=len(h.render.texts)
        assert (await speech(h,route,text))['reason']=='exhibition_age_selected'
        assert h.state.reported_age==age and h.state.age_years==full
        assert h.state.age_band_source=='exhibition_voice'
        assert h.state.age_policy==policy_for_band(band,source='exhibition_voice').as_dict()
        assert trusted_canonical_age(h.state.snapshot())['age_years']==full
        context=h.app._bounded_dialogue_context(h.sid)
        assert context.profile.age==full and context.profile.age_band==band
        assert f'"age_band":"{band}"' in context.system_prompt
        assert h.state.dialogue_memory.bounded_turns[-1]['age']==age
        assert {int(k) for k,v in h.app.led.output.snapshot().items() if v}=={BUTTON_FILL_ELEMENTS[full]}
        if age != full:
            assert h.state.active_audio_id=='AGE_ACCEPTED'
            assert ('четыре с половиной' if full==4 else 'шесть с половиной') in h.render.texts[-1]
            assert len(h.render.texts)==before+1
            assert all('text' not in x for x in h.app.last_canonical_effects)
            await h.complete()
            assert h.state.active_audio_id=='PHRASE_107'
        else:
            assert h.state.active_audio_id=='PHRASE_107'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('route',['typed','recognized_pcm'])
async def test_ambiguous_age_uses_neutral_button_fallback_and_keeps_same_visit(route):
    h=ChildHarness()
    try:
        await age_entry(h)
        owner=h.state
        result=await speech(h,route,'4–5')
        assert result['reason']=='exhibition_age_clarification_started'
        assert h.state is owner and h.state.age_years is None
        assert h.state.active_audio_id=='AGE_CLARIFY'
        assert 'PHRASE_074' not in str(result['outputs'])
        assert (await h.button(4))['reason']=='exhibition_age_selected'
        assert h.state.age_years==4
    finally:
        await h.close()


def assert_provisional(h,name):
    assert h.state.pending_name==name and h.state.display_name is None
    assert not h.state.onboarding_name_collected
    assert not h.state.dialogue_memory.bounded_turns
    assert h.state.visit_profile().display_name is None
    assert name not in h.app._bounded_dialogue_context(h.sid).system_prompt
    assert name not in json.dumps(h.state.snapshot(),ensure_ascii=False)
    assert name not in json.dumps(h.app.last_canonical_effects,ensure_ascii=False)
    assert name not in json.dumps(h.app.last_canonical_event,ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('answer',['yes','button'])
async def test_asr_name_requires_current_question_terminal_and_explicit_new_answer(answer):
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm','Ома',.99)
        assert_provisional(h,'Ома')
        assert h.state.active_audio_id=='NAME_CONFIRMATION'
        assert 'Ома?' in h.render.texts[-1]
        old=dict(h.app._active_audio[h.sid])
        for payload in [dict(response_id='PHRASE_099',playback_lease=old['playback_lease']),
                        dict(response_id='NAME_CONFIRMATION',playback_lease='wrong')]:
            await h.send('AudioCompleted',payload)
            assert not h.state.name_confirmation_ready
        await h.button(1)
        assert_provisional(h,'Ома')
        await h.complete()
        if answer=='button':
            await h.button(1)  # Same held edge cannot answer the now-ready question.
            assert_provisional(h,'Ома')
            await h.release(1);await h.button(1)
        else:
            await speech(h,'recognized_pcm','Да')
        assert h.state.display_name=='Ома' and h.state.pending_name is None
        assert h.state.onboarding_name_collected
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Ома'}]
        assert h.state.mode=='exhibition_age_prompt'
        assert 'Ома' in h.app._bounded_dialogue_context(h.sid).system_prompt
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('correction',['Нет, меня зовут Рома','Нет, я Рома'])
async def test_confidently_wrong_asr_can_be_explicitly_corrected_without_committing_wrong_name(correction):
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm','Ома',.99);await h.complete()
        await speech(h,'recognized_pcm',correction,.99)
        assert_provisional(h,'Рома')
        assert h.state.name_confirmation_attempts==2
        await h.complete();await speech(h,'recognized_pcm','Да')
        assert h.state.display_name=='Рома'
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Рома'}]
        assert 'Ома' not in h.app._bounded_dialogue_context(h.sid).system_prompt
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_name_retries_are_bounded_and_refusal_stays_optional():
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm','Ома');await h.complete()
        await speech(h,'recognized_pcm','Нет')
        assert h.state.display_name is None and h.state.pending_name is None
        await h.complete()
        await speech(h,'recognized_pcm','Рома');await h.complete()
        await speech(h,'recognized_pcm','Меня зовут Саша')
        assert h.state.mode=='exhibition_age_prompt'
        assert h.state.display_name is None and h.state.pending_name is None
        assert not h.state.dialogue_memory.bounded_turns
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending',['skip','help','hangup','end','refusal','urgent'])
async def test_pending_name_is_cleared_by_navigation_and_never_reappears(ending):
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm','Ома')
        old=dict(h.app._active_audio[h.sid])
        original_state=h.state
        if ending=='skip':await h.button(7)
        elif ending=='help':await h.button(5)
        elif ending=='hangup':await h.send('HandsetPlaced')
        elif ending=='end':await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        elif ending=='urgent':
            from app.foundation.input_safety import InputSafetyDecision
            class Urgent:
                async def classify(self,text):return InputSafetyDecision('urgent','fixture',True)
            h.app.safety_classifier=Urgent()
            await speech(h,'recognized_pcm','Мне угрожают')
        else:await speech(h,'recognized_pcm','Нет, спасибо')
        current=h.app.canonical_runtime.sessions.get(h.sid,original_state)
        assert current.pending_name is None and current.display_name is None
        assert not current.onboarding_name_collected and not current.dialogue_memory.bounded_turns
        await h.send('AudioCompleted',dict(response_id='NAME_CONFIRMATION',**{k:old[k] for k in ('token','generation','playback_id','playback_lease')}))
        current=h.app.canonical_runtime.sessions.get(h.sid,original_state)
        assert current.pending_name is None and current.display_name is None
        assert 'Ома' not in h.app._canonical_audio_context(h.sid).system_prompt
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_typed_ingress_cannot_forge_asr_authority_with_client_flags():
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await h.send('SpeechReceived',{'_name_requires_confirmation':True,'_name_confident':False},text='Рома')
        assert h.state.display_name=='Рома' and h.state.pending_name is None
        assert h.state.mode=='exhibition_age_prompt'
    finally:
        await h.close()
