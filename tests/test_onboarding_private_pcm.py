"""Current owned PCM and visit isolation; mocked recognizer/TTS, no physical device."""
import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from app.foundation.config import Settings
from app.foundation.s2s import CachedTTSProvider
from app.foundation.session_journal import SessionJournal
from app.foundation.stand import HardwareIntegrationStand
from test_child_button_access import ChildHarness
from test_onboarding_voice_slots import speech, age_entry


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['name','half_age'])
@pytest.mark.parametrize('ending',['end','new_visit'])
async def test_private_slot_pcm_has_current_owner_no_shared_cache_or_journal(tmp_path,kind,ending):
    h=ChildHarness()
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application
    try:
        if kind=='name':
            await h.name();await h.complete()
        else:
            await age_entry(h)
        journal=SessionJournal(root=tmp_path/'synthetic-journal',enabled=True)
        h.app._dialogue_journal=journal
        cache=CachedTTSProvider(h.render)
        h.app.bounded_s2s.tts=cache
        journal_before=deepcopy(journal._sessions)
        cache_before=deepcopy(cache.cache)
        await speech(h,'recognized_pcm','Ома' if kind=='name' else '4.5')
        assert journal._sessions==journal_before and cache.cache==cache_before
        assert not list(tmp_path.rglob('*.jsonl'))
        stand.application=h.app;stand.active_session_id=h.sid
        handle=h.played[-1]
        assert handle.response_id==('NAME_CONFIRMATION' if kind=='name' else 'AGE_ACCEPTED')
        assert stand._pcm_owner_current(handle,None)
        for bad in [replace(handle,token=handle.token+1),
                    replace(handle,generation=handle.generation+1),
                    replace(handle,playback_lease='old-lease')]:
            assert not stand._pcm_owner_current(bad,None)
        h.state.safety_severity='concern'
        assert not stand._pcm_owner_current(handle,None)
        h.state.safety_severity='none'
        if ending=='end':
            await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        else:
            await h.send('HandsetPlaced');await h.name();await h.complete()
        assert not stand._pcm_owner_current(handle,None)
        assert h.state.pending_name is None and h.state.reported_age is None
        version=h.state.state_version
        await h.send('AudioCompleted',dict(response_id=handle.response_id,token=handle.token,
            generation=handle.generation,playback_id=handle.playback_id,playback_lease=handle.playback_lease))
        assert h.state.state_version==version and h.state.display_name is None
        assert journal._sessions==journal_before and cache.cache==cache_before
    finally:
        stand.application=original
        await h.close();await stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['name','half_age'])
