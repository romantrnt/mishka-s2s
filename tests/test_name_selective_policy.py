"""Selective name policy: synthetic replies/native STT receipts, no real child data."""
import pytest
from test_child_button_access import ChildHarness
from test_onboarding_voice_slots import speech

@pytest.mark.asyncio
@pytest.mark.parametrize('text,name',[('Рома','Рома'),('Меня зовут Рома Синтетический','Рома'),('Ли','Ли'),('Меня зовут Ли Синтетический','Ли')])
async def test_clear_name_has_same_policy_for_bare_prefixed_short_and_surname(text,name):
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        result=await speech(h,'recognized_pcm',text,.99)
        assert result['reason']=='exhibition_age_question_started'
        assert h.state.display_name==name and h.state.pending_name is None
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':name}]
        assert h.state.active_audio_id=='PHRASE_108'
    finally:await h.close()

@pytest.mark.asyncio
async def test_explicit_name_correction_during_age_selection_keeps_visit_and_age_question_answerable():
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'typed','Рома');await h.complete()
        owner=h.state
        result=await speech(h,'recognized_pcm','Нет, меня зовут Саша Синтетический',.99)
        assert result['reason']=='exhibition_name_corrected'
        assert h.state is owner and h.state.mode=='exhibition_age_selection'
        assert h.state.display_name=='Саша' and h.state.age_years is None
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Саша'}]
        assert h.state.active_audio_id is None
        await speech(h,'recognized_pcm','Мне четыре')
        assert h.state.age_years==4
    finally:await h.close()

@pytest.mark.asyncio
async def test_uncertain_name_question_has_owned_output_and_listening_lights():
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    from app.foundation.lighting import BUTTON_DIGIT_ELEMENTS
    h=ChildHarness()
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    h.app.led=stand.application.led
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm','Кажется, Рома',.99)
        assert h.state.pending_name=='Рома' and h.state.display_name is None
        assert h.app.led.effect_snapshot()['phase']=='output'
        await h.complete()
        assert h.app.led.effect_snapshot()['phase']=='listening'
        assert {'button_1','button_5','button_7'} <= set(h.state.expected_inputs)
        assert h.app.led.output.snapshot()[str(BUTTON_DIGIT_ELEMENTS[1][1])]==1
    finally:
        await h.close();await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('text',['Рома или Саша','Рома…','Меня зовут наверное Рома','Меня зовут кажется, Рома','Наверное, меня зовут Рома'])
async def test_only_observable_uncertainty_asks_once_and_keeps_first_name_private(text):
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm',text,.99)
        assert h.state.pending_name=='Рома' and h.state.display_name is None
        assert h.state.active_audio_id=='NAME_CONFIRMATION'
        assert not h.state.dialogue_memory.bounded_turns
        assert 'Рома' not in h.app._bounded_dialogue_context(h.sid).system_prompt
        await h.complete()
        await speech(h,'recognized_pcm','Да')
        assert h.state.display_name=='Рома' and h.state.active_audio_id=='PHRASE_108'
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Рома'}]
    finally:await h.close()

@pytest.mark.asyncio
async def test_confirmation_early_key_stale_terminal_and_fast_post_terminal_correction():
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    h=ChildHarness();stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application;h.app.led=stand.application.led
    try:
        await h.name();await h.complete()
        stand.application=h.app;stand.active_session_id=h.sid
        await speech(h,'recognized_pcm','Кажется, Рома',.99)
        assert stand._enabled_buttons(h.app.sessions.get(h.sid))==[5,7]
        assert not stand._headless_audio_accepting()
        old=dict(h.app._active_audio[h.sid])
        await h.button(1)
        assert h.state.display_name is None
        await h.send('AudioCompleted',{'response_id':'NAME_CONFIRMATION','playback_lease':'old'})
        assert not h.state.name_confirmation_ready
        await h.complete()
        assert stand._enabled_buttons(h.app.sessions.get(h.sid))==[1,5,7]
        assert stand._headless_audio_accepting()
        await h.button(1)  # Held early key cannot answer the newly-ready question.
        assert h.state.display_name is None
        await h.release(1)
        await speech(h,'recognized_pcm','Нет')
        assert h.state.pending_name is None and h.state.active_audio_id is None
        assert stand._headless_audio_accepting()
        assert stand._enabled_buttons(h.app.sessions.get(h.sid))==[5,7]
        await speech(h,'recognized_pcm','Меня зовут Саша Синтетический',.99)
        assert h.state.display_name=='Саша' and h.state.active_audio_id=='PHRASE_108'
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Саша'}]
        assert h.render.texts.count('Я верно услышал: Рома? Скажи да или нажми один, поправь меня или пропусти — семь.')==1
        current=h.state.active_audio_id
        await h.send('AudioCompleted',dict(response_id='NAME_CONFIRMATION',**{k:old[k] for k in ('token','generation','playback_id','playback_lease')}))
        assert h.state.active_audio_id==current
    finally:
        stand.application=original;await h.close();await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['yes','skip','failure'])
