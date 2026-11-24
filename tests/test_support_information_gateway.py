"""Safe root4 shares factual authority, never expands model control permissions."""
import asyncio
import pytest

from test_contextual_game_flow import Harness
from test_stage1_nested_game_integrity import SyntheticVoice


def support():
    h=Harness('<say>Сегодня тепло, двадцать градусов.</say>',mode='root_menu')
    h.app.bounded_s2s.tts=h.app.bounded_s2s.voice=SyntheticVoice()
    return h


@pytest.mark.asyncio
@pytest.mark.parametrize('root',[4,6])
@pytest.mark.parametrize('text,kind,content',[
 ('Какая сейчас погода?','weather','TYPED_WEATHER'),
 ('Какое сегодня число?','date','TYPED_DATE'),
 ('Кто мой врач?','card_doctors','PHRASE_101'),
])
async def test_explicit_facts_preempt_model_from_both_root_buttons(root,text,kind,content):
    h=support()
    try:
        await h.key(root)
        mode='support_intake' if root==4 else 'bear_dialogue'
        assert h.state.mode==mode
        result=await h.send('SpeechReceived',text=text)
        assert not h.model.calls
        assert any(e.get('type')=='typed_info_request' and e.get('kind')==kind for e in result['outputs'])
        assert h.state.mode==mode and h.state.active_audio_id==content
        await h.done()
        assert h.state.mode==mode and h.state.dialogue_information_kind is None
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['Когда мой следующий приём?','Куда мне сейчас идти?','Кто мой врач?'])
async def test_guest_private_request_never_calls_provider_or_model(text):
    h=support();calls=[]
    async def forbidden(*args,**kwargs):calls.append(True);raise AssertionError('private provider')
    h.app.schedule_weather.schedule=forbidden
    try:
        await h.key(4);await h.send('SpeechReceived',text=text)
        assert not calls and not h.model.calls
        assert h.state.active_audio_id=='PHRASE_101' and h.state.mode=='support_intake'
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['Где ты живёшь?','Почему меняется погода?','Что делает врач?','Расскажи про облака'])
async def test_ordinary_support_keeps_reason_mode_and_bounded_context(text):
    h=support()
    try:
        await h.key(4)
        result=await h.send('SpeechReceived',text=text)
        assert result['reason']=='support_dialogue_turn' and len(h.model.calls)==1
        assert h.state.mode=='support_intake' and h.state.dialogue_information_kind is None
        assert not any(e.get('type')=='typed_info_request' for e in result['outputs'])
        assert h.app.conversation_contexts[h.state.session_id].node_id=='support_intake'
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,severity,mode',[
 ('Мне больно, какая сейчас погода?','concern','support_intake'),
 ('Пожар! Какая сейчас погода?','urgent','safety_stop'),
 ('Позови взрослого, какая сейчас погода?','concern','safety_stop'),
])
async def test_factual_words_do_not_override_support_safety(text,severity,mode):
    h=support();calls=[]
    async def forbidden(*a,**kw):calls.append(True);raise AssertionError('weather must not run')
    h.app.schedule_weather.weather=forbidden
    try:
        await h.key(4);await h.send('SpeechReceived',text=text)
        assert h.state.mode==mode and h.state.safety_severity==severity
        assert not h.model.calls and not calls and h.state.dialogue_information_kind is None
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cause',['ordinary','back','HandsetPlaced','SessionReset','help','concern'])
async def test_late_weather_cannot_cross_new_request_return_end_or_safety(cause):
    h=support();entered=asyncio.Event();release=asyncio.Event();task=None
    original=h.app.schedule_weather.weather
    async def delayed(*a,**kw):
        entered.set()
        try:await release.wait()
        except asyncio.CancelledError:await release.wait()
        return await original(*a,**kw)
    h.app.schedule_weather.weather=delayed
    try:
        await h.key(4)
        task=asyncio.create_task(h.send('SpeechReceived',text='Какая сейчас погода?'))
        await asyncio.wait_for(entered.wait(),2)
        if cause=='ordinary':await h.send('SpeechReceived',text='Расскажи про облака')
        elif cause=='back':await h.key(7)
        elif cause in {'help','concern'}:await h.send('SpeechReceived',text='Позови взрослого' if cause=='help' else 'Мне больно')
        else:await h.send(cause)
        current=h.app.canonical_runtime.sessions.get(h.state.session_id)
        frame=dict(h.app.led.output.snapshot())
        owner=dict(h.app._active_audio.get(h.state.session_id) or {})
        release.set();await asyncio.wait_for(task,2)
        assert not any(item.get('response_id')=='TYPED_WEATHER' for item in h.app.audio.commands)
        assert dict(h.app._active_audio.get(h.state.session_id) or {})==owner
        assert h.app.led.output.snapshot()==frame
        assert current is None or current.dialogue_information_kind is None
        if cause=='ordinary':assert h.state.mode=='support_intake' and len(h.model.calls)==1
        if cause=='concern':assert h.state.mode=='support_intake' and h.state.safety_severity=='concern'
    finally:
        release.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('first,second,content',[
 ('Какая сейчас погода?','Какая сейчас погода?','PHRASE_100'),
 ('Кто мой врач?','Когда мой следующий приём?','PHRASE_101'),
 ('Какая сейчас погода?','Какое сегодня число?','TYPED_DATE'),
])
async def test_old_terminal_including_shared_fallback_recording_cannot_clear_new_owner(first,second,content):
    from app.foundation.schedule_weather import ScheduleWeatherProviderError
    h=support()
    async def unavailable(*a,**kw):raise ScheduleWeatherProviderError('synthetic outage')
    h.app.schedule_weather.weather=unavailable
    try:
        await h.key(4);await h.send('SpeechReceived',text=first)
        old=dict(h.app._active_audio[h.state.session_id])
        await h.send('SpeechReceived',text=second)
        owner=dict(h.app._active_audio[h.state.session_id]);kind=h.state.dialogue_information_kind
        assert owner['content_id']==content and owner['playback_lease']!=old['playback_lease']
        for terminal in ['AudioCompleted','AudioFailed']:
            await h.send(terminal,{**old,'response_id':old['content_id'],'error_code':'synthetic'})
            assert h.app._active_audio[h.state.session_id]==owner and h.state.dialogue_information_kind==kind
        result=await h.done()
        assert result['reason']=='typed_information_finished' and h.state.mode=='support_intake'
        assert h.state.dialogue_information_kind is None
        assert any(e.get('type')=='capture_open' for e in result['outputs'])
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('severity',[None,'none','concern','urgent','unknown'])
async def test_new_support_tool_capability_requires_explicit_safe_phase(severity):
    h=support()
    try:
        for tool in ['date','weather','schedule','card_doctors']:
            kwargs={} if severity is None else {'safety_severity':severity}
            result=h.app.transition_authorizer.authorize_tool('support_intake',tool,profile_status='identified',**kwargs)
            assert result['ok']==(severity=='none')
        for action,target in [('request_information','weather'),('offer_game','quiz'),('open_catalog','games')]:
            assert not h.app.transition_authorizer.authorize_model_action('support_intake',action,target)['ok']
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('identity,up',[(False,False),(False,True),(True,False),(True,True)])
async def test_four_real_entry_routes_use_fact_authority_without_changing_hook(tmp_path,identity,up):
    import json
    from test_panel_session_actions import PanelStand
    from test_contextual_game_flow import Model
    from test_card_doctors_profile import NAME
    h=PanelStand(tmp_path,identity=identity);model=Model('<say>Догадка не должна звучать.</say>')
    h.app.bounded_s2s.llm=model
    try:
        if identity:await h.ready()
        if up:await h.simulator.action({'action':'handset','state':'up'})
        await h.control('start');await h.menu();await h.key(4)
        if not up:
            assert h.state.mode=='awaiting_handset' and not h.state.audio_open
            result=await h.simulator.action({'action':'enable_audio',**h.scope(),'state_version':h.state.state_version})
            assert result['reason']=='panel_audio_pending_started'
        assert h.state.mode=='support_intake' and h.state.handset==('up' if up else 'down')
        for text,content in [('Какая сейчас погода?','TYPED_WEATHER'),
                             ('Кто мой врач?','TYPED_CARD_DOCTORS' if identity else 'PHRASE_101'),
                             ('Когда мой следующий приём?','PHRASE_101')]:
            await h.stand.inject('text',{'text':text},'stand',wait_for_audio=False)
            assert h.state.active_audio_id==content
            assert h.state.mode=='support_intake' and h.state.handset==('up' if up else 'down')
            assert not model.calls
            assert NAME not in json.dumps(h.stand.panel_snapshot(),ensure_ascii=False)
            await h.done()
        await h.control('end')
        assert not h.app._active_audio and not h.state.interaction_open
        if identity:
            await h.ready()
            assert h.state.mode=='profile_ready' and not h.state.interaction_open
        await h.control('start')
        assert h.state.interaction_open
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cause',['date','back','concern'])
async def test_late_renderer_cannot_play_after_same_mode_replacement_or_exit(cause):
    h=support();entered=asyncio.Event();release=asyncio.Event();task=None
    original=h.app._render_typed_audio
    async def render(sid,text,effect,token,current):
        if effect.get('content_id')=='TYPED_WEATHER':
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
        return await original(sid,text,effect,token,current)
    h.app._render_typed_audio=render
    try:
        await h.key(4)
        task=asyncio.create_task(h.send('SpeechReceived',text='Какая сейчас погода?'))
        await asyncio.wait_for(entered.wait(),2)
        if cause=='date':await h.send('SpeechReceived',text='Какое сегодня число?')
        elif cause=='back':await h.key(7)
        else:await h.send('SpeechReceived',text='Мне больно')
        owner=dict(h.app._active_audio.get(h.state.session_id) or {})
        release.set();await asyncio.wait_for(task,2)
        assert not any(c.get('response_id')=='TYPED_WEATHER' for c in h.app.audio.commands)
        assert dict(h.app._active_audio.get(h.state.session_id) or {})==owner
        if cause=='date':assert h.state.dialogue_information_kind=='date'
    finally:
        release.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['Какая погода завтра?','Какая погода в Москве?','Для какого города эта погода?'])
