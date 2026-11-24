"""Actual local WS/admission/gesture cleanup; synthetic events/PCM, no devices."""
import asyncio
import copy
import httpx
import pytest
from app.foundation.domain import Event
from app.foundation.s2s import AudioChunk
from test_browser_capture_lifecycle import BrowserCapture, until
from test_contextual_game_flow import Model, ApprovedActivityRequests
from app.foundation.activity_requests import ActivityRequestGate

class PCM:
    async def synthesize(self, text, token, cancelled):
        return AudioChunk((.1,) * 2400, 24000, token)
    async def convert_private(self, audio, cancelled): return audio

async def setup():
    h = await BrowserCapture().start()
    h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = PCM()
    h.stand._admits_physical_users = h.api.state.lifecycle.admits_users
    h.seq = 0
    return h

async def event(h, kind, payload=None, text=None):
    h.seq += 1
    return await h.app.receive_async(Event(kind, h.sid, h.seq, 1., 'ownership-fixture', payload or {}, str(h.seq)), text)

async def done(h):
    lease = h.app._active_audio[h.sid]
    return await event(h, 'AudioCompleted', {**lease, 'response_id':lease['content_id']})

async def button(h, ws, key, edge, gesture=None, cancelled=False):
    h.seq += 1; request = f'request-{h.seq}'
    payload={'type':'simulator_action','action':'button','button_id':key,'press':edge,
             'epoch':h.stand.remote_devices.epoch,'request_id':request}
    if gesture is not None: payload['gesture_id']=gesture
    if cancelled: payload['cancelled']=True
    ws.messages.put_nowait(payload)
    await until(lambda:any(x.get('type')=='button_ack' and x.get('request_id')==request
        and x.get('stage') in {'completed','rejected'} for x in ws.sent))
    return next(x for x in ws.sent if x.get('type')=='button_ack'
                and x.get('request_id')==request and x.get('stage')!='queued')

@pytest.mark.asyncio
async def test_same_epoch_disconnect_cancels_owned_down_and_first_fresh_tap_works():
    h=await setup()
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='wellbeing_check';s.support_question_kind='pain_or_discomfort'
        old,task=await h.connect();epoch=h.stand.remote_devices.epoch
        await button(h,old,1,'press','old')
        assert s.support_question_kind=='fear' and s.accepted_buttons_down==[1]
        old.messages.put_nowait(None);await asyncio.wait_for(task,3)
        assert s.accepted_buttons_down==[] and h.stand.remote_devices.epoch==epoch
        fresh,_=await h.connect()
        ack=await button(h,fresh,1,'press','fresh');await button(h,fresh,1,'release','fresh')
        assert ack['stage']=='completed' and s.mode=='root_menu' and not s.accepted_buttons_down
    finally: await h.close()

@pytest.mark.asyncio
async def test_browser_cancel_and_disconnect_never_clear_physical_input_owner():
    h=await setup()
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='wellbeing_check';s.support_question_kind='pain_or_discomfort'
        ws,task=await h.connect()
        await h.stand.inject_physical({'type':'ButtonPressed','button_id':1,'_source':'physical_hid','_sequence':100})
        assert s.support_question_kind=='fear' and s.accepted_buttons_down==[1]
        await button(h,ws,1,'press','not-physical')
        ack=await button(h,ws,1,'release','not-physical',True)
        assert ack['reason']=='button_gesture_not_owned' and s.accepted_buttons_down==[1]
        ws.messages.put_nowait(None);await asyncio.wait_for(task,3)
        assert s.accepted_buttons_down==[1]
        await h.stand.inject_physical({'type':'ButtonReleased','button_id':1,'_source':'physical_hid','_sequence':101})
        assert not s.accepted_buttons_down
        assert '_button_gesture_owners' not in s.snapshot()
    finally: await h.close()

@pytest.mark.asyncio
async def test_old_cancel_does_not_release_new_same_binding_gesture():
    h=await setup()
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='wellbeing_check';s.support_question_kind='pain_or_discomfort'
        ws,_=await h.connect()
        await button(h,ws,1,'press','old');await button(h,ws,1,'release','old',True)
        await button(h,ws,1,'press','new')
        before=s.state_version
        ack=await button(h,ws,1,'release','old',True)
        assert ack['reason']=='button_gesture_not_owned' and s.accepted_buttons_down==[1]
        assert s.state_version==before and s.mode=='root_menu'
        await button(h,ws,1,'release','new');assert not s.accepted_buttons_down
    finally: await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_first', [False, True])
async def test_completed_or_cancelled_gesture_cannot_late_replay_a_press(cancel_first):
    h=await setup()
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='wellbeing_check';s.support_question_kind='pain_or_discomfort'
        ws,_=await h.connect()
        if not cancel_first:
            await button(h,ws,1,'press','once')
        await button(h,ws,1,'release','once',True)
        before=s.state_version; question=s.support_question_kind
        ack=await button(h,ws,1,'press','once')
        assert ack['reason']=='button_gesture_completed'
        assert s.state_version==before and s.support_question_kind==question and not s.accepted_buttons_down
    finally: await h.close()

