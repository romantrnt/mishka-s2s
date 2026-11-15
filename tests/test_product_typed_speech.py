import asyncio
import json
from datetime import datetime, timezone
from dataclasses import replace
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event, PlaybackHandle
from app.foundation.s2s import AudioChunk
from app.foundation.canonical_services import TypedInfoRenderer
from app.foundation.schedule_weather import ScheduleWeatherProviderError

class Harness:
    def __init__(self):
        self.app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
        self.seq=0
    async def send(self,kind,payload=None):
        self.seq+=1
        return await self.app.receive_async(Event(kind,'typed',self.seq,1.,'test',payload or {},f't:{self.seq}',1.))
    async def button(self,b):return await self.send('ButtonPressed',{'button_id':b})
    async def start(self):await self.send('HandsetLifted');await self.button(7)
    async def close(self):await self.app._cleanup_session_resources('typed')
    @property
    def state(self):return self.app.canonical_runtime.sessions['typed']
    def callback(self):
        a=self.app._active_audio['typed']
        return {**{k:a[k] for k in ('token','generation','playback_id','playback_lease')},'response_id':a['content_id']}

@pytest.mark.asyncio
@pytest.mark.parametrize('button,response',[(1,'TYPED_DATE'),(2,'TYPED_WEATHER')])
async def test_public_facts_reach_owned_audio_without_profile_or_text_diagnostics(button,response):
    h=Harness()
    try:
        await h.start()
        assert h.app.audio.commands[-1]['response_id']=='TYPED_MENU'
        result=await h.button(button)
        assert h.app.audio.commands[-1]['response_id']==response
        assert h.state.active_audio_id==response
        assert 'typed_data' not in repr(result)
        assert not h.app.bounded_s2s.tts.cache
        assert (await h.send('AudioCompleted',h.callback()))['reason']=='typed_information_finished'
        assert not h.state.active_audio_id and 'typed' not in h.app._active_audio
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('button,phrase',[(2,'PHRASE_100'),(3,'PHRASE_101'),(4,'PHRASE_101')])
async def test_unavailable_facts_are_audible_without_fake_information(button,phrase):
    h=Harness()
    try:
        await h.start();h.app.schedule_weather=None
        await h.button(button)
        assert h.app.audio.commands[-1]['response_id']==phrase
        assert h.state.mode=='schedule_weather'
    finally:await h.close()

@pytest.mark.asyncio
async def test_blocked_schedule_grant_has_recorded_fallback_not_silence():
    h=Harness()
    try:
        await h.start()
        h.state.profile_status='identified'
        h.app.optional_identity=True
        class Forbidden:
            def __getattr__(self,n):raise AssertionError('No schedule grant')
        h.app.schedule_weather=Forbidden()
        result=await h.button(3)
        assert result['outputs'][0]['resolution']=='typed_information_blocked_by_identity'
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_101'
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['HandsetPlaced','SessionReset','back','new_request','ButtonReleased'])
async def test_slow_tts_cannot_cross_owner_but_release_does_not_cancel_it(action):
    h=Harness();entered=asyncio.Event();release=asyncio.Event()
    try:
        await h.start()
        original=h.app.bounded_s2s.tts.delegate.synthesize
        async def delayed(text,token,cancelled):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
            return await original(text,token,cancelled)
        h.app.bounded_s2s.tts.delegate.synthesize=delayed
        h.app.audio.commands.clear()
        task=asyncio.create_task(h.button(1));await entered.wait()
        if action=='back':await h.button(7)
        elif action=='new_request':
            # A request with no private grant changes the lease to its own fallback.
            await h.button(3)
        else:await h.send(action,{'button_id':1} if action=='ButtonReleased' else None)
        release.set();result=await task
        ids=[x['response_id'] for x in h.app.audio.commands]
        assert ('TYPED_DATE' in ids)==(action=='ButtonReleased'),(ids,result)
    finally:release.set();await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('generation',99),('playback_id','other'),('playback_lease','other')])
async def test_typed_callbacks_require_exact_owner(field,value):
    h=Harness()
    try:
        await h.start();await h.button(1)
        payload=h.callback();payload[field]=value
        await h.send('AudioCompleted',payload)
        assert h.state.active_audio_id=='TYPED_DATE'
    finally:await h.close()

@pytest.mark.asyncio
async def test_stale_typed_callback_cannot_stop_new_recorded_help():
    h=Harness()
    try:
        await h.start();await h.button(1);old=h.callback()
        await h.button(5)
        await h.send('AudioCompleted',old)
        assert h.state.mode=='wellbeing_check'
        assert h.app._active_audio['typed']['content_id']=='PHRASE_084'
    finally:await h.close()