async def test_support_weather_scope_does_not_guess_other_time_or_place(text):
    h=support();calls=[]
    async def forbidden(*a,**kw):calls.append(True);raise AssertionError('scope must not call provider')
    h.app.schedule_weather.weather=forbidden
    try:
        await h.key(4);await h.send('SpeechReceived',text=text)
        assert h.state.mode=='support_intake' and h.state.active_audio_id=='TYPED_WEATHER'
        assert not calls and not h.model.calls
    finally:await h.close()


@pytest.mark.asyncio
async def test_old_fact_provider_cannot_enter_fresh_authorized_session(tmp_path):
    from test_panel_session_actions import PanelStand
    h=PanelStand(tmp_path,identity=True);entered=asyncio.Event();release=asyncio.Event();task=None
    original=h.app.schedule_weather.weather
    async def delayed(*a,**kw):
        entered.set()
        try:await release.wait()
        except asyncio.CancelledError:await release.wait()
        return await original(*a,**kw)
    h.app.schedule_weather.weather=delayed
    try:
        await h.ready();await h.simulator.action({'action':'handset','state':'up'})
        await h.menu();await h.key(4)
        old_request=h.state.identity_request_id
        task=asyncio.create_task(h.stand.inject('text',{'text':'Какая сейчас погода?'},'stand',wait_for_audio=False))
        await asyncio.wait_for(entered.wait(),2)
        await h.control('end');await h.ready()
        assert h.state.identity_request_id!=old_request and not h.state.interaction_open
        await h.control('start')
        owner=dict(h.app._active_audio['stand']);fresh=h.state
        release.set();await asyncio.wait_for(task,2)
        assert h.state is fresh and h.state.mode=='voice_welcome'
        assert h.app._active_audio['stand']==owner
        assert not any((item.get('payload') or item).get('response_id')=='TYPED_WEATHER' for item in h.stand.commands)
    finally:
        release.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action',['<action type="request_information" kind="weather"/>','<action type="offer_game" game_id="quiz"/>'])
async def test_root4_model_cannot_grant_information_or_activity_actions(action):
    h=support();h.model.answers=[action]
    try:
        await h.key(4)
        result=await h.send('SpeechReceived',text='Расскажи про облака')
        assert len(h.model.calls)==1 and h.state.mode=='support_intake'
        assert h.state.pending_game_offer is None and h.state.dialogue_information_kind is None
        assert not any(e.get('type')=='typed_info_request' for e in result['outputs'])
        assert h.state.active_audio_id=='PHRASE_104'
    finally:await h.close()
