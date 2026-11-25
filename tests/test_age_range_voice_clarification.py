"""Owned age clarification through actual app ingress; recognition text is a fixture."""
import pytest
from test_child_button_access import ChildHarness
from test_onboarding_voice_slots import age_entry,speech

@pytest.mark.asyncio
@pytest.mark.parametrize('route',['typed','recognized_pcm'])
@pytest.mark.parametrize('text',['4–5','четыре–пять'])
async def test_range_gets_one_natural_question_then_healthy_voice_answer(route,text):
    h=ChildHarness()
    try:
        await age_entry(h)
        owner=h.state
        result=await speech(h,route,text)
        assert result['reason']=='exhibition_age_clarification_started'
        assert h.state is owner and h.state.age_years is None
        assert h.state.active_audio_id=='AGE_CLARIFY'
        assert h.render.texts[-1]=='Сколько тебе лет сейчас? Назови одно число.'
        assert {f'button_{i}' for i in range(1,8)} <= set(h.state.expected_inputs)
        await h.complete()
        assert 'speech' in h.state.expected_inputs and h.state.active_audio_id is None
        assert (await speech(h,'recognized_pcm','Мне пять лет'))['reason']=='exhibition_age_selected'
        assert h.state is owner and h.state.age_years==5
    finally:await h.close()

@pytest.mark.asyncio
async def test_range_question_budget_survives_unknown_and_repeated_range_without_audio_loop():
    h=ChildHarness()
    try:
        await age_entry(h)
        await speech(h,'recognized_pcm','4–5');await h.complete()
        asked=h.render.texts.count('Сколько тебе лет сейчас? Назови одно число.')
        await speech(h,'recognized_pcm','ээ')
        assert h.state.active_audio_id=='PHRASE_075'
        await h.complete()
        for _ in range(3):
            result=await speech(h,'recognized_pcm','четыре–пять')
            assert result['reason']=='exhibition_age_range_unresolved'
            assert h.state.age_years is None and h.state.reported_age is None
            assert h.state.active_audio_id is None and 'speech' in h.state.expected_inputs
            assert not h.state.dialogue_memory.bounded_turns
        assert h.render.texts.count('Сколько тебе лет сейчас? Назови одно число.')==asked==1
        await speech(h,'recognized_pcm','Мне шесть годиков')
        assert h.state.age_years==6
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('ending',['button','hangup','end','audio_failed','validator_denied'])
async def test_range_keeps_buttons_visit_and_owned_failure_or_end_fences(ending):
    from app.foundation.validation import ValidationDecision
    h=ChildHarness()
    class Blocked:
        async def validate(self,text):return ValidationDecision('block',False,'fixture',1.,'fixture')
    try:
        await age_entry(h)
        if ending=='validator_denied':
            validator=h.app.bounded_s2s.response_validator
            h.app.bounded_s2s.response_validator=Blocked()
        await speech(h,'recognized_pcm','4–5')
        if ending=='validator_denied':
            h.app.bounded_s2s.response_validator=validator
            assert h.state.active_audio_id is None and h.state.age_years is None
            await speech(h,'recognized_pcm','Четыре')
            assert h.state.age_years==4
            return
        old=dict(h.app._active_audio[h.sid])
        if ending=='button':
            await h.button(4)
            assert h.state.age_years==4
        elif ending=='hangup':await h.send('HandsetPlaced')
        elif ending=='end':await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        else:
            await h.complete(failed=True)
            assert 'speech' in h.state.expected_inputs and h.state.age_years is None
            await speech(h,'recognized_pcm','Мне пять')
            assert h.state.age_years==5
        version=h.app.canonical_runtime.sessions.get(h.sid).state_version if h.sid in h.app.canonical_runtime.sessions else None
        await h.send('AudioCompleted',dict(response_id='AGE_CLARIFY',**{k:old[k] for k in ('token','generation','playback_id','playback_lease')}))
        current=h.app.canonical_runtime.sessions.get(h.sid)
        assert (current.state_version if current else None)==version
    finally:await h.close()

@pytest.mark.asyncio
async def test_range_owned_pcm_closes_native_capture_then_current_terminal_opens_fresh_voice():
    import asyncio
    from dataclasses import replace
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    h=ChildHarness()
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application
    try:
        await age_entry(h)
        stand.application=h.app;stand.active_session_id=h.sid
        before=stand._headless_frame_owner()
        assert before[1]()
        await speech(h,'recognized_pcm','4–5')
        handle=h.played[-1]
        assert stand._pcm_owner_current(handle,None)
        assert not stand._pcm_owner_current(replace(handle,playback_lease='old'),None)
        assert not stand._headless_audio_accepting() and not before[1]()
        await h.send('AudioCompleted',{'response_id':'AGE_CLARIFY','playback_lease':'wrong'})
        assert not stand._headless_audio_accepting()
        await h.complete()
        assert stand._headless_audio_accepting()
        assert (await speech(h,'recognized_pcm','Пять'))['reason']=='exhibition_age_selected'
        assert h.state.age_years==5 and not stand._pcm_owner_current(handle,None)
    finally:
        stand.application=original
        await h.close();await stand.aclose()

@pytest.mark.asyncio
async def test_saved_actual_wrong_unmarked_name_exposes_limit_and_explicit_correction_recovers():
    """Saved actual CPU result replay, NOT a fresh model decode or child acceptance.

    Corrected eSpeak SHA bc1dfaf6a88edb4c7c685ca21ac29996e429dd77ca42e91c95e0817db92067ec.
    Source row004 espeak-name-roma-clean; model e76620f8; intact synthetic Рома→Ома.
    """
    import asyncio
    from app.foundation.config import Settings
    from app.foundation.s2s import STTResult
    from app.foundation.stand import HardwareIntegrationStand
    from test_game_audio_reminder_race import SPEECH,SILENCE
    class SavedActualResult:
        calls=0
        async def transcribe(self,audio,cancelled):
            self.calls+=1
            return STTResult('Ома.',0.5699511807787089,16434.28937399949,False,{})
    h=ChildHarness()
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application
    try:
        await h.name();await h.complete()
        stand.application=h.app;stand.active_session_id=h.sid
        # Advance the real CRC transport contiguously, without injecting scenario inputs.
        while stand.device_sequence < h.app.sessions.get(h.sid).last_sequence:
            await stand.inject('heartbeat',{},h.sid)
        stand._headless_audio_queue=asyncio.Queue(maxsize=64)
        h.app.s2s_audio.recognizer=SavedActualResult()
        owner=stand._headless_frame_owner()
        assert owner[1]()
        stand._enqueue_headless_audio_frame(SPEECH+SILENCE,owner)
        await stand._headless_audio_queue.put(None)
        await stand._headless_audio_worker()
        completions=tuple(h.app._audio_completions.get(h.sid,()))
        if completions:await asyncio.wait_for(asyncio.gather(*completions),2)
        assert h.app.s2s_audio.recognizer.calls==1
        # No observable uncertainty exists in this admitted saved receipt: do not
        # pretend a score can detect the wrong first phoneme or add fake alternatives.
        assert h.state.display_name=='Ома' and h.state.pending_name is None
        assert h.state.active_audio_id=='PHRASE_108'
        await h.complete()
        await speech(h,'recognized_pcm','Нет, я Рома',.99)
        assert h.state.mode=='exhibition_age_selection'
        assert h.state.display_name=='Рома' and h.state.pending_name is None
        assert h.state.dialogue_memory.bounded_turns==[{'kind':'name','value':'Рома'}]
    finally:
        stand.application=original
        await h.close();await stand.aclose()