@pytest.mark.asyncio
async def test_schedule_selects_dated_active_or_next_not_array_first_last():
    now=datetime(2026,11,15,10,tzinfo=timezone.utc)
    class Service:
        async def schedule(self,*a,**kw):return [
            {'time':'09:00','title':'Past','status':'completed','start_at':'2026-11-15T09:00:00Z'},
            {'time':'18:00','title':'Cancelled','status':'cancelled','start_at':'2026-11-15T18:00:00Z'},
            {'time':'12:00','title':'Next','status':'scheduled','start_at':'2026-11-15T12:00:00Z'},
            {'time':'09:30','title':'Current','status':'in_progress','start_at':'2026-11-15T09:30:00Z'},
            {'time':'11:00','title':'Soonest','status':'scheduled','start_at':'2026-11-15T11:00:00Z'}]
    r=TypedInfoRenderer(Service(),{},'ref',now=lambda:now)
    assert 'Soonest' in (await r.render('what_next'))['text']
    assert 'Current' in (await r.render('where_now'))['text']

@pytest.mark.asyncio
async def test_undated_array_cannot_claim_current_destination():
    class Service:
        async def schedule(self,*a,**kw):return [{'time':'18:00','title':'Undated destination'}]
    r=TypedInfoRenderer(Service(),{},'ref')
    for kind in ('what_next','where_now'):
        with pytest.raises(ScheduleWeatherProviderError):await r.render(kind)

@pytest.mark.asyncio
@pytest.mark.parametrize('response,authorized',[('TYPED_DATE',False),('TYPED_WEATHER',False),('TYPED_WHAT_NEXT',True)])
async def test_actual_headless_pcm_port_emits_correct_owned_completion(response,authorized):
    from test_owned_private_pcm import setup,install,start,HANDLE,AUDIO
    stand=await setup()
    try:
        state=stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode='schedule_weather'
        state.identity_capabilities=['schedule'] if authorized else []
        if not authorized:state.profile_ref=None;state.identity_request_id=None
        handle=replace(HANDLE,response_id=response)
        install(stand,handle)
        await asyncio.gather(*start(stand,handle,AUDIO))
        assert len(stand.events)==1
        assert stand.events[0].payload['response_id']==response
        assert stand.events[0].event_type=='AudioCompleted'
        assert stand.playback.played
    finally:await stand.aclose()

@pytest.mark.asyncio
async def test_private_typed_pcm_requires_live_schedule_scope():
    from test_owned_private_pcm import setup,install,start,HANDLE,AUDIO
    stand=await setup()
    try:
        state=stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode='schedule_weather';state.identity_capabilities=['personalize']
        handle=replace(HANDLE,response_id='TYPED_WHAT_NEXT');install(stand,handle)
        await asyncio.gather(*start(stand,handle,AUDIO))
        assert not stand.playback.played and not stand.events
    finally:await stand.aclose()

@pytest.mark.asyncio
async def test_typed_pcm_requires_handset_up_even_with_matching_lease():
    from test_owned_private_pcm import setup,install,start,HANDLE,AUDIO
    stand=await setup()
    try:
        state=stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode='schedule_weather';state.handset='down'
        handle=replace(HANDLE,response_id='TYPED_DATE');install(stand,handle)
        await asyncio.gather(*start(stand,handle,AUDIO))
        assert not stand.playback.played and not stand.events
    finally:await stand.aclose()

@pytest.mark.asyncio
async def test_cancellation_resistant_tts_jobs_remain_bounded():
    h=Harness();entered=asyncio.Event();release=asyncio.Event();calls=[];pending=[]
    try:
        await h.start()
        original=h.app.bounded_s2s.tts.delegate.synthesize
        async def delayed(text,token,cancelled):
            calls.append(text);entered.set()
            while not release.is_set():
                try:await release.wait()
                except asyncio.CancelledError:pass
            return await original(text,token,cancelled)
        h.app.bounded_s2s.tts.delegate.synthesize=delayed
        for _ in range(2):
            entered.clear();pending.append(asyncio.create_task(h.button(1)))
            await asyncio.wait_for(entered.wait(),2)
        result=await h.button(1)
        assert len(calls)==2 and len(h.app._owned_text_jobs)==2
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_100'
        release.set();await asyncio.gather(*pending)
        await asyncio.sleep(0)
        assert not h.app._owned_text_jobs
        assert h.app.audio.commands[-1]['response_id']=='PHRASE_100'
    finally:
        release.set();await asyncio.gather(*pending,return_exceptions=True);await h.close()

@pytest.mark.asyncio
async def test_dated_schedule_controls_spoken_time_and_labels_future_day():
    now=datetime(2026,11,15,10,tzinfo=timezone.utc)
    class Service:
        async def schedule(self,*args,**kwargs):return [
            {'time':'WRONG','title':'Appointment','status':'scheduled','start_at':'2026-11-16T12:30:00+03:00','minutes_until':1}]
    r=TypedInfoRenderer(Service(),{},'ref',now=lambda:now)
    text=(await r.render('what_next'))['text']
    assert '16.11.2026' in text and '09:30' in text
    assert 'WRONG' not in text and 'через 1' not in text

@pytest.mark.asyncio
async def test_onhook_day_menu_is_silent_and_announced_after_lift():
    h=Harness()
    try:
        result=await h.button(7)
        assert result['reason']=='button_mode' and h.state.mode=='schedule_weather'
        assert not h.app.audio.commands and not h.state.active_audio_id
        result=await h.send('HandsetLifted')
        assert result['reason']=='typed_menu_after_handset'
        assert h.app.audio.commands[-1]['response_id']=='TYPED_MENU'
    finally:await h.close()
