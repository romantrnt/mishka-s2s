"""Typed native-busy result through sync/pending app ingress; no real ASR/model."""
import asyncio
from copy import deepcopy

import pytest

from app.foundation.s2s import SpeechRecognizerBusyError,MockSpeechRecognizer
from test_child_button_access import ChildHarness
from test_onboarding_voice_slots import age_entry
from test_game_audio_reminder_race import SPEECH,SILENCE

BUSY_TEXT='Мне нужна секундочка. Можно нажать кнопку или сказать ещё раз чуть позже.'
class BusyRecognizer:
    async def transcribe(self,audio,cancelled):raise SpeechRecognizerBusyError('fixture occupied slots')

async def audio(h,pending=False):
    h.seq=max(h.seq,h.app.sessions.get(h.sid).last_sequence,h.state.last_sequence_by_source.get('uart_audio',0))+1
    result=await h.app.receive_audio_async(h.sid,SPEECH+SILENCE,h.seq,wait_for_turn=not pending)
    tasks=tuple(h.app._audio_completions.get(h.sid,()))
    if tasks:await asyncio.wait_for(asyncio.gather(*tasks),2)
    return result

@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['name','age'])
@pytest.mark.parametrize('pending',[False,True])
async def test_typed_busy_has_one_owned_system_message_and_preserves_private_choice(phase,pending):
    h=ChildHarness();updates=[]
    async def update(sid,payload):updates.append(payload)
    try:
        if phase=='age':await age_entry(h)
        else:await h.name();await h.complete()
        h.app.audio_update_hook=update
        h.app.s2s_audio.recognizer=BusyRecognizer()
        before=(h.state.mode,h.state.age_years,h.state.display_name,h.state.voice_retry_count,
                deepcopy(h.state.dialogue_memory.bounded_turns))
        result=await audio(h,pending)
        final=updates[-1] if pending and updates else result
        assert final['reason']=='audio_retry' and final['audio_status']=='stt_busy'
        assert any(x.get('action')=='audio_retry' and x.get('reason')=='stt_busy' for x in final['outputs'])
        assert h.state.active_audio_id=='STT_BUSY'
        assert h.render.texts[-1]==BUSY_TEXT and h.render.texts.count(BUSY_TEXT)==1
        assert (h.state.mode,h.state.age_years,h.state.display_name,h.state.voice_retry_count,
                h.state.dialogue_memory.bounded_turns)==before
        assert len(updates)==(1 if pending else 0)
        await h.complete()
        await audio(h,pending)
        assert h.render.texts.count(BUSY_TEXT)==1 and h.state.active_audio_id is None
        h.app.s2s_audio.recognizer=MockSpeechRecognizer('Мне шесть годиков' if phase=='age' else 'Рома',.99)
        await audio(h,pending)
        assert (h.state.age_years==6 if phase=='age' else h.state.pending_name=='Рома')
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('ending',['button','end','skip'])
async def test_old_pending_busy_cannot_speak_into_a_replaced_private_phase(ending):
    h=ChildHarness();entered=asyncio.Event();release=asyncio.Event();updates=[]
    class HeldBusy:
        async def transcribe(self,samples,cancelled):
            entered.set()
            await release.wait()
            raise SpeechRecognizerBusyError('fixture occupied slots')
    async def update(sid,payload):updates.append(payload)
    try:
        if ending=='skip':await h.name();await h.complete()
        else:await age_entry(h)
        h.app.audio_update_hook=update;h.app.s2s_audio.recognizer=HeldBusy()
        h.seq=max(h.seq,h.app.sessions.get(h.sid).last_sequence)+1
        result=await h.app.receive_audio_async(h.sid,SPEECH+SILENCE,h.seq,wait_for_turn=False)
        assert result['pending']
        await asyncio.wait_for(entered.wait(),2)
        if ending=='button':await h.button(4)
        elif ending=='skip':await h.button(7)
        else:await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        before=(h.state.state_version,h.state.mode,h.state.age_years,h.state.active_audio_id)
        release.set()
        tasks=tuple(h.app._audio_completions.get(h.sid,()))
        if tasks:await asyncio.wait_for(asyncio.gather(*tasks),2)
        assert (h.state.state_version,h.state.mode,h.state.age_years,h.state.active_audio_id)==before
        assert BUSY_TEXT not in h.render.texts and not updates
    finally:
        release.set();await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['name','age'])