async def test_uncertain_age_phase_name_correction_returns_to_same_age_without_reasking(terminal):
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'typed','Рома Синтетический');await h.complete()
        owner=h.state
        old_context=h.app._bounded_dialogue_context(h.sid)
        await speech(h,'recognized_pcm','Нет, меня зовут наверное Саша Синтетический',.99)
        assert h.state.mode=='exhibition_name_capture' and h.state.pending_name=='Саша'
        assert h.state.display_name is None and not h.state.onboarding_name_collected
        assert not h.state.dialogue_memory.bounded_turns
        assert 'Рома' not in h.app._bounded_dialogue_context(h.sid).system_prompt
        assert 'Саша' not in h.app._bounded_dialogue_context(h.sid).system_prompt
        if terminal=='skip':await h.button(7);await h.release(7)
        elif terminal=='failure':await h.complete(failed=True)
        else:await h.complete();await speech(h,'recognized_pcm','Да')
        assert h.state is owner and h.state.mode=='exhibition_age_selection'
        assert h.state.active_audio_id is None and h.state.age_years is None
        assert h.state.display_name==('Саша' if terminal=='yes' else None)
        assert h.state.dialogue_memory.bounded_turns==([{'kind':'name','value':'Саша'}] if terminal=='yes' else [])
        assert 'Рома' not in h.app._bounded_dialogue_context(h.sid).system_prompt
        assert 'Синтетический' not in h.app._bounded_dialogue_context(h.sid).system_prompt
        await speech(h,'recognized_pcm','Мне четыре')
        assert h.state.age_years==4
    finally:await h.close()

@pytest.mark.asyncio
async def test_repeated_uncertainty_is_bounded_and_ordinary_age_never_becomes_name():
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm','Кажется, Рома',.99);await h.complete()
        await speech(h,'recognized_pcm','Кажется, Саша',.99)
        assert h.state.active_audio_id=='PHRASE_108' and h.state.display_name is None
        assert h.state.pending_name is None and not h.state.dialogue_memory.bounded_turns
        await h.complete();await speech(h,'recognized_pcm','Четыре')
        assert h.state.age_years==4 and h.state.display_name is None
    finally:await h.close()

@pytest.mark.asyncio
async def test_explicit_correction_refreshes_existing_private_context_without_erasing_turn_budget():
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'typed','Рома Синтетический');await h.complete()
        context=h.app._bounded_dialogue_context(h.sid)
        original_started=context.started_at
        original_history=list(context.history)
        assert 'Рома' in context.system_prompt
        await speech(h,'recognized_pcm','Нет, меня зовут Саша Синтетический',.99)
        assert h.app.conversation_contexts[h.sid] is context
        assert 'Рома' not in context.system_prompt
        assert 'Синтетический' not in context.system_prompt
        assert 'Саша' in context.system_prompt
        assert context.started_at==original_started and context.history==original_history
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Саша'}]
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('text',['Меня зовут, наверное, Рома','Рома, наверное','Меня зовут Рома, кажется'])
async def test_uncertainty_marker_is_read_from_name_body_not_unrelated_clause(text):
    h=ChildHarness()
    try:
        await h.name();await h.complete();await speech(h,'recognized_pcm',text,.99)
        assert h.state.pending_name=='Рома' and h.state.display_name is None
        assert h.state.active_audio_id=='NAME_CONFIRMATION'
    finally:await h.close()

@pytest.mark.asyncio
async def test_unrelated_later_clause_is_not_uncertainty_about_name():
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        await speech(h,'recognized_pcm','Рома, наверное пойду играть',.99)
        assert h.state.display_name=='Рома' and h.state.pending_name is None
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain',[False,True])
async def test_native_crc_first_name_policy_and_fresh_voice_after_current_question(uncertain):
    import asyncio
    from app.foundation.config import Settings
    from app.foundation.s2s import STTResult
    from app.foundation.stand import HardwareIntegrationStand
    from test_game_audio_reminder_race import SPEECH,SILENCE
    h=ChildHarness();stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application;h.app.led=stand.application.led
    calls=[]
    async def native_reply(text):
        class Result:
            async def transcribe(self,samples,cancelled):
                calls.append(text)
                return STTResult(text,.99,0.,False,{})
        h.app.s2s_audio.recognizer=Result()
        while stand.device_sequence<h.app.sessions.get(h.sid).last_sequence:
            await stand.inject('heartbeat',{},h.sid)
        stand._headless_audio_queue=asyncio.Queue(maxsize=64)
        owner=stand._headless_frame_owner();assert owner[1]()
        stand._enqueue_headless_audio_frame(SPEECH+SILENCE,owner)
        await stand._headless_audio_queue.put(None);await stand._headless_audio_worker()
        tasks=tuple(h.app._audio_completions.get(h.sid,()))
        if tasks:await asyncio.wait_for(asyncio.gather(*tasks),2)
    try:
        await h.name();await h.complete()
        stand.application=h.app;stand.active_session_id=h.sid
        await native_reply('Меня зовут кажется Рома Синтетический' if uncertain else 'Рома Синтетический')
        if uncertain:
            assert h.state.pending_name=='Рома' and h.state.display_name is None
            assert stand._enabled_buttons(h.app.sessions.get(h.sid))==[5,7]
            assert not stand._headless_audio_accepting()
            await h.complete()
            assert stand._enabled_buttons(h.app.sessions.get(h.sid))==[1,5,7]
            await native_reply('Да')
        assert h.state.display_name=='Рома' and h.state.pending_name is None
        assert h.state.active_audio_id=='PHRASE_108'
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Рома'}]
        assert all('Синтетический' not in text for text in h.render.texts)
        assert len(calls)==(2 if uncertain else 1)
    finally:
        stand.application=original;await h.close();await stand.aclose()