@pytest.mark.asyncio
async def test_legacy_ws_without_gesture_keeps_real_press_release_and_disconnect_cleanup():
    h=await setup()
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='wellbeing_check';s.support_question_kind='pain_or_discomfort'
        ws,task=await h.connect()
        await button(h,ws,1,'press');await button(h,ws,1,'release')
        assert s.support_question_kind=='fear' and not s.accepted_buttons_down
        await button(h,ws,5,'press');assert s.accepted_buttons_down==[5]
        ws.messages.put_nowait(None);await asyncio.wait_for(task,3)
        assert not s.accepted_buttons_down and s.mode=='wellbeing_check'
    finally: await h.close()

@pytest.mark.asyncio
async def test_queued_ack_is_not_admission_and_revoked_queued_press_cannot_reappear():
    h=await setup();entered,release=asyncio.Event(),asyncio.Event()
    original=h.stand.remote_devices.action
    async def blocked(payload,**kwargs):
        if payload.get('gesture_id')=='old':
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
        return await original(payload,**kwargs)
    h.stand.remote_devices.action=blocked
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='wellbeing_check';s.support_question_kind='pain_or_discomfort'
        old,task=await h.connect()
        old.messages.put_nowait({'type':'simulator_action','action':'button','button_id':1,'press':'press','gesture_id':'old','request_id':'old','epoch':0})
        await asyncio.wait_for(entered.wait(),2)
        assert any(x.get('stage')=='queued' for x in old.sent)
        assert not any(x.get('stage')=='completed' for x in old.sent)
        assert not s.accepted_buttons_down and s.support_question_kind=='pain_or_discomfort'
        old.messages.put_nowait({'type':'simulator_action','action':'button','button_id':1,'press':'press','gesture_id':'queued','epoch':0})
        old.messages.put_nowait(None)
        await until(lambda:not h.stand.hub.is_owner(old))
        fresh,_=await h.connect();await button(h,fresh,1,'press','fresh');await button(h,fresh,1,'release','fresh')
        assert s.support_question_kind=='fear'
        release.set();await asyncio.wait_for(task,3)
        assert s.support_question_kind=='fear' and not s.accepted_buttons_down
    finally: release.set();await h.close()

@pytest.mark.asyncio
async def test_actual_ws_ready_seven_disconnect_cancels_then_short_fresh_seven_stays_in_game():
    h=await setup()
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.age_band='6-7'
        clock=[10.];h.app.monotonic_now=h.app.canonical_runtime.monotonic_now=lambda:clock[0]
        h.app.bounded_s2s.llm=Model('<action type="offer_game" game_id="quiz"/>')
        h.app.bounded_s2s.activity_request_gate=ActivityRequestGate(ApprovedActivityRequests())
        old,task=await h.connect()  # Owned PCM needs an admitted browser output binding.
        await event(h,'SpeechReceived',text='Хочу поиграть в викторину.')
        assert s.pending_game_offer=='quiz';await done(h)
        await event(h,'SpeechReceived',text='Да, давай.')
        await done(h);await done(h)
        assert s.mode=='game_active' and s.game_audio_stage=='playing'
        await button(h,old,7,'press','old-seven')
        for _ in range(7):
            clock[0]=h.app._game_timer_scopes[h.sid][-1];await h.app.flush_game_timers()
        assert s.game_engine_state.seven_exit_phase=='ready'
        old.messages.put_nowait(None);await asyncio.wait_for(task,3)
        assert s.mode=='game_active' and s.game_engine_state.seven_exit_phase=='idle'
        fresh,_=await h.connect()
        await button(h,fresh,7,'press','fresh-seven');await button(h,fresh,7,'release','fresh-seven')
        assert s.mode=='game_active' and s.game_engine_state.seven_exit_phase=='idle'
        assert not s.accepted_buttons_down
    finally: await h.close()

@pytest.mark.asyncio
async def test_cancel_resistant_button_tts_cannot_publish_after_binding_revoked():
    h=await setup();entered,release=asyncio.Event(),asyncio.Event()
    class LatePCM(PCM):
        async def synthesize(self, text, token, cancelled):
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
            return await super().synthesize(text,token,cancelled)
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='root_menu'
        h.app.bounded_s2s.tts=LatePCM()
        old,task=await h.connect()
        old.messages.put_nowait({'type':'simulator_action','action':'button','button_id':6,
            'press':'press','gesture_id':'late-audio','request_id':'late-audio','epoch':0})
        await asyncio.wait_for(entered.wait(),3)
        old.messages.put_nowait(None)
        await until(lambda:not h.stand.hub.is_owner(old))
        fresh,_=await h.connect()
        release.set();await asyncio.wait_for(task,3);await asyncio.sleep(0)
        assert not any(x.get('type')=='audio_pcm_output' for x in old.sent+fresh.sent)
        assert not h.app._active_audio and not s.accepted_buttons_down
        assert not h.stand._revoked_button_owners
    finally: release.set();await h.close()