async def test_late_private_slot_render_after_end_cannot_play_into_new_visit(kind):
    h=ChildHarness()
    pending=None
    try:
        if kind=='name':
            await h.name();await h.complete()
        else:
            await age_entry(h)
        h.render.entered.clear();h.render.block=asyncio.Event()
        played_before=len(h.played)
        pending=asyncio.create_task(speech(h,'recognized_pcm','Ома' if kind=='name' else '4.5'))
        await asyncio.wait_for(h.render.entered.wait(),2)
        await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        h.render.block.set();await pending
        assert len(h.played)==played_before and not h.app._active_audio
        assert h.state.pending_name is None and h.state.reported_age is None
        await h.send('HandsetPlaced');await h.name();await h.complete()
        assert h.state.display_name is None and h.state.reported_age is None
    finally:
        if h.render.block:h.render.block.set()
        if pending:await asyncio.gather(pending,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_low_confidence_name_cannot_create_candidate_or_confirmation():
    h=ChildHarness()
    try:
        await h.name();await h.complete()
        result=await speech(h,'recognized_pcm','Ома',.2)
        assert h.state.pending_name is None and h.state.display_name is None
        assert result['reason']=='audio_retry'
        assert h.state.active_audio_id is None  # ASR gate rejects before canonical name capture.
        assert not h.state.dialogue_memory.bounded_turns
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['validator_denied','playback_failed'])
async def test_failed_name_confirmation_clears_candidate_and_keeps_real_age_fallback(failure):
    from app.foundation.validation import ValidationDecision
    h=ChildHarness()
    checked=[]
    class Blocked:
        async def validate(self,text):
            checked.append(text)
            return ValidationDecision('block',False,'fixture_block',1.,'fixture')
    try:
        await h.name();await h.complete()
        if failure=='validator_denied':h.app.bounded_s2s.response_validator=Blocked()
        await speech(h,'recognized_pcm','Ома')
        if failure=='playback_failed':await h.complete(failed=True)
        else:assert checked and 'Ома?' in checked[0]
        assert h.state.mode=='exhibition_age_prompt'
        assert h.state.pending_name is None and h.state.display_name is None
        assert not h.state.dialogue_memory.bounded_turns
        assert h.state.active_audio_id=='PHRASE_108'
        assert (await h.button(4))['reason']=='exhibition_age_selected'
        assert h.state.age_years==4
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['name_hint','name_confirmation','age_fallback'])
async def test_onboarding_output_closes_native_capture_and_only_fresh_post_terminal_frames_dispatch(kind):
    h=ChildHarness()
    stand=HardwareIntegrationStand(Settings(profile='mock',headless_turn_taking=True),background_scheduler=False)
    original=stand.application
    dispatched=[]
    try:
        if kind=='age_fallback':
            await age_entry(h)
        else:
            await h.name()
            if kind=='name_confirmation':await h.complete()
        stand.application=h.app;stand.active_session_id=h.sid;stand.hardware_mode='physical'
        stand._headless_audio_queue=asyncio.Queue(maxsize=64)
        if kind!='name_hint':
            before=stand._headless_frame_owner()
            assert before[1]()
            stand._enqueue_headless_audio_frame((.1,.2),before)
            await speech(h,'typed' if kind=='age_fallback' else 'recognized_pcm','4–5' if kind=='age_fallback' else 'Ома')
        assert h.state.active_audio_id=={'name_hint':'NAME_BUTTON_HINT','name_confirmation':'NAME_CONFIRMATION','age_fallback':'PHRASE_075'}[kind]
        assert not stand._interaction_audio_accepting()
        assert not stand._headless_audio_accepting()
        during=stand._headless_frame_owner()
        assert not during[1]()
        queued=stand._headless_audio_queue.qsize()
        stand._enqueue_headless_audio_frame((.5,.6),during)
        assert stand._headless_audio_queue.qsize()==queued
        old_playback=dict(h.app._active_audio[h.sid])
        await h.send('AudioCompleted',{'response_id':h.state.active_audio_id,'playback_lease':'wrong'})
        assert not stand._headless_audio_accepting()
        await h.complete()
        assert stand._headless_audio_accepting() and not during[1]()
        # A foreign/stale APP playback record is not the canonical output owner.
        h.app._active_audio[h.sid]={**old_playback,'session_id':'foreign'}
        assert stand._headless_audio_accepting()
        h.app._active_audio.pop(h.sid)
        fresh=stand._headless_frame_owner()
        assert fresh[1]()
        stand._enqueue_headless_audio_frame((.7,.8),fresh)
        async def capture(samples,rate,sid,**kwargs):
            dispatched.append((tuple(samples),kwargs['_ingress_guard']()))
            return {}
        stand.inject_pcm=capture
        await stand._headless_audio_queue.put(None)
        await stand._headless_audio_worker()
        assert len(dispatched)==1 and dispatched[0][0]==pytest.approx((.7,.8)) and dispatched[0][1]
    finally:
        stand.hardware_mode='simulator';stand.application=original
        await h.close();await stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['name_hint','name_confirmation'])
async def test_onboarding_capture_is_closed_during_synthesis_before_app_playback_registration(kind):
    h=ChildHarness()
    stand=HardwareIntegrationStand(Settings(profile='mock',headless_turn_taking=True),background_scheduler=False)
    original=stand.application
    pending=None
    try:
        await h.name(hint=kind!='name_hint')
        if kind=='name_confirmation':await h.complete()
        stand.application=h.app;stand.active_session_id=h.sid;stand.hardware_mode='physical'
        h.render.entered.clear();h.render.block=asyncio.Event()
        pending=asyncio.create_task(h.complete() if kind=='name_hint' else speech(h,'recognized_pcm','Ома'))
        await asyncio.wait_for(h.render.entered.wait(),2)
        assert h.state.active_audio_id==('NAME_BUTTON_HINT' if kind=='name_hint' else 'NAME_CONFIRMATION')
        assert h.state.playback_lease and h.sid not in h.app._active_audio
        assert not stand._interaction_audio_accepting() and not stand._headless_frame_owner()[1]()
        h.render.block.set();await pending
        assert h.sid in h.app._active_audio and not stand._interaction_audio_accepting()
        await h.complete()
        assert stand._interaction_audio_accepting()
    finally:
        if h.render.block:h.render.block.set()
        if pending:await asyncio.gather(pending,return_exceptions=True)
        stand.hardware_mode='simulator';stand.application=original
        await h.close();await stand.aclose()