@pytest.mark.parametrize('failed',[False,True])
async def test_busy_pcm_has_current_native_owner_and_terminal_reopens_same_choice(phase,failed):
    from dataclasses import replace
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    h=ChildHarness()
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application
    try:
        if phase=='age':await age_entry(h)
        else:await h.name();await h.complete()
        stand.application=h.app;stand.active_session_id=h.sid
        old=stand._headless_frame_owner()
        assert old[1]()
        h.app.s2s_audio.recognizer=BusyRecognizer()
        await audio(h)
        handle=h.played[-1]
        assert handle.response_id=='STT_BUSY' and stand._pcm_owner_current(handle,None)
        assert not stand._pcm_owner_current(replace(handle,playback_lease='old'),None)
        assert not stand._headless_audio_accepting() and not old[1]()
        await h.send('AudioCompleted',{'response_id':'STT_BUSY','playback_lease':'old'})
        assert not stand._headless_audio_accepting()
        await h.complete(failed)
        assert stand._headless_audio_accepting() and not stand._pcm_owner_current(handle,None)
        if phase=='age':
            await h.button(4);assert h.state.age_years==4
        else:
            await h.button(7);assert h.state.mode=='exhibition_age_prompt'
    finally:
        stand.application=original
        await h.close();await stand.aclose()

@pytest.mark.asyncio
async def test_busy_output_stays_validated_and_denial_leaves_buttons_and_recovery():
    from app.foundation.validation import ValidationDecision
    h=ChildHarness();checked=[]
    class Blocked:
        async def validate(self,text):
            checked.append(text)
            return ValidationDecision('block',False,'fixture',1.,'fixture')
    try:
        await age_entry(h)
        h.app.s2s_audio.recognizer=BusyRecognizer()
        before_texts=list(h.render.texts)
        h.app.bounded_s2s.response_validator=Blocked()
        result=await audio(h)
        assert result['audio_status']=='stt_busy' and result['reason']=='audio_retry'
        assert checked==[BUSY_TEXT] and h.render.texts==before_texts
        assert h.state.active_audio_id is None and h.state.age_years is None
        assert (await h.button(4))['reason']=='exhibition_age_selected'
        assert h.state.age_years==4
    finally:await h.close()

@pytest.mark.asyncio
async def test_other_modes_keep_visual_busy_without_fabricated_private_prompt():
    h=ChildHarness()
    try:
        await h.name();await h.button(7);await h.release(7);await h.button(4)
        h.state.mode='bear_dialogue';h.state.active_audio_id=h.state.playback_lease=None
        h.app._active_audio.pop(h.sid,None)
        h.app.s2s_audio.recognizer=BusyRecognizer()
        before=deepcopy(h.state.dialogue_memory.bounded_turns)
        result=await audio(h)
        assert result['reason']=='audio_retry' and result['audio_status']=='stt_busy'
        assert BUSY_TEXT not in h.render.texts and h.state.mode=='bear_dialogue'
        assert h.state.dialogue_memory.bounded_turns==before
    finally:await h.close()

@pytest.mark.asyncio
async def test_client_busy_payload_has_no_system_feedback_authority():
    h=ChildHarness()
    try:
        await age_entry(h)
        before=h.state.state_version
        result=await h.send('VoiceIntentReady',{'intent':'input_busy','safety_severity':'none','stt_busy_authorized':True})
        assert result['reason']=='stt_busy_untrusted'
        assert h.state.state_version==before and h.state.active_audio_id is None
        assert BUSY_TEXT not in h.render.texts
    finally:await h.close()