@pytest.mark.asyncio
async def test_http_gesture_validation_and_owner_forgery_do_not_touch_physical_hold():
    h=await setup()
    try:
        s=h.app.canonical_runtime.sessions[h.sid];s.mode='wellbeing_check';s.support_question_kind='pain_or_discomfort'
        await h.stand.inject_physical({'type':'ButtonPressed','button_id':1,'_source':'physical_hid','_sequence':100})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.api),base_url='http://local',
            headers={'x-stand-token':'capture-lifecycle-test'}) as client:
            for gesture in ('', 'bad / id', 'x'*81, None, 1):
                response=await client.post('/stand/simulator/action',json={'action':'button','button_id':1,
                    'press':'release','cancelled':True,'gesture_id':gesture,'epoch':0})
                assert response.json()['error']['code']=='validation_error'
            response=await client.post('/stand/simulator/action',json={'action':'button','button_id':1,
                'press':'release','cancelled':True,'gesture_id':'forged','input_owner':'physical:0:physical_hid','epoch':0})
            assert response.json()['error']['code']=='validation_error'
            response=await client.post('/stand/button-released',json={'button_id':1,'cancelled':True,
                'gesture_id':'forged','_owner':'physical:0:physical_hid','source':'physical_hid'})
            assert response.json()['reason']=='button_gesture_not_owned'
        assert s.accepted_buttons_down==[1]
        await h.stand.inject_physical({'type':'ButtonReleased','button_id':1,'_source':'physical_hid','_sequence':101})
        assert not s.accepted_buttons_down
        cloned=copy.deepcopy(s)
        assert cloned._button_gesture_owners==s._button_gesture_owners
        assert not any(key.startswith('_button_gesture') for key in s.snapshot())
    finally: await h.close()

def test_gesture_receipts_are_bounded_inert_and_legacy_edges_still_work():
    from app.foundation.canonical_contract import CanonicalRuntime, CanonicalState, CanonicalEvent
    runtime=CanonicalRuntime();state=CanonicalState('bounded',mode='root_menu',handset='up')
    runtime.sessions[state.session_id]=state
    def edge(kind,button):
        return CanonicalEvent(kind,state.session_id,1,0,'legacy_uart',{'button_id':button})
    for button_id in range(1,8):
        for index in range(20):
            with runtime.button_input_scope('browser_ws:synthetic',f'{button_id}:{index}'):
                assert runtime._track_button_gesture(state,edge('ButtonPressed',button_id))==(True,None)
                assert runtime._track_button_gesture(state,edge('ButtonReleased',button_id))==(False,None)
    assert len(state._button_gesture_terminals)==7
    assert all(len(recent)==2 for recent in state._button_gesture_terminals.values())
    assert not state._button_gesture_owners and not state.accepted_buttons_down
    assert runtime._track_button_gesture(state,edge('ButtonPressed',1))==(True,None)
    assert runtime._track_button_gesture(state,edge('ButtonReleased',1))==(False,None)
    assert copy.deepcopy(state)._button_gesture_terminals==state._button_gesture_terminals

@pytest.mark.asyncio
async def test_profile_replacement_carries_held_owner_and_reset_discards_receipts():
    from test_max_fresh_ready import Harness
    h=Harness();runtime=h.app.canonical_runtime
    try:
        await h.send('HandsetLifted')
        with runtime.button_input_scope('browser_ws:profile','old'):
            await h.send('ButtonPressed',{'button_id':7})
        old=h.state
        assert old.accepted_buttons_down==[7]
        await h.ready()
        assert h.state is not old and h.state.accepted_buttons_down==[7]
        with runtime.button_input_scope('browser_ws:other','old'):
            assert (await h.send('ButtonReleased',{'button_id':7,'cancelled':True}))['reason']=='button_gesture_not_owned'
        with runtime.button_input_scope('browser_ws:profile','old'):
            await h.send('ButtonReleased',{'button_id':7,'cancelled':True})
        assert not h.state.accepted_buttons_down and not h.state.readiness_held_buttons
        assert h.state._button_gesture_terminals[7]==[('browser_ws:profile','old')]
        await h.app.reset_session('software')
        fresh=runtime.sessions.get('software')
        assert fresh is None or (not fresh.accepted_buttons_down and not getattr(fresh,'_button_gesture_owners',{}))
    finally:
        await h.boundary.close();await h.app.shutdown_identity()
        await h.app._cleanup_session_resources('software');h.led.cancel_effects(clear=True)
